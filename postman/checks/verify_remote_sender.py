"""Exercise main-to-remote source sync and a single simulated sender over HTTP.

Requires the existing Zoho Postman simulation on port 4915. All data and the
remote test service are disposable, and the sender is a local stub, never Meta.
"""
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
KEY = "dummy-remote-sender-verification-key"
os.environ.update(THERAPY_SCHEDULER_DATABASE_URL="sqlite:///:memory:",
    MAIN_WORKFLOW_ENABLED="true", MAIN_WORKFLOW_DATA_SOURCE="test", MAIN_WORKFLOW_LIVE="false",
    MAIN_WORKFLOW_REMOTE_SENDER="true", MAIN_WORKFLOW_ADMIN_TOKEN=KEY,
    FLOW_TEST_LIVE="false", FLOW_TEST_RECIPIENTS_B64="e30=", WHATSAPP_PHONE_NUMBER_ID="123456789",
    FLOW_TEST_MESSAGE_MODE="session", ZOHO_CREATOR_CLIENT_ID="dummy", ZOHO_CREATOR_CLIENT_SECRET="dummy",
    ZOHO_CREATOR_REFRESH_TOKEN="dummy", FLOW_TEST_ZOHO_CLIENT_ID="dummy",
    FLOW_TEST_ZOHO_CLIENT_SECRET="dummy", FLOW_TEST_ZOHO_REFRESH_TOKEN="dummy",
    ZOHO_CREATOR_API_URL="http://127.0.0.1:4915/zoho", ZOHO_CREATOR_ACCOUNTS_URL="http://127.0.0.1:4915/zoho")

from sqlalchemy import delete, select
from sqlalchemy.sql.ddl import sort_tables
from flow_test.models import FlowJob, FlowOffer, FlowSetting
from flow_test.service import FlowTest
from flow_test.storage import TEST_TABLES
from flow_test.zoho_routes import dispatch_zoho
from main_workflow import mount
from main_workflow.retained_data import ExistingTestWorkflow, TestDataRuntime
from web_app import TherapySchedulerHandler
import flow_test.reset_live as remote_config


def main():
    with TemporaryDirectory(prefix="remote-sender-check-") as directory:
        url = "sqlite:///" + (Path(directory) / "test.sqlite").as_posix()
        seeded = FlowTest(url)
        with seeded.sessions.begin() as session:
            for table in reversed(sort_tables(TEST_TABLES)):
                if table.name != "flow_test_settings":
                    session.execute(delete(table))
            session.execute(delete(FlowSetting).where(FlowSetting.key.not_in({"scenario", "mode"})))
            session.get(FlowSetting, "mode").value = "live"
        seeded.engine.dispose()
        sent = []
        remote = FlowTest(url, live=True, send=lambda kind, payload: sent.append(kind) or "dummy-message-id")
        local = ExistingTestWorkflow(url, send=lambda *_args: (_ for _ in ()).throw(AssertionError("Local worker sent")))
        target = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
        if target.weekday() == 6:
            target += timedelta(days=1)
        remote.next_offer_date = local.next_offer_date = lambda: target
        remote_instance = SimpleNamespace(flow=remote, previews={})

        class RemoteHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def read_json(self):
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def send_json(self, data):
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                assert self.path == "/test/api/zoho/import"
                assert self.headers.get("Authorization") == f"Bearer {KEY}"
                import web_app

                context = web_app.WORKSPACE_CONTEXT.set({"sessions": remote.sessions,
                    "previews": remote_instance.previews, "admin_user": "simulated-worker"})
                try:
                    dispatch_zoho(self, remote_instance, "/api/zoho/import")
                finally:
                    web_app.WORKSPACE_CONTEXT.reset(context)

        remote_server = ThreadingHTTPServer(("127.0.0.1", 0), RemoteHandler)
        remote_thread = Thread(target=remote_server.serve_forever, daemon=True)
        remote_thread.start()
        remote_config.BASE_URL = f"http://127.0.0.1:{remote_server.server_port}"
        local_instance = SimpleNamespace(flow=local, token=KEY, previews={}, zoho_dispatch=TestDataRuntime.zoho_dispatch)
        mount._runtime = local_instance
        server = ThreadingHTTPServer(("127.0.0.1", 0), TherapySchedulerHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))

        def api(path, body=None):
            request = Request(base + "/api/" + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
            with opener.open(request, timeout=120) as response:
                return json.load(response)

        try:
            run = subprocess.run([sys.executable, "-m", "main_workflow.sync", "--apply"],
                cwd=ROOT, env=dict(os.environ, MAIN_WORKFLOW_APP_URL=base),
                capture_output=True, text=True, timeout=120)
            assert run.returncode == 0, run.stdout + run.stderr
            assert json.loads(run.stdout)["patients"] == 8
            assert len(local.scenario["patients"]) == len(remote.scenario["patients"]) == 8
            assert sent == []
            recipient = local.recipients["TEST-A"]
            draft = api("schedules/generate", {"start": str(target), "patients": ["TEST-A"], "searchSeconds": 20})
            saved = api(f"schedules/{draft['previewId']}/save", {})
            assert saved["offers"] == 1
            with local.sessions() as session:
                job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
                assert job.status == "WAITING_FOR_CONTACT" and job.attempts == 0
            assert local.process_batch() == 0 and sent == []
            assert api("process", {})["worker"] == "Azure test sender"
            assert remote.process_batch() == 0 and sent == []
            remote.receive({"entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "123456789"}, "messages": [{
                    "id": "dummy-inbound", "from": recipient, "timestamp": str(int(time.time())),
                    "text": {"body": "START"},
                }],
            }}]}]})
            assert remote.process_batch() == 1 and sent == ["OFFER"]
            remote.process_batch()  # Process the inbound event; it must not resend the offer.
            assert sent == ["OFFER"]
            with local.sessions() as session:
                job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
                offer = session.scalar(select(FlowOffer))
                assert job.status == "SENT" and job.attempts == 1 and offer.state == "PENDING"
            print(json.dumps({"remote_import_and_local_roster": "passed", "local_worker_did_not_send": True,
                "existing_worker_sent_once": True, "wait_then_start_session_window": "passed",
                "real_meta_messages_sent": 0, "storage": "disposable SQLite", "source": "Postman mock"}, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            remote_server.shutdown()
            remote_server.server_close()
            remote_thread.join(timeout=5)
            local.engine.dispose()
            remote.engine.dispose()
            mount._runtime = None


if __name__ == "__main__":
    main()

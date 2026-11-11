"""Verify opt-in main HTTP/CLI operations against local Postman simulations.

Use the existing zoho-test simulation on 4915 and failure simulation on 4914.
All records and credentials are fictional; the main database here is temporary.
"""

from datetime import date
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.update(THERAPY_SCHEDULER_DATABASE_URL="sqlite:///:memory:", FLOW_TEST_LIVE="false",
                  MAIN_WORKFLOW_ENABLED="true", MAIN_WORKFLOW_LIVE="false",
                  MAIN_WORKFLOW_ADMIN_TOKEN="dummy-main-admin-key-at-least-32-characters",
                  MAIN_WORKFLOW_ADMIN_ID="simulation-operator", MAIN_WORKFLOW_ZOHO_OWNER="fictional-clinic",
                  MAIN_WORKFLOW_ZOHO_APP="eic-pilot", MAIN_WORKFLOW_ZOHO_PLAN_REPORT="Plan_Report",
                  MAIN_WORKFLOW_ZOHO_CONSULTANT_REPORT="All_Consultants", MAIN_WORKFLOW_ZOHO_SCHEDULE_FORM="Scheduler",
                  MAIN_WORKFLOW_ZOHO_SCHEDULE_REPORT="Scheduler_Report", MAIN_WORKFLOW_SOURCE_TIMEZONE="Asia/Kolkata",
                  MAIN_WORKFLOW_ZOHO_CLIENT_ID="dummy", MAIN_WORKFLOW_ZOHO_CLIENT_SECRET="dummy",
                  MAIN_WORKFLOW_ZOHO_REFRESH_TOKEN="dummy", MAIN_WORKFLOW_ZOHO_API_URL="http://127.0.0.1:4915/zoho",
                  MAIN_WORKFLOW_ZOHO_ACCOUNTS_URL="http://127.0.0.1:4915/zoho")

from sqlalchemy import create_engine, select
from database.db import Base
from flow_test.models import FlowOffer
from main_workflow import mount
from main_workflow.config import WorkflowConfig
from main_workflow.provider import ZohoProvider
from main_workflow.service import MainWorkflow
from main_workflow.storage import prepare_storage
from main_workflow.retained_data import TestDataRuntime
from flow_test.service import FlowTest
from web_app import TherapySchedulerHandler


def main():
    with TemporaryDirectory(prefix="main-workflow-") as directory:
        url = "sqlite:///" + (Path(directory) / "normal.sqlite").as_posix()
        use_test_data = "--test-data" in sys.argv
        if use_test_data:
            os.environ.update(MAIN_WORKFLOW_DATA_SOURCE="test", MAIN_WORKFLOW_TEST_DATABASE_URL=url,
                FLOW_TEST_RECIPIENTS_B64="e30=", ZOHO_CREATOR_CLIENT_ID="dummy", ZOHO_CREATOR_CLIENT_SECRET="dummy",
                ZOHO_CREATOR_REFRESH_TOKEN="dummy", FLOW_TEST_ZOHO_REFRESH_TOKEN="dummy",
                ZOHO_CREATOR_API_URL="http://127.0.0.1:4915/zoho", ZOHO_CREATOR_ACCOUNTS_URL="http://127.0.0.1:4915/zoho")
            seeded = FlowTest(url)
            seeded.engine.dispose()
            instance = TestDataRuntime()
            flow, engine = instance.flow, instance.flow.engine
            from main_workflow.provider import workflow_connection

            config, _provider = workflow_connection()
        else:
            os.environ["MAIN_WORKFLOW_DATA_SOURCE"] = "main"
            engine = create_engine(url)
            Base.metadata.create_all(engine)
            config = WorkflowConfig.from_environment()
            prepare_storage(engine, config.source_name)
            flow = MainWorkflow(engine, config)
            instance = mount.MainRuntime.__new__(mount.MainRuntime)
            instance.flow, instance.config, instance.token = flow, config, config.admin_token
            instance.previews, instance.provider = {}, ZohoProvider(config)
        target = date(2026, 10, 23)
        flow.next_offer_date = lambda: target
        mount._runtime = instance
        server = ThreadingHTTPServer(("127.0.0.1", 0), TherapySchedulerHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))

        def api(path, body=None):
            request = Request(base + "/api/" + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"Authorization": f"Bearer {config.admin_token}", "Content-Type": "application/json"})
            with opener.open(request, timeout=90) as response:
                return json.load(response)

        def command(module, *args, success=True, failure_source=False):
            environment = dict(os.environ, MAIN_WORKFLOW_APP_URL=base)
            if failure_source:
                environment.update(MAIN_WORKFLOW_ZOHO_API_URL="http://127.0.0.1:4914/zoho",
                                   MAIN_WORKFLOW_ZOHO_ACCOUNTS_URL="http://127.0.0.1:4914/zoho",
                                   ZOHO_CREATOR_API_URL="http://127.0.0.1:4914/zoho",
                                   ZOHO_CREATOR_ACCOUNTS_URL="http://127.0.0.1:4914/zoho")
            result = subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT, env=environment,
                                    text=True, capture_output=True, timeout=120)
            assert (result.returncode == 0) == success, result.stdout + result.stderr
            return json.loads(result.stdout) if success else result.stdout

        def state():
            return api(f"state?date={target}")

        def reply(code, action):
            with flow.sessions.begin() as session:
                offer = session.scalar(select(FlowOffer).where(FlowOffer.patient_code == code).order_by(FlowOffer.created_at.desc()))
                flow._job(session, f"main-check:{code}:{action}", "INBOUND", {
                    "from": offer.recipient, "timestamp": str(int(time.time())),
                    "button": {"payload": f"{flow.button_prefix}:{offer.token}:{action}"}})
            flow.drain()

        try:
            imported = command("main_workflow.sync", "--apply")
            assert imported["patients"] == 8 and imported["messages_sent"] == 0
            assert not state()["appointments"]
            preview = api("schedules/generate", {"start": str(target), "patients": [f"TEST-{c}" for c in "ABCDEFG"], "searchSeconds": 20})
            saved = api(f"schedules/{preview['previewId']}/save", {})
            assert saved["offers"] == 7
            assert api(f"schedules/{preview['previewId']}/save", {}) == saved
            flow.drain()
            reply("TEST-B", "HELP")
            assert any(item["patient"] == "TEST-B" for item in state()["inbox"])
            reply("TEST-B", "YES")
            b = next(a for a in state()["appointments"] if a["patient"] == "TEST-B")
            api("attendance", {"id": b["id"], "status": "COMPLETED"})
            reply("TEST-F", "NO")
            released = next(a for a in state()["appointments"] if a["patient"] == "TEST-F")
            h = next(p for p in api("patients")["patients"] if p["code"] == "TEST-H")
            reservation = api(f"appointments/{released['id']}/reservation-preview", {"patientId": h["id"], "wholeVisit": True})
            api(f"appointments/{released['id']}/reserve", {"patientId": h["id"], "wholeVisit": True,
                "mode": "with_messaging", "previewToken": reservation["preview_token"],
                "requestId": "main-replacement", "overrideWarnings": bool(reservation["warnings"])})
            flow.drain()
            reply("TEST-H", "YES")
            unchanged_bookings = state()["appointments"]
            time.sleep(1.1)
            delta = command("main_workflow.sync", "--apply")
            assert delta["downloaded"] == {"Plan_Report": 0, "All_Consultants": 0}
            assert state()["appointments"] == unchanged_bookings
            exported = api("zoho/export/push", {"date": str(target)})
            assert exported["created"] > 0
            if use_test_data:
                assert exported["assumedAcceptedVisits"] > 0
            else:
                assert exported["assumedAcceptedVisits"] == 0 and exported["awaitingStaff"]
                assert state()["appointments"] == unchanged_bookings
            assert command("main_workflow.push", "--date", str(target), "--apply")["pushed"] == 0
            checkpoint = api("zoho/status")["checkpoint"]
            time.sleep(1.1)
            command("main_workflow.sync", "--apply", success=False, failure_source=True)
            assert api("zoho/status")["checkpoint"] == checkpoint
            # Explicit outcomes remain the same in either workspace.
            assert next(a for a in state()["appointments"] if a["id"] == b["id"])["status"] == "COMPLETED"
            assert next(a for a in state()["appointments"] if a["id"] == released["id"])["status"] == "NEEDS_RESCHEDULING"
            print(json.dumps({"source_import": "passed", "shared_main_ui_review_save": "passed",
                              "reply_help_decline_replacement_attendance": "passed", "empty_delta": "passed",
                              "main_target_calendar_export_and_idempotent_cli": "passed",
                              "outage_preserves_checkpoint": True, "real_messages_sent": 0,
                              "storage": "retained test SQLite" if use_test_data else "temporary normal SQLite",
                              "ui_path": "/", "external_target": "local Postman simulation"}, indent=2))
            if "--serve" in sys.argv:
                print(json.dumps({"preview_url": base + "/", "admin_key": config.admin_token,
                                  "notice": "Dummy local credentials; temporary fictional database"}), flush=True)
                worker.join()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            mount._runtime = None
            engine.dispose()


if __name__ == "__main__":
    main()

"""Check fresh and incremental main imports against the existing local Zoho mock.

Run zoho-test.sim.yaml on port 4915 first. Uses empty temporary SQLite, dummy
credentials and the ordinary main /api routes; never touches a real database.
"""

from datetime import datetime, timedelta, timezone
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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
KEY = "dummy-incremental-main-admin-at-least-32-characters"
os.environ.update(
    THERAPY_SCHEDULER_DATABASE_URL="sqlite:///:memory:",
    MAIN_WORKFLOW_ENABLED="true", MAIN_WORKFLOW_DATA_SOURCE="test", MAIN_WORKFLOW_LIVE="false",
    MAIN_WORKFLOW_ADMIN_TOKEN=KEY, FLOW_TEST_LIVE="false", FLOW_TEST_RECIPIENTS_B64="e30=",
    ZOHO_CREATOR_CLIENT_ID="dummy", ZOHO_CREATOR_CLIENT_SECRET="dummy", ZOHO_CREATOR_REFRESH_TOKEN="dummy",
    FLOW_TEST_ZOHO_CLIENT_ID="dummy", FLOW_TEST_ZOHO_CLIENT_SECRET="dummy", FLOW_TEST_ZOHO_REFRESH_TOKEN="dummy",
    ZOHO_CREATOR_API_URL="http://127.0.0.1:4915/zoho", ZOHO_CREATOR_ACCOUNTS_URL="http://127.0.0.1:4915/zoho",
)

from sqlalchemy import func, select

from database.models import Patient, Therapist, TreatmentRequirement
from flow_test.models import FlowSetting
from flow_test.storage import create_test_engine, initialize_test_tables
from main_workflow import mount
from main_workflow.retained_data import TestDataRuntime
from web_app import TherapySchedulerHandler


def main():
    with TemporaryDirectory(prefix="main-incremental-") as directory:
        url = "sqlite:///" + (Path(directory) / "empty.sqlite").as_posix()
        engine = create_test_engine(url)
        initialize_test_tables(engine)
        with engine.begin() as connection:
            connection.execute(FlowSetting.__table__.insert(), [
                {"key": "scenario", "value": "malaparamba-fictional-v3"},
                {"key": "mode", "value": "preview"},
            ])
        engine.dispose()
        os.environ["MAIN_WORKFLOW_TEST_DATABASE_URL"] = url
        os.environ.pop("MAIN_WORKFLOW_TEST_DATABASE_SCHEMA", None)
        instance = TestDataRuntime()
        flow = instance.flow
        target = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
        if target.weekday() == 6:
            target += timedelta(days=1)
        flow.next_offer_date = lambda: target
        mount._runtime = instance
        server = ThreadingHTTPServer(("127.0.0.1", 0), TherapySchedulerHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))

        def request(url, body=None, authenticated=False):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {KEY}"
            req = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with opener.open(req, timeout=120) as response:
                return json.load(response)

        def api(path, body=None):
            return request(base + "/api/" + path, body, True)

        def sync():
            completed = subprocess.run([sys.executable, "-m", "main_workflow.sync", "--apply"],
                cwd=ROOT, env=dict(os.environ, MAIN_WORKFLOW_APP_URL=base),
                capture_output=True, text=True, timeout=120)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return json.loads(completed.stdout)

        def counts():
            with flow.sessions() as session:
                return tuple(session.scalar(select(func.count(model.id)))
                             for model in (Patient, Therapist, TreatmentRequirement))

        def appointments():
            return api(f"state?date={target}")["appointments"]

        try:
            assert counts() == (0, 0, 0)
            assert api("patients")["patients"] == []
            first = sync()
            assert first["downloaded"] == {"Plan_Report": 8, "All_Consultants": 2}
            assert counts() == (8, 2, 10)
            assert appointments() == []
            preview_id = next(iter(instance.previews))
            saved = api(f"schedules/{preview_id}/save", {})
            assert saved["added"] > 0, saved
            before = {item["id"]: item for item in appointments()}
            stamp = datetime.now(timezone.utc).strftime("%d-%b-%Y %H:%M:%S")
            request("http://127.0.0.1:4915/zoho/__test__/add-patient", {"at": stamp})
            time.sleep(1.1)  # The upper sync bound is exclusive and whole-second.
            delta = sync()
            assert delta["downloaded"] == {"Plan_Report": 1, "All_Consultants": 1}, delta
            assert counts() == (9, 3, 11)
            assert {item["id"]: item for item in appointments()} == before
            with flow.sessions() as session:
                patient = session.scalar(select(Patient).where(Patient.patient_code == "NEW-009"))
                consultant = session.scalar(select(Therapist).where(Therapist.therapist_code == "ZC-900002"))
                requirement = session.scalar(select(TreatmentRequirement).where(TreatmentRequirement.patient_id == patient.id))
                assert requirement.assigned_therapist_id == consultant.id
                assert consultant.active
                patient_id, consultant_id = patient.id, consultant.id
            draft = api("schedules/generate", {"start": str(target), "patients": ["NEW-009"], "searchSeconds": 20})
            fresh = instance.previews[draft["previewId"]]["result"].appointments
            assert any(item.patient_id == patient_id and item.therapist_id == consultant_id for item in fresh)
            api(f"schedules/{draft['previewId']}/save", {})
            after = {item["id"]: item for item in appointments()}
            assert all(after[key] == value for key, value in before.items())
            assert len(after) > len(before)
            time.sleep(1.1)
            repeat = sync()
            assert repeat["downloaded"] == {"Plan_Report": 0, "All_Consultants": 0}, repeat
            assert counts() == (9, 3, 11)
            assert {item["id"]: item for item in appointments()} == after
            print(json.dumps({"initial_empty_import": "passed", "paginated_initial_reports": "passed",
                "new_patient_and_consultant_delta": "passed", "new_patient_uses_new_consultant": "passed",
                "new_patient_schedule_save": "passed", "saved_bookings_preserved": "passed",
                "repeated_sync_without_duplicates": "passed", "final_counts": counts(),
                "real_messages_sent": 0, "target": "local Postman mock; temporary SQLite only"}, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            instance.close()
            mount._runtime = None


if __name__ == "__main__":
    main()

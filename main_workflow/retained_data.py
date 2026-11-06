"""Run the main application over existing test records without seeding or DDL."""

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading

from services.whatsapp_worker import embedded_worker_enabled
from typing import Any, Callable

from sqlalchemy import func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from config import load_local_env_file
from database.models import Location, Patient, TreatmentRequirement
from database.workflow_lock import workflow_locks
from flow_test.models import FlowJob, FlowOffer, FlowSetting
from flow_test.service import FlowTest, ROOT, TestTransport, load_recipient_names, load_recipients
from flow_test.storage import TEST_TABLES, create_test_engine
from main_workflow.service import MainWorkflow
from scheduling.types import ScheduleResult


def selected_test_target() -> tuple[str, str | None]:
    """Select an explicit override, configured test store, or existing local preview."""
    load_local_env_file(ROOT / ".env")
    override = os.getenv("MAIN_WORKFLOW_TEST_DATABASE_URL", "").strip()
    if override:
        return override, os.getenv("MAIN_WORKFLOW_TEST_DATABASE_SCHEMA") or None
    configured = os.getenv("FLOW_TEST_DATABASE_URL", "").strip()
    if configured:
        return configured, os.getenv("FLOW_TEST_DATABASE_SCHEMA") or None
    preview = os.getenv("FLOW_TEST_PREVIEW_DATABASE_URL", "").strip()
    if preview:
        return preview, os.getenv("FLOW_TEST_PREVIEW_DATABASE_SCHEMA") or None
    path = ROOT / "data/flow_preview.sqlite"
    if not path.is_file():
        raise ValueError("Configure an existing test database; the main app never creates test seed data")
    return "sqlite:///" + path.as_posix(), None


def access_key(*, required: bool = True) -> str:
    """Reuse the existing test access key unless a main key is explicitly supplied."""
    load_local_env_file(ROOT / ".env")
    key = os.getenv("MAIN_WORKFLOW_ADMIN_TOKEN", "").strip() or os.getenv("FLOW_TEST_ADMIN_TOKEN", "").strip()
    path = ROOT / "data/admin.token"
    if not key and path.is_file():
        key = path.read_text(encoding="utf-8").strip()
    if not key and required:
        raise ValueError("Configure an operations key or the existing test access key first")
    return key


class ExistingTestWorkflow(MainWorkflow):
    """Use the normal app's solver settings with the retained test queue protocol."""

    is_test = True
    button_prefix = "ft"
    message_prefix = "TEST ONLY: "

    def __init__(self, database_url: str, schema: str | None = None,
                 *, send: Callable[[str, dict], str] | None = None) -> None:
        target = make_url(database_url)
        if target.get_backend_name() == "sqlite" and target.database != ":memory:" and not Path(target.database or "").is_file():
            raise ValueError("The selected SQLite test file does not exist; no file was created")
        self.engine = create_test_engine(database_url, schema)
        self.database_schema = schema
        try:
            names = set(inspect(self.engine).get_table_names(schema=schema))
            if not {table.name for table in TEST_TABLES} <= names:
                raise ValueError("The selected test store is incomplete; no tables or records were created")
            from database.treatment_migrations import has_course_identity
            if not has_course_identity(self.engine, schema):
                raise ValueError("Upgrade treatment courses with python -m main_workflow.migrate --apply")
            self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
            self.lock, self.inbound_lock = workflow_locks(self.engine, schema)
            self.admin_id = os.getenv("MAIN_WORKFLOW_ADMIN_ID", "main_app_operator")
            self.source_name = "nidhin.kumar_turnb/test-gokul"
            self.export_target = self.source_name + "/Scheduler_Report"
            self.export_key_prefix = "test"
            self.offer_minutes = int(os.getenv("FLOW_TEST_OFFER_MINUTES", "600"))
            self.send_batch_size = int(os.getenv("FLOW_TEST_SEND_BATCH_SIZE", "5"))
            self.message_mode = os.getenv("FLOW_TEST_MESSAGE_MODE", "session").strip().lower()
            if (not 1 <= self.offer_minutes <= 1440 or not 1 <= self.send_batch_size <= 20
                    or self.message_mode not in {"session", "template"}):
                raise ValueError("Invalid test queue timing or message mode")
            contacts = ROOT / "recipients.local.json"
            has_contacts = contacts.is_file() or bool(os.getenv("FLOW_TEST_RECIPIENTS_B64"))
            self.recipients = load_recipients(contacts) if has_contacts else {}
            self.recipient_names = load_recipient_names(contacts) if has_contacts else {}
            with self.sessions() as session:
                marker, mode = session.get(FlowSetting, "scenario"), session.get(FlowSetting, "mode")
                if (not marker or marker.value not in {"malaparamba-fictional-v1", "malaparamba-fictional-v2", "malaparamba-fictional-v3"}
                        or not mode or mode.value not in {"preview", "live"}):
                    raise ValueError("The selected database is not a recognized existing test store")
                self.live = mode.value == "live"  # Preserve persisted queue semantics.
                self.configure_reliability()
                from flow_test.reliability_schema import missing_columns
                if missing_columns(self.engine, schema):
                    raise ValueError("Upgrade this queue with python -m main_workflow.migrate --apply before starting workers")
                imported = session.get(FlowSetting, "zoho_test_scenario")
                if imported:
                    self.scenario = json.loads(imported.value)
                else:
                    locations = list(session.scalars(select(Location).order_by(Location.id)))
                    self.scenario = {"version": marker.value, "centre": locations[0].name if locations else "Centre",
                        "patients": [{"code": p.patient_code, "name": p.name, "initial_roster": True,
                            "requirements": [{"weekly": r.sessions_per_week} for r in session.scalars(
                                select(TreatmentRequirement).where(TreatmentRequirement.patient_id == p.id,
                                                                  TreatmentRequirement.active.is_(True)))]}
                            for p in session.scalars(select(Patient))]}
            self.remote_worker = self.live and os.getenv("MAIN_WORKFLOW_REMOTE_SENDER", "false").lower() == "true"
            if self.remote_worker and send is None and (target.get_backend_name() != "postgresql" or schema != "flow_test"):
                raise ValueError("The existing remote sender requires the configured PostgreSQL flow_test schema")
            if self.remote_worker and not os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip().isdigit():
                raise ValueError("Configure the existing test worker's WhatsApp phone number ID before enabling its sender")
            local_sender = self.live and os.getenv("MAIN_WORKFLOW_LIVE", "false").lower() == "true" and not self.remote_worker
            self.messaging_enabled = local_sender or self.remote_worker
            self.transport = None if send else TestTransport(local_sender, self.recipients)
            self.send = send or self.transport.send
            for patient in self.scenario["patients"]:
                if not patient.get("phone") and patient["code"] in self.recipients:
                    patient["phone"] = self.recipients[patient["code"]]
            self.use_source_contacts(self.scenario)
            with self.sessions() as session:
                revision = session.get(FlowSetting, "zoho_test_fingerprint")
                self._source_revision = revision.value if revision else None
            if local_sender:
                with self.lock, self.inbound_lock, self.sessions.begin() as session:
                    for job in session.scalars(select(FlowJob).where(FlowJob.status == "WAITING_FOR_ENABLE")):
                        if job.kind == "OFFER":
                            payload = json.loads(job.payload)
                            offer = session.get(FlowOffer, payload["token"])
                            number = self.recipients.get(offer.patient_code) if offer else None
                            if not number:
                                job.error = "Verify the current parent contact before enabling this offer"
                                continue
                            payload["recipient"], offer.recipient = number, number
                            payload.pop("session_expires_at", None)
                            job.payload = json.dumps(payload, ensure_ascii=False)
                        job.status, job.error = "QUEUED", None
                    self._recover_processing(session)
        except Exception:
            self.engine.dispose()
            raise

    def _job(self, session, key: str, kind: str, payload: dict) -> FlowJob:
        digest = hashlib.sha256(key.encode()).hexdigest()
        exists = session.scalar(select(FlowJob.id).where(FlowJob.event_key == digest))
        job = FlowTest._job(session, key, kind, payload)
        if exists is None and kind in {"OFFER", "TEXT"} and self.live and not self.messaging_enabled:
            job.status = "WAITING_FOR_ENABLE"
            job.error = "Messaging is OFF in the main app; no send was attempted"
        return job

    def _publish(self, session, target: date, appointment_id: int | None = None) -> int:
        newest = session.scalar(select(func.max(FlowJob.id))) or 0
        count = FlowTest._publish(self, session, target, appointment_id)
        if self.live and not self.messaging_enabled:
            # Fence in the saving transaction, before another deployed test
            # worker can see a QUEUED/contact-waiting offer and send it.
            for job in session.scalars(select(FlowJob).where(FlowJob.id > newest, FlowJob.kind == "OFFER")):
                job.status = "WAITING_FOR_ENABLE"
                job.error = "Messaging is OFF in the main app; no send was attempted"
        return count

    def process_one(self, *, job_id: int | None = None) -> bool:
        if self.remote_worker:
            return False
        if self.live and not self.messaging_enabled:
            with self.sessions() as session:
                query = select(FlowJob.id).where(FlowJob.status == "QUEUED", FlowJob.kind.in_(("INBOUND", "STATUS")))
                if job_id is not None:
                    query = query.where(FlowJob.id == job_id)
                job_id = session.scalar(query.order_by(FlowJob.id).limit(1))
            return super().process_one(job_id=job_id) if job_id is not None else False
        return super().process_one(job_id=job_id)

    def process_batch(self) -> int:
        if self.remote_worker:
            return 0
        if self.live and not self.messaging_enabled:
            return int(self.process_one())
        return super().process_batch()

    def simulate(self, token: str, action: str) -> None:
        if self.live:
            raise ValueError("Simulated replies require a preview database; retained live test history is preserved")
        return FlowTest.simulate(self, token, action)

    def state(self, target: date) -> dict:
        result = FlowTest.state(self, target)
        result.update(workspace="main", test_data=True, allow_simulation=not self.live,
                      messaging_paused=self.live and not self.messaging_enabled)
        if result["messaging_paused"]:
            result["mode"] = "MESSAGING OFF — existing test database; offers stay queued"
        elif self.remote_worker:
            result["mode"] = "LIVE — messages sent by the existing test worker"
        return result

    def refresh_source(self) -> None:
        """Refresh the local roster after the remote worker imports source data."""
        if not self.remote_worker:
            return
        with self.sessions() as session:
            revision = session.get(FlowSetting, "zoho_test_fingerprint")
            value = revision.value if revision else None
            changed = value != self._source_revision
            source = session.get(FlowSetting, "zoho_test_scenario") if changed else None
            marker = session.get(FlowSetting, "scenario") if changed and not source else None
            scenario = json.loads(source.value) if source else (
                {"version": marker.value, "centre": "Centre", "patients": []} if marker else None)
        if changed and scenario is not None:
            self.use_source_contacts(scenario)
            self._source_revision = value

    def release_held_offers(self) -> None:
        """Release previously paused offers after remote source reconciliation."""
        if not self.remote_worker:
            return
        with self.lock, self.inbound_lock, self.sessions.begin() as session:
            for job in session.scalars(select(FlowJob).where(FlowJob.status == "WAITING_FOR_ENABLE")):
                if job.kind == "OFFER":
                    payload = json.loads(job.payload)
                    offer = session.get(FlowOffer, payload["token"])
                    number = self.recipients.get(offer.patient_code) if offer else None
                    if not number:
                        job.error = "Verify the current parent contact before enabling this offer"
                        continue
                    payload["recipient"], offer.recipient = number, number
                    payload.pop("session_expires_at", None)
                    job.payload = json.dumps(payload, ensure_ascii=False)
                job.status, job.error = "QUEUED", None

    def save_calendar(self, result: ScheduleResult, target: date, *, expected_offer_on_save: bool) -> dict:
        response = super().save_calendar(result, target, expected_offer_on_save=expected_offer_on_save)
        if self.live and not self.messaging_enabled:
            response["message"] += " Messaging is OFF; offers stay queued without a send attempt."
        return response


class TestDataRuntime:
    """One main-app runtime; /test aliases it rather than starting a second sender."""

    def __init__(self) -> None:
        from main_workflow.config import local_no_auth, requires_access_key

        self.token = (access_key(required=False) or secrets.token_urlsafe(32)) if (local_no_auth() or not requires_access_key()) else access_key()
        self.flow = ExistingTestWorkflow(*selected_test_target())
        if self.flow.remote_worker:
            self.token = access_key()
        self.previews = {}
        self.stop = threading.Event()
        self.worker = None
        if self.flow.messaging_enabled and not self.flow.remote_worker and embedded_worker_enabled():
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    @staticmethod
    def zoho_dispatch(request: Any, instance: Any, path: str) -> None:
        from flow_test.zoho_routes import dispatch_zoho

        if path == "/api/zoho/import":
            body = request.read_json()
            if body.get("source") not in {None, instance.flow.source_name}:
                import web_app

                raise web_app.ApiError(409, "The import source does not match the selected test database")
            if instance.flow.remote_worker:
                from flow_test.reset_live import BASE_URL
                from flow_test.zoho_daily import test_api
                import web_app

                try:
                    result = test_api(BASE_URL, instance.token, "import", body)
                    instance.flow.refresh_source()
                    instance.flow.release_held_offers()
                    instance.previews.clear()
                except (ValueError, OSError) as exc:
                    raise web_app.ApiError(409, f"Remote test sync needs review: {exc}") from None
                request.send_json(result)
                return
            request.read_json = lambda: body
        dispatch_zoho(request, instance, path)

    def work(self) -> None:
        while not self.stop.wait(1):
            try:
                self.flow.expire_offers()
                self.flow.queue_template_reminders()
                self.flow.process_batch()
            except Exception:
                print("Main test-data worker paused one cycle; inspect the queue")

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=25)
        self.flow.engine.dispose()

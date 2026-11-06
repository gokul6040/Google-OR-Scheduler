"""Reuse daily operations against existing main records, without test seeding."""

import json
from datetime import date
import os

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from config import SOLVER_SETTINGS
from database.models import AuditLog, Location, Patient, TreatmentRequirement
from database.workflow_lock import workflow_locks
from flow_test.models import FlowJob, FlowOffer, FlowSetting
from flow_test.service import FlowTest, TestTransport
from main_workflow.config import WorkflowConfig
from main_workflow.storage import check_storage


class MainWorkflow(FlowTest):
    """Operational adapter only; all scheduling still uses the shared OR engine."""

    is_test = False
    solver_settings = SOLVER_SETTINGS
    message_prefix = ""
    export_key_prefix = "main"
    button_prefix = "mw"

    def __init__(self, engine: Engine, config: WorkflowConfig, *, send=None) -> None:
        # Do not call FlowTest.__init__: it owns test storage and fictional seeds.
        check_storage(engine)
        self.engine = engine
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.config = config
        self.source_name = config.source_name
        self.export_target = config.export_target
        self.admin_id = config.admin_id
        self.live = config.live
        self.configure_reliability()
        self.database_schema = None
        self.lock, self.inbound_lock = workflow_locks(engine)
        self.recipient_names = {}
        self.offer_minutes = int(os.getenv("MAIN_WORKFLOW_OFFER_MINUTES", "600"))
        self.send_batch_size = int(os.getenv("MAIN_WORKFLOW_SEND_BATCH_SIZE", "5"))
        if not 1 <= self.offer_minutes <= 1440 or not 1 <= self.send_batch_size <= 20:
            raise ValueError("Use 1–1440 offer minutes and a send batch size of 1–20")
        self.message_mode = os.getenv("MAIN_WORKFLOW_MESSAGE_MODE", "session").strip().lower()
        if self.message_mode not in {"session", "template"}:
            raise ValueError("MAIN_WORKFLOW_MESSAGE_MODE must be session or template")
        with self.sessions() as session:
            if session.get(FlowSetting, "main_workflow_source").value != self.source_name:
                raise ValueError("The configured source does not match the prepared main database")
            imported = session.get(FlowSetting, "zoho_test_scenario")
            if imported:
                self.scenario = json.loads(imported.value)
            else:
                # Project existing records into the roster; never create patients/plans.
                locations = list(session.scalars(select(Location).order_by(Location.id)))
                self.scenario = {
                    "version": "main-workflow-v1", "source": self.source_name,
                    "centre": locations[0].name if locations else "Centre", "patients": [
                        {"code": patient.patient_code, "name": patient.name, "initial_roster": True,
                         "requirements": [{"weekly": req.sessions_per_week} for req in session.scalars(
                             select(TreatmentRequirement).where(TreatmentRequirement.patient_id == patient.id,
                                                                TreatmentRequirement.active.is_(True)))]}
                        for patient in session.scalars(select(Patient))
                    ],
                }
        self.recipients = {item["code"]: item["phone"] for item in self.scenario["patients"] if item.get("phone")}
        self.transport = None if send else TestTransport(self.live, self.recipients, env_prefix="MAIN_WORKFLOW")
        self.send = send or self.transport.send
        with self.lock, self.inbound_lock, self.sessions.begin() as session:
            mode = "live" if self.live else "preview"
            previous = session.get(FlowSetting, "main_workflow_mode")
            if previous and previous.value != mode and session.scalar(select(FlowOffer.token).limit(1)):
                raise ValueError("Do not switch a populated workflow between preview and live. Use a separate database copy for trials")
            if previous is None:
                session.add(FlowSetting(key="main_workflow_mode", value=mode))
            else:
                previous.value = mode
            self._recover_processing(session)
        if self.transport:
            self.transport.allowed = self.contact_numbers()
        self.use_source_contacts(self.scenario)
        self.expire_offers()

    def use_source_contacts(self, scenario: dict) -> None:
        """Keep existing active patients in daily demand without inventing phones."""
        with self.lock:
            merged = json.loads(json.dumps(scenario))
            known = {item["code"] for item in merged["patients"]}
            with self.sessions() as session:
                for patient in session.scalars(select(Patient).where(Patient.active.is_(True))):
                    if patient.patient_code not in known:
                        merged["patients"].append({"code": patient.patient_code, "name": patient.name,
                            "initial_roster": True, "requirements": [{"weekly": item.sessions_per_week}
                            for item in session.scalars(select(TreatmentRequirement).where(
                                TreatmentRequirement.patient_id == patient.id, TreatmentRequirement.active.is_(True)))]})
            self.scenario = merged
            self.recipients = {item["code"]: item["phone"] for item in merged["patients"] if item.get("phone")}
            if self.transport:
                self.transport.allowed = self.contact_numbers()

    def _audit(self, session, action: str, entity: str) -> None:
        session.add(AuditLog(action=action, entity=entity, user=self.admin_id))

    def _publish(self, session, target, appointment_id=None) -> int:
        # The main workflow never invents preview recipient bindings.
        from database.models import Appointment, AppointmentStatus

        source = session.get(FlowSetting, "zoho_test_scenario")
        if source:
            self.use_source_contacts(json.loads(source.value))
        query = select(Patient.patient_code).join(Appointment).where(
            Appointment.date == target, Appointment.status == AppointmentStatus.PROPOSED, Patient.active.is_(True))
        if appointment_id is not None:
            anchor = session.get(Appointment, appointment_id)
            if anchor is not None:
                query = query.where(Patient.id == anchor.patient_id)
        if any(code not in self.recipients for code in session.scalars(query)):
            raise ValueError("Import verified parent contacts before publishing the main schedule")
        return super()._publish(session, target, appointment_id)

    def state(self, target: date) -> dict:
        result = super().state(target)
        result.update(workspace="main", mode="LIVE — parent messages" if self.live else "PREVIEW — no messages sent")
        return result

    def reset_test_data(self, confirmation: str) -> dict:
        raise ValueError("Database reset is unavailable in the main workflow")

    def simulate(self, token: str, action: str) -> dict:
        raise ValueError("Simulated parent replies are unavailable in the main workflow")

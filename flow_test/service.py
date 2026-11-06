"""Daily test workflow backed by a separate DB and registered parent contacts.

Only the shared scheduler, models and WhatsApp transport are reused. This module
never imports the production web server, its initialization, or any Zoho service.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
from dataclasses import asdict, replace
from functools import cached_property
from datetime import date, datetime, timedelta, timezone
from datetime import datetime as DateTime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.sql.ddl import sort_tables
from sqlalchemy.orm import Session, sessionmaker

from config import FLOW_TEST_SOLVER_SETTINGS, SolverSettings
from database.models import (
    AdminBookingDecision, Appointment, AppointmentStatus, AuditLog, Department, Location, Patient,
    PatientAvailability, Therapist, TherapistAvailability, TreatmentRequirement,
)
from database.repository import limit_new_bookings_to_day, load_scheduling_problem, save_proposed_schedule
from database.workflow_lock import WorkflowLock, workflow_locks
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.slot_config import SLOTS, slot_label
from scheduling.types import ScheduledAppointment, ScheduleResult, SchedulingProblem
from services.admin_reservation_service import reserve_admin_slot
from services.parent_response_service import (
    process_daily_responses, record_parent_response, weekly_progress,
)
from services.whatsapp_service import (
    send_reply_buttons, send_template_message, send_text_message,
    sender_settings_from_environment, WhatsAppConfigurationError,
)
from services.therapy_schedule_template import (
    PARAMETERS as THERAPY_TEMPLATE_PARAMETERS, PROFILE as THERAPY_TEMPLATE_PROFILE,
    prepare_template_job, queue_daily_reminders, schedule_initial_job,
    template_fields, uses_daily_template,
)

from flow_test.models import FlowContactWindow, FlowInbox, FlowJob, FlowOffer, FlowSetting
from flow_test.storage import TEST_TABLES, create_test_engine, initialize_test_tables
from flow_test.reliability import Reliability, PAUSE_KEY


ROOT = Path(__file__).resolve().parent
SETTINGS = FLOW_TEST_SOLVER_SETTINGS
ACTIVE = (AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION,
          AppointmentStatus.CONFIRMED)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _recipient_data(path: Path) -> dict:
    """Prefer deployment-injected contacts; local files stay out of images."""
    configured = os.getenv("FLOW_TEST_RECIPIENTS_B64", "").strip()
    if configured:
        # Base64 survives Windows CLI argument quoting; Azure stores it as a
        # secret. Encoding itself does not provide confidentiality.
        raw = base64.b64decode(configured, validate=True).decode("utf-8")
    else:
        raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Recipients must be an object keyed by fictional patient code")
    return data


def load_recipients(path: Path) -> dict[str, str]:
    """Read only explicitly consented coworker numbers; never infer contacts."""
    data = _recipient_data(path)
    recipients = {}
    for code, item in data.items():
        if not isinstance(item, dict):
            raise ValueError("Each recipient needs phone and consent fields")
        phone = re.sub(r"[\s+()-]", "", str(item.get("phone", "")))
        if not phone:
            continue
        if not re.fullmatch(r"[1-9]\d{7,14}", phone):
            raise ValueError(f"Invalid international phone format for {code}")
        if item.get("consent") is not True:
            raise ValueError(f"Set consent to true only for an agreed test recipient: {code}")
        recipients[code] = phone
    return recipients


def load_recipient_names(path: Path) -> dict[str, str]:
    """Read optional greeting names without using unverified WhatsApp profiles."""
    data = _recipient_data(path)
    names = {}
    for code, item in data.items():
        if not isinstance(item, dict):
            raise ValueError("Each recipient needs phone and consent fields")
        name = item.get("name", "")
        if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError(f"Use a single-line greeting name of at most 80 characters for {code}")
        if name.strip():
            names[code] = name.strip()
    return names


def format_offer_message(fields: dict[str, str]) -> str:
    """Present one complete visit in natural language, including all its therapies."""
    name = fields.get("greeting_name") or fields["patient"].removeprefix("TEST ONLY — ")
    therapy = fields.get("therapy", "therapy")
    day = fields.get("date_display", fields.get("date", "the scheduled date"))
    schedule = fields.get("schedule", fields["visit_details"])
    reference = fields.get("reference", "")
    heading = "TEST ONLY — fictional appointment" if fields.get("test_only", True) else "Your therapy appointment"
    message = (
        f"{heading}\n\nHi {name},\n"
        f"You have {therapy} scheduled on {day} at {fields['centre']}.\n\n"
        f"{schedule}\n\nCan you attend? Cannot attend means unavailable for the whole day."
        + (f"\nReference: {reference}" if reference else "")
    )
    if len(message) <= 1024:
        return message
    # Avoid repeating every therapy in the introduction. Keep the complete
    # visit, identity, branch, times and action meaning within Meta's body limit.
    return (
        f"{'TEST ONLY' if fields.get('test_only', True) else 'Appointment'} · {fields.get('patient_code', name)}\n"
        f"{fields.get('date', day)} · IST\n{fields['centre']}\n\n"
        f"{fields.get('compact_schedule', schedule)}\n\n"
        "Yes: confirm visit. Cannot attend: unavailable for the whole day. Help: ask staff."
        + (f"\nRef: {reference}" if reference else "")
    )


class TestTransport:
    """Explicit live transport; preview mode makes no HTTP requests."""

    def __init__(self, live: bool, recipients: dict[str, str], *, env_prefix: str = "FLOW_TEST"):
        self.live = live
        self.allowed = set(recipients.values())
        self.sender = sender_settings_from_environment() if live else None
        self.mode = os.getenv(f"{env_prefix}_MESSAGE_MODE", "session").strip().lower()
        if self.mode not in {"session", "template"}:
            raise ValueError("FLOW_TEST_MESSAGE_MODE must be session or template")
        self.profile = os.getenv(f"{env_prefix}_TEMPLATE_PROFILE", "legacy").strip()
        if self.profile not in {"legacy", THERAPY_TEMPLATE_PROFILE}:
            raise ValueError("Unknown WhatsApp template profile")
        self.timed_schedule = os.getenv(f"{env_prefix}_TIMED_TEMPLATE_ENABLED", "false").lower() == "true"
        if self.timed_schedule and (self.mode != "template" or self.profile != THERAPY_TEMPLATE_PROFILE):
            raise ValueError("Timed offers require template mode and the therapy_schedule_v1 profile")
        self.template = os.getenv(f"{env_prefix}_TEMPLATE_NAME", "").strip()
        self.language = os.getenv(f"{env_prefix}_TEMPLATE_LANGUAGE",
                                 "" if self.profile == THERAPY_TEMPLATE_PROFILE else "en_US").strip()
        if self.mode == "session":
            self.parameters = []
        elif self.profile == THERAPY_TEMPLATE_PROFILE:
            self.parameters = list(THERAPY_TEMPLATE_PARAMETERS)
        else:
            self.parameters = json.loads(os.getenv(
                f"{env_prefix}_TEMPLATE_PARAMETERS", '["{patient}", "{visit_details}"]',
            ))
        if self.mode == "template" and (not isinstance(self.parameters, list) or not self.parameters or not all(
            isinstance(value, str) for value in self.parameters
        )):
            raise ValueError("Template parameters must be a nonempty JSON string array")
        sample = dict.fromkeys(("patient", "visit_details", "reference", "centre", "date", "time",
                                "greeting_name", "therapy", "date_display", "schedule",
                                "session_details", "therapy_count", "message_context"), "TEST")
        try:
            for value in self.parameters:
                value.format_map(sample)
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid template parameter placeholder") from exc
        if live and self.mode == "template" and not self.template:
            raise ValueError("Template mode needs FLOW_TEST_TEMPLATE_NAME")
        if live and self.mode == "template" and not self.language:
            raise ValueError("Set the exact approved template language before enabling messages")

    def send(self, kind: str, payload: dict[str, Any]) -> str:
        """Return the provider message ID without logging contacts or credentials."""
        recipient = payload["recipient"]
        if self.live and recipient not in self.allowed:
            raise ValueError("Recipient is not a registered test contact")
        if not self.live:
            return f"preview-{secrets.token_hex(8)}"
        if kind == "OFFER":
            marked = payload.get("template_profile") == THERAPY_TEMPLATE_PROFILE
            configured = self.mode == "template" and self.profile == THERAPY_TEMPLATE_PROFILE
            if marked != configured:
                raise WhatsAppConfigurationError("Offer and configured template profile do not match; review the queue")
            if self.mode == "session":
                if payload.get("session_expires_at", 0) <= time.time():
                    raise ValueError("Coworker must message first to open a 24-hour window")
                fields = payload["fields"]
                result = send_reply_buttons(
                    self.sender, recipient,
                    format_offer_message(fields),
                    tuple((f"{payload.get('button_prefix', 'ft')}:{payload['token']}:{action}", title) for action, title in (
                        ("YES", "Yes"), ("NO", "Cannot attend"), ("HELP", "Help"),
                    )),
                )
            else:
                values = tuple(value.format_map(payload["fields"]) for value in self.parameters)
                result = send_template_message(
                    self.sender, recipient, self.template, self.language, values,
                    tuple(f"{payload.get('button_prefix', 'ft')}:{payload['token']}:{action}"
                          for action in (("YES", "NO") if configured else ("YES", "NO", "HELP"))),
                )
        else:
            result = send_text_message(self.sender, recipient, payload["text"])
        return str(result["messages"][0]["id"])


class FlowTest(Reliability):
    """Serialize local test mutations and persist offers, jobs and audit records."""

    is_test = True
    solver_settings = SETTINGS
    message_prefix = "TEST ONLY: "
    button_prefix = "ft"

    def __init__(
        self, database_url: str | None = None, *, live: bool = False,
        recipients: dict[str, str] | None = None,
        recipient_names: dict[str, str] | None = None,
        database_schema: str | None = None,
        send: Callable[[str, dict[str, Any]], str] | None = None,
        scenario: dict | None = None,
    ):
        self.live = live
        self.configure_reliability()
        self.message_mode = os.getenv("FLOW_TEST_MESSAGE_MODE", "session").strip().lower()
        if self.message_mode not in {"session", "template"}:
            raise ValueError("FLOW_TEST_MESSAGE_MODE must be session or template")
        self.recipients = recipients or {}
        self.recipient_names = recipient_names or {}
        # Inbound callbacks must be persisted while a send is waiting on HTTP.
        # Imports/resets take both locks, always in workflow-then-inbound order.
        self.scenario = (json.loads(json_text(scenario)) if scenario is not None else
                         json.loads((ROOT / "scenario.json").read_text(encoding="utf-8")))
        self.offer_minutes = int(os.getenv("FLOW_TEST_OFFER_MINUTES", "600"))
        if not 1 <= self.offer_minutes <= 1440:
            raise ValueError("Offer minutes must be between 1 and 1440")
        self.send_batch_size = int(os.getenv("FLOW_TEST_SEND_BATCH_SIZE", "5"))
        if not 1 <= self.send_batch_size <= 20:
            raise ValueError("Send batch size must be between 1 and 20")
        # Validate transport before creating any database records.
        self.transport = None if send else TestTransport(live, self.recipients)
        self.send = send or self.transport.send
        if database_url is None:
            (ROOT / "data").mkdir(exist_ok=True)
            name = "flow_live.sqlite" if live else "flow_preview.sqlite"
            database_url = f"sqlite:///{(ROOT / 'data' / name).as_posix()}"
        self.database_schema = database_schema
        self.engine = create_test_engine(database_url, database_schema)
        self.lock, self.inbound_lock = workflow_locks(self.engine, database_schema)
        try:
            with self.lock, self.inbound_lock:
                initialize_test_tables(self.engine, database_schema)
        except Exception:
            self.engine.dispose()
            raise
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.lock, self.inbound_lock, self.sessions.begin() as session:
            imported = session.get(FlowSetting, "zoho_test_scenario")
            if imported is not None:
                self.scenario = json.loads(imported.value)
                self.recipients = {
                    item["code"]: item["phone"] for item in self.scenario["patients"]
                }
            marker = session.get(FlowSetting, "scenario")
            mode = session.get(FlowSetting, "mode")
            expected_mode = "live" if live else "preview"
            scenario_version = self.scenario.get("version", "malaparamba-fictional-v1")
            if mode is not None and mode.value != expected_mode:
                raise ValueError("Live and preview modes require separate test databases")
            if marker is None:
                if session.scalar(select(Patient.id).limit(1)) is not None:
                    raise ValueError("Test seed requires an empty database")
                self._seed(session)
                session.add(FlowSetting(key="scenario", value=scenario_version))
                session.add(FlowSetting(key="mode", value=expected_mode))
            elif marker.value != scenario_version:
                self._expand_scenario(session, marker, scenario_version)
            self._recover_processing(session)
        if self.transport:
            self.transport.allowed = self.contact_numbers()
        self.expire_offers()

    def contact_numbers(self, session: Session | None = None) -> set[str]:
        """Route current parents and existing offer recipients to this test flow."""
        if session is None:
            with self.inbound_lock, self.sessions() as session:
                return self.contact_numbers(session)
        source = session.get(FlowSetting, "zoho_test_scenario")
        contacts = ({item["phone"] for item in json.loads(source.value)["patients"] if item.get("phone")}
                    if source else set(self.recipients.values()))
        return contacts | set(session.scalars(select(FlowOffer.recipient).where(
            ~FlowOffer.recipient.startswith("preview:"),
        )))

    def use_source_contacts(self, scenario: dict) -> None:
        """Use validated Zoho Parent No. values for new offers and live transport."""
        with self.lock:
            self.scenario = scenario
            self.recipients = {item["code"]: item["phone"] for item in scenario["patients"]}
            if self.transport:
                self.transport.allowed = self.contact_numbers()

    def _seed(self, session) -> None:
        rows = self.scenario.get("locations") or [{"name": self.scenario["centre"], "capacity": self.scenario["capacity"]}]
        locations = {}
        for row in rows:
            location = Location(name=row["name"], capacity=row["capacity"])
            session.add(location)
            locations[row.get("branch", self.scenario["centre"])] = location
        default_location = next(iter(locations.values()))
        departments = {name: Department(name=name) for name in {t["therapy"] for t in self.scenario["therapists"]}}
        session.add_all(departments.values())
        session.flush()
        therapists = {}
        for row in self.scenario["therapists"]:
            location = locations.get(row.get("branch"), default_location)
            therapist = Therapist(
                therapist_code=row["code"], name=row["name"],
                department_id=departments[row["therapy"]].id, location_id=location.id,
                active=row.get("active", True),
            )
            session.add(therapist)
            session.flush()
            therapists[row["code"]] = therapist
            for weekday in range(6):
                for slot in range(len(SLOTS)):
                    if slot not in row.get("slots", range(len(SLOTS))):
                        session.add(TherapistAvailability(
                            therapist_id=therapist.id, day_of_week=weekday,
                            slot_index=slot, available=False,
                        ))
        self._add_patients(session, self.scenario["patients"], default_location, departments, therapists)

    def _add_patients(self, session, rows, location, departments, therapists) -> None:
        """Add fictional children and their explicitly configured treatment plans."""
        for row in rows:
            patient = Patient(patient_code=row["code"], name=row["name"])
            session.add(patient)
            session.flush()
            for index, requirement in enumerate(row["requirements"]):
                session.add(TreatmentRequirement(
                    patient_id=patient.id, department_id=departments[requirement["therapy"]].id,
                    assigned_therapist_id=therapists[requirement["therapist"]].id,
                    location_id=therapists[requirement["therapist"]].location_id, sessions_per_week=requirement["weekly"],
                    sequence_order=index, active=True,
                    duration_months=requirement.get("duration_months"),
                    plan_start_date=(date.fromisoformat(requirement["plan_start_date"])
                                     if requirement.get("plan_start_date") else None),
                    plan_added_at=(datetime.fromisoformat(requirement["plan_added_at"])
                                   if requirement.get("plan_added_at") else None),
                ))
            for weekday in range(6):
                for slot in range(len(SLOTS)):
                    if slot not in row.get("slots", range(len(SLOTS))):
                        session.add(PatientAvailability(
                            patient_id=patient.id, day_of_week=weekday,
                            slot_index=slot, available=False,
                        ))

    def _expand_scenario(self, session, marker, version: str) -> None:
        """Upgrade the known four-child fixture without rewriting its history."""
        if (marker.value not in {"malaparamba-fictional-v1", "malaparamba-fictional-v2"}
                or version != "malaparamba-fictional-v3"):
            raise ValueError("Unknown test scenario upgrade; review the existing data")
        location = session.scalar(select(Location).where(Location.name == self.scenario["centre"]))
        departments = {d.name: d for d in session.scalars(select(Department))}
        therapists = {t.therapist_code: t for t in session.scalars(select(Therapist))}
        original = {f"TEST-{letter}" for letter in
                    ("ABCD" if marker.value.endswith("v1") else "ABCDEFGH")}
        existing = set(session.scalars(select(Patient.patient_code)))
        added = [row for row in self.scenario["patients"] if row["code"] not in original]
        if (location is None or not original <= existing
                or {row["code"] for row in added} != ({f"TEST-{c}" for c in "ABCDEFGH"} - original)
                or any(row["code"] in existing for row in added)
                or any(row["code"] not in therapists or row["therapy"] not in departments
                       for row in self.scenario["therapists"])):
            raise ValueError("Test scenario expansion conflicts with existing records; staff review required")
        for row in self.scenario["therapists"]:
            therapist = therapists[row["code"]]
            if (therapist.department_id != departments[row["therapy"]].id
                    or therapist.location_id != location.id):
                raise ValueError("Existing test therapist assignments changed; staff review required")
        self._add_patients(session, added, location, departments, therapists)
        for row in self.scenario["therapists"]:
            therapist = therapists[row["code"]]
            for item in session.scalars(select(TherapistAvailability).where(
                TherapistAvailability.therapist_id == therapist.id,
            )):
                item.available = True
        patient_ids = select(Patient.id).where(Patient.patient_code.in_(original))
        for item in session.scalars(select(PatientAvailability).where(
            PatientAvailability.patient_id.in_(patient_ids),
        )):
            item.available = True
        marker.value = version
        self._audit(session, "flow_scenario_expanded", version)

    def reset_test_data(self, confirmation: str) -> dict:
        """Clear only this isolated test store and reseed inputs without sending."""
        expected = "RESET flow_test" if self.live else "RESET preview"
        if confirmation != expected:
            raise ValueError(f"Reset requires the exact confirmation: {expected}")
        if self.live and (self.database_schema != "flow_test"
                          or self.engine.dialect.name != "postgresql"):
            raise ValueError("Live reset is restricted to the isolated PostgreSQL flow_test schema")
        with self.sender_lock, self.ack_sender_lock, self.lock, self.inbound_lock, self.sessions.begin() as session:
            mode = session.get(FlowSetting, "mode")
            marker = session.get(FlowSetting, "scenario")
            if (mode is None or mode.value != ("live" if self.live else "preview")
                    or marker is None or marker.value not in {
                        "malaparamba-fictional-v1", "malaparamba-fictional-v2",
                        "malaparamba-fictional-v3",
                    }):
                raise ValueError("Reset requires a recognized fictional test workspace")
            known = {row["code"] for row in self.scenario["patients"]}
            if any(code not in known for code in session.scalars(select(Patient.patient_code).where(Patient.active.is_(True)))):
                raise ValueError("Unexpected child records found; reset stopped for review")
            # Explicit tables and the engine's schema map prevent cross-schema
            # deletion. Ordinary DELETE preserves PostgreSQL ID sequences.
            session.expunge_all()
            for table in reversed(sort_tables(TEST_TABLES)):
                session.execute(delete(table))
            self._seed(session)
            cutoff = int(time.time())
            version = self.scenario.get("version", "malaparamba-fictional-v1")
            session.add_all([
                FlowSetting(key="scenario", value=version),
                FlowSetting(key="mode", value="live" if self.live else "preview"),
                FlowSetting(key="reset_cutoff", value=str(cutoff)),
            ])
            self._audit(session, "flow_test_reset", version)
            if self.scenario.get("source"):
                session.add(FlowSetting(key="zoho_test_scenario", value=json_text(self.scenario)))
        if self.transport:
            self.transport.allowed = self.contact_numbers()
        return {"reset": True, "schema": self.database_schema or "preview",
                "patients": len(self.scenario["patients"]),
                "therapists": len(self.scenario["therapists"]), "messages_sent": 0}

    @staticmethod
    def _job(session, key: str, kind: str, payload: dict) -> FlowJob:
        digest = hashlib.sha256(key.encode()).hexdigest()
        job = session.scalar(select(FlowJob).where(FlowJob.event_key == digest))
        if job is None:
            job = FlowJob(event_key=digest, kind=kind, payload=json_text(payload))
            if kind == "TEXT" and payload.get("send_before"):
                job.deadline_at = DateTime.utcfromtimestamp(payload["send_before"])
            session.add(job)
            session.flush()
        return job

    @staticmethod
    def _audit(session, action: str, entity: str) -> None:
        session.add(AuditLog(action=action, entity=entity, user="flow_test_admin"))

    @staticmethod
    def _snapshot(items) -> str:
        return json_text([[a.id, a.date.isoformat(), a.slot_index, a.patient_id,
                           a.therapist_id, a.department_id, a.location_id, a.bundle_id]
                          for a in sorted(items, key=lambda a: a.id)])

    @staticmethod
    def _offer_items(session, offer: FlowOffer) -> list[Appointment]:
        ids = json.loads(offer.appointment_ids)
        items = list(session.scalars(select(Appointment).where(Appointment.id.in_(ids))))
        if len(items) != len(ids) or FlowTest._snapshot(items) != offer.snapshot:
            raise ValueError("Offer refers to a changed appointment")
        return sorted(items, key=lambda a: a.slot_index)

    @staticmethod
    def _session_deadline(session, recipient: str) -> float:
        business_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        window = session.get(FlowContactWindow, (recipient, business_id))
        if window is None:
            return 0
        return (window.last_inbound - datetime(1970, 1, 1)).total_seconds() + 86400

    def _daily_result(
        self, session, target: date, patient_codes: list[str] | None,
        settings: SolverSettings,
    ) -> tuple[SchedulingProblem, ScheduleResult]:
        """Bound new slots to one day, retaining the full week's fixed demand."""
        if target.weekday() == 6:
            raise ValueError("Choose Monday through Saturday")
        source = session.get(FlowSetting, "zoho_test_scenario")
        if source:
            self.use_source_contacts(json.loads(source.value))
        if patient_codes is None:
            patient_codes = [p["code"] for p in self.scenario["patients"] if p["initial_roster"]]
        elif not patient_codes or not set(patient_codes) <= {p["code"] for p in self.scenario["patients"]}:
            raise ValueError("Select known fictional patients for the draft")
        start = target - timedelta(days=target.weekday())
        problem = load_scheduling_problem(session, start, planning_start_date=target)
        input_fingerprint = self._planning_fingerprint(session, problem)
        selected_ids = set(session.scalars(select(Patient.id).where(
            Patient.patient_code.in_(patient_codes), Patient.active.is_(True),
        )))
        pinned_counts = Counter(a.requirement_id for a in problem.pinned)
        problem.requirements = [
            r if r.patient_id in selected_ids else replace(r, sessions_per_week=pinned_counts[r.id])
            for r in problem.requirements
        ]
        limit_new_bookings_to_day(problem, target)
        result = generate_weekly_schedule(problem, settings=settings)
        result.statistics["input_fingerprint"] = input_fingerprint
        if result.status not in {"FEASIBLE", "OPTIMAL"} or any(result.validation.values()):
            raise ValueError("No valid daily schedule could be generated")
        return problem, result

    @staticmethod
    def _planning_fingerprint(session, problem: SchedulingProblem) -> str:
        """Detect changed bookings, requirements or constraints since review."""
        values = {
            "requirements": [asdict(r) for r in sorted(problem.requirements, key=lambda r: r.id)],
            "pinned": [asdict(a) for a in sorted(problem.pinned, key=lambda a: (
                a.date, a.slot_index, a.patient_id, a.requirement_id, a.bundle_id))],
            "therapists": sorted(problem.active_therapists),
            "therapist_availability": sorted(problem.therapist_availability),
            "patient_availability": sorted(problem.patient_availability),
            "preferences": sorted(problem.patient_preferences.items()),
            "absences": sorted(problem.absences),
            "patient_absences": sorted(problem.patient_absences),
            "locations": sorted(problem.location_capacities.items()),
            "patients": [tuple(row) for row in session.execute(select(Patient.id, Patient.active).order_by(Patient.id))],
        }
        return hashlib.sha256(json.dumps(values, default=str, sort_keys=True).encode()).hexdigest()

    def preview_daily(
        self, target: date, patient_codes: list[str] | None = None,
        *, settings: SolverSettings | None = None,
    ) -> tuple[ScheduleResult, tuple[ScheduledAppointment, ...]]:
        """Return an unsaved daily plan and the fixed visits counted in its week."""
        with self.lock, self.sessions() as session:
            problem, result = self._daily_result(session, target, patient_codes, settings or self.solver_settings)
            return result, tuple(problem.pinned)

    def generate(self, target: date, patient_codes: list[str] | None = None) -> dict:
        """Save a daily draft while counting all saved bookings in that week."""
        with self.lock, self.sessions.begin() as session:
            problem, result = self._daily_result(session, target, patient_codes, self.solver_settings)
            added = 0
            for item in result.appointments:
                if item in problem.pinned:
                    continue
                session.add(Appointment(
                    patient_id=item.patient_id, treatment_requirement_id=item.requirement_id,
                    therapist_id=item.therapist_id, department_id=item.department_id,
                    location_id=item.location_id, date=item.date, slot_index=item.slot_index,
                    status=AppointmentStatus.PROPOSED,
                    bundle_id=f"FT-{secrets.token_hex(5)}-{item.bundle_id}"[:80], source="flow_test",
                ))
                added += 1
            # Give all members of each solver bundle the same persisted ID.
            session.flush()
            new_items = list(session.scalars(select(Appointment).where(
                Appointment.date == target, Appointment.status == AppointmentStatus.PROPOSED,
            )))
            groups = {}
            for item in new_items:
                key = (item.patient_id, item.date)
                item.bundle_id = groups.setdefault(key, item.bundle_id)
            self._audit(session, "flow_daily_draft", target.isoformat())
            return {"added": added, "unmet": [
                {"requirement_id": u.requirement_id, "missing": u.missing_sessions, "reason": u.reason}
                for u in result.unmet
            ]}

    def publish(self, target: date, appointment_id: int | None = None) -> int:
        """Queue each complete draft visit once, including replacement proposals."""
        with self.lock, self.sessions.begin() as session:
            return self._publish(session, target, appointment_id)

    @staticmethod
    def next_offer_date() -> date:
        """Use the centre's local tomorrow, independent of the server timezone."""
        return datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)

    def save_calendar(
        self, result: ScheduleResult, target: date, *, expected_offer_on_save: bool,
    ) -> dict:
        """Save one reviewed day and atomically queue tomorrow's proposed visits."""
        if (
            result.status not in {"OPTIMAL", "FEASIBLE"}
            or any(result.validation.values())
        ):
            raise ValueError("This preview did not pass validation")
        if any(item.date != target for item in result.appointments):
            raise ValueError("This preview must contain only the selected day")
        with self.lock, self.sessions.begin() as session:
            offer_on_save = target == self.next_offer_date()
            if offer_on_save != expected_offer_on_save:
                raise ValueError(
                    "The local date changed. Generate a fresh preview before "
                    "saving so you can review whether WhatsApp offers will be sent."
                )
            current = load_scheduling_problem(
                session, target - timedelta(days=target.weekday()), planning_start_date=target,
            )
            if result.statistics.get("input_fingerprint") != self._planning_fingerprint(session, current):
                raise ValueError("Bookings or scheduling inputs changed after this preview. Generate and review a fresh preview before saving.")
            # The existing saver commits internally. Joining the outer transaction
            # in rollback-only mode suppresses that commit so offer checks and
            # queue creation can still roll back the entire save, including SQLite.
            with sessionmaker(
                bind=session.connection(), join_transaction_mode="rollback_only",
                expire_on_commit=False,
            )() as saving_session:
                added = save_proposed_schedule(saving_session, result, target, 1)
            offers = self._publish(session, target) if offer_on_save else 0
            waiting = sum(json.loads(job.payload).get("fields", {}).get("date") == target.isoformat()
                          for job in session.scalars(select(FlowJob).where(
                              FlowJob.kind == "OFFER", FlowJob.status == "WAITING_FOR_CONTACT"))) if offer_on_save else 0
            message = f"Saved {added} new appointments. "
            if offer_on_save:
                message += (f"Queued {offers} WhatsApp visit offer(s) for tomorrow."
                            if self.live else
                            f"Queued {offers} preview visit offer(s) for tomorrow. No messages sent.")
                if waiting:
                    message += f" {waiting} offer(s) are waiting for a parent message to open the WhatsApp contact window."
            else:
                message += (
                    "WhatsApp offers are queued automatically only when "
                    "saving tomorrow's calendar."
                )
            return {"message": message, "added": added, "offers": offers, "waitingForContact": waiting,
                    "offerDate": target.isoformat() if offers else None}

    def _publish(self, session, target: date, appointment_id: int | None = None) -> int:
        """Queue offers using the caller's transaction, without sending."""
        from flow_test.zoho_import import require_reconciled_source

        require_reconciled_source(session)
        if uses_daily_template(self) and target.weekday() == 6:
            raise ValueError("Sunday is a holiday; no therapy-template offers may target Sunday")
        source = session.get(FlowSetting, "zoho_test_scenario")
        if source:
            # Another replica may have imported new contacts since this process
            # started. Publishing must use the committed source, not its cache.
            self.use_source_contacts(json.loads(source.value))
        query = select(Appointment).join(Patient).where(
            Appointment.date == target, Appointment.status == AppointmentStatus.PROPOSED,
            Patient.active.is_(True),
        ).order_by(Appointment.id)
        drafts = list(session.scalars(query))
        if appointment_id is not None:
            selected = session.get(Appointment, appointment_id)
            if selected is None or selected not in drafts:
                raise ValueError("Choose a current proposed appointment")
            drafts = [a for a in drafts if a.patient_id == selected.patient_id and a.bundle_id == selected.bundle_id]
        groups = {}
        for item in drafts:
            groups.setdefault((item.patient_id, item.bundle_id), []).append(item)
        # Validate the entire batch before queuing anything.
        for patient_id, _ in groups:
            code = session.get(Patient, patient_id).patient_code
            if self.live and code not in self.recipients:
                raise ValueError(f"Add a parent phone number for {code} before publishing")
        for (patient_id, _), items in groups.items():
            patient = session.get(Patient, patient_id)
            token = secrets.token_hex(10)
            recipient = self.recipients.get(patient.patient_code, f"preview:{patient.patient_code}")
            offer = FlowOffer(
                created_at=datetime.utcnow(),
                token=token, patient_code=patient.patient_code, recipient=recipient,
                appointment_ids=json_text([a.id for a in items]), snapshot=self._snapshot(items),
            )
            session.add(offer)
            details = "; ".join(
                f"{session.get(Department, a.department_id).name} {slot_label(a.slot_index)}"
                for a in sorted(items, key=lambda a: a.slot_index)
            )
            therapy_names = list(dict.fromkeys(
                session.get(Department, a.department_id).name
                for a in sorted(items, key=lambda a: a.slot_index)
            ))
            schedule = "\n".join(
                f"{session.get(Department, a.department_id).name}: "
                f"{SLOTS[a.slot_index].start_time.strftime('%I:%M %p').lstrip('0')}–"
                f"{SLOTS[a.slot_index].end_time.strftime('%I:%M %p').lstrip('0')} IST"
                for a in sorted(items, key=lambda a: a.slot_index)
            )
            fields = {
                "patient": f"TEST ONLY — {patient.name}" if self.is_test else patient.name,
                "test_only": self.is_test, "reference": token,
                "patient_code": patient.patient_code,
                "greeting_name": self.recipient_names.get(patient.patient_code) or patient.name or patient.patient_code,
                "therapy": " and ".join(therapy_names), "schedule": schedule,
                "compact_schedule": "\n".join(
                    f"{session.get(Department, a.department_id).name}: "
                    f"{SLOTS[a.slot_index].start_time:%H:%M}–{SLOTS[a.slot_index].end_time:%H:%M}"
                    for a in sorted(items, key=lambda a: a.slot_index)),
                "date_display": target.strftime("%A, %d %B %Y"),
                "centre": session.get(Location, items[0].location_id).name, "date": target.isoformat(),
                "time": slot_label(min(a.slot_index for a in items)),
                "visit_details": f"{'TEST ONLY | ' if self.is_test else ''}{target.isoformat()} IST | {details} | Ref {token}",
            }
            if uses_daily_template(self):
                fields.update(template_fields(
                    (session.get(Department, a.department_id).name,
                     SLOTS[a.slot_index].start_time, SLOTS[a.slot_index].end_time)
                    for a in sorted(items, key=lambda a: a.slot_index)
                ))
            if self.message_mode == "session" and len(format_offer_message(fields)) > 1024:
                raise ValueError(f"The WhatsApp offer for {patient.patient_code} is too long. Review therapy labels before publishing.")
            outgoing = self._job(session, f"offer:{token}", "OFFER", {
                "token": token, "recipient": recipient, "fields": fields, "button_prefix": self.button_prefix,
            })
            if uses_daily_template(self):
                schedule_initial_job(self, outgoing, json.loads(outgoing.payload), target)
            if self.live:
                outgoing.deadline_at = min(DateTime.combine(a.date, SLOTS[a.slot_index].start_time,
                    tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).replace(tzinfo=None) for a in items)
            if self.live and self.message_mode == "session" and self._session_deadline(session, recipient) <= time.time():
                outgoing.status = "WAITING_FOR_CONTACT"
                outgoing.error = "Parent must message first; no send was attempted"
            for item in items:
                item.status = AppointmentStatus.PENDING_CONFIRMATION
            self._audit(session, "flow_offer_published", f"offer:{token}")
        return len(groups)


    def reserve_with_message(
        self, source_id: int, *, patient_id: int, request_id: str,
        preview_token: str, whole_visit: bool = True, override_warnings: bool = False,
    ) -> dict:
        """Atomically reserve a replacement and queue its confirmation offer."""
        with self.lock, self.sessions.begin() as session:
            # Serialize competing reservations of this opening across processes.
            session.execute(select(Appointment).where(
                Appointment.id == source_id,
            ).with_for_update()).scalar_one_or_none()
            retry = session.get(AdminBookingDecision, request_id) is not None
            result = reserve_admin_slot(
                session, source_id=source_id, patient_id=patient_id,
                request_id=request_id, preview_token=preview_token,
                whole_visit=whole_visit, override_warnings=override_warnings,
                mode="with_messaging", allow_pending_offer=True,
                admin_user=getattr(self, "admin_id", "flow_test_admin"),
            )
            ids = result["appointment_ids"]
            appointment = session.get(Appointment, ids[0])
            if not retry:
                self._publish(session, appointment.date, appointment.id)
                session.flush()
            # A replay returns the original offer even after a reply or closure;
            # it must never resend it or turn a confirmed visit back into pending.
            offer = next((o for o in session.scalars(select(FlowOffer))
                          if set(json.loads(o.appointment_ids)) == set(ids)), None)
            if offer is None:
                raise ValueError("The original booking has no offer. Review it in Operations.")
            return {**result, "offer_token": offer.token, "offer_state": offer.state,
                    "status": appointment.status.value,
                    "mode": "live" if self.live else "preview"}

    def receive(self, payload: dict) -> int:
        """Durably enqueue registered-contact events; caller verifies signature."""
        count = 0
        business_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        with self.inbound_lock, self.sessions.begin() as session:
            contacts = self.contact_numbers(session)
            reset = session.get(FlowSetting, "reset_cutoff")
            reset_cutoff = int(reset.value) if reset else None
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if value.get("metadata", {}).get("phone_number_id") != business_id or not business_id:
                        continue
                    for message in value.get("messages", []):
                        if message.get("from") not in contacts or not message.get("id"):
                            continue
                        # Use the event's timestamp, never callback arrival time:
                        # replaying an old callback cannot reopen a service window.
                        try:
                            inbound_time = datetime.utcfromtimestamp(int(message.get("timestamp", "")))
                        except (ValueError, OverflowError, OSError):
                            inbound_time = None
                        if (reset_cutoff is not None and inbound_time is not None
                                and int(message["timestamp"]) <= reset_cutoff):
                            continue
                        if inbound_time is not None and inbound_time <= datetime.utcnow() + timedelta(minutes=5):
                            key = (message["from"], business_id)
                            window = session.get(FlowContactWindow, key)
                            if window is None:
                                session.add(FlowContactWindow(recipient=key[0], business_id=key[1], last_inbound=inbound_time))
                            elif inbound_time > window.last_inbound:
                                window.last_inbound = inbound_time
                            session.flush()
                            if self._session_deadline(session, message["from"]) > time.time():
                                for waiting in session.scalars(select(FlowJob).where(
                                    FlowJob.kind == "OFFER", FlowJob.status == "WAITING_FOR_CONTACT",
                                )):
                                    if json.loads(waiting.payload).get("recipient") == message["from"]:
                                        waiting.status, waiting.error = "QUEUED", None
                        self._job(session, f"in:{business_id}:{message['id']}", "INBOUND", message)
                        count += 1
                    for status in value.get("statuses", []):
                        if status.get("recipient_id") not in contacts:
                            continue
                        if reset_cutoff is not None:
                            known_message = session.scalar(select(FlowJob.id).where(
                                FlowJob.message_id == status.get("id"),
                                FlowJob.kind.in_(("OFFER", "TEXT")),
                            ).limit(1))
                            if known_message is None:
                                continue
                        if status.get("id") and status.get("status") in {"sent", "delivered", "read", "failed"}:
                            self._job(session, f"status:{business_id}:{status['id']}:{status['status']}:{status.get('timestamp')}", "STATUS", status)
            return count

    def simulate(self, token: str, action: str) -> None:
        """Stage an explicit preview reply; this endpoint is disabled in live mode."""
        if self.live:
            raise ValueError("Simulated replies are available only in preview mode")
        if action not in {"YES", "NO", "VISIT", "DAY", "HELP", "RESCHEDULE"}:
            raise ValueError("Unknown preview action")
        with self.lock, self.sessions.begin() as session:
            offer = session.get(FlowOffer, token)
            if offer is None:
                raise ValueError("Unknown offer")
            self._job(session, f"simulation:{secrets.token_hex(12)}", "INBOUND", {
                "from": offer.recipient, "timestamp": str(int(time.time())),
                "button": {"payload": f"ft:{token}:{action}"},
            })

    def _inbox(self, session, job, reason: str, text: str, code: str | None = None) -> None:
        if session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == job.event_key)) is None:
            session.add(FlowInbox(event_key=job.event_key, patient_code=code, reason=reason, text=text[:4000]))

    def _inbound(self, session, job: FlowJob) -> None:
        message = json.loads(job.payload)
        text = str(message.get("text", {}).get("body", "")).strip()
        if text.upper() == "START":
            self._audit(session, "flow_coworker_ready", "contact_window")
            return
        button = message.get("button", {}).get("payload") or message.get("interactive", {}).get("button_reply", {}).get("id")
        match = re.fullmatch(re.escape(self.button_prefix) + r":([a-f0-9]{20}):(YES|NO|VISIT|DAY|HELP|RESCHEDULE)", str(button or ""))
        action, token = "", ""
        if match:
            token, action = match.groups()
        else:
            command = re.fullmatch(r"(YES|NO|VISIT|DAY|HELP|RESCHEDULE)\s+([a-f0-9]{20})", text, re.I)
            if command:
                action, token = command.group(1).upper(), command.group(2).lower()
            elif text.upper() in {"YES", "NO", "VISIT", "DAY", "HELP", "RESCHEDULE"}:
                context = message.get("context", {}).get("id")
                original = session.scalar(select(FlowJob).where(FlowJob.message_id == context)) if context else None
                if original and original.kind == "OFFER":
                    token = json.loads(original.payload)["token"]
                    action = text.upper()
        offer = session.get(FlowOffer, token) if token else None
        if offer is None or offer.recipient != message.get("from"):
            self._inbox(session, job, "Unmatched message — staff review", text or "Unrecognized reply")
            return
        code = offer.patient_code
        try:
            timestamp = datetime.utcfromtimestamp(int(message.get("timestamp", "")))
            if timestamp < offer.created_at - timedelta(minutes=1) or timestamp > datetime.utcnow() + timedelta(minutes=5):
                raise ValueError("Invalid event time")
            items = self._offer_items(session, offer)
            if offer.state not in {"PENDING", "CONFIRMED", "EXPIRED"} or any(a.status not in ACTIVE for a in items):
                raise ValueError("Offer is closed or appointment is no longer active")
            if offer.expires_at and offer.expires_at <= timestamp:
                raise ValueError("Offer expired")
        except (ValueError, OverflowError, OSError):
            self._inbox(session, job, "Stale reply — staff review", text or action, code)
            return
        if action in {"HELP", "RESCHEDULE"}:
            self._audit(session, f"flow_reply_{action.lower()}", f"offer:{token}")
            self._inbox(session, job, f"Parent requested {action.lower()}", text or action, code)
            self._job(session, f"help:{job.event_key}", "TEXT", {
                "recipient": offer.recipient, "token": token, "expected_state": offer.state,
                "send_before": int(message["timestamp"]) + 86400,
                "text": self.message_prefix + ("Your request is in the test admin inbox. Your current booking is unchanged."
                         if self.is_test else "Your request is in the staff inbox. Your current booking is unchanged."),
            })
        elif action in {"YES", "NO", "VISIT", "DAY"}:
            whole_day = action in {"NO", "DAY"}
            affected = items
            if whole_day:
                affected = list(session.scalars(select(Appointment).where(
                    Appointment.patient_id == items[0].patient_id,
                    Appointment.date == items[0].date,
                    Appointment.status.in_(ACTIVE),
                ).order_by(Appointment.id)))
                # The unchanged response service rejects a day absence when
                # delivered therapy exists. Preserve that history for staff review.
                completed = session.scalar(select(Appointment.id).where(
                    Appointment.patient_id == items[0].patient_id,
                    Appointment.date == items[0].date,
                    Appointment.status == AppointmentStatus.COMPLETED,
                ).limit(1))
                if completed is not None:
                    self._inbox(session, job, "Day unavailable conflicts with completed therapy — staff review", text or action, code)
                    return
            anchors = {}
            for item in affected:
                anchors.setdefault(item.bundle_id or item.id, item.id)
            for index, appointment_id in enumerate(anchors.values()):
                record_parent_response(
                    session, event_id=f"flow-{job.event_key}-{index}", appointment_id=appointment_id,
                    reply="YES" if action == "YES" else "NO",
                    unavailable_scope=None if action == "YES" else ("day" if whole_day else "visit"),
                )
            # Core response processing commits internally. Bind it to a SAVEPOINT
            # on our outer transaction so appointment changes and jobs commit together.
            session.flush()
            with sessionmaker(
                bind=session.connection(), join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )() as repair_session:
                process_daily_responses(repair_session, items[0].date, settings=self.solver_settings)
            session.expire_all()
            offer = session.get(FlowOffer, token)
            if whole_day:
                affected_ids = {item.id for item in affected}
                for related in session.scalars(select(FlowOffer).where(
                    FlowOffer.patient_code == code,
                    FlowOffer.state.in_(("PENDING", "CONFIRMED")),
                )):
                    if affected_ids.intersection(json.loads(related.appointment_ids)):
                        related.state = "DECLINED"
                        related.expires_at = None
                        if related.token != token:
                            self._audit(session, "flow_reply_day", f"offer:{related.token}")
            offer.state = "CONFIRMED" if action == "YES" else "DECLINED"
            if action == "YES":
                offer.expires_at = None
            expiry_item = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"expiry:{token}"))
            if expiry_item is not None:
                expiry_item.resolved = 1
            # Proposed replacements require admin review before any offer is sent.
            self._audit(session, f"flow_reply_{action.lower()}", f"offer:{token}")
            self._job(session, f"ack:{job.event_key}", "TEXT", {
                "recipient": offer.recipient, "token": token, "expected_state": offer.state,
                "send_before": int(message["timestamp"]) + 86400,
                "text": self.message_prefix + ("Visit confirmed." if action == "YES" else
                         f"Unavailable for the whole day on {items[0].date.isoformat()}. Your bookings on that date have been released; remaining weekly therapy needs are retained." if whole_day else
                         "Visit decline recorded. Your remaining weekly therapy needs are retained."),
            })

    def review_inactive_patient_offers(self, session, patient: Patient, today: date) -> None:
        """Withdraw unsent offers and route offers that may have reached a family to staff."""
        offers = session.scalars(select(FlowOffer).where(
            FlowOffer.patient_code == patient.patient_code, FlowOffer.state.in_(("PENDING", "EXPIRED")),
        ))
        for offer in offers:
            items = list(session.scalars(select(Appointment).where(
                Appointment.id.in_(json.loads(offer.appointment_ids)),
                Appointment.date >= today, Appointment.status == AppointmentStatus.PENDING_CONFIRMATION,
            )))
            if not items:
                continue
            key = hashlib.sha256(f"offer:{offer.token}".encode()).hexdigest()
            job = session.scalar(select(FlowJob).where(FlowJob.event_key == key))
            if job and job.status in {
                "QUEUED", "RETRY_WAIT", "WAITING_FOR_CONTACT", "WAITING_FOR_SYNC", "WAITING_FOR_ENABLE", "BLOCKED",
            }:
                job.status, job.error = "OBSOLETE", "Patient marked inactive before this offer was sent."
                offer.state, offer.expires_at = "CLOSED", None
                for item in items:
                    item.status = AppointmentStatus.CANCELLED_OTHER
                continue
            review_key = f"inactive:{offer.token}"
            if not session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == review_key)):
                session.add(FlowInbox(
                    event_key=review_key, patient_code=patient.patient_code,
                    reason="Inactive patient — review existing offer",
                    text=f"Review the {items[0].date.isoformat()} visit and any parent reply before finalising.",
                ))

    def _prepare_send(self, session, job, payload: dict) -> bool:
        """Validate a claimed outbound job before any network request."""
        from flow_test.zoho_import import reconciliation_required

        offer = session.get(FlowOffer, payload["token"])
        if offer is None:
            raise ValueError("Offer no longer exists")
        items = self._offer_items(session, offer)
        if job.kind == "OFFER" and not session.scalar(select(Patient.active).where(
            Patient.patient_code == offer.patient_code,
        )):
            job.status, job.error = "OBSOLETE", "Patient is inactive."
            return False
        if session.get(FlowSetting, PAUSE_KEY):
            job.status = "QUEUED"
            return False
        if job.attempts > self.max_send_attempts:
            self._terminal(session, job, "Retry limit reached. Contact the parent by phone.")
            return False
        if self.live:
            # Session-window expiry is handled below by waiting for contact.
            # The durable final send cutoff is the visit's first start time.
            deadlines = [DateTime.combine(a.date, SLOTS[a.slot_index].start_time,
                         tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).replace(tzinfo=None)
                         for a in items]
            if job.kind == "TEXT":
                deadlines = [datetime.utcfromtimestamp(payload.get("send_before", 0))]
            job.deadline_at = min(deadlines) if deadlines else datetime.utcnow()
            if job.deadline_at <= datetime.utcnow():
                self._terminal(session, job, "Sending deadline reached. Contact the parent by phone.")
                return False
        if job.kind == "OFFER" and (offer.state != "PENDING" or any(
                a.status != AppointmentStatus.PENDING_CONFIRMATION for a in items)):
            job.status = "OBSOLETE"
            return False
        if job.kind == "TEXT" and (offer.state != payload.get("expected_state")
                or time.time() >= payload.get("send_before", 0)):
            job.status = "OBSOLETE"
            return False
        if job.kind == "OFFER" and reconciliation_required(session):
            job.status = "WAITING_FOR_SYNC"
            job.error = "No send was attempted. Reconcile the Zoho source with --apply --reconcile."
            return False
        if job.kind == "OFFER" and self.live and any(
                a.date < datetime.now(ZoneInfo("Asia/Kolkata")).date() for a in items):
            raise ValueError("Live offers cannot use a past appointment date")
        if job.kind == "OFFER" and not prepare_template_job(self, session, job, payload):
            return False
        if job.kind == "OFFER" and self.live and self.message_mode == "session":
            deadline = self._session_deadline(session, payload["recipient"])
            if deadline <= time.time():
                job.status = "WAITING_FOR_CONTACT"
                job.error = "Coworker must send START; no send was attempted"
                return False
            payload["session_expires_at"] = deadline
        if self.transport:
            self.transport.allowed = self.contact_numbers(session)
        return True

    @staticmethod
    def _pacing_key(recipient: str) -> str:
        return "send_after:" + hashlib.sha256(recipient.encode()).hexdigest()

    def _recipient_ready(self, session, recipient: str) -> bool:
        if not self.live:
            return True
        saved = session.get(FlowSetting, self._pacing_key(recipient))
        return (saved is None or float(saved.value) <= time.time())

    def _pace_recipient(self, session, payload: dict) -> None:
        if self.live:
            key = self._pacing_key(payload["recipient"])
            saved = session.get(FlowSetting, key)
            if saved is None:
                session.add(FlowSetting(key=key, value=str(time.time() + 6)))
            else:
                saved.value = str(time.time() + 6)

    def _recover_processing(self, session) -> None:
        """Recover only after proving no sender owns this queue.

        Nonblocking acquisition is essential: callers already hold the workflow
        lock, whereas a sender takes sender-then-workflow. Never invert that order.
        """
        if not self.sender_lock.acquire(blocking=False):
            self._maintain_delivery(session)
            return
        try:
            if self.ack_sender_lock.acquire(blocking=False):
                try:
                    self._recover_abandoned(session)
                finally:
                    self.ack_sender_lock.release()
            else:
                self._maintain_delivery(session)
        finally:
            self.sender_lock.release()

    def _recover_abandoned(self, session) -> None:
        for job in session.scalars(select(FlowJob).where(FlowJob.status == "PROCESSING")):
            safe = job.kind in {"INBOUND", "STATUS"} or (job.locked_by and job.send_started_at is None)
            job.status = "QUEUED" if safe else "UNKNOWN"
            job.locked_by, job.locked_until = None, None
            job.error = "Interrupted processing; queued again before sending" if safe else "Interrupted processing; check delivery before any resend"
            if job.status == "UNKNOWN":
                self._send_failure_inbox(session, job)
        self._maintain_delivery(session)

    @cached_property
    def sender_lock(self):
        return WorkflowLock(self.engine, "sender", self.database_schema)

    @cached_property
    def ack_sender_lock(self):
        return WorkflowLock(self.engine, "ack_sender", self.database_schema)

    @cached_property
    def dispatch_control(self):
        from services.whatsapp_dispatch import DispatchControl
        control = DispatchControl(self.engine)
        control.check()
        return control

    def _business_number(self) -> str:
        if self.transport and self.transport.live:
            return self.transport.sender.phone_number_id
        return os.getenv("WHATSAPP_PHONE_NUMBER_ID", "injected-test-transport")

    def _claim_job(self, session, job_id: int) -> bool:
        """Claim durably only if another worker has not already taken the job."""
        result = session.execute(update(FlowJob).where(
            FlowJob.id == job_id, FlowJob.status == "QUEUED",
        ).values(status="PROCESSING", attempts=FlowJob.attempts + 1,
                 locked_by=self.worker_id, locked_until=datetime.utcnow() + timedelta(minutes=2),
                 send_started_at=None, next_attempt_at=None))
        return result.rowcount == 1

    def _sent(self, job_id: int, kind: str, payload: dict, provider_id: str) -> None:
        with self.sessions.begin() as session:
            job = session.get(FlowJob, job_id)
            job.message_id = provider_id
            job.status = "SENT" if self.live else "SIMULATED"
            job.accepted_at = datetime.utcnow()
            job.error, job.error_code, job.locked_by, job.locked_until = None, None, None, None
            self._pace_recipient(session, payload)
            if kind == "OFFER" and not payload.get("reminder"):
                offer = session.get(FlowOffer, payload["token"])
                if offer and offer.state == "PENDING":
                    offer.expires_at = datetime.utcnow() + timedelta(minutes=self.offer_minutes)

    def _failed_job(self, job_id: int, kind: str, exc: Exception) -> None:
        with self.sessions.begin() as session:
            job = session.get(FlowJob, job_id)
            if kind in {"OFFER", "TEXT"}:
                self._record_send_failure(session, job, exc)
                self._pace_recipient(session, json.loads(job.payload))
            else:
                job.status = "FAILED"
                job.error = f"{type(exc).__name__}: review this job"

    def _send_failure_inbox(self, session, job: FlowJob) -> None:
        key = f"unknown:{job.event_key}"
        if session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == key)):
            return
        payload = json.loads(job.payload)
        offer = session.get(FlowOffer, payload["token"]) if payload.get("token") else None
        session.add(FlowInbox(event_key=key, patient_code=offer.patient_code if offer else None,
                    reason="WhatsApp send needs staff review",
                    text="Delivery unknown. The provider may have accepted this message. Check delivery before taking action; it will not be resent automatically. Booking unchanged."))

    def _delivery_failure(self, session, original: FlowJob, status: dict) -> None:
        """Surface provider-confirmed failures without copying message bodies."""
        errors = status.get("errors", [])
        codes = sorted({str(error.get("code")) for error in errors
                        if isinstance(error, dict) and str(error.get("code", "")).isdigit()}) if isinstance(errors, list) else []
        code_text = f" Meta code(s): {', '.join(codes[:5])}." if codes else ""
        original.error = "Meta reported a delivery failure." + code_text
        key = f"delivery:{original.event_key}"
        if session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == key)) is None:
            offer = session.get(FlowOffer, json.loads(original.payload).get("token"))
            session.add(FlowInbox(
                event_key=key, patient_code=offer.patient_code if offer else None,
                reason="WhatsApp delivery failed — staff review",
                text="Meta reported a delivery failure." + code_text +
                     " Review delivery and contact the family before taking action. "
                     "The message will not be resent automatically; the booking is unchanged.",
            ))

    @staticmethod
    def _queue_order():
        return case((FlowJob.kind == "INBOUND", 0), (FlowJob.kind == "STATUS", 1),
                    (FlowJob.kind == "TEXT", 2), else_=3), FlowJob.id

    def process_events(self, limit: int = 20) -> int:
        """Apply replies/receipts independently of the outbound sender mutex."""
        count = 0
        with self.lock:
            for _ in range(limit):
                with self.sessions() as session:
                    job_id = session.scalar(select(FlowJob.id).where(
                        FlowJob.status == "QUEUED", FlowJob.kind.in_(("INBOUND", "STATUS")),
                        or_(FlowJob.next_attempt_at.is_(None), FlowJob.next_attempt_at <= datetime.utcnow())
                    ).order_by(*self._queue_order()).limit(1))
                if job_id is None:
                    break
                count += int(self._process_event(job_id))
        return count

    def _process_event(self, job_id: int) -> bool:
        with self.lock:
            with self.sessions.begin() as session:
                job = session.get(FlowJob, job_id)
                if not job or job.kind not in {"INBOUND", "STATUS"} or not self._claim_job(session, job_id):
                    return False
                kind, payload = job.kind, json.loads(job.payload)
            try:
                with self.sessions.begin() as session:
                    job = session.get(FlowJob, job_id)
                    if kind == "INBOUND":
                        self._inbound(session, job)
                    elif kind == "STATUS":
                        original = session.scalar(select(FlowJob).where(FlowJob.message_id == payload["id"]))
                        if original is None:
                            # A webhook can arrive before the HTTP response is
                            # saved. Retain it while a send to this parent is active.
                            active = list(session.scalars(select(FlowJob).where(
                                FlowJob.status == "PROCESSING", FlowJob.kind.in_(("OFFER", "TEXT")))))
                            if any(json.loads(item.payload).get("recipient") == payload.get("recipient_id") for item in active):
                                job.status = "QUEUED"
                                job.next_attempt_at = datetime.utcnow() + timedelta(seconds=1)
                                return True
                        if original and json.loads(original.payload).get("recipient") == payload.get("recipient_id"):
                            ranks = {None: 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
                            if ranks[payload["status"]] >= ranks.get(original.delivery, 0):
                                original.delivery = payload["status"]
                                if payload["status"] == "failed":
                                    self._delivery_failure(session, original, payload)
                                if payload["status"] in {"delivered", "read", "failed"}:
                                    key = f"attention:{original.event_key}:WhatsApp delivery overdue"
                                    for warning in session.scalars(select(FlowInbox).where(FlowInbox.event_key == key)):
                                        warning.resolved = 1
                    job.status = "DONE"
            except Exception as exc:
                self._failed_job(job_id, kind, exc)
            return True

    def process_batch(self) -> int:
        """Prioritize replies, then bounded acknowledgment/offer dispatch."""
        events = self.process_events()
        return events if events else self._dispatch_batch(self.send_batch_size)

    def queue_template_reminders(self) -> int:
        """Queue due reminders; sending remains the existing worker's job."""
        return queue_daily_reminders(self)

    def _dispatch_batch(self, size: int, job_id: int | None = None,
                        *, kinds: tuple = ("OFFER", "TEXT"), acknowledgments: bool = False) -> int:
        # Only this mutex spans network I/O. Calendar/read/reply operations use
        # the short workflow mutex instead; reset explicitly takes both.
        with (self.ack_sender_lock if acknowledgments else self.sender_lock):
            with self.lock, self.sessions.begin() as session:
                self._recover_processing(session)
            selected, recipients, count = [], set(), 0
            cursor = None
            while len(recipients) < size:
                with self.lock, self.sessions() as session:
                    query = select(FlowJob).where(FlowJob.status == "QUEUED",
                                                  FlowJob.kind.in_(kinds))
                    if session.get(FlowSetting, PAUSE_KEY):
                        break
                    if job_id is not None:
                        query = query.where(FlowJob.id == job_id)
                    # Keyset pagination preserves TEXT priority without loading
                    # the entire queue, including when many parents are cooling.
                    priority = case((FlowJob.kind == "TEXT", 0), else_=1)
                    if cursor is not None:
                        query = query.where((priority > cursor[0]) |
                                            ((priority == cursor[0]) & (FlowJob.id > cursor[1])))
                    jobs = list(session.scalars(query.order_by(priority, FlowJob.id).limit(100)))
                if not jobs:
                    break
                for candidate in jobs:
                    cursor = (0 if candidate.kind == "TEXT" else 1, candidate.id)
                    with self.lock, self.sessions.begin() as session:
                        job = session.get(FlowJob, candidate.id)
                        payload = json.loads(job.payload)
                        recipient = payload["recipient"]
                        if recipient in recipients or not self._recipient_ready(session, recipient):
                            continue
                        # Validate before consuming a global slot or attempting
                        # HTTP. Claims/preparation/start marker are committed
                        # before the workflow lock is released.
                        if not self._claim_job(session, job.id):
                            continue
                        try:
                            ready = self._prepare_send(session, job, payload)
                        except Exception as exc:
                            self._record_send_failure(session, job, exc)
                            count += 1
                            continue
                        if not ready:
                            count += 1
                            continue
                        try:
                            ticket = None
                            if self.live:
                                ticket = self.dispatch_control.reserve(self._business_number(), recipient,
                                                                       f"{self.worker_id}:{job.id}",
                                                                       priority=job.kind == "TEXT")
                                if ticket is None:
                                    job.status = "QUEUED"
                                    job.attempts -= 1
                                    job.locked_by, job.locked_until = None, None
                                    continue
                            job.send_started_at = datetime.utcnow()
                            job.locked_until = datetime.utcnow() + timedelta(minutes=2)
                            selected.append((job.id, job.kind, payload, ticket))
                            recipients.add(recipient)
                            count += 1
                        except Exception:
                            # Control storage errors are pre-send failures: hold
                            # safely for the next cycle, never bypass the limiter.
                            job.status, job.error = "QUEUED", "Dispatch preparation unavailable; no send attempted"
                            job.attempts -= 1
                            job.locked_by, job.locked_until = None, None
                    if len(recipients) >= size:
                        break
            if selected:
                with ThreadPoolExecutor(max_workers=size) as pool:
                    pending = {pool.submit(self.send, kind, payload): (ident, kind, payload, ticket)
                               for ident, kind, payload, ticket in selected}
                    for future in as_completed(pending):
                        ident, kind, payload, ticket = pending[future]
                        error = None
                        try:
                            provider_id = future.result()
                        except Exception as exc:
                            error = exc
                        with self.lock:
                            if error is None:
                                self._sent(ident, kind, payload, provider_id)
                            else:
                                self._failed_job(ident, kind, error)
                        if ticket is not None:
                            # A failure writing control telemetry must not turn
                            # an already-saved accepted message into UNKNOWN.
                            try:
                                self.dispatch_control.outcome(self._business_number(), payload["recipient"], ticket, error)
                            except Exception as exc:
                                print(f"WhatsApp control update error type={type(exc).__name__}")
            return count

    def process_one(self, *, job_id: int | None = None) -> bool:
        """Process one job using the same priorities and shared dispatch controls."""
        with self.lock, self.sessions() as session:
            query = select(FlowJob).where(FlowJob.status == "QUEUED",
                or_(FlowJob.next_attempt_at.is_(None), FlowJob.next_attempt_at <= datetime.utcnow()))
            if job_id is not None:
                query = query.where(FlowJob.id == job_id)
            job = session.scalar(query.order_by(*self._queue_order()).limit(1))
            if job and job.kind in {"INBOUND", "STATUS"}:
                event_id = job.id
            else:
                event_id = None
        if event_id is not None:
            return self._process_event(event_id)
        return bool(self._dispatch_batch(1, job_id))

    def drain(self, limit: int = 100) -> int:
        """Process a bounded batch, useful for the preview UI and deterministic tests."""
        count = 0
        while count < limit and self.process_one():
            count += 1
        return count

    def expire_offers(self) -> int:
        """Flag unanswered offers; release only when admin explicitly approves."""
        with self.lock, self.sessions.begin() as session:
            count = 0
            for offer in session.scalars(select(FlowOffer).where(
                FlowOffer.state == "PENDING", FlowOffer.expires_at <= datetime.utcnow(),
            )):
                offer.state = "EXPIRED"
                session.add(FlowInbox(
                    event_key=f"expiry:{offer.token}", patient_code=offer.patient_code,
                    reason="Offer expired — admin follow-up; booking retained", text="No automatic cancellation",
                ))
                count += 1
            return count

    def release_offer(self, token: str) -> None:
        """Explicit admin release after follow-up; do not invent an absence scope."""
        with self.lock, self.sessions.begin() as session:
            offer = session.get(FlowOffer, token)
            if offer is None or offer.state not in {"PENDING", "EXPIRED"}:
                raise ValueError("Only unanswered offers may be released here")
            for item in self._offer_items(session, offer):
                if item.status != AppointmentStatus.PENDING_CONFIRMATION:
                    raise ValueError("Refresh this changed booking")
                item.status = AppointmentStatus.NEEDS_RESCHEDULING
            offer.state = "CLOSED"
            self._audit(session, "flow_admin_release", f"offer:{token}")

    def attendance(self, appointment_id: int, status: str) -> None:
        """Record individual delivered therapies, allowing partial visit attendance."""
        if status not in {"COMPLETED", "NO_SHOW"}:
            raise ValueError("Choose COMPLETED or NO_SHOW")
        with self.lock, self.sessions.begin() as session:
            item = session.get(Appointment, appointment_id)
            if item is None or item.status != AppointmentStatus.CONFIRMED:
                raise ValueError("Attendance requires a confirmed appointment")
            item.status = AppointmentStatus(status)
            # Any remaining callback from this offer requires staff review.
            for offer in session.scalars(select(FlowOffer).where(FlowOffer.state == "CONFIRMED")):
                if appointment_id in json.loads(offer.appointment_ids):
                    offer.state = "CLOSED"
            self._audit(session, f"flow_attendance_{status.lower()}", f"appointment:{appointment_id}")

    def resolve(self, inbox_id: int) -> None:
        with self.lock, self.sessions.begin() as session:
            item = session.get(FlowInbox, inbox_id)
            if item is None:
                raise ValueError("Unknown inbox item")
            item.resolved = 1
            self._audit(session, "flow_inbox_resolved", f"inbox:{inbox_id}")

    def state(self, target: date) -> dict:
        """Return the admin's test state with masked coworker contacts."""
        with self.lock, self.sessions() as session:
            source = session.get(FlowSetting, "zoho_test_scenario")
            if source:
                self.use_source_contacts(json.loads(source.value))
            patients = {p.id: p for p in session.scalars(select(Patient))}
            departments = {d.id: d.name for d in session.scalars(select(Department))}
            therapists = {t.id: t.name for t in session.scalars(select(Therapist))}
            requirements = {r.id: r for r in session.scalars(select(TreatmentRequirement))}
            appointments = [{
                "id": a.id, "patient": patients[a.patient_id].patient_code,
                "requirement_id": a.treatment_requirement_id,
                "source_plan_id": requirements[a.treatment_requirement_id].source_plan_id,
                "therapy": departments[a.department_id], "therapist": therapists[a.therapist_id],
                "date": a.date.isoformat(), "time": slot_label(a.slot_index), "status": a.status.value,
            } for a in session.scalars(select(Appointment).where(Appointment.date == target).order_by(Appointment.slot_index, Appointment.id))]
            offers = [{
                "token": o.token, "patient": o.patient_code, "state": o.state,
                "appointment_ids": json.loads(o.appointment_ids),
                "expires_at": f"{o.expires_at.isoformat()}Z" if o.expires_at else None,
            } for o in session.scalars(select(FlowOffer).order_by(FlowOffer.created_at))
                if any(a[1] == target.isoformat() for a in json.loads(o.snapshot))]
            offer_keys = {
                hashlib.sha256(f"offer:{offer['token']}".encode()).hexdigest(): offer
                for offer in offers
            }
            for job in session.scalars(select(FlowJob).where(FlowJob.event_key.in_(offer_keys))):
                offer = offer_keys[job.event_key]
                fields = json.loads(job.payload).get("fields", {})
                offer.update({
                    "send_status": job.status, "delivery": job.delivery,
                    "send_error": job.error,
                    "message_preview": format_offer_message(fields) if fields else "",
                })
            by_entity = {f"offer:{offer['token']}": offer for offer in offers}
            for audit in session.scalars(select(AuditLog).where(
                AuditLog.entity.in_(by_entity), AuditLog.action.in_(
                    [f"flow_reply_{action}" for action in ("yes", "no", "visit", "day", "help", "reschedule")]
                    + ["flow_assumed_yes"]
                ),
            ).order_by(AuditLog.id.desc())):
                offer = by_entity[audit.entity]
                if audit.action == "flow_assumed_yes":
                    offer["assumed_accepted"] = True
                    continue
                if "last_reply" not in offer:
                    offer["last_reply"] = audit.action.removeprefix("flow_reply_").upper()
                    offer["replied_at"] = f"{audit.timestamp.isoformat()}Z"
            progress = [{
                "patient": patients[p.patient_id].patient_code,
                "requirement_id": p.requirement_id,
                "source_plan_id": requirements[p.requirement_id].source_plan_id,
                "therapy": departments[requirements[p.requirement_id].department_id],
                "target": p.required, "completed": p.completed,
                "booked": p.booked, "remaining": p.remaining,
            } for p in weekly_progress(session, target)]
            return {
                "mode": "LIVE — real coworker messages" if self.live else "PREVIEW — no messages sent",
                "message_mode": self.message_mode,
                "date": target.isoformat(), "scenario": self.scenario,
                "recipients": {code: f"••••{number[-4:]}" for code, number in self.recipients.items()},
                "windows": {
                    code: {"open": self._session_deadline(session, number) > time.time()}
                    for code, number in self.recipients.items()
                },
                "appointments": appointments, "offers": offers, "progress": progress,
                "jobs": [{"id": j.id, "kind": j.kind, "status": j.status,
                          "offer_token": json.loads(j.payload).get("token"),
                          "delivery": j.delivery, "attempts": j.attempts, "error": j.error,
                          "next_attempt_at": f"{j.next_attempt_at.isoformat()}Z" if j.next_attempt_at else None,
                          "accepted_at": f"{j.accepted_at.isoformat()}Z" if j.accepted_at else None,
                          "deadline_at": f"{j.deadline_at.isoformat()}Z" if j.deadline_at else None,
                          "error_code": j.error_code}
                         for j in session.scalars(select(FlowJob).order_by(FlowJob.id.desc()).limit(100))],
                "sending_paused": bool(session.get(FlowSetting, PAUSE_KEY)),
                "inbox": [{"id": i.id, "patient": i.patient_code, "reason": i.reason,
                           "text": i.text, "resolved": bool(i.resolved)}
                          for i in session.scalars(select(FlowInbox).order_by(FlowInbox.id.desc()).limit(100))],
                "audit": [{"action": a.action, "entity": a.entity,
                           "time": f"{a.timestamp.isoformat()}Z"}
                          for a in session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(40))],
            }

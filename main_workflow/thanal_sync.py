"""Structured Thanal daily source adapter. No scheduling saves or external writes."""

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from database.models import Appointment, Patient, Therapist, TreatmentRequirement
from database.repository import _plan_end_date
from flow_test.models import FlowJob, FlowOffer, FlowSetting
from flow_test.performance_schema import SOURCE_VERSION_KEY, source_version_for
from flow_test.zoho_import import CHECKPOINT_KEY, SOURCE_TIMEZONES, TIMEZONE_KEY, checkpoint_time, put_setting
from services.thanal_consultant_import import MAP_KEY, prepare_consultants, sync_consultants
from services.thanal_registration_reader import prepare_registrations
from services.thanal_source_reader import prescription, _lookup, _text
from main_workflow.config import WorkflowConfig

BRANCH = "Iqraa Thanal Super Speciality EIC Centre Calicut"
PLANS = "All_Therapy_Schedule_Plans"
PROFILE = "thanal_lookup_v1"


def select_fields(reports: dict) -> dict:
    """Drop unrelated personal/clinical fields before sending the snapshot to the app."""
    fields = {"All_Registrations": {"ID", "Registration_Id", "Name2", "Phone_Number", "Branch", "Status", "Added_Time"},
              "All_Consultants": {"ID", "Name", "Branch", "Status", "Consultations"},
              PLANS: {"ID", "Branch", "Name", "Added_Time", "Status", "Schedule"}}
    def lookup(value, label):
        return {k: v for k, v in value.items() if k in {"ID", label}} if isinstance(value, dict) else value
    selected = {}
    for report, keys in fields.items():
        selected[report] = []
        for row in reports[report]:
            item = {k: v for k, v in row.items() if k in keys}
            if "Branch" in item:
                item["Branch"] = lookup(item["Branch"], "Branch_Name")
            if report == "All_Consultants" and isinstance(item.get("Consultations"), list):
                item["Consultations"] = [lookup(v, "Value") for v in item["Consultations"]]
            if report == PLANS:
                item["Name"] = lookup(item.get("Name"), "Name2")
                if isinstance(item.get("Schedule"), list):
                    schedule = []
                    for raw in item["Schedule"]:
                        if not isinstance(raw, dict):
                            schedule.append(raw)
                            continue
                        part = {k: v for k, v in raw.items() if k in {
                            "ID", "Therapy", "Consultant", "Per_Week", "No_Of_Week"}}
                        part["Therapy"] = lookup(part.get("Therapy"), "Value")
                        part["Consultant"] = lookup(part.get("Consultant"), "Name")
                        schedule.append(part)
                    item["Schedule"] = schedule
            selected[report].append(item)
    return selected


def enabled(config: WorkflowConfig) -> bool:
    """Activate only for the explicitly selected three-report Thanal contract."""
    return (config.source_name == "thanal/eic" and config.plan_report == PLANS
            and config.registration_report == "All_Registrations"
            and config.consultant_report == "All_Consultants")


def prepare_snapshot(reports: dict) -> dict:
    """Validate latest scoped plans; unrelated phone errors never quarantine demand."""
    if set(reports) != {PLANS, "All_Registrations", "All_Consultants"}:
        raise ValueError("Thanal sync requires all three complete structured reports")
    if not isinstance(reports[PLANS], list) or any(not isinstance(r, dict) for r in reports[PLANS]):
        raise ValueError("Plan report must contain a list of records")
    registrations = prepare_registrations(reports["All_Registrations"])
    if any(i["reason"] in {"missing_or_duplicate_source_id", "invalid_record"}
           for i in registrations["issues"]):
        raise ValueError("Registration source identities are incomplete or duplicated")
    consultants = prepare_consultants(reports["All_Consultants"])
    if any("duplicate" in i["reason"] or "ambiguous" in i["reason"] or "conflicting" in i["reason"]
           for i in consultants["issues"]):
        raise ValueError("Consultant source identities are ambiguous")
    regs = {r["id"]: r for r in registrations["records"]}
    doctors = {r["id"]: r for r in consultants["records"]}
    bad_reg_ids = {i["source_id"] for i in registrations["issues"] if i["reason"] in {
        "missing_or_invalid_registration_code", "missing_or_invalid_name", "duplicate_registration_code"}}
    versions, seen = defaultdict(list), set()
    issues = []
    for row in reports[PLANS]:
        ident = _text(row.get("ID"))
        if not ident.isdigit() or ident in seen:
            raise ValueError("Plan source IDs are missing or duplicated")
        seen.add(ident)
        branch = _lookup(row.get("Branch"), "Branch_Name")
        if branch["label"] != BRANCH or not branch["id"].isdigit():
            raise ValueError("Plan response contains a record outside the approved Calicut branch")
        child = _lookup(row.get("Name"), "Name2")
        if not child["id"] or child["id"] not in regs:
            issues.append({"plan_id": ident, "code": None, "reason": "missing_registration_link"})
            continue
        try:
            added = datetime.strptime(_text(row.get("Added_Time")), "%d/%m/%Y %H:%M:%S")
        except ValueError:
            # Cannot decide which course is current if one creation time is unknown.
            raise ValueError("Plan creation time is invalid; latest-course selection is unsafe") from None
        versions[child["id"]].append((added, row))
    patients = []
    for reg_id, candidates in versions.items():
        reg = regs[reg_id]
        newest = max(stamp for stamp, _ in candidates)
        winners = [row for stamp, row in candidates if stamp == newest]
        row = winners[0]
        try:
            if len(winners) != 1:
                raise ValueError("ambiguous_latest_plan")
            if reg_id in bad_reg_ids:
                raise ValueError("invalid_registration_identity")
            schedule = row.get("Schedule")
            if not isinstance(schedule, list) or not schedule:
                raise ValueError("missing_therapy_rows")
            therapies, used = [], set()
            for part in schedule:
                therapy = _lookup(part.get("Therapy"), "Value")
                clinician = _lookup(part.get("Consultant"), "Name")
                doctor = doctors.get(clinician["id"])
                if doctor is None:
                    raise ValueError("missing_or_invalid_consultant")
                if doctor["branch"]["id"] != str(row["Branch"]["ID"]):
                    raise ValueError("consultant_branch_mismatch")
                if therapy["id"] in used or not therapy["id"].isdigit():
                    raise ValueError("duplicate_or_missing_therapy")
                capability = next((t for t in doctor["therapies"] if t["id"] == therapy["id"]), None)
                if capability is None:
                    raise ValueError("consultant_therapy_mismatch")
                used.add(therapy["id"])
                values = prescription(part)
                if values["sessions_per_week"] > 99:
                    raise ValueError("invalid_weekly_frequency")
                try:
                    _plan_end_date(newest.date(), values["duration_months"])
                except (ValueError, OverflowError):
                    raise ValueError("invalid_treatment_duration") from None
                therapies.append({"therapy_id": therapy["id"], "therapy": capability["label"],
                                  "consultant_id": clinician["id"], **values})
            patients.append({"registration": reg, "plan_id": str(row["ID"]),
                             "added": newest.isoformat(), "branch_id": str(row["Branch"]["ID"]),
                             "therapies": therapies})
        except (ValueError, TypeError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "malformed_therapy_row"
            issues.append({"plan_id": str(row["ID"]), "code": reg["code"], "reason": reason})
    return {"registrations": registrations, "consultants": consultants, "patients": patients,
            "issues": issues, "summary": {"registrations": len(regs), "consultants": len(doctors),
                "downloaded_plans": len(seen), "patients": len(patients),
                "requirements": sum(len(p["therapies"]) for p in patients),
                "weekly_sessions": sum(t["sessions_per_week"] for p in patients for t in p["therapies"]),
                "branches": 1,
                "quarantined_plans": len(issues), "consultant_issues": len(consultants["issues"]),
                "registration_diagnostics": registrations["summary"]["issue_counts"],
                "plan_issue_counts": dict(Counter(i["reason"] for i in issues)), "messages_sent": 0}}


def apply_snapshot(flow: Any, reports: dict, *, window: dict) -> dict:
    """Atomically update masters, courses, contacts and checkpoint under the workflow locks."""
    if flow.is_test or flow.source_name != "thanal/eic":
        raise ValueError("Structured import requires the Thanal main workflow")
    data = prepare_snapshot(reports)
    if not isinstance(window, dict) or set(window) != {"from", "to"}:
        raise ValueError("Supply the complete sync window")
    until = checkpoint_time(window["to"])
    if until > datetime.now(until.tzinfo) or (window["from"] and checkpoint_time(window["from"]) >= until):
        raise ValueError("Invalid sync window")
    with flow.lock, flow.inbound_lock, flow.sessions.begin() as session:
        checkpoint = session.get(FlowSetting, CHECKPOINT_KEY)
        current = json.loads(checkpoint.value) if checkpoint else None
        if window["from"] != current:
            raise ValueError("Sync checkpoint changed; fetch status and retry")
        source = session.get(FlowSetting, "main_workflow_source")
        if source is None or source.value != "thanal/eic":
            raise ValueError("Database source does not match Thanal")
        # Reuse the consultant-only importer on THIS transaction, suppressing
        # its inner commit. An error later rolls back all three sources together.
        sessions = sessionmaker(bind=session.connection(), join_transaction_mode="rollback_only")
        nested = SimpleNamespace(source_name=flow.source_name, is_test=False, sessions=sessions,
                                 lock=flow.lock, inbound_lock=flow.inbound_lock, _audit=flow._audit)
        valid_ids = {r["id"] for r in data["consultants"]["records"]}
        valid_rows = [r for r in reports["All_Consultants"] if str(r.get("ID")) in valid_ids]
        if valid_rows:
            sync_consultants(nested, valid_rows, apply=True)
        session.expire_all()
        binding_row = session.get(FlowSetting, MAP_KEY)
        bindings = json.loads(binding_row.value) if binding_row else {"branches": {}, "therapies": {}}
        doctors = {r.therapist_code: r for r in session.scalars(select(Therapist))}
        local_patients = {p.patient_code: p for p in session.scalars(select(Patient))}
        identity = session.get(FlowSetting, "thanal_registration_identity_map")
        identities = json.loads(identity.value) if identity else {}
        regs = data["registrations"]["records"]
        # Preserve all registrations as source data, not as automatically active patients.
        for reg in regs:
            if reg["id"] in identities and identities[reg["id"]] != reg["code"]:
                raise ValueError("Changing a linked registration code requires identity review")
        prior = session.get(FlowSetting, "zoho_test_scenario")
        old_scenario = json.loads(prior.value) if prior else {"patients": []}
        roster = []
        imported = set()
        for patient_data in data["patients"]:
            reg, plan_id = patient_data["registration"], patient_data["plan_id"]
            code = reg["code"]
            if any(rid != reg["id"] and old_code == code for rid, old_code in identities.items()):
                raise ValueError("Registration code is already bound to another Creator record")
            patient = local_patients.get(code)
            if patient is None:
                eligible = bool(reg["phone"]) and all(
                    doctors["ZC-" + t["consultant_id"]].active for t in patient_data["therapies"])
                patient = Patient(patient_code=code, name=reg["name"], active=eligible)
                session.add(patient)
                session.flush()
                local_patients[code] = patient
            patient.name = reg["name"]  # Preserve staff-controlled scheduling eligibility.
            identities[reg["id"]] = code
            existing = list(session.scalars(select(TreatmentRequirement).where(
                TreatmentRequirement.patient_id == patient.id)))
            if any(r.source_plan_id is None for r in existing):
                raise ValueError("Existing unbound prescriptions need course identity review")
            desired = set()
            for order, therapy in enumerate(patient_data["therapies"]):
                dept = bindings["therapies"][therapy["therapy_id"]]["local_id"]
                branch = bindings["branches"][patient_data["branch_id"]]["local_id"]
                doctor = doctors["ZC-" + therapy["consultant_id"]]
                req = next((r for r in existing if r.source_plan_id == plan_id and r.department_id == dept), None)
                if req and (req.assigned_therapist_id != doctor.id or req.location_id != branch):
                    if session.scalar(select(Appointment.id).where(Appointment.treatment_requirement_id == req.id).limit(1)):
                        raise ValueError("A booked plan's doctor or centre changed; staff review required")
                if req is None:
                    req = TreatmentRequirement(patient_id=patient.id, source_plan_id=plan_id, department_id=dept)
                    session.add(req)
                elif (req.sessions_per_week != therapy["sessions_per_week"]
                      or req.duration_months != therapy["duration_months"] or req.sequence_order != order):
                    if session.scalar(select(Appointment.id).where(Appointment.treatment_requirement_id == req.id).limit(1)):
                        flow._job_warning(session, f"thanal-change:{req.id}", "Prescription edited — review saved visits",
                                          "Latest frequency/duration/order applied. Existing bookings and attendance retained.", code)
                req.assigned_therapist_id, req.location_id = doctor.id, branch
                req.sessions_per_week, req.duration_months = therapy["sessions_per_week"], therapy["duration_months"]
                req.sequence_order, req.active = order, True
                req.plan_added_at = datetime.fromisoformat(patient_data["added"])
                req.plan_start_date, req.source_status = req.plan_added_at.date(), None
                session.flush()
                desired.add(req.id)
            for req in existing:
                if req.id not in desired:
                    req.active = False  # History/bookings remain pinned to the original prescription.
                    if session.scalar(select(Appointment.id).where(Appointment.treatment_requirement_id == req.id).limit(1)):
                        flow._job_warning(session, f"thanal-retired:{req.id}", "Previous prescription needs booking review",
                                          "New course or removed therapy imported. Existing bookings are retained on the old prescription.", code)
            item = {"code": code, "name": reg["name"], "source_plan_id": plan_id,
                    "initial_roster": True, "requirements": [{"weekly": t["sessions_per_week"]}
                                                              for t in patient_data["therapies"]]}
            if reg["phone"]:
                item["phone"] = reg["phone"]
            roster.append(item)
            imported.add(code)
        quarantined = {i["code"] for i in data["issues"] if i["code"]}
        missing_codes = {p["code"] for p in old_scenario["patients"]} - imported - quarantined
        quarantined |= missing_codes
        for code in missing_codes:
            flow._job_warning(session, "thanal-plan:missing:" + hashlib.sha256(code.encode()).hexdigest(),
                              "Therapy plan needs source review",
                              "Previously imported child has no current valid Calicut plan. Existing bookings are retained.", code)
        for issue in data["issues"]:
            key = "thanal-plan:" + hashlib.sha256(issue["plan_id"].encode()).hexdigest()
            flow._job_warning(session, key, "Therapy plan needs source correction",
                              "Correct the latest source plan: " + issue["reason"], issue["code"])
        for code in imported:
            from flow_test.models import FlowInbox
            for warning in session.scalars(select(FlowInbox).where(
                    FlowInbox.patient_code == code, FlowInbox.event_key.like("thanal-plan:%"))):
                warning.resolved = 1
        scenario = {"version": "main-workflow-v1", "source": flow.source_name, "centre": BRANCH,
                    "patients": roster, "profile": PROFILE}
        phones = {p["code"]: p.get("phone") for p in roster}
        for job in session.scalars(select(FlowJob).where(FlowJob.kind == "OFFER", FlowJob.status.in_(
                ("QUEUED", "RETRY_WAIT", "WAITING_FOR_CONTACT", "WAITING_FOR_SYNC", "WAITING_FOR_ENABLE", "BLOCKED")))):
            if job.send_started_at is not None or job.message_id:
                continue
            payload = json.loads(job.payload)
            if payload.get("reminder"):
                continue
            offer = session.get(FlowOffer, payload.get("token"))
            number = phones.get(offer.patient_code) if offer else None
            if not number or not offer or offer.state != "PENDING":
                continue
            payload["recipient"], offer.recipient = number, number
            payload.pop("session_expires_at", None)
            job.payload = json.dumps(payload)
            if job.status == "WAITING_FOR_SYNC":
                job.status, job.error = "QUEUED", None
        version = source_version_for(scenario)
        version_row = session.get(FlowSetting, SOURCE_VERSION_KEY)
        if version_row is None:
            session.add(FlowSetting(key=SOURCE_VERSION_KEY, value=version))
        else:
            version_row.value = version
        # Store only the required fields, never full registration clinical records.
        put_setting(session, "thanal_registration_snapshot", regs)
        put_setting(session, "thanal_registration_identity_map", identities)
        put_setting(session, "zoho_test_scenario", scenario)
        put_setting(session, "zoho_test_source", {"profile": PROFILE})
        put_setting(session, "zoho_quarantined_patients", sorted(quarantined))
        put_setting(session, TIMEZONE_KEY, SOURCE_TIMEZONES)
        partial = bool(data["issues"])
        if not partial:
            put_setting(session, CHECKPOINT_KEY, window["to"])
        result = {**data["summary"], "sourceProfile": PROFILE, "checkpoint": current if partial else window["to"],
                  "partial": partial, "quarantined": sorted(quarantined), "changed": True}
        put_setting(session, "zoho_test_sync", result)
        from services.patient_activation_service import prepare_activation_review, store_activation_review
        store_activation_review(session, prepare_activation_review(reports, data))
        flow._audit(session, "thanal_daily_source_sync", "source:thanal/eic")
    flow.use_source_contacts(scenario)
    return result

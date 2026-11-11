"""Thanal's appointment contract: source identities and explicit lookup bindings."""

from datetime import date, datetime
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from flow_test.models import FlowSetting
from services.thanal_consultant_import import MAP_KEY

FORM = "Schedule_Adhoc_Trial"
REPORT = "Schedule_Adhoc_Trial_Report"
LOOKUP_KEY = "thanal_export_lookup_map"
FIELDS = frozenset({"Branch", "Registration", "For_Parent", "Date_field", "Department",
                    "Consultation_Type", "Consultant", "Appointment_Time_Slot", "Slot_Number",
                    "Status", "Scheduler_Key", "Date_Time", "End_Time", "Duration"})
LOOKUPS = frozenset({"Branch", "Registration", "Department", "Consultation_Type", "Consultant",
                      "Appointment_Time_Slot"})
STATUSES = {"CONFIRMED": "Scheduled", "COMPLETED": "Completed",
            "CANCELLED_PATIENT": "Cancelled", "CANCELLED_OTHER": "Cancelled"}


def enabled(flow: Any) -> bool:
    """Select the approved Thanal destination without changing other adapters."""
    config = getattr(flow, "config", None)
    return (getattr(flow, "source_name", None) == "thanal/eic" and config is not None
            and config.schedule_form == FORM and config.schedule_report == REPORT)


def matches(settings: Any) -> bool:
    return (settings.account_owner, settings.app_link_name, settings.form_link_name,
            settings.report_link_name) == ("thanal", "eic", FORM, REPORT)


def read_setting(session: Any, key: str, default: Any) -> Any:
    row = session.get(FlowSetting, key)
    return json.loads(row.value) if row else default


def record_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError(f"{field}: a verified Zoho lookup binding is missing")
    return value


@lru_cache(maxsize=1)
def default_catalog() -> dict:
    path = Path(__file__).with_name("thanal_export_lookups.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def bindings_from_catalog(source: dict, branch: str, catalog: dict) -> dict:
    """Join observed lookup relationships to imported IDs, never display names."""
    from scheduling.slot_config import SLOTS

    result = {"version": 1, "registration_display": catalog.get("registration_display"),
              "departments": {}, "consultations": {}, "slots": {}}
    if branch not in catalog.get("approved_branch_ids", []):
        raise ValueError("Branch: export lookup evidence does not match this appointment's centre")
    for source_id, therapy in source.get("therapies", {}).items():
        candidates = {r["department_id"] for r in catalog.get("department_bindings", [])
                      if r.get("consultation_id") == source_id and r.get("branch_id") == branch}
        if len(candidates) == 1:
            local_id = str(therapy["local_id"])
            result["consultations"][local_id] = source_id
            result["departments"][local_id] = next(iter(candidates))
    for index, slot in enumerate(SLOTS):
        candidates = {(r.get("ID"), r.get("Slot_Number")) for r in catalog.get("slot_bindings", [])
                      if r.get("branch_id") == branch and r.get("Start_Time") == slot.start_time.strftime("%H:%M:%S")
                      and r.get("End_Time") == slot.end_time.strftime("%H:%M:%S")
                      and str(r.get("appointment_slot_number")) == str(r.get("Slot_Number"))}
        if len(candidates) == 1:
            identifier, number = next(iter(candidates))
            if str(number).isdigit():
                result["slots"][str(index)] = {"id": identifier, "number": int(number)}
    return result


def export_context(session: Any, requirement_ids: list[int]) -> dict:
    """Load shared source identity maps once for the export snapshot."""
    from sqlalchemy import select
    from database.models import TreatmentRequirement

    return {"identities": read_setting(session, "thanal_registration_identity_map", {}),
            "source": read_setting(session, MAP_KEY, {}),
            "overrides": read_setting(session, LOOKUP_KEY, None), "branches": {},
            "requirements": set(session.scalars(select(TreatmentRequirement.id).where(
                TreatmentRequirement.id.in_(requirement_ids))))}


def appointment_fields(session: Any, item: Any, patient: Any, doctor: Any, namespace: str,
                       *, context: dict | None = None) -> dict:
    """Build exactly the approved fields; never resolve identities from names."""
    context = context if context is not None else export_context(session, [item.treatment_requirement_id])
    if item.treatment_requirement_id not in context["requirements"]:
        raise ValueError("Treatment plan: the saved appointment has no linked prescription")
    identities = context["identities"]
    registrations = [key for key, code in identities.items() if code == patient.patient_code]
    if len(registrations) != 1:
        raise ValueError("Registration: the child's source registration is missing or ambiguous")
    source = context["source"]
    branches = [key for key, row in source.get("branches", {}).items()
                if row.get("local_id") == item.location_id]
    if len(branches) != 1:
        raise ValueError("Branch: the appointment's source centre is missing or ambiguous")
    bindings = context["overrides"]
    if bindings is None:
        if branches[0] not in context["branches"]:
            context["branches"][branches[0]] = bindings_from_catalog(source, branches[0], default_catalog())
        bindings = context["branches"][branches[0]]
    if bindings.get("version") != 1:
        raise ValueError("Export lookups: finish the verified Thanal lookup setup first")
    if bindings.get("registration_display") != "Name2":
        raise ValueError("Registration: verify that the lookup displays the child's name")
    for_parent = False  # User-confirmed: all exported child appointments are unchecked.
    department = bindings.get("departments", {}).get(str(item.department_id))
    consultation = bindings.get("consultations", {}).get(str(item.department_id))
    slot = bindings.get("slots", {}).get(str(item.slot_index), {})
    if type(slot.get("number")) is not int or slot["number"] < 0:
        raise ValueError("Slot Number: the Zoho slot numbering has not been verified")
    status = STATUSES.get(item.status.value)
    if status is None:
        raise ValueError("Status: this appointment status needs an approved Zoho mapping")
    consultant = doctor.therapist_code.removeprefix("ZC-") if doctor.therapist_code.startswith("ZC-") else None
    fields = {
        "Branch": record_id(branches[0], "Branch"),
        "Registration": record_id(registrations[0], "Registration"),
        "For_Parent": for_parent, "Date_field": item.date.strftime("%d/%m/%Y"),
        "Department": record_id(department, "Department"),
        "Consultation_Type": record_id(consultation, "Consultation Type"),
        "Consultant": record_id(consultant, "Consultant"),
        "Appointment_Time_Slot": record_id(slot.get("id"), "Appointment Time Slot"),
        "Slot_Number": slot["number"], "Status": status, "Scheduler_Key": f"{namespace}:{item.id}",
        **timing_fields(item.date, item.slot_index),
    }
    validate_fields(fields, allow_preview=namespace == "preview")
    return fields


def timing_fields(day: date, slot_index: int) -> dict:
    """Supply the local clinic times needed by Zoho's session-creation workflow."""
    from scheduling.slot_config import SLOTS

    if type(slot_index) is not int or not 0 <= slot_index < len(SLOTS):
        raise ValueError("Appointment time: invalid scheduling slot")
    slot = SLOTS[slot_index]
    start = datetime.combine(day, slot.start_time)
    end = datetime.combine(day, slot.end_time)
    return {"Date_Time": start.strftime("%d/%m/%Y %H:%M"),
            "End_Time": end.strftime("%d/%m/%Y %H:%M:%S"),
            "Duration": int((end - start).total_seconds() // 60)}


def validate_fields(fields: dict, *, allow_preview: bool = False) -> None:
    if set(fields) != FIELDS:
        raise ValueError("Unexpected Thanal appointment payload")
    for field in LOOKUPS:
        record_id(fields[field], field)
    if type(fields["For_Parent"]) is not bool:
        raise ValueError("For Parent must be a checkbox value")
    if type(fields["Slot_Number"]) is not int or fields["Slot_Number"] < 0:
        raise ValueError("Slot Number must use verified Zoho numbering")
    try:
        day = datetime.strptime(fields["Date_field"], "%d/%m/%Y").date()
    except (ValueError, TypeError):
        raise ValueError("Date must use the verified day/month/year format") from None
    from scheduling.slot_config import SLOTS
    if type(fields["Duration"]) is not int or not any(
            all(fields[name] == value for name, value in timing_fields(day, index).items())
            for index in range(len(SLOTS))):
        raise ValueError("Appointment time: date, slot start/end and duration must agree")
    if fields["Status"] not in set(STATUSES.values()):
        raise ValueError("Unsupported Thanal appointment status")
    key = fields["Scheduler_Key"]
    if not isinstance(key, str) or not (key.startswith("main-") or (allow_preview and key.startswith("preview:"))):
        raise ValueError("Thanal exports require their stable main-workflow Scheduler Key")


def validate_schema(fields: dict) -> None:
    """Verify actual field types and reject new unhandled mandatory fields."""
    missing = FIELDS - fields.keys()
    if missing:
        raise ValueError("Thanal export fields are missing: " + ", ".join(sorted(missing)))
    for name in LOOKUPS:
        if fields[name].get("type") != 12 or fields[name].get("is_lookup_field") is not True:
            raise ValueError(f"{name} must remain a single-select Zoho lookup")
    for name, kind in {"For_Parent": 16, "Date_field": 10, "Slot_Number": 5, "Status": 12,
                       "Date_Time": 11, "End_Time": 11, "Duration": 5}.items():
        if fields[name].get("type") != kind or fields[name].get("is_lookup_field"):
            raise ValueError(f"Unexpected Thanal field type: {name}")
    key = fields["Scheduler_Key"]
    if key.get("type") != 1 or key.get("unique") is not True or int(key.get("max_char", 255)) < 60:
        raise ValueError("Scheduler_Key must be unique Single Line with at least 60 characters")
    choices = {row.get("value") for row in fields["Status"].get("choices", []) if isinstance(row, dict)}
    if not set(STATUSES.values()) <= choices:
        raise ValueError("Thanal Status must include Scheduled, Completed and Cancelled")
    if any(row.get("mandatory") and name not in FIELDS for name, row in fields.items()):
        raise ValueError("Thanal has an additional required field that needs an export mapping")



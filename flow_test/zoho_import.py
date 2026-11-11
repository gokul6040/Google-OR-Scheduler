"""Convert the two approved Zoho test reports without changing the core OR."""

import csv
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import re
import time
from typing import Iterator
import unicodedata
from zoneinfo import ZoneInfo

from sqlalchemy import select

from database.models import Appointment, Department, Location, Patient, Therapist, TreatmentRequirement
from database.repository import _plan_end_date
from flow_test.models import FlowInbox, FlowJob, FlowOffer, FlowSetting
from services.zoho_source_mapping import REGISTRATION_REPORT, normalize_reports, report_names

OWNER = "nidhin.kumar_turnb"
APP = "test-gokul"
REPORTS = ("Plan_Report", "All_Consultants")
SCENARIO_KEY = "zoho_test_scenario"
CHECKPOINT_KEY = "zoho_test_checkpoint"
SOURCE_TIMEZONES = {"Added_Time": "Asia/Kolkata", "Modified_Time": "Asia/Kolkata"}
TIMEZONE_KEY = "zoho_test_source_timezone"


class SourceRecordError(ValueError):
    """Source validation failure with a bounded record reference for staff."""


@contextmanager
def patient_record(row: dict, rejected: list | None):
    """Quarantine an identified patient error without discarding good patients."""
    try:
        with source_record("Plan_Report", row):
            yield
    except SourceRecordError as exc:
        if rejected is None:
            raise
        rejected.append({"code": clean_text(row.get("RegistrationID")),
                         "id": clean_text(row.get("ID")), "reason": str(exc)})


@contextmanager
def source_record(report: str, row: dict) -> Iterator[None]:
    try:
        yield
    except SourceRecordError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        identifier = clean_text(row.get("ID"))[:80] or "missing ID"
        message = str(exc) if isinstance(exc, ValueError) else "Missing or invalid source fields"
        raise SourceRecordError(f"{report}, record {identifier}: {message}") from None


def reconciliation_required(session) -> bool:
    """Keep publishing paused when retained source timestamps need migration."""
    if (session.get(FlowSetting, CHECKPOINT_KEY) is None
            and session.get(FlowSetting, "zoho_test_source") is None):
        return False  # The original fictional fixture has no Zoho source contract.
    marker = session.get(FlowSetting, TIMEZONE_KEY)
    try:
        return marker is None or json.loads(marker.value) != SOURCE_TIMEZONES
    except (TypeError, ValueError):
        return True


def require_reconciled_source(session) -> None:
    """Reject new external commitments until the source cache is reconciled."""
    if reconciliation_required(session):
        marker = session.get(FlowSetting, "scenario")
        command = "main_workflow.sync" if marker and marker.value == "main-workflow-v1" else "flow_test.zoho_daily"
        raise ValueError(
            "Zoho source timestamps need reconciliation. Run "
            f"python -m {command} --apply --reconcile before "
            "publishing offers or finalising the schedule. Existing bookings are retained."
        )


def clean_text(value) -> str:
    """Normalize pasted whitespace without guessing missing or misspelled data."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value))
    return " ".join(value.translate({ord(c): None for c in "\ufeff\u200b\u2060"}).split())


def branch_key(value) -> str:
    return clean_text(value).removesuffix(" — FICTIONAL TEST").casefold()


def treatment_start(added: datetime) -> date:
    """Added Time is already IST; never add the offset a second time."""
    return added.date()


def sync_now() -> datetime:
    """UTC clock used to reject accidental future checkpoints."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def source_time(value: str) -> datetime:
    """Parse a source wall-clock value; its field determines the time zone."""
    try:
        return datetime.strptime(clean_text(value), "%d-%b-%Y %H:%M:%S")
    except (TypeError, ValueError):
        raise ValueError("Missing or invalid Zoho Added_Time/Modified_Time") from None


def source_instant(value: str, field: str) -> datetime:
    """Compare source fields/checkpoints as instants despite different zones."""
    return source_time(value).replace(tzinfo=ZoneInfo(SOURCE_TIMEZONES[field])).astimezone(timezone.utc)


def mapped_reports(reports: dict) -> dict:
    """Normalize known fields before validating or retaining a source snapshot."""
    try:
        return normalize_reports(reports)
    except ValueError as exc:
        raise SourceRecordError(str(exc)) from None


def modified_time(row: dict, *, snapshot: bool) -> datetime:
    """Use creation time for version ties only when a snapshot omits edit time."""
    value = row.get("Modified_Time")
    if snapshot and "Modified_Time" not in row:
        value = row.get("Added_Time")
    return source_time(value)


def registration_contacts(rows: list[dict]) -> dict[str, str]:
    """Choose the newest registration per exact registration code, never name."""
    versions, seen = {}, set()
    for row in rows:
        with source_record(REGISTRATION_REPORT, row):
            source_id = clean_text(row.get("ID"))
            code = clean_text(row.get("RegistrationID"))
            if (not source_id.isdigit() or len(source_id) > 80 or source_id in seen
                    or not code or len(code) > 40):
                raise ValueError("Missing, duplicate or invalid registration identity")
            seen.add(source_id)
            added = source_time(row.get("Added_Time"))
            modified = modified_time(row, snapshot=True)
            if modified < added:
                raise ValueError("Modified Time cannot precede Added Time")
            versions.setdefault(code, []).append(((added, modified), row))
    contacts = {}
    for code, candidates in versions.items():
        newest = max(version for version, _ in candidates)
        winners = [row for version, row in candidates if version == newest]
        with source_record(REGISTRATION_REPORT, winners[0]):
            if len(winners) != 1:
                raise ValueError("Ambiguous registration versions with identical timestamps")
            contacts[code] = winners[0].get("Phone", "")
    return contacts


def rebase_cached_times(reports: dict, previous_zones: dict[str, str] | str | None) -> dict:
    """Translate retained legacy source rows during explicit reconciliation."""
    zones = previous_zones if isinstance(previous_zones, dict) else {
        field: previous_zones or "UTC" for field in SOURCE_TIMEZONES
    }
    result = {}
    for report, rows in reports.items():
        result[report] = []
        for row in rows:
            row = dict(row)
            for field, zone in SOURCE_TIMEZONES.items():
                if field not in row:
                    continue
                row[field] = source_time(row[field]).replace(tzinfo=ZoneInfo(zones[field])).astimezone(
                    ZoneInfo(zone)).strftime("%d-%b-%Y %H:%M:%S")
            result[report].append(row)
    return result


def phone_number(value: str) -> str:
    """Store every parent number as 91 + a 10-digit Indian mobile, the format WhatsApp uses.

    Accepts 9876543210, 09876543210, +91 98765 43210, 0091 9876543210 and 919876543210.
    Anything else (wrong length, landlines, foreign numbers) is never guessed: it raises,
    so the patient is set aside for staff to correct in Zoho.
    """
    raw = clean_text(value)
    number = re.sub(r"[\s().\-]", "", raw)
    if not number:
        raise ValueError("Missing parent phone number")
    # An explicit "+" or "00" means the number already carries its country code.
    # Keep that fact: a foreign code must never be turned into an Indian number.
    international = number.startswith("+") or number.startswith("00")
    number = number[1:] if number.startswith("+") else number[2:] if number.startswith("00") else number
    if not number.isdigit():
        raise ValueError("Parent phone number contains letters or symbols; correct it in Zoho")
    if international:
        if not number.startswith("91"):
            raise ValueError("Parent phone number has a foreign country code; foreign numbers need staff review")
    else:
        if len(number) == 11 and number.startswith("0"):
            number = number[1:]
        if len(number) == 10:
            number = "91" + number
    if not re.fullmatch(r"91[6-9]\d{9}", number):
        raise ValueError("Parent phone number is not a valid Indian mobile number (10 digits starting 6-9); "
                         "foreign or landline numbers need staff review")
    return number


def checkpoint_time(value: str) -> datetime:
    """Parse a timezone-qualified, second-resolution sync boundary."""
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.microsecond:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Use a timezone-qualified sync checkpoint at whole-second precision") from None


def merge_reports(previous: dict | None, incoming: dict, window: dict | None,
                  *, full_snapshot: bool = False, timezone_migration: bool = False) -> dict:
    """Merge by Creator ID; rows absent from a delta are never deleted."""
    incoming = mapped_reports(incoming)
    if previous is not None:
        previous = mapped_reports(previous)
    snapshot_mode = REGISTRATION_REPORT in incoming
    if previous and REGISTRATION_REPORT in previous and not snapshot_mode:
        raise SourceRecordError("Keep All_Registrations in every sync after enabling registration lookup")
    since = until = None
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"from", "to"}:
            raise ValueError("Supply the complete sync window")
        since = checkpoint_time(window["from"]) if window["from"] is not None else None
        until = checkpoint_time(window["to"])
        if since is not None and since >= until:
            raise ValueError("Sync window end must be after its checkpoint")
        if until > sync_now():
            raise ValueError("Sync window end cannot be in the future")
    merged = {}
    for report in report_names(incoming):
        rows = incoming[report]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SourceRecordError(f"{report}: invalid report rows; expected records with source IDs")
        previous_rows = {clean_text(row["ID"]): row for row in (previous or {}).get(report, [])}
        cache = dict(previous_rows)
        if report == REGISTRATION_REPORT:
            cache = {}  # Registrations are a complete contact snapshot, not a delta.
        seen = set()
        for row in rows:
            with source_record(report, row):
                source_id = clean_text(row.get("ID"))
                if not source_id.isdigit() or source_id in seen:
                    raise ValueError("Missing or duplicate source record ID")
                seen.add(source_id)
                added = source_instant(row.get("Added_Time"), "Added_Time")
                modified = modified_time(row, snapshot=snapshot_mode).replace(
                    tzinfo=ZoneInfo(SOURCE_TIMEZONES["Modified_Time"])).astimezone(timezone.utc)
                if modified < added:
                    raise ValueError("Modified Time cannot precede Added Time")
                if until and (added >= until or modified >= until or
                              (since and not full_snapshot and not snapshot_mode
                               and added < since and modified < since)):
                    raise ValueError("Source record lies outside the requested sync window; retry the pull")
                old = previous_rows.get(source_id)
                if (old and report in {"Plan_Report", REGISTRATION_REPORT}
                        and clean_text(old.get("RegistrationID")) != clean_text(row.get("RegistrationID"))):
                    raise ValueError("Changing an existing source Registration ID requires staff review")
                if (old and not timezone_migration and "Modified_Time" in old and "Modified_Time" in row
                        and source_time(old["Modified_Time"]) > source_time(row["Modified_Time"])):
                    raise ValueError("Source returned an older record version; retry the pull")
                cache[source_id] = row
        merged[report] = list(cache.values())
    return merged


def convert_reports(reports: dict, plan_start: date | None = None, *, fictional: bool = True,
                    source: str = f"{OWNER}/{APP}", rejected: list | None = None) -> dict:
    """Convert source branches and each latest plan's Added Time to OR inputs.

    The optional old plan_start argument is accepted for existing job clients;
    source Added Time now supplies each plan's start date.
    """
    reports = mapped_reports(reports)
    snapshot_mode = REGISTRATION_REPORT in reports
    contacts = registration_contacts(reports[REGISTRATION_REPORT]) if REGISTRATION_REPORT in reports else None
    consultants, patients, source_ids, branches, therapy_labels = {}, [], set(), {}, {}
    def branch(value, context):
        name = clean_text(value)
        if not name or len(name) > 100:
            raise ValueError(f"{context}: supply a Branch of at most 100 characters")
        return branches.setdefault(branch_key(name), name)

    for row in reports["All_Consultants"]:
        with source_record("All_Consultants", row):
            source_id = clean_text(row.get("ID"))
            name = clean_text(row.get("Name"))
            therapy = clean_text(row.get("ConsultationType"))
            therapy = therapy_labels.setdefault(therapy.casefold(), therapy)
            source_branch = branch(row.get("Branch"), f"Consultant {source_id}")
            key = (branch_key(source_branch), name.casefold(), therapy.casefold())
            status = clean_text(row.get("Status")).casefold()
            if (not source_id.isdigit() or not name or len(name) > 120
                    or not therapy or len(therapy) > 120
                    or status not in {"active", "inactive"} or key in consultants
                    or source_id in source_ids):
                raise ValueError(f"Invalid or ambiguous consultant record {source_id!r} in {source_branch}")
            source_ids.add(source_id)
            source_time(row.get("Added_Time"))
            modified_time(row, snapshot=snapshot_mode)
            consultants[key] = {"code": f"ZC-{source_id}", "name": name,
                                "therapy": therapy, "branch": source_branch, "active": status == "active"}
    # The newest course remains current even when an older course is edited.
    versions = {}
    for row in reports["Plan_Report"]:
        with source_record("Plan_Report", row):
            code = clean_text(row.get("RegistrationID"))
            version = (source_time(row.get("Added_Time")), modified_time(row, snapshot=snapshot_mode))
            versions.setdefault(code, []).append((version, row))
    latest = {}
    for code, candidates in versions.items():
        newest = max(version for version, _ in candidates)
        winners = [row for version, row in candidates if version == newest]
        if len({clean_text(row.get("ID")) for row in winners}) > 1:
            raise SourceRecordError("Plan_Report: Ambiguous patient plan versions with identical Modified Time "
                                    "and Added Time; source records " + ", ".join(
                                        clean_text(row.get("ID"))[:80] for row in winners))
        latest[code] = winners[0]
    seen = set()
    for row in sorted(latest.values(), key=lambda item: clean_text(item.get("RegistrationID"))):
        with patient_record(row, rejected):
            code = clean_text(row.get("RegistrationID"))
            source_id = clean_text(row.get("ID"))
            name = clean_text(row.get("PatientName"))
            source_branch = branch(row.get("Branch"), f"Patient {code!r}")
            if contacts is not None and code not in contacts:
                raise ValueError("No matching registration for this therapy plan")
            if (not code or len(code) > 40 or code in seen
                    or not source_id.isdigit() or len(source_id) > 80 or source_id in source_ids
                    or not name or len(name) > 120):
                raise ValueError(f"Invalid or duplicate patient record {code!r}")
            seen.add(code)
            source_ids.add(source_id)
            added = source_time(row.get("Added_Time"))
            modified_time(row, snapshot=snapshot_mode)
            schedule = row.get("Schedule")
            if not isinstance(schedule, str) or not schedule.strip():
                raise ValueError(f"Missing Schedule for {code}")
            requirements, therapies = [], set()
            for entry in re.split(r"[;\r\n]+", unicodedata.normalize("NFKC", schedule).strip()):
                if not entry.strip():
                    continue
                try:
                    fields = next(csv.reader([entry], skipinitialspace=True, strict=True))
                except csv.Error:
                    raise ValueError(f"Malformed Schedule for {code}") from None
                if len(fields) != 4:
                    raise ValueError(f"Schedule for {code} needs therapy, weekly frequency, months, consultant")
                therapy, weekly, months, consultant_name = (clean_text(value) for value in fields)
                consultant = consultants.get((branch_key(source_branch), consultant_name.casefold(), therapy.casefold()))
                if (not weekly.isdecimal() or not months.isdecimal()
                        or int(weekly) < 1 or int(months) < 1
                        or therapy.casefold() in therapies or consultant is None):
                    raise ValueError(f"Invalid therapy/frequency/duration or consultant match for {code} in branch {source_branch}")
                therapies.add(therapy.casefold())
                try:
                    _plan_end_date(treatment_start(added), int(months))
                except (ValueError, OverflowError):
                    raise ValueError(f"Invalid calendar duration for {code}") from None
                requirements.append({"therapy": consultant["therapy"],
                                     "therapist": consultant["code"], "weekly": int(weekly),
                                     "branch": source_branch,
                                     "duration_months": int(months), "plan_start_date": treatment_start(added).isoformat(),
                                     "plan_added_at": added.isoformat()})
            if not requirements:
                raise ValueError(f"Missing Schedule entries for {code}")
            patients.append({"code": code, "name": name, "branch": source_branch,
                             "source_plan_id": source_id,
                             "initial_roster": True,
                             "phone": phone_number(contacts[code] if contacts is not None else row.get("Parent_No", "")),
                             "requirements": requirements})
    if not patients or not consultants:
        raise ValueError("The initial source needs at least one patient and consultant")
    locations = [{"branch": name, "name": f"{name} — FICTIONAL TEST" if fictional else name, "capacity": max(1, sum(
        c["active"] and branch_key(c["branch"]) == key for c in consultants.values()))}
        for key, name in sorted(branches.items())]
    return {"version": "malaparamba-fictional-v3" if fictional else "main-workflow-v1", "source": source,
            "centre": locations[0]["name"], "capacity": locations[0]["capacity"], "locations": locations,
            "therapists": sorted(consultants.values(), key=lambda item: item["code"]),
            "patients": patients}


def put_setting(session, key: str, value) -> None:
    item = session.get(FlowSetting, key)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if item is None:
        session.add(FlowSetting(key=key, value=serialized))
    else:
        item.value = serialized


def plan_warning(session, warnings: list, code: str, requirement_id: int, text: str) -> None:
    """Persist each distinct plan-change warning without duplicating the inbox."""
    warnings.append({"patient": code, "requirement_id": requirement_id, "message": text})
    key = "plan:" + hashlib.sha256(f"{code}:{requirement_id}:{text}".encode()).hexdigest()
    if session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == key)) is None:
        session.add(FlowInbox(event_key=key, patient_code=code,
                              reason="Zoho plan changed — review saved appointments", text=text))


def apply_reports(flow, reports: dict, plan_start: date | None = None, *, window: dict | None = None,
                  full_snapshot: bool = False) -> dict:
    """Commit merged source records, projections and checkpoint in one transaction."""
    with flow.lock, flow.inbound_lock:
        try:
            result = _apply_reports_locked(flow, reports, plan_start, window=window, full_snapshot=full_snapshot)
        except ValueError as exc:
            # The import transaction has rolled back. Persist only the review
            # item separately; clinical data, cache and checkpoint stay intact.
            message = str(exc)
            key = "zoho-invalid:" + hashlib.sha256(message.encode()).hexdigest()
            with flow.sessions.begin() as session:
                issue = session.scalar(select(FlowInbox).where(FlowInbox.event_key == key))
                if issue is None:
                    session.add(FlowInbox(event_key=key, reason="Zoho import blocked — invalid source record",
                                          text=message + ". Correct the source record in Zoho and rerun the import. "
                                          "Existing appointments and the successful checkpoint were retained."))
                else:
                    issue.resolved = 0
            raise
        return result


def _apply_reports_locked(flow, reports: dict, plan_start: date | None, *, window: dict | None,
                          full_snapshot: bool) -> dict:
    with flow.lock, flow.inbound_lock, flow.sessions.begin() as session:
        if window is not None and not isinstance(window, dict):
            raise ValueError("Supply the complete sync window")
        checkpoint = session.get(FlowSetting, CHECKPOINT_KEY)
        current = json.loads(checkpoint.value) if checkpoint else None
        source_zone = session.get(FlowSetting, TIMEZONE_KEY)
        if type(full_snapshot) is not bool or (full_snapshot and window is None):
            raise ValueError("Full reconciliation requires a bounded sync window")
        cache = session.get(FlowSetting, "zoho_test_source")
        previous_zones = json.loads(source_zone.value) if source_zone else None
        if (current is not None or cache is not None) and previous_zones != SOURCE_TIMEZONES and not full_snapshot:
            raise ValueError("Source timestamp timezone changed; run --reconcile once before incremental sync")
        if window is not None and window.get("from") != current:
            raise ValueError("Sync checkpoint changed; fetch status and retry without advancing it")
        if window is None and current is not None:
            raise ValueError("Use a checkpoint window for this incremental source")
        previous_reports = json.loads(cache.value) if cache else None
        timezone_migration = previous_zones != SOURCE_TIMEZONES
        if previous_reports and timezone_migration:
            previous_reports = rebase_cached_times(previous_reports, previous_zones)
        reports = merge_reports(previous_reports, reports, window,
                                full_snapshot=full_snapshot,
                                timezone_migration=bool(full_snapshot and timezone_migration))
        is_test = getattr(flow, "is_test", True)
        rejected = []
        scenario = convert_reports(reports, plan_start, fictional=is_test,
                                   source=getattr(flow, "source_name", f"{OWNER}/{APP}"), rejected=rejected)
        quarantined = [row["code"] for row in rejected]
        put_setting(session, "zoho_quarantined_patients", quarantined)
        corrected_codes = {row["code"] for row in scenario["patients"]}
        for issue in session.scalars(select(FlowInbox).where(FlowInbox.event_key.like("zoho-quarantine:%"))):
            if issue.patient_code in corrected_codes:
                issue.resolved = 1
        for row in rejected:
            key = "zoho-quarantine:" + hashlib.sha256(row["id"].encode()).hexdigest()
            issue = session.scalar(select(FlowInbox).where(FlowInbox.event_key == key))
            if issue is None:
                session.add(FlowInbox(event_key=key, patient_code=row["code"],
                    reason="Zoho patient record needs correction",
                    text=row["reason"] + ". Valid patients were imported. This patient has no new offers; correct Zoho and retry. The checkpoint has not advanced."))
            else:
                issue.resolved = 0
        digest = hashlib.sha256(json.dumps(scenario, sort_keys=True).encode()).hexdigest()
        marker = session.get(FlowSetting, "scenario")
        if marker is None or marker.value != scenario["version"]:
            raise ValueError("Import requires the known v3 test workspace")
        previous = session.get(FlowSetting, SCENARIO_KEY)
        if is_test and previous is None and session.scalar(select(Appointment.id).limit(1)) is not None:
            raise ValueError("Existing fixture appointments prevent switching to Zoho. Start a deliberate fresh test first.")
        old_hash = session.get(FlowSetting, "zoho_test_fingerprint")
        changed = old_hash is None or json.loads(old_hash.value) != digest
        locations = {branch_key(item.name): item for item in session.scalars(select(Location))}
        for row in scenario["locations"]:
            key = branch_key(row["branch"])
            location = locations.get(key)
            if location is None:
                location = Location(name=row["name"], capacity=row["capacity"], active=True)
                session.add(location)
                session.flush()
                locations[key] = location
            location.capacity, location.active = row["capacity"], True
        departments = {clean_text(item.name).casefold(): item for item in session.scalars(select(Department))}
        therapists = {item.therapist_code: item for item in session.scalars(select(Therapist))}
        source_codes = {item["code"] for item in scenario["therapists"]}
        for row in scenario["therapists"]:
            location = locations[branch_key(row["branch"])]
            therapy_key = row["therapy"].casefold()
            if therapy_key not in departments:
                department = Department(name=row["therapy"], active=True)
                session.add(department)
                session.flush()
                departments[therapy_key] = department
            therapist = therapists.get(row["code"])
            if therapist is None:
                if not is_test and any(clean_text(item.name).casefold() == row["name"].casefold()
                                       for item in therapists.values()):
                    raise ValueError("An existing consultant has this name under another source ID. "
                                     "Review the consultant identity mapping before the main import; no records changed.")
                therapist = Therapist(therapist_code=row["code"], name=row["name"],
                                      department_id=departments[therapy_key].id,
                                      location_id=location.id, active=row["active"])
                session.add(therapist)
                session.flush()
                therapists[row["code"]] = therapist
            elif (therapist.department_id != departments[therapy_key].id
                  or therapist.location_id != location.id):
                raise ValueError("Consultant capability changes require staff review")
            if therapist.active != row["active"] and session.scalar(select(Appointment.id).where(
                    Appointment.therapist_id == therapist.id).limit(1)) is not None:
                key = f"zoho-consultant-status:{therapist.id}:{row['active']}"
                if session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == key)) is None:
                    session.add(FlowInbox(event_key=key, reason="Consultant status changed — review bookings",
                        text="Consultant status was updated. Saved history and appointments were retained; review future visits. Inactive staff receive no new generated bookings."))
            therapist.name, therapist.active = row["name"], row["active"]
        for code, therapist in therapists.items():
            if is_test and code not in source_codes:
                therapist.active = False
        if not is_test:
            session.flush()
            # Preserve the original capacity rule across imported and existing doctors.
            for location in locations.values():
                location.capacity = max(1, sum(therapist.active and therapist.location_id == location.id
                                               for therapist in therapists.values()))
        warnings = []
        # Bind only the previously imported projection, before considering the
        # incoming plan. Never label old bookings with an incoming renewal's ID.
        previous_plans = {}
        for old in (previous_reports or {}).get("Plan_Report", []):
            code = clean_text(old.get("RegistrationID"))
            version = (source_time(old.get("Modified_Time", old["Added_Time"])), source_time(old["Added_Time"]))
            if code not in previous_plans or version > previous_plans[code][0]:
                previous_plans[code] = (version, clean_text(old["ID"]))
        # A retained projection knows its course exactly. Older installations
        # without this field used Modified Time first, as the fallback above does.
        for old in json.loads(previous.value)["patients"] if previous else []:
            if old.get("source_plan_id"):
                previous_plans[old["code"]] = (None, old["source_plan_id"])
        by_code = {item.patient_code: item for item in session.scalars(select(Patient))}
        if is_test and previous is None:
            source_patient_codes = {item["code"] for item in scenario["patients"]}
            for code, item in by_code.items():
                if code not in source_patient_codes:
                    item.active = False
                    for requirement in session.scalars(select(TreatmentRequirement).where(
                            TreatmentRequirement.patient_id == item.id)):
                        requirement.active = False
        for row in scenario["patients"]:
            patient = by_code.get(row["code"])
            if patient is None:
                patient = Patient(patient_code=row["code"], name=row["name"], active=True)
                session.add(patient)
                session.flush()
            # Scheduling eligibility is set in the directory, independently of source data.
            patient.name = row["name"]
            patient_requirements = list(session.scalars(select(TreatmentRequirement).where(
                TreatmentRequirement.patient_id == patient.id)))
            incoming_departments = {departments[r["therapy"].casefold()].id
                                    for r in row["requirements"]}
            active = [item for item in patient_requirements if item.active]
            for item in active:
                if item.source_plan_id is None:
                    if row["code"] in previous_plans:
                        item.source_plan_id = previous_plans[row["code"]][1]
                    elif item.department_id in incoming_departments and session.scalar(select(Appointment.id).where(
                            Appointment.treatment_requirement_id == item.id).limit(1)) is None:
                        # First source adoption may reuse an unbooked local
                        # requirement. There is no appointment history to relabel.
                        item.source_plan_id = row["source_plan_id"]
                if item.source_plan_id != row["source_plan_id"]:
                    # Retire demand, preserving the prescription and every
                    # appointment attached to it. Existing offers stay valid.
                    item.active = False
                    if session.scalar(select(Appointment.id).where(
                            Appointment.treatment_requirement_id == item.id,
                            Appointment.date >= min(date.fromisoformat(r["plan_start_date"])
                                                    for r in row["requirements"]),
                            Appointment.status.in_(("PROPOSED", "PENDING_CONFIRMATION", "CONFIRMED")),
                    ).limit(1)) is not None:
                        plan_warning(session, warnings, row["code"], item.id,
                                     "New treatment course imported. Existing bookings for the previous "
                                     "course are retained; review them separately. They do not count "
                                     "toward the new course.")
            # Include inactive therapies from this same course so removing and
            # re-adding a therapy does not duplicate its identity or history.
            course_requirements = [item for item in patient_requirements
                                   if item.source_plan_id == row["source_plan_id"]]
            active = [item for item in course_requirements if item.active]
            retained = set()
            for index, requirement in enumerate(row["requirements"]):
                department = departments[requirement["therapy"].casefold()]
                location = locations[branch_key(requirement["branch"])]
                existing = next((item for item in course_requirements if item.department_id == department.id), None)
                values = {"department_id": department.id,
                          "assigned_therapist_id": therapists[requirement["therapist"]].id,
                          "location_id": location.id, "sessions_per_week": requirement["weekly"],
                          "sequence_order": index, "duration_months": requirement["duration_months"],
                          "plan_start_date": date.fromisoformat(requirement["plan_start_date"])}
                if existing is None:
                    existing = TreatmentRequirement(patient_id=patient.id, active=True,
                                                    source_plan_id=row["source_plan_id"], **values)
                    session.add(existing)
                    session.flush()
                elif any(getattr(existing, key) != value for key, value in values.items()):
                    if session.scalar(select(Appointment.id).where(
                            Appointment.treatment_requirement_id == existing.id).limit(1)) is not None:
                        structural = any(getattr(existing, key) != values[key] for key in (
                            "assigned_therapist_id", "location_id"))
                        if structural:
                            plan_warning(session, warnings, row["code"], existing.id,
                                         f"{requirement['therapy']}: new doctor or branch needs staff review. "
                                         f"Requested: {therapists[requirement['therapist']].name} at {requirement['branch']}. "
                                         "The existing requirement and bookings are retained; other records were imported.")
                            retained.add(existing.id)
                            continue
                        labels = {"sessions_per_week": "Sessions per week", "plan_start_date": "Treatment start",
                                  "duration_months": "Duration in months", "sequence_order": "Therapy order"}
                        changes = "; ".join(f"{labels.get(key, key)}: {getattr(existing, key)} → {value}"
                                            for key, value in values.items() if getattr(existing, key) != value)
                        plan_warning(session, warnings, row["code"], existing.id,
                                     f"{requirement['therapy']}: {changes}. Latest target and dates applied; "
                                     "saved bookings and completed sessions are unchanged. Review any excess or out-of-plan bookings.")
                    for key, value in values.items():
                        setattr(existing, key, value)
                    existing.source_status = None
                existing.active = True
                existing.plan_added_at = datetime.fromisoformat(requirement["plan_added_at"])
                retained.add(existing.id)
            for item in active:
                if item.id not in retained:
                    if session.scalar(select(Appointment.id).where(
                            Appointment.treatment_requirement_id == item.id).limit(1)) is not None:
                        if item.sessions_per_week != 0:
                            plan_warning(session, warnings, row["code"], item.id,
                                         "Therapy removed from latest plan. Future demand is now zero; "
                                         "existing bookings and completed sessions are retained for staff review.")
                        # Keep the historical assignment available to validate
                        # pinned bookings, but generate no new demand for it.
                        item.sessions_per_week = 0
                        item.source_status = "removed_from_source"
                    else:
                        item.active = False
        # Rebind only offers for which no provider send has been attempted.
        # Sent or uncertain offers remain tied to their original recipient.
        phones = {item["code"]: item["phone"] for item in scenario["patients"]}
        for offer in session.scalars(select(FlowOffer).where(FlowOffer.state == "PENDING")):
            number = phones.get(offer.patient_code)
            if not number:
                continue
            key = hashlib.sha256(f"offer:{offer.token}".encode()).hexdigest()
            job = session.scalar(select(FlowJob).where(FlowJob.event_key == key))
            if (not job or job.kind != "OFFER" or job.status not in {
                    "QUEUED", "RETRY_WAIT", "BLOCKED", "WAITING_FOR_CONTACT", "WAITING_FOR_SYNC", "WAITING_FOR_ENABLE"} or job.message_id):
                continue
            changed_recipient = number != offer.recipient
            if not changed_recipient and job.status != "WAITING_FOR_SYNC":
                continue
            payload = json.loads(job.payload)
            payload["recipient"] = number
            payload.pop("session_expires_at", None)
            job.payload = json.dumps(payload, ensure_ascii=False)
            offer.recipient = number
            # Explicitly rejected attempts can follow corrected contacts, but a
            # source import must not erase their retry delay/configuration pause.
            if job.status in {"RETRY_WAIT", "BLOCKED"}:
                flow._audit(session, "flow_offer_recipient_updated", f"offer:{offer.token}")
                continue
            if flow.live and flow.message_mode == "session" and flow._session_deadline(session, number) <= time.time():
                job.status = "WAITING_FOR_CONTACT"
                job.error = "Parent must message first; no send was attempted"
            else:
                job.status, job.error = "QUEUED", None
            if flow.live and not getattr(flow, "messaging_enabled", True):
                job.status = "WAITING_FOR_ENABLE"
                job.error = "Messaging is OFF in the main app; no send was attempted"
            flow._audit(session, "flow_offer_recipient_updated" if changed_recipient else
                        "flow_offer_sync_resumed", f"offer:{offer.token}")
        put_setting(session, SCENARIO_KEY, scenario)
        # Same transaction as the roster: readers cache parent numbers by this version.
        from flow_test.performance_schema import SOURCE_VERSION_KEY, source_version_for
        source_version = source_version_for(scenario)
        version_row = session.get(FlowSetting, SOURCE_VERSION_KEY)  # Stored raw, not JSON-encoded.
        if version_row is None:
            session.add(FlowSetting(key=SOURCE_VERSION_KEY, value=source_version))
        else:
            version_row.value = source_version
        put_setting(session, "zoho_test_source", reports)
        put_setting(session, "zoho_test_fingerprint", digest)
        put_setting(session, TIMEZONE_KEY, SOURCE_TIMEZONES)
        if window is not None and not rejected:
            put_setting(session, CHECKPOINT_KEY, window["to"])
        result = {"patients": len(scenario["patients"]), "consultants": len(scenario["therapists"]), "requirements": sum(
            len(item["requirements"]) for item in scenario["patients"]),
                  "weekly_sessions": sum(req["weekly"] for item in scenario["patients"] for req in item["requirements"]),
                  "changed": changed, "plan_start_source": "Added_Time (Asia/Kolkata date)",
                  "branches": len(scenario["locations"]),
                  "warnings": warnings,
                  "synced_at": datetime.utcnow().isoformat() + "Z",
                  "checkpoint": window["to"] if window and not rejected else current,
                  "partial": bool(rejected), "quarantined": quarantined}
        put_setting(session, "zoho_test_sync", result)
        if changed:
            flow._audit(session, "flow_zoho_imported", scenario["source"])
    flow.use_source_contacts(scenario)
    flow._synced_source_version = source_version
    return result

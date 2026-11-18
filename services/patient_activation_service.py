"""Current Thanal source checks for staff-controlled patient activation."""

from collections import defaultdict
from datetime import datetime, timezone
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import AuditLog, Department, Patient, Therapist, TreatmentRequirement
from services.thanal_source_reader import prescription

ACTION = "patient_activation_review"
ENTITY = "source:thanal/eic"
NO_PLAN = "No therapy plan was found for this patient in the Calicut report. Add or link their plan in Zoho."


def prepare_activation_review(reports: dict, snapshot: dict | None = None) -> dict:
    """Collect all fixable source problems, without changing patient eligibility."""
    if snapshot is None:
        from main_workflow.thanal_sync import prepare_snapshot
        snapshot = prepare_snapshot(reports)
    regs = snapshot["registrations"]["records"]
    by_id = {r["id"]: r for r in regs}
    doctors = {r["id"]: r for r in snapshot["consultants"]["records"]}
    issues = defaultdict(list)
    for reg in regs:
        if not reg["phone"]:
            issues[reg["code"]].append("The phone number is missing or invalid. Enter a valid 10-digit Indian mobile number in Zoho Registrations.")
    for problem in snapshot["registrations"]["issues"]:
        if problem["reason"] in {"missing_or_invalid_name", "duplicate_registration_code"}:
            reg = by_id.get(problem["source_id"])
            if reg:
                issues[reg["code"]].append(
                    "The patient's name is missing or too long. Correct it in Zoho Registrations."
                    if problem["reason"] == "missing_or_invalid_name" else
                    "More than one registration uses this registration code. Correct the duplicate in Zoho.")
    latest = {}
    for row in reports["All_Therapy_Schedule_Plans"]:
        reg = by_id.get((row.get("Name") or {}).get("ID"))
        if reg:
            stamp = datetime.strptime(row["Added_Time"], "%d/%m/%Y %H:%M:%S")
            if reg["code"] not in latest or stamp > latest[reg["code"]][0]:
                latest[reg["code"]] = (stamp, row)
    for code, (_, plan) in latest.items():
        problems = issues[code]
        rows = plan.get("Schedule")
        if not isinstance(rows, list) or not rows:
            problems.append("The therapy plan has no therapy details. Add the therapy, consultant, weekly sessions and duration in Zoho.")
            continue
        seen = set()
        for part in rows:
            if not isinstance(part, dict):
                problems.append("A therapy row could not be read. Correct its details in Zoho.")
                continue
            therapy = part.get("Therapy") if isinstance(part.get("Therapy"), dict) else {}
            consultant = part.get("Consultant") if isinstance(part.get("Consultant"), dict) else {}
            label = therapy.get("Value") or "This therapy"
            doctor = doctors.get(consultant.get("ID"))
            if not therapy.get("ID"):
                problems.append("A therapy has not been selected. Choose the therapy in Zoho.")
            elif therapy["ID"] in seen:
                problems.append(f"{label} appears more than once in the latest plan. Correct the duplicate therapy row in Zoho.")
            seen.add(therapy.get("ID"))
            if not consultant.get("ID"):
                problems.append(f"{label}: no consultant is selected. Choose a consultant in Zoho.")
            elif doctor is None:
                problems.append(f"{label}: the assigned consultant's details are incomplete. Check their name, centre, status and therapies in Zoho Consultants.")
            else:
                # Consultant Active/Inactive is staff-owned locally; activation_review checks it live.
                if doctor["branch"]["id"] != plan["Branch"]["ID"]:
                    problems.append(f"{label}: {doctor['name']} belongs to another centre. Assign a consultant from the Calicut centre in Zoho.")
                if therapy.get("ID") not in {t["id"] for t in doctor["therapies"]}:
                    problems.append(f"{label}: {doctor['name']} is not listed for this therapy. Correct the consultant's therapy details or assignment in Zoho.")
            try:
                values = prescription(part)
                if values["sessions_per_week"] > 99:
                    problems.append(f"{label}: the weekly session count is too high. Correct it in Zoho.")
            except ValueError:
                problems.append(f"{label}: weekly sessions or treatment months are missing or invalid. Enter a number greater than zero for both in Zoho.")
    for problem in snapshot["issues"]:
        code = problem["code"]
        if not code:
            continue
        reason = problem["reason"]
        if reason == "ambiguous_latest_plan":
            issues[code].append("Two plans have the same latest creation time. Ask the Zoho team to identify the current plan.")
        elif reason == "invalid_treatment_duration":
            issues[code].append("The treatment duration is outside the supported date range. Correct the number of months in Zoho.")
        elif not issues[code]:
            issues[code].append("The latest therapy plan has incomplete details. Check the registration, therapy and consultant links in Zoho.")
    return {"source": "thanal/eic", "checkedAt": datetime.now(timezone.utc).isoformat(),
            "planCodes": sorted(latest),
            "issues": {code: list(dict.fromkeys(items)) for code, items in issues.items() if items}}


def store_activation_review(session: Session, review: dict) -> None:
    """Publish source readiness in the same transaction as a successful import."""
    session.add(AuditLog(action=ACTION, entity=ENTITY, user="zoho_source_sync",
                         new_value=json.dumps(review)))


def inactive_consultant_issues(session: Session, patient: Patient) -> list[str]:
    """The child's current plan names a consultant whom staff marked inactive in our app."""
    rows = session.execute(
        select(Department.name, Therapist.name)
        .select_from(TreatmentRequirement)
        .join(Therapist, Therapist.id == TreatmentRequirement.assigned_therapist_id)
        .join(Department, Department.id == TreatmentRequirement.department_id)
        .where(TreatmentRequirement.patient_id == patient.id, TreatmentRequirement.active.is_(True),
               Therapist.active.is_(False))
        .order_by(Department.name, Therapist.name))
    return [f"{therapy}: {doctor} is inactive. Mark them active on the Clinical team page, "
            "or assign an active consultant in Zoho." for therapy, doctor in rows]


def activation_review(session: Session, patient: Patient) -> dict:
    """Use the latest complete source check, never the spreadsheet as an allowlist."""
    row = session.scalar(select(AuditLog).where(AuditLog.action == ACTION, AuditLog.entity == ENTITY)
                         .order_by(AuditLog.id.desc()).limit(1))
    if row is None:
        baseline = session.scalar(select(AuditLog.id).where(AuditLog.action == "patient_roster_baseline").limit(1))
        if baseline:
            return {"canActivate": False, "issues": ["Patient details have not been checked yet. Run the Zoho sync before marking this patient active."],
                    "checkedAt": None, "required": True}
        return {"canActivate": True, "issues": [], "checkedAt": None, "required": False}
    review = json.loads(row.new_value)
    issues = list(review["issues"].get(patient.patient_code, []))
    if patient.patient_code not in review["planCodes"]:
        issues.insert(0, NO_PLAN)
    issues += inactive_consultant_issues(session, patient)
    return {"canActivate": not issues, "issues": issues, "checkedAt": review["checkedAt"],
            "required": True, "nextStep": "Correct these details in Zoho, run the Zoho sync, then try Mark active again."}

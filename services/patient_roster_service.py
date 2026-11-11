"""Explicit one-time patient eligibility reconciliation, never a daily allowlist."""

from collections import defaultdict
from datetime import date
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import AuditLog, Patient
from services.patient_status_service import change_patient_statuses


def prepare_roster(entries: list[dict], snapshot: dict, reports: dict) -> list[dict]:
    """Match exact registration codes; retain invalid-plan children for the directory.

    Missing contacts now prevent activation under the manager's revised policy.
    No names are used for identity matching and no clinician is substituted.
    """
    listed = {entry["code"]: entry for entry in entries}
    if len(listed) != len(entries) or not listed:
        raise ValueError("Supply a nonempty roster with unique registration codes")
    if any(not isinstance(code, str) or not code or len(code) > 40 for code in listed):
        raise ValueError("Roster registration codes must fit the patient identity field")
    by_code, by_id = defaultdict(list), {}
    for reg in snapshot["registrations"]["records"]:
        by_code[reg["code"]].append(reg)
        by_id[reg["id"]] = reg
    plan_codes = {by_id[row["Name"]["ID"]]["code"]
                  for row in reports["All_Therapy_Schedule_Plans"]
                  if isinstance(row.get("Name"), dict) and row["Name"].get("ID") in by_id}
    valid = {p["registration"]["code"]: p for p in snapshot["patients"]}
    doctors = {r["id"]: r for r in snapshot["consultants"]["records"]}
    issues, contacts = defaultdict(list), defaultdict(list)
    for issue in snapshot["issues"]:
        issues[issue["code"]].append(issue["reason"])
    for issue in snapshot["registrations"]["issues"]:
        if issue["reason"] in {"invalid_phone", "missing_phone"}:
            contacts[issue["source_id"]].append(issue["reason"])
    result = []
    for code in sorted(set(listed) | plan_codes):
        if not code or len(code) > 40:
            # No safe persistent Patient identity can be invented for such a row.
            continue
        regs = by_code[code]
        reasons = list(issues[code])
        if len(regs) != 1:
            reasons.append("registration_not_found" if not regs else "ambiguous_registration_code")
        elif not regs[0]["phone"]:
            reasons.append("invalid_phone")
        if code not in valid and not issues[code]:
            reasons.append("no_valid_calicut_therapy_plan")
        if code in valid:
            for therapy in valid[code]["therapies"]:
                if not doctors[therapy["consultant_id"]]["active"]:
                    reasons.append("assigned_consultant_inactive")
        name = regs[0]["name"] if len(regs) == 1 else listed.get(code, {}).get("name")
        if not name or len(name) > 120:
            reasons.append("missing_or_invalid_name")
            name = None
        result.append({"code": code, "name": name,
                       "listed_name": listed.get(code, {}).get("name"),
                       "listed": code in listed, "active": code in listed and not reasons,
                       "reasons": sorted(set(reasons)),
                       "contact_issues": contacts[regs[0]["id"]] if len(regs) == 1 else []})
    return result


def apply_roster(session: Session, decisions: list[dict], *, operation_id: str,
                 today: date, admin_user: str = "scheduler_manager") -> dict:
    """Apply once within the caller's transaction, preserving plans and history.

    A replay is refused, so tomorrow's new patients or later staff changes cannot
    be overwritten by accidentally rerunning the same spreadsheet.
    """
    entity = "roster:" + operation_id
    if len(entity) > 80 or not operation_id:
        raise ValueError("Supply a short stable roster operation ID")
    if session.scalar(select(AuditLog.id).where(
            AuditLog.action == "patient_roster_baseline", AuditLog.entity == entity)):
        raise ValueError("This one-time roster has already been applied")
    codes = [row["code"] for row in decisions]
    if not codes or len(codes) != len(set(codes)):
        raise ValueError("Roster decisions require unique patient identities")
    patients = {p.patient_code: p for p in session.scalars(select(Patient).with_for_update())}
    created = changed = 0
    old = {code: {"active": p.active, "name": p.name} for code, p in patients.items()}
    desired = {r["code"]: r for r in decisions}
    for row in decisions:
        if row["active"] and (not row["listed"] or row["reasons"]):
            raise ValueError("A patient with source blockers cannot be activated")
        patient = patients.get(row["code"])
        if patient is None:
            patient = Patient(patient_code=row["code"], name=row["name"], active=False)
            session.add(patient)
            patients[row["code"]] = patient
            created += 1
        elif row["name"] is not None:
            patient.name = row["name"]
    session.flush()  # Allocate new patient IDs once; batch inserts on PostgreSQL.
    transitions = []
    for code, patient in patients.items():
        row = desired.get(code)
        active = bool(row and row["active"])
        reason = ("Source review: " + ", ".join(row["reasons"]) if row and row["reasons"]
                  else "Not on the manager's one-time active-patient roster")
        transitions.append((patient, active, reason))
    changed = change_patient_statuses(session, transitions, today, admin_user)
    result = {"created": created, "status_changes": changed,
              "active": sum(p.active for p in patients.values()),
              "inactive": sum(not p.active for p in patients.values()), "patients": len(patients),
              "listed": sum(r["listed"] for r in decisions),
              "listed_blocked": sum(r["listed"] and not r["active"] for r in decisions)}
    session.add(AuditLog(action="patient_roster_baseline", entity=entity, user=admin_user,
                         old_value=json.dumps(old), new_value=json.dumps({"summary": result,
                             "decisions": [{"code": r["code"], "active": r["active"],
                                            "reasons": r["reasons"]} for r in decisions]})))
    return result

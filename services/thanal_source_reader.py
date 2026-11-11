"""Read Thanal's lookup/subform contract without importing clinical records.

This projection is intentionally separate from the scalar scheduler importer.
Consultant adoption is implemented separately in thanal_consultant_import.
Patient/plan database integration is still pending. No display string is parsed.
"""

from collections import Counter
from datetime import datetime
from typing import Any

REPORTS = ("All_Registrations", "All_Consultants", "All_Therapy_Schedule_Plans")


def prescription(row: dict) -> dict[str, int]:
    """Apply the confirmed Thanal contract: frequency, then MONTHS.

    Despite its API name, No_Of_Week contains the month count for this report.
    This source-specific mapping must never be applied to unrelated Zoho apps.
    """
    values = [_text(row.get(field)) for field in ("Per_Week", "No_Of_Week")]
    if any(not value.isdecimal() or int(value) <= 0 for value in values):
        raise ValueError("Supply positive weekly frequency and treatment duration in months")
    return {"sessions_per_week": int(values[0]), "duration_months": int(values[1])}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Expected a scalar source field")
    return str(value).strip()


def _lookup(value: Any, label: str) -> dict:
    if value in (None, "", {}):
        return {"id": "", "label": ""}
    if not isinstance(value, dict):
        raise ValueError("Expected a single lookup object")
    return {"id": _text(value.get("ID")), "label": _text(value.get(label))}


def _lookups(value: Any) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a lookup list")
    return [_lookup(item, "Value") for item in value]


def project_reports(reports: dict) -> dict:
    """Select required fields and join by record ID; retain unresolved fields raw.

    All plan statuses are included as explicitly instructed. This does not activate
    discharged registrations, infer missing consultants, or change local eligibility.
    Returned names/phones are sensitive and must not be logged or committed.
    """
    if set(reports) != set(REPORTS):
        raise ValueError("Supply all three complete Thanal source reports")
    indexed = {}
    for report in REPORTS:
        rows = reports[report]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{report}: expected source records")
        index = {}
        for row in rows:
            ident = _text(row.get("ID"))
            if not ident.isdigit() or ident in index:
                raise ValueError(f"{report}: missing or duplicate record ID")
            index[ident] = row
        indexed[report] = index
    registrations = {ident: {
        "id": ident, "code": _text(row.get("Registration_Id")),
        "name": _text(row.get("Name2")), "phone": _text(row.get("Phone_Number")),
        "branch": _lookup(row.get("Branch"), "Branch_Name"),
        "status": _text(row.get("Status")), "added_time": _text(row.get("Added_Time")),
    } for ident, row in indexed[REPORTS[0]].items()}
    consultants = {ident: {
        "id": ident, "name": _text(row.get("Name")),
        "branch": _lookup(row.get("Branch"), "Branch_Name"),
        "departments": _lookups(row.get("Departments", [])),
        "therapies": _lookups(row.get("Consultations", [])),
        "employment_type": _text(row.get("Consultation_Type")),
        "status": _text(row.get("Status")),
        "added_time": _text(row.get("Added_Time")),
    } for ident, row in indexed[REPORTS[1]].items()}
    plans, issues = [], []
    for ident, row in indexed[REPORTS[2]].items():
        child = _lookup(row.get("Name"), "Name2")
        registration = registrations.get(child["id"])
        if registration is None:
            issues.append({"plan_id": ident, "reason": "missing_registration_link"})
        elif _text(row.get("Name.Registration_Id")) not in ("", registration["code"]):
            issues.append({"plan_id": ident, "reason": "registration_code_conflict"})
        schedule = row.get("Schedule", [])
        if not isinstance(schedule, list) or any(not isinstance(s, dict) for s in schedule):
            raise ValueError("Plan Schedule must be a list of therapy rows")
        therapies = []
        for item in schedule:
            clinician = _lookup(item.get("Consultant"), "Name")
            if not clinician["id"]:
                issues.append({"plan_id": ident, "reason": "missing_consultant_assignment"})
            elif clinician["id"] not in consultants:
                issues.append({"plan_id": ident, "reason": "unknown_consultant_id"})
            therapies.append({
                "id": _text(item.get("ID")), "therapy": _lookup(item.get("Therapy"), "Value"),
                "consultant_id": clinician["id"],
                # Retain raw source fields separately from the confirmed prescription.
                "per_week": _text(item.get("Per_Week")),
                "number_of_weeks": _text(item.get("No_Of_Week")),
                "number_of_months": _text(item.get("No_Of_Months")),
                "months_field_returned": "No_Of_Months" in item,
            })
            try:
                therapies[-1]["prescription"] = prescription(item)
            except ValueError:
                therapies[-1]["prescription"] = None
                issues.append({"plan_id": ident, "reason": "invalid_frequency_or_duration"})
        plans.append({"id": ident, "registration_id": child["id"],
                      "branch": _lookup(row.get("Branch"), "Branch_Name"),
                      "status": _text(row.get("Status")),
                      "added_time": _text(row.get("Added_Time")),
                      "therapies": therapies})
    return {"registrations": registrations, "consultants": consultants,
            "plans": plans, "issues": issues}


def readiness_summary(source: dict) -> dict:
    """Return aggregate diagnostics only; no names, phone numbers or source IDs."""
    issues = Counter(item["reason"] for item in source["issues"])
    versions = {}
    for plan in source["plans"]:
        try:
            added = datetime.strptime(plan["added_time"], "%d/%m/%Y %H:%M:%S")
        except ValueError:
            issues["invalid_plan_added_time"] += 1
            continue
        versions.setdefault(plan["registration_id"], []).append((added, plan))
    current = []
    for rows in versions.values():
        latest = max(added for added, _ in rows)
        newest = [p for added, p in rows if added == latest]
        if len(newest) != 1:
            issues["ambiguous_latest_plan"] += 1
        else:
            current.append(newest[0])
    therapies = [item for plan in current for item in plan["therapies"]]
    return {
        "registrations": len(source["registrations"]), "consultants": len(source["consultants"]),
        "plans": len(source["plans"]), "unambiguous_current_plans": len(current),
        "current_therapy_rows": len(therapies), "issues_across_all_plan_versions": dict(issues),
        "current_rows_without_months_field": sum(not r["months_field_returned"] for r in therapies),
        "consultants_without_added_time": sum(not r["added_time"] for r in source["consultants"].values()),
        "plan_status_filter": "none", "ready_for_import": False,
        "remaining_integration_work": ["patient_plan_snapshot_contract", "reuse_consultant_capability_bindings",
                                       "patient_plan_database_import_adapter"],
    }

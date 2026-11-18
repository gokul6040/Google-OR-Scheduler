"""Consultant-only, atomic Thanal import; no patients, appointments or messages.

Read the complete report, validate it, then explicitly apply it to a prepared
main workflow. Missing rows are not deletions. Consultant IDs are never split
into separate people merely because they offer several therapies.
"""

from collections import Counter
import json
from typing import Any

from sqlalchemy import select

from database.models import (Appointment, Department, Location, Therapist,
                             TherapistCapability, TherapistLocation, TreatmentRequirement)
from flow_test.models import FlowSetting
from services.thanal_source_reader import _lookup, _lookups, _text

MAP_KEY = "thanal_consultant_identity_map"


def prepare_consultants(rows: list[dict]) -> dict:
    """Validate the complete report; return selected records plus bounded issues."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("Supply a nonempty complete All_Consultants report")
    records, issues, seen = [], [], set()
    labels = {"branches": {}, "therapies": {}}
    reverse = {"branches": {}, "therapies": {}}
    for index, row in enumerate(rows, 1):
        ident = ""
        try:
            if not isinstance(row, dict):
                raise ValueError("invalid_record")
            ident = _text(row.get("ID"))
            if not ident.isdigit() or len(ident) > 37 or ident in seen:
                raise ValueError("missing_or_duplicate_consultant_id")
            seen.add(ident)
            name = _text(row.get("Name"))
            if not name or len(name) > 120:
                raise ValueError("missing_or_invalid_name")
            status = _text(row.get("Status")).casefold()
            if status not in {"active", "inactive"}:
                raise ValueError("missing_or_unknown_status")
            branch = _lookup(row.get("Branch"), "Branch_Name")
            therapies = _lookups(row.get("Consultations"))
            if not therapies:
                raise ValueError("missing_therapy_capabilities")
            for kind, values in (("branches", [branch]), ("therapies", therapies)):
                for value in values:
                    key, label = value["id"], value["label"]
                    if not key.isdigit() or not label or len(label) > 120:
                        raise ValueError("missing_or_invalid_" + kind + "_lookup")
                    if key in labels[kind] and labels[kind][key] != label:
                        raise ValueError("conflicting_" + kind + "_labels")
                    normalized = label.casefold()
                    if normalized in reverse[kind] and reverse[kind][normalized] != key:
                        raise ValueError("ambiguous_" + kind + "_identity")
                    labels[kind][key], reverse[kind][normalized] = label, key
            unique = {therapy["id"]: therapy for therapy in therapies}
            records.append({"id": ident, "code": "ZC-" + ident, "name": name,
                            "active": status == "active", "branch": branch,
                            "therapies": [unique[key] for key in sorted(unique)]})
        except (ValueError, TypeError) as exc:
            # No row values/names/phones in the reason. IDs enable source review.
            issues.append({"row": index, "source_id": ident, "reason": str(exc)})
    return {"records": records, "issues": issues, "labels": labels,
            "summary": {"downloaded": len(rows), "valid": len(records),
                        "active": sum(r["active"] for r in records),
                        "inactive": sum(not r["active"] for r in records),
                        "multi_therapy_consultants": sum(len(r["therapies"]) > 1 for r in records),
                        "issue_counts": dict(Counter(i["reason"] for i in issues))}}


def sync_consultants(flow: Any, rows: list[dict], *, apply: bool = False) -> dict:
    """Dry-run by default; commit one complete verified master or change nothing."""
    if getattr(flow, "source_name", "") != "thanal/eic" or flow.is_test:
        raise ValueError("Consultant import requires the prepared Thanal main workflow")
    prepared = prepare_consultants(rows)
    result = {**prepared["summary"], "issues": list(prepared["issues"]),
              "mode": "VALIDATION ONLY", "messages_sent": 0, "consultants_written": 0}
    with flow.lock, flow.inbound_lock, flow.sessions.begin() as session:
        source = session.get(FlowSetting, "main_workflow_source")
        if source is None or source.value != "thanal/eic":
            raise ValueError("Database source identity does not match Thanal")
        marker = session.get(FlowSetting, MAP_KEY)
        mapping = json.loads(marker.value) if marker else {"branches": {}, "therapies": {}}
        doctors = {t.therapist_code: t for t in session.scalars(select(Therapist))}
        for row in prepared["records"]:
            doctor = doctors.get(row["code"])
            if doctor is None and any(t.name.casefold() == row["name"].casefold() for t in doctors.values()):
                result["issues"].append({"source_id": row["id"], "reason": "existing_consultant_identity_needs_review"})
            if doctor:
                branch = mapping["branches"].get(row["branch"]["id"])
                capability_ids = {mapping["therapies"].get(t["id"], {}).get("local_id") for t in row["therapies"]}
                has_history = session.scalar(select(Appointment.id).where(Appointment.therapist_id == doctor.id).limit(1))
                has_plan = session.scalar(select(TreatmentRequirement.id).where(
                    TreatmentRequirement.assigned_therapist_id == doctor.id,
                    TreatmentRequirement.active.is_(True)).limit(1))
                existing_caps = set(session.scalars(select(TherapistCapability.department_id).where(
                    TherapistCapability.therapist_id == doctor.id))) | {doctor.department_id}
                if (has_history or has_plan) and (not branch or branch["local_id"] != doctor.location_id
                                                  or not existing_caps <= capability_ids):
                    result["issues"].append({"source_id": row["id"], "reason": "booked_consultant_structure_needs_review"})
        result["ready"] = not result["issues"]
        if not apply:
            return result
        if not result["ready"]:
            raise ValueError("Consultant validation has unresolved issues; no records were imported")

        def bind(kind: str, source_id: str, label: str) -> int:
            model = Location if kind == "branches" else Department
            binding = mapping[kind].get(source_id)
            local = session.get(model, binding["local_id"]) if binding else None
            if binding and local is None:
                raise ValueError("Stored source binding points to a missing record; no import committed")
            if local is None:
                local = session.scalar(select(model).where(model.name == label))
                if local and any(b["local_id"] == local.id for sid, b in mapping[kind].items() if sid != source_id):
                    raise ValueError("Source identities would merge; no import committed")
                if local is None:
                    active_count = sum(r["active"] and r["branch"]["id"] == source_id
                                       for r in prepared["records"])
                    local = model(name=label, active=True,
                                  **({"capacity": max(1, active_count)} if kind == "branches" else {}))
                    session.add(local)
                    session.flush()
            # A changed display label never creates a new identity or relabels history.
            mapping[kind][source_id] = {"local_id": local.id, "source_label": label}
            return local.id

        for row in prepared["records"]:
            branch_id = bind("branches", row["branch"]["id"], row["branch"]["label"])
            capabilities = {bind("therapies", t["id"], t["label"]) for t in row["therapies"]}
            doctor = doctors.get(row["code"])
            if doctor is None:
                doctor = Therapist(therapist_code=row["code"], name=row["name"], department_id=min(capabilities),
                                   location_id=branch_id, active=row["active"])
                session.add(doctor)
                session.flush()
                doctors[row["code"]] = doctor
            # Zoho's status seeds a NEW doctor only; staff own Active/Inactive afterwards
            # (Clinical team page). Existing doctors keep their status.
            doctor.name, doctor.location_id = row["name"], branch_id
            if doctor.department_id not in capabilities:
                doctor.department_id = min(capabilities)
            existing = {cap.department_id: cap for cap in session.scalars(select(TherapistCapability).where(
                TherapistCapability.therapist_id == doctor.id))}
            for department_id in capabilities - existing.keys():
                session.add(TherapistCapability(therapist_id=doctor.id, department_id=department_id))
            for department_id in existing.keys() - capabilities:
                session.delete(existing[department_id])
            assigned = list(session.scalars(select(TherapistLocation).where(TherapistLocation.therapist_id == doctor.id)))
            for assignment in assigned:
                if assignment.location_id != branch_id:
                    session.delete(assignment)
            if not any(a.location_id == branch_id for a in assigned):
                session.add(TherapistLocation(therapist_id=doctor.id, location_id=branch_id))
        if marker is None:
            session.add(FlowSetting(key=MAP_KEY, value=json.dumps(mapping)))
        else:
            marker.value = json.dumps(mapping)
        flow._audit(session, "thanal_consultants_imported", f"consultants:{len(prepared['records'])}")
        result.update(mode="APPLIED", consultants_written=len(prepared["records"]))
    return result

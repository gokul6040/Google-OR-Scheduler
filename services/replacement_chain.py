"""Reviewable pull-forward chains for unoffered proposed visits in one week."""

from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

from sqlalchemy import select, text

from database.models import (AdminBookingDecision, AdminSlotReservation, Appointment,
                             AppointmentStatus, AuditLog, Patient)
from database.repository import load_scheduling_problem
from flow_test.models import FlowOffer
from scheduling.rules import placement_rejection_reason
from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import SLOTS, slot_label
from scheduling.types import BundleItem, TherapyBundle
from services.admin_reservation_service import RELEASED_STATUSES, is_reserved_source
from services.rolling_schedule_service import monday


def _digest(value):
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def preview_chain(session, flow, source_id):
    """Build an unsaved nearest-first chain, moving proposed visits only."""
    source = session.get(Appointment, source_id)
    if source is None or source.status not in RELEASED_STATUSES:
        raise ValueError("Choose a released appointment to review a replacement chain")
    if is_reserved_source(session, source.id):
        raise ValueError("This opening is already reserved. Refresh the schedule.")
    start = monday(source.date)
    end = start + timedelta(days=5)
    if source.date > end:
        raise ValueError("Choose a Monday–Saturday opening")
    problem = load_scheduling_problem(session, start, horizon_days=6, planning_start_date=source.date)
    patients = {p.id: p for p in session.scalars(select(Patient))}
    requirements = {r.id: r for r in problem.requirements}
    rows = list(session.scalars(select(Appointment).where(
        Appointment.date >= source.date, Appointment.date <= end,
        Appointment.status.in_((AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION,
                                AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED)),
    ).order_by(Appointment.date, Appointment.slot_index, Appointment.id)))
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.patient_id, row.date, row.bundle_id or f"PIN-{row.id}")].append(row)
    offered_ids = {int(item) for raw in session.scalars(select(FlowOffer.appointment_ids))
                   for item in json.loads(raw)}
    reserved_ids = set(session.scalars(select(AdminSlotReservation.appointment_id)))
    candidates = [sorted(group, key=lambda item: item.slot_index) for group in grouped.values()
                  if all(item.status == AppointmentStatus.PROPOSED and item.id not in offered_ids
                         and item.id not in reserved_ids for item in group)]
    current = list(problem.pinned)
    openings = [(source.date, source.slot_index, source.therapist_id, source.department_id, source.location_id)]
    moves, moved_ids = [], set()
    while openings:
        openings.sort()
        target_date, target_slot, therapist_id, department_id, location_id = openings.pop(0)
        for group in sorted(candidates, key=lambda items: (items[0].date, items[0].slot_index, items[0].id)):
            anchor = group[0]
            if (anchor.id in moved_ids or anchor.date <= target_date or not patients[anchor.patient_id].active
                    or (anchor.therapist_id, anchor.department_id, anchor.location_id)
                       != (therapist_id, department_id, location_id)):
                continue
            if any(item.treatment_requirement_id not in requirements for item in group):
                continue
            old = [item for item in current if item.patient_id == anchor.patient_id and item.date == anchor.date
                   and item.bundle_id == (anchor.bundle_id or f"PIN-{anchor.id}")]
            if len(old) != len(group):
                continue
            if any(item.patient_id == anchor.patient_id and item.date == target_date for item in current):
                continue
            items = []
            for row in group:
                requirement = requirements[row.treatment_requirement_id]
                if (row.patient_id != requirement.patient_id or row.therapist_id != requirement.therapist_id
                        or row.department_id != requirement.department_id or row.location_id != requirement.location_id):
                    break
                items.append(BundleItem(requirement.id, requirement.department_id, requirement.therapist_id,
                                        requirement.location_id, requirement.plan_start_date, requirement.plan_end_date))
            if len(items) != len(group):
                continue
            bundle = TherapyBundle(anchor.bundle_id or f"PIN-{anchor.id}", anchor.patient_id, 0, tuple(items))
            if placement_rejection_reason(problem, bundle, (target_date - start).days, target_slot):
                continue
            moved = [replace(item, date=target_date, slot_index=target_slot + row.slot_index - anchor.slot_index)
                     for item, row in zip(sorted(old, key=lambda item: item.slot_index), group)]
            trial = [item for item in current if item not in old] + moved
            issues, _ = validate_schedule(problem, trial)
            if issues:
                continue
            moves.append({"appointmentIds": [item.id for item in group], "patientCode": patients[anchor.patient_id].patient_code,
                "patientName": patients[anchor.patient_id].name or patients[anchor.patient_id].patient_code,
                "fromDate": anchor.date.isoformat(), "fromSlot": anchor.slot_index,
                "fromTime": slot_label(anchor.slot_index), "toDate": target_date.isoformat(),
                "toSlot": target_slot, "toTime": slot_label(target_slot), "sessions": len(group)})
            moved_ids.update(item.id for item in group)
            openings.extend((item.date, item.slot_index, item.therapist_id, item.department_id, item.location_id)
                            for item in group)
            current = trial
            break
    fingerprint = _digest({"source": (source.id, source.date, source.slot_index, source.status.value),
                           "problem": flow._planning_fingerprint(session, problem), "moves": moves,
                           "reserved": sorted(reserved_ids), "offered": sorted(offered_ids)})
    return {"sourceId": source_id, "moves": moves, "previewToken": fingerprint,
            "searchEnd": end.isoformat(), "message": "Review the full chain before saving. Confirmed and offered visits are not moved."}


def save_chain(session, flow, source_id, preview_token, request_id, admin_user):
    """Recheck and commit the reviewed chain together, with idempotent retries."""
    if not isinstance(request_id, str) or not request_id or len(request_id) > 120:
        raise ValueError("A request ID is required")
    previous = session.get(AdminBookingDecision, request_id)
    if previous:
        if previous.fingerprint != preview_token:
            raise ValueError("This request ID belongs to another review")
        return {"message": "Replacement chain already saved", "moved": len(json.loads(previous.appointment_ids))}
    if session.bind.dialect.name == "postgresql":
        # Serialize writes to the schedule/constraints while rechecking and saving.
        names = ("admin_booking_decisions", "admin_slot_reservations", "appointments", "flow_test_offers",
                 "locations", "patient_absences", "patient_availability", "patients", "therapist_absences",
                 "therapist_availability", "therapists", "treatment_requirements")
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("LOCK TABLE " + ", ".join('"' + name + '"' for name in names) + " IN SHARE ROW EXCLUSIVE MODE"))
        previous = session.get(AdminBookingDecision, request_id)
        if previous:
            if previous.fingerprint != preview_token:
                raise ValueError("This request ID belongs to another review")
            return {"message": "Replacement chain already saved", "moved": len(json.loads(previous.appointment_ids))}
    reviewed = preview_chain(session, flow, source_id)
    if reviewed["previewToken"] != preview_token:
        raise ValueError("The schedule changed. Review the replacement chain again.")
    if not reviewed["moves"]:
        raise ValueError("There are no eligible proposed visits to move")
    decision = AdminBookingDecision(request_id=request_id, fingerprint=preview_token,
        appointment_ids=json.dumps([move["appointmentIds"][0] for move in reviewed["moves"]]),
        warnings="[]", admin_user=admin_user)
    session.add(decision)
    session.flush()
    first = reviewed["moves"][0]
    session.add(AdminSlotReservation(source_appointment_id=source_id, appointment_id=first["appointmentIds"][0],
                                     decision_id=request_id, counts_toward_requirement=True))
    from datetime import date
    for move in reviewed["moves"]:
        for offset, appointment_id in enumerate(move["appointmentIds"]):
            item = session.get(Appointment, appointment_id)
            item.date = date.fromisoformat(move["toDate"])
            item.slot_index = move["toSlot"] + offset
            item.source = "manual"
    session.add(AuditLog(action="replacement_chain_approved", entity=f"appointment:{source_id}",
        new_value=json.dumps(reviewed["moves"]), user=admin_user))
    session.flush()
    return {"message": "Replacement chain saved", "moved": len(reviewed["moves"])}

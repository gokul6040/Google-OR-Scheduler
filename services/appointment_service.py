from __future__ import annotations

from dataclasses import replace
import json

from sqlalchemy import select

from database.models import Appointment, AppointmentStatus, AuditLog
from scheduling.schedule_validator import validate_schedule
from scheduling.types import ScheduledAppointment
from services.patient_status_service import lock_patient


def move_appointment(session, problem, existing, appointment: Appointment, new_date, new_slot):
    """Move one appointment or its entire consecutive bundle as a manual change."""
    patient = lock_patient(session, appointment.patient_id)
    if patient is None or not patient.active:
        return False, ["Reactivate the patient before moving an appointment to a new slot."]
    group = [appointment]
    if appointment.bundle_id:
        group = list(session.scalars(select(Appointment).where(
            Appointment.bundle_id == appointment.bundle_id,
            Appointment.patient_id == appointment.patient_id,
            Appointment.date == appointment.date,
            Appointment.status.in_([
                AppointmentStatus.PROPOSED,
                AppointmentStatus.PENDING_CONFIRMATION,
                AppointmentStatus.CONFIRMED,
            ]),
        ).order_by(Appointment.slot_index)))
    current_items = [
        ScheduledAppointment(
            item.patient_id,
            item.treatment_requirement_id,
            item.therapist_id,
            item.department_id,
            item.location_id,
            item.date,
            item.slot_index,
            item.bundle_id or f"A-{item.id}",
            item.status.value,
        )
        for item in group
    ]
    # Sessions are identities, not positions: move the visit onto the centre's next
    # back-to-back sessions from the chosen start, in clock order.
    from scheduling.slot_config import timetable_for
    from services.shift_service import clock_key

    order = sorted(range(len(group)), key=lambda index: clock_key(group[index].slot_index))
    group = [group[index] for index in order]
    current_items = [current_items[index] for index in order]
    original_start = group[0].slot_index
    # A start time outside the first doctor's shift gets a plain reason (the move is refused either way).
    from database.models import Therapist
    from scheduling.slot_config import known_slot, slot_label
    from services.shift_service import load_shift_context

    doctor = session.get(Therapist, group[0].therapist_id)
    if doctor is not None and known_slot(new_slot) and new_slot not in load_shift_context(session).therapist_sessions(
            doctor.id, doctor.location_id):
        return False, [f"{slot_label(new_slot)} isn't a session time in this doctor's shift. "
                       "Choose one of their shift times."]
    targets = timetable_for(problem, appointment.location_id).block(new_slot, len(group))
    if targets is None:
        return False, ["This visit's therapies do not fit back-to-back from that starting time."]
    replacements = [
        replace(current, date=new_date, slot_index=slot)
        for current, slot in zip(current_items, targets)
    ]
    candidate = [item for item in existing if item not in current_items] + replacements
    issues, _ = validate_schedule(problem, candidate)
    if issues:
        return False, sorted({issue.message for issue in issues})
    old = {
        "date": str(appointment.date),
        "start_slot": original_start,
        "appointments": len(group),
    }
    for item, replacement in zip(group, replacements):
        item.date = replacement.date
        item.slot_index = replacement.slot_index
        item.source = "manual"
    session.add(AuditLog(
        action="appointment_bundle_moved",
        entity=f"appointment:{appointment.id}",
        old_value=json.dumps(old),
        new_value=json.dumps({
            "date": str(new_date), "start_slot": new_slot, "appointments": len(group),
        }),
    ))
    session.commit()
    return True, []

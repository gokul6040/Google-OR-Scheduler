"""Course progress ("6/16 sessions") and the repeated-cancellation warning.

User decisions (12 Nov 2026):
- A course needs sessions/week x months x 4 sessions; it ends at that total,
  not on a calendar date.
- A saved session counts until it is cancelled, declined or marked no-show.
- Everyone starts at 0 when the app begins saving visits; a new Zoho plan
  record is a new course and starts again at 0.
- Four cancelled sessions in a row raise a warning for staff.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import (
    Appointment, AppointmentStatus, Department, PatientAbsence, TreatmentRequirement,
)
from database.repository import ACTIVE_SCHEDULE_STATUSES, course_session_counts, course_target

CANCELLATION_STREAK_WARNING = 4

# Statuses that always mean the child's session did not go ahead for them.
MISSED_STATUSES = {AppointmentStatus.CANCELLED_PATIENT, AppointmentStatus.NO_SHOW}
# Released sessions count as missed only when the child was recorded as unavailable
# (a parent "No" or a patient absence). A doctor's absence or system housekeeping
# (a replaced proposal, a patient made inactive) is not the family cancelling.
RELEASED_STATUSES = {AppointmentStatus.NEEDS_RESCHEDULING, AppointmentStatus.CANCELLED_OTHER}


def course_progress(session: Session, patient_ids: Iterable[int] | None = None) -> dict[int, list[dict]]:
    """Return each active therapy's course progress, keyed by patient, in plan order."""
    query = select(TreatmentRequirement, Department.name).join(
        Department, Department.id == TreatmentRequirement.department_id,
    ).where(TreatmentRequirement.active.is_(True))
    if patient_ids is not None:
        query = query.where(TreatmentRequirement.patient_id.in_(list(patient_ids)))
    requirements = list(session.execute(query.order_by(
        TreatmentRequirement.patient_id, TreatmentRequirement.sequence_order, TreatmentRequirement.id)))
    counts = course_session_counts(session, [item.id for item, _ in requirements])
    progress: dict[int, list[dict]] = defaultdict(list)
    for item, therapy in requirements:
        target = course_target(item.sessions_per_week, item.duration_months)
        sessions = counts[item.id]
        progress[item.patient_id].append({
            "requirementId": item.id,
            "therapy": therapy,
            "sessions": sessions,
            "target": target,
            "complete": target is not None and sessions >= target,
        })
    return dict(progress)


def cancellation_streaks(session: Session, today: date,
                         patient_ids: Iterable[int] | None = None) -> dict[int, int]:
    """Count each child's most recent cancelled sessions in a row.

    Sessions are read in time order. A cancelled, declined or no-show session adds
    one; a session that has already happened (today or earlier) and was not
    cancelled resets the count. Upcoming saved sessions do not reset it, so the
    warning stays until the child actually attends again.
    """
    query = select(Appointment.patient_id, Appointment.date, Appointment.slot_index, Appointment.status)
    absence_query = select(PatientAbsence)
    if patient_ids is not None:
        ids = list(patient_ids)
        query = query.where(Appointment.patient_id.in_(ids))
        absence_query = absence_query.where(PatientAbsence.patient_id.in_(ids))
    absences = defaultdict(list)
    for item in session.scalars(absence_query):
        absences[(item.patient_id, item.date)].append(item)

    def child_unavailable(patient_id: int, day: date, slot: int) -> bool:
        for item in absences.get((patient_id, day), ()):
            start = item.start_slot or 0
            end = item.end_slot if item.end_slot is not None else start
            if item.full_day or start <= slot <= end:
                return True
        return False

    streaks: dict[int, int] = defaultdict(int)
    rows = session.execute(query.order_by(
        Appointment.patient_id, Appointment.date, Appointment.slot_index, Appointment.id))
    for patient_id, day, slot, status in rows:
        if status in MISSED_STATUSES or (
                status in RELEASED_STATUSES and child_unavailable(patient_id, day, slot)):
            streaks[patient_id] += 1
        elif status in ACTIVE_SCHEDULE_STATUSES and day <= today:
            streaks[patient_id] = 0
    return {patient_id: count for patient_id, count in streaks.items() if count}

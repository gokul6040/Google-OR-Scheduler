"""Staff-controlled clinician scheduling status and its audit history.

Our app owns each existing clinician's Active/Inactive status. Source syncs
(Zoho) set it only when a clinician is first imported.
"""

import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Appointment, AppointmentStatus, AuditLog, Location, Therapist

UPCOMING_STATUSES = (AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION,
                     AppointmentStatus.CONFIRMED)


def lock_therapist(session: Session, therapist_id: int) -> Therapist | None:
    return session.scalar(select(Therapist).where(Therapist.id == therapist_id).with_for_update())


def upcoming_bookings(session: Session, therapist_id: int, today: date) -> int:
    """Saved future visits that stay on the clinician's calendar for staff review."""
    return session.scalar(select(func.count(Appointment.id)).where(
        Appointment.therapist_id == therapist_id, Appointment.date >= today,
        Appointment.status.in_(UPCOMING_STATUSES))) or 0


def change_therapist_status(
    session: Session, therapist: Therapist, active: bool, reason: str, admin_user: str,
) -> None:
    """Apply a status change in the caller's transaction.

    Inactive clinicians get no new generated bookings; saved bookings are kept
    for staff review (existing rule). Activating never leaves the centre's
    stored ceiling below its active clinician count (rule S-03); the ceiling is
    never lowered, so kept bookings cannot make a centre unplannable.
    """
    if therapist.active == active:
        return
    session.add(AuditLog(
        action="therapist_status_changed", entity=f"therapist:{therapist.id}", user=admin_user,
        old_value=json.dumps({"active": therapist.active}),
        new_value=json.dumps({"active": active, "reason": reason if not active else None}),
    ))
    therapist.active = active
    if active:
        session.flush()
        location = session.get(Location, therapist.location_id)
        if location is not None:
            count = session.scalar(select(func.count(Therapist.id)).where(
                Therapist.location_id == location.id, Therapist.active.is_(True))) or 0
            location.capacity = max(location.capacity or 0, count, 1)

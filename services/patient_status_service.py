"""Store patient scheduling status and its audit history."""

import json
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from database.models import Appointment, AppointmentStatus, AuditLog, Patient


def change_patient_status(
    session: Session, patient: Patient, active: bool, reason: str, today: date, admin_user: str,
) -> None:
    """Apply a status transition in the caller's transaction."""
    if patient.active == active:
        return
    session.add(AuditLog(
        action="patient_status_changed", entity=f"patient:{patient.id}", user=admin_user,
        old_value=json.dumps({"active": patient.active}),
        new_value=json.dumps({"active": active, "reason": reason if not active else None}),
    ))
    patient.active = active
    if not active:
        session.execute(update(Appointment).where(
            Appointment.patient_id == patient.id,
            Appointment.date >= today,
            Appointment.status == AppointmentStatus.PROPOSED,
        ).values(status=AppointmentStatus.CANCELLED_OTHER))


def lock_patient(session: Session, patient_id: int) -> Patient | None:
    return session.scalar(select(Patient).where(Patient.id == patient_id).with_for_update())

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Appointment, AppointmentStatus, AuditLog


CANCELLABLE = {AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION,
               AppointmentStatus.CONFIRMED}


def cancel_saved_visit(session: Session, appointment_id: int, *, scope: str, reason: str,
                      actor: str, flow: Any = None) -> dict:
    """Cancel a session/visit and its obsolete offers in the caller's transaction."""
    if not isinstance(scope, str) or scope not in {"session", "visit"}:
        raise ValueError("Choose this session or the entire visit")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
        raise ValueError("Enter a cancellation reason (up to 1000 characters)")
    appointment = session.get(Appointment, appointment_id)
    if appointment is None:
        raise ValueError("Appointment not found")
    # Use the same patient lock as manual reservations. The workflow route also
    # takes the sender mutex before the shared workflow lock.
    from services.patient_status_service import lock_patient
    lock_patient(session, appointment.patient_id)
    query = select(Appointment).where(Appointment.id == appointment_id)
    if scope == "visit" and appointment.bundle_id:
        query = select(Appointment).where(
            Appointment.patient_id == appointment.patient_id,
            Appointment.date == appointment.date,
            Appointment.bundle_id == appointment.bundle_id,
        )
    items = list(session.scalars(query.order_by(Appointment.id).with_for_update()
                               .execution_options(populate_existing=True)))
    if any(item.status not in CANCELLABLE | {AppointmentStatus.CANCELLED_PATIENT} for item in items):
        raise ValueError("Only proposed, pending or confirmed sessions can be cancelled. Completed therapy is unchanged.")
    changed = [item for item in items if item.status in CANCELLABLE]
    for item in changed:
        old = item.status.value
        item.status = AppointmentStatus.CANCELLED_PATIENT
        session.add(AuditLog(action="staff_appointment_cancelled", entity=f"appointment:{item.id}",
                             user=actor, old_value=old, new_value=json.dumps({
                                 "status": item.status.value, "reason": reason.strip(), "scope": scope})))
    if changed and flow is not None:
        from flow_test.models import FlowJob, FlowOffer
        changed_ids = {item.id for item in changed}
        from database.models import Patient
        patient = session.get(Patient, appointment.patient_id)
        tokens = set()
        for offer in session.scalars(select(FlowOffer).where(
                FlowOffer.patient_code == patient.patient_code,
                FlowOffer.state.in_(("PENDING", "CONFIRMED", "EXPIRED")))):
            ids = set(json.loads(offer.appointment_ids))
            if not ids.intersection(changed_ids):
                continue
            offer.state, offer.expires_at = "CLOSED", None
            tokens.add(offer.token)
            remaining = session.scalar(select(Appointment.id).where(
                Appointment.id.in_(ids - changed_ids), Appointment.status == AppointmentStatus.PENDING_CONFIRMATION))
            if remaining:
                flow._job_warning(session, f"staff-cancel:{offer.token}", "Visit changed — review remaining sessions",
                                  "The old offer was closed after staff cancelled part of the visit. Confirm or re-offer the remaining sessions with the family.", patient.patient_code)
        jobs = session.scalars(select(FlowJob).where(
            FlowJob.kind.in_(("OFFER", "TEXT")),
            FlowJob.status.notin_(("SENT", "SIMULATED", "DONE", "OBSOLETE")))) if tokens else ()
        for job in jobs:
            if json.loads(job.payload).get("token") in tokens:
                job.status, job.error = "OBSOLETE", "Staff cancelled a session covered by this offer."
                job.next_attempt_at = job.locked_until = job.locked_by = None
    session.flush()
    return {"appointments": items, "cancelledIds": [item.id for item in changed],
            "alreadyCancelled": not changed, "scope": scope}


def cancel_patient_appointment(session, appointment):
    old = appointment.status.value
    appointment.status = AppointmentStatus.CANCELLED_PATIENT
    session.add(AuditLog(
        action="appointment_cancelled", entity=f"appointment:{appointment.id}",
        old_value=old, new_value=AppointmentStatus.CANCELLED_PATIENT.value,
    ))
    session.commit()

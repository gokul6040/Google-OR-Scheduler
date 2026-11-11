"""Opt-in four-variable therapy template formatting and durable reminders.

These helpers only format or queue jobs. They never call WhatsApp themselves.
"""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import AppointmentStatus, Patient
from flow_test.models import FlowJob, FlowOffer, FlowSetting


PROFILE = "therapy_schedule_v1"
IST = ZoneInfo("Asia/Kolkata")
REMINDER_CONTEXT = "*You haven't replied yet. Kindly reply Yes or No to confirm your availability for tomorrow.*"
PARAMETERS = ("{centre}", "{session_details}", "{therapy_count}", "{message_context}")


def clock_label(value: time) -> str:
    """Format real slot times without changing their duration."""
    return value.strftime("%I:%M %p").lstrip("0").lower()


def template_fields(sessions: Iterable[tuple[str, time, time]]) -> dict[str, str]:
    """Build numbered parameters 2, 3 and the initial parameter 4."""
    rows = list(sessions)
    if not rows:
        raise ValueError("The therapy template requires at least one scheduled session")
    return {
        "session_details": " ".join(
            f"🟥{' '.join(therapy.split())}: {clock_label(start)} to {clock_label(end)}"
            for therapy, start, end in rows
        ),
        "therapy_count": "1 therapy" if len(rows) == 1 else f"{len(rows)} therapies",
        "message_context": ".",
    }


def uses_daily_template(flow: Any) -> bool:
    transport = getattr(flow, "transport", None)
    return (transport is not None and getattr(transport, "mode", None) == "template"
            and getattr(transport, "profile", "legacy") == PROFILE)


def scheduled_template_enabled(flow: Any) -> bool:
    return uses_daily_template(flow) and getattr(flow.transport, "timed_schedule", False)


def schedule_initial_job(flow: Any, job: FlowJob, payload: dict, target: date) -> None:
    """Hold saved tomorrow offers until 09:00 on the preceding IST day."""
    payload["template_profile"] = PROFILE
    if scheduled_template_enabled(flow):
        send_at = datetime.combine(target - timedelta(days=1), time(9), tzinfo=IST)
        payload.update(template_timed=True, not_before=send_at.timestamp())
        if datetime.now(IST) < send_at:
            job.status = "RETRY_WAIT"
            job.next_attempt_at = send_at.astimezone(timezone.utc).replace(tzinfo=None)
    job.payload = json.dumps(payload, ensure_ascii=False)


def pending_inbound(session: Session, recipient: str) -> bool:
    """Let a received reply finish processing before dispatching a reminder."""
    return any(json.loads(job.payload).get("from") == recipient for job in session.scalars(
        select(FlowJob).where(FlowJob.kind == "INBOUND", FlowJob.status.in_(("QUEUED", "PROCESSING")))
    ))


def source_contacts(session: Session) -> dict[str, str] | None:
    source = session.get(FlowSetting, "zoho_test_scenario")
    if source is None:
        return None
    return {patient["code"]: patient.get("phone", "")
            for patient in json.loads(source.value).get("patients", [])}


def current_contact_matches(session: Session, offer: FlowOffer) -> bool:
    contacts = source_contacts(session)
    return contacts is None or contacts.get(offer.patient_code) == offer.recipient


REMINDER_GAP = timedelta(hours=1)
REMINDER_TIMES = (time(14), time(16))


def reminder_due(accepted_utc: datetime, day: date) -> datetime | None:
    """When an unanswered offer's single reminder is due (IST), on the day before the visit.

    The parent always has at least one hour with the first offer. Offers sent before 13:00
    are reminded at 14:00; offers sent 13:00-15:00 at 16:00; later offers one hour after
    they were sent. ``None`` means the hour ends on the visit day, so no reminder is sent.
    """
    earliest = accepted_utc.replace(tzinfo=timezone.utc).astimezone(IST) + REMINDER_GAP
    for slot in REMINDER_TIMES:
        candidate = datetime.combine(day, slot, tzinfo=IST)
        if candidate >= earliest:
            return candidate
    return earliest if earliest.date() == day else None


def queue_daily_reminders(flow: Any) -> int:
    """Queue one reminder per sent, unanswered, unexpired offer (14:00, 16:00 or sent + 1 hour)."""
    if (not scheduled_template_enabled(flow) or not flow.live
            or not getattr(flow, "messaging_enabled", True) or getattr(flow, "remote_worker", False)):
        return 0
    now = datetime.now(IST)
    target = now.date() + timedelta(days=1)
    # No reminder is ever due before 14:00 on the day before the visit.
    if now < datetime.combine(now.date(), REMINDER_TIMES[0], tzinfo=IST) or target.weekday() == 6:
        return 0
    utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    count = 0
    with flow.lock, flow.sessions.begin() as session:
        contacts = source_contacts(session)
        unsubscribed = flow.unsubscribed_numbers(session) if hasattr(flow, "unsubscribed_numbers") else {}
        waiting_replies = {json.loads(job.payload).get("from") for job in session.scalars(
            select(FlowJob).where(FlowJob.kind == "INBOUND", FlowJob.status.in_(("QUEUED", "PROCESSING")))
        )}
        for offer in session.scalars(select(FlowOffer).where(
            FlowOffer.state == "PENDING", FlowOffer.expires_at > utc_now,
        )):
            original_key = hashlib.sha256(f"offer:{offer.token}".encode()).hexdigest()
            original = session.scalar(select(FlowJob).where(FlowJob.event_key == original_key))
            if (original is None or original.status != "SENT" or not original.message_id
                    or original.delivery == "failed" or original.accepted_at is None):
                continue
            reminder_at = reminder_due(original.accepted_at, now.date())
            if reminder_at is None or now < reminder_at:
                continue  # Not due yet (checked again on a later tick), or too late.
            payload = json.loads(original.payload)
            if (payload.get("template_profile") != PROFILE or payload.get("reminder")
                    or payload.get("fields", {}).get("date") != target.isoformat()
                    or payload.get("recipient") != offer.recipient
                    or (contacts is not None and contacts.get(offer.patient_code) != offer.recipient)
                    or offer.recipient in waiting_replies
                    or offer.recipient in unsubscribed
                    or not session.scalar(select(Patient.active).where(Patient.patient_code == offer.patient_code))):
                continue
            try:
                items = flow._offer_items(session, offer)
            except ValueError:
                continue
            if not items or any(item.date != target or item.status != AppointmentStatus.PENDING_CONFIRMATION
                                for item in items):
                continue
            event = f"reminder:{offer.token}:{target.isoformat()}"
            key = hashlib.sha256(event.encode()).hexdigest()
            if session.scalar(select(FlowJob.id).where(FlowJob.event_key == key)) is not None:
                continue
            payload.update(reminder=True, template_timed=True, not_before=reminder_at.timestamp())
            payload["fields"]["message_context"] = REMINDER_CONTEXT
            job = flow._job(session, event, "OFFER", payload)
            job.deadline_at = offer.expires_at
            flow._audit(session, "flow_template_reminder_queued", f"offer:{offer.token}")
            count += 1
    return count


def prepare_template_job(flow: Any, session: Session, job: FlowJob, payload: dict) -> bool:
    """Recheck timing and response state immediately before a network send."""
    from services.whatsapp_service import WhatsAppConfigurationError

    if payload.get("template_profile") != PROFILE:
        return True
    if not uses_daily_template(flow):
        raise WhatsAppConfigurationError("This offer requires the therapy_schedule_v1 template profile")
    if payload.get("template_timed") and not scheduled_template_enabled(flow):
        job.status, job.error = "WAITING_FOR_ENABLE", "The timed therapy-template workflow is disabled"
        return False
    now = datetime.now(IST)
    try:
        target = date.fromisoformat(payload.get("fields", {}).get("date", ""))
    except ValueError:
        flow._terminal(session, job, "The offer has an invalid appointment date. Review the booking.")
        return False
    if target.weekday() == 6 or target < now.date():
        flow._terminal(session, job, "The offer is for a past date or Sunday. Review the booking.")
        return False
    if target == now.date() and payload.get("reminder"):
        # A delayed reminder says "confirm for tomorrow"; on the visit day that wording
        # is wrong. Close it instead of scheduling a retry for yesterday.
        job.status = "OBSOLETE"
        job.error = ("Reminder not sent: it was delayed until the visit day, so its "
                     "'tomorrow' wording no longer fits. The original offer stands.")
        job.next_attempt_at = None
        return False
    if target == now.date() and not payload.get("reminder"):
        # The approved daily template says tomorrow. Use dated session buttons
        # only inside a verified contact window; never mislabel today's visit.
        deadline = flow._session_deadline(session, payload["recipient"])
        if deadline <= now.timestamp():
            job.status, job.error = "WAITING_FOR_CONTACT", "Same-day offer needs a parent message to open the contact window."
            flow._attention(session, job, "Same-day replacement needs contact", job.error)
            return False
        payload.update(session_override=True, session_expires_at=deadline)
        return True
    if target != now.date() + timedelta(days=1):
        due = datetime.combine(target - timedelta(days=1), time(9), tzinfo=IST)
        if due <= now:
            # Never schedule a retry in the past; that would loop until a limit stops it.
            flow._terminal(session, job, "The offer missed its sending time. Review the booking.")
            return False
        job.status, job.error = "RETRY_WAIT", "Offer retained until its valid sending day."
        job.next_attempt_at = due.astimezone(timezone.utc).replace(tzinfo=None)
        return False
    if now.timestamp() < payload.get("not_before", 0):
        job.status = "RETRY_WAIT"
        job.next_attempt_at = datetime.fromtimestamp(payload["not_before"], timezone.utc).replace(tzinfo=None)
        job.attempts = max(0, job.attempts - 1)
        return False
    if payload.get("reminder"):
        offer = session.get(FlowOffer, payload["token"])
        utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
        if (offer is None or offer.state != "PENDING" or offer.expires_at is None
                or offer.expires_at <= utc_now or not current_contact_matches(session, offer)):
            job.status, job.error = "OBSOLETE", "The reminder is no longer applicable"
            return False
        if pending_inbound(session, offer.recipient):
            job.status = "RETRY_WAIT"
            job.next_attempt_at = utc_now + timedelta(seconds=30)
            job.attempts = max(0, job.attempts - 1)
            return False
        job.deadline_at = min(job.deadline_at, offer.expires_at) if job.deadline_at else offer.expires_at
    return True

"""Durable outbound recovery shared by main operations and isolated previews."""

from datetime import datetime, timedelta
import json
import math
import os
import random
import secrets

from sqlalchemy import or_, select

from flow_test.models import FlowInbox, FlowJob, FlowOffer, FlowSetting
from services.whatsapp_service import WhatsAppConfigurationError, WhatsAppDeliveryError


OUTBOUND = ("OFFER", "TEXT")
PAUSE_KEY = "whatsapp_configuration_pause"


def setting_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class Reliability:
    """Methods run under the existing cross-process workflow mutex."""

    def configure_reliability(self) -> None:
        self.worker_id = secrets.token_hex(16)
        self.max_send_attempts = setting_int("WHATSAPP_MAX_SEND_ATTEMPTS", 5, 1, 20)
        self.retry_seconds = setting_int("WHATSAPP_RETRY_BASE_SECONDS", 30, 1, 3600)
        self.delivery_wait_minutes = setting_int("WHATSAPP_DELIVERY_ALERT_MINUTES", 15, 1, 1440)

    def _attention(self, session, job, reason: str, message: str, *, reopen: bool = True) -> None:
        """Create the staff item, or reopen it with fresh text when the problem recurs.

        ``reopen=False`` is for a condition re-checked every cycle (delivery overdue):
        once staff dismiss it, the same unchanged condition must not nag again.
        """
        key = f"attention:{job.event_key}:{reason}"
        item = session.scalar(select(FlowInbox).where(FlowInbox.event_key == key))
        if item is None:
            token = json.loads(job.payload).get("token")
            offer = session.get(FlowOffer, token) if token else None
            session.add(FlowInbox(event_key=key, patient_code=offer.patient_code if offer else None,
                                  reason=reason, text=message + " Booking unchanged."))
        elif reopen and item.resolved:
            item.resolved, item.text = 0, message + " Booking unchanged."

    def _terminal(self, session, job, message: str) -> None:
        job.status, job.error, job.next_attempt_at = "NEEDS_ATTENTION", message, None
        job.locked_by, job.locked_until = None, None
        self._attention(session, job, "WhatsApp needs attention", message)

    def _maintain_delivery(self, session) -> None:
        now = datetime.utcnow()
        for job in session.scalars(select(FlowJob).where(FlowJob.kind.in_(OUTBOUND),
                FlowJob.status.in_(("QUEUED", "RETRY_WAIT", "WAITING_FOR_CONTACT", "BLOCKED",
                                   "WAITING_FOR_SYNC", "WAITING_FOR_ENABLE")),
                or_(FlowJob.deadline_at <= now, (FlowJob.status == "RETRY_WAIT") &
                    (FlowJob.next_attempt_at <= now)))):
            if job.deadline_at and job.deadline_at <= now:
                self._terminal(session, job, "Sending deadline reached. Contact the parent by phone.")
            elif job.status == "RETRY_WAIT" and job.next_attempt_at and job.next_attempt_at <= now:
                job.status = "QUEUED"
        for job in session.scalars(select(FlowJob).where(FlowJob.status == "SENT",
                or_(FlowJob.delivery.is_(None), FlowJob.delivery == "sent"),
                FlowJob.accepted_at <= now - timedelta(minutes=self.delivery_wait_minutes))):
            if job.delivery not in {"delivered", "read", "failed"}:
                self._attention(session, job, "WhatsApp delivery overdue",
                    "Accepted by Meta; delivery remains unconfirmed. Follow up with the parent; do not blindly resend.",
                    reopen=False)
        for job in session.scalars(select(FlowJob).where(FlowJob.status == "UNKNOWN",
                ~select(FlowInbox.id).where(FlowInbox.event_key == "unknown:" + FlowJob.event_key).exists())):
            self._send_failure_inbox(session, job)

    def _mark_send_started(self, job_id: int) -> None:
        # Persist BEFORE touching the network. A crash in this gap is unknown,
        # which intentionally favors avoiding duplicates over automatic recovery.
        with self.sessions.begin() as session:
            job = session.get(FlowJob, job_id)
            job.send_started_at = datetime.utcnow()
            job.locked_until = datetime.utcnow() + timedelta(minutes=2)

    def _record_send_failure(self, session, job, exc: Exception) -> None:
        now = datetime.utcnow()
        job.locked_by, job.locked_until = None, None
        if isinstance(exc, WhatsAppDeliveryError):
            job.error_code = str(exc.code) if exc.code is not None else None
        if isinstance(exc, WhatsAppConfigurationError) or (
                isinstance(exc, WhatsAppDeliveryError) and exc.configuration_failure):
            job.send_started_at = None
            job.status = "BLOCKED"
            job.error = "Sending paused: fix WhatsApp credentials/template/configuration, then resume sending."
            paused = session.get(FlowSetting, PAUSE_KEY)
            if paused is None:
                session.add(FlowSetting(key=PAUSE_KEY, value=job.error))
            else:
                paused.value = job.error
            self._attention(session, job, "WhatsApp configuration needs fixing", job.error)
        elif isinstance(exc, WhatsAppDeliveryError) and exc.retryable:
            # An explicit rejection proves this attempt was not accepted.
            job.send_started_at = None
            delay = min(3600, self.retry_seconds * 2 ** min(job.attempts - 1, 12))
            retry_after = exc.retry_after if math.isfinite(exc.retry_after) else 86400
            delay = max(random.uniform(delay, delay * 1.25), retry_after)
            due = now + timedelta(seconds=min(delay, 86400 * 365))
            if job.attempts >= self.max_send_attempts:
                self._terminal(session, job, "Retry limit reached. Contact the parent by phone.")
            elif job.deadline_at and due >= job.deadline_at:
                self._terminal(session, job, "Next retry would miss the sending deadline. Contact the parent by phone.")
            else:
                job.status, job.next_attempt_at = "RETRY_WAIT", due
                job.error = f"Temporary Meta rejection ({job.error_code}). Attempt {job.attempts}/{self.max_send_attempts}; retry scheduled."
        elif isinstance(exc, WhatsAppDeliveryError) and exc.rejected:
            job.send_started_at = None
            self._terminal(session, job, f"Meta rejected this message (code {job.error_code}). Staff review required.")
        elif job.send_started_at is None:
            self._terminal(session, job, "Message preparation failed before sending. Staff review required.")
        else:
            job.status = "UNKNOWN"
            job.error = "Delivery unknown. Check delivery before any resend; staff review required."
            self._send_failure_inbox(session, job)

    def resume_sending(self) -> dict:
        """Staff explicitly acknowledge a configuration fix; unknown sends stay put."""
        sender = None
        if self.transport and self.transport.live:
            from services.whatsapp_service import sender_settings_from_environment
            sender = sender_settings_from_environment()
        with self.lock, self.sessions.begin() as session:
            if sender:
                self.transport.sender = sender
            paused = session.get(FlowSetting, PAUSE_KEY)
            if paused:
                session.delete(paused)
            self._maintain_delivery(session)
            count = 0
            for job in session.scalars(select(FlowJob).where(FlowJob.status == "BLOCKED")):
                job.status, job.error, job.send_started_at = "QUEUED", None, None
                count += 1
            self._audit(session, "whatsapp_configuration_resumed", "whatsapp")
        return {"requeued": count}

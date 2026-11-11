"""Scheduled clean-up of old operational rows (review item #27).

Management decision (8 November 2026): keep operational history for 180 days.

Removed after the retention period:
* WhatsApp queue jobs that finished (DONE, SENT, SIMULATED, OBSOLETE);
* offers whose visit day is older than the period, with their appointment links;
* staff-inbox items that were resolved;
* Zoho push tracking for therapy dates older than the period.

Removed whenever expired (purely technical): per-parent 6-second send timers and
expired schedule previews.

Never removed: appointments, therapy plans, patients, the audit log, unfinished or
failed jobs, unresolved inbox items, open export batches. Session counts and
carry-over use appointments and plans, so they are unaffected.

Deletes run in small batches, each in its own short transaction under the
workflow lock, so sending and replies are never held up for long.
"""

from datetime import datetime, timedelta
import os

from sqlalchemy import delete, select

from flow_test.models import FlowExportItem, FlowInbox, FlowJob, FlowOffer, FlowOfferAppointment, FlowSetting
from flow_test.preview_store import TTL as PREVIEW_TTL
from flow_test.models import FlowPreview

FINISHED_JOB_STATUSES = ("DONE", "SENT", "SIMULATED", "OBSOLETE")
PACING_PREFIX = "send_after:"
DEFAULT_RETENTION_DAYS = 180
BATCH = 2000


def retention_days() -> int:
    """``WORKFLOW_RETENTION_DAYS`` (30-3650), default 180."""
    value = int(os.getenv("WORKFLOW_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    if not 30 <= value <= 3650:
        raise ValueError("WORKFLOW_RETENTION_DAYS must be between 30 and 3650")
    return value


def _delete_in_batches(flow, id_query, delete_for) -> int:
    """Repeat select-ids-then-delete until nothing matches; one short transaction each."""
    total = 0
    while True:
        with flow.lock, flow.sessions.begin() as session:
            ids = list(session.scalars(id_query.limit(BATCH)))
            if not ids:
                return total
            for statement in delete_for(ids):
                session.execute(statement)
            total += len(ids)
        if len(ids) < BATCH:
            return total


def cleanup_operational_data(flow, *, now: datetime | None = None, days: int | None = None) -> dict:
    """Delete expired operational rows; returns how many of each were removed."""
    now = now or datetime.utcnow()
    days = retention_days() if days is None else days
    cutoff = now - timedelta(days=days)
    counts = {}

    counts["jobs"] = _delete_in_batches(
        flow,
        select(FlowJob.id).where(FlowJob.status.in_(FINISHED_JOB_STATUSES), FlowJob.created_at < cutoff),
        lambda ids: [delete(FlowJob).where(FlowJob.id.in_(ids))])

    old_offer = ((FlowOffer.visit_date < cutoff.date())
                 | (FlowOffer.visit_date.is_(None) & (FlowOffer.created_at < cutoff)))
    counts["offers"] = _delete_in_batches(
        flow,
        select(FlowOffer.token).where(old_offer),
        lambda tokens: [delete(FlowOfferAppointment).where(FlowOfferAppointment.token.in_(tokens)),
                        delete(FlowOffer).where(FlowOffer.token.in_(tokens))])

    counts["inbox"] = _delete_in_batches(
        flow,
        select(FlowInbox.id).where(FlowInbox.resolved == 1, FlowInbox.created_at < cutoff),
        lambda ids: [delete(FlowInbox).where(FlowInbox.id.in_(ids))])

    counts["exportTracking"] = _delete_in_batches(
        flow,
        select(FlowExportItem.appointment_id).where(FlowExportItem.therapy_date < cutoff.date()),
        lambda ids: [delete(FlowExportItem).where(FlowExportItem.appointment_id.in_(ids))])

    # Technical leftovers: expired send timers (value = epoch seconds) and previews.
    epoch = (now - datetime(1970, 1, 1)).total_seconds()
    expired_timers = []
    with flow.sessions() as session:
        for key, value in session.execute(select(FlowSetting.key, FlowSetting.value).where(
                FlowSetting.key.like(PACING_PREFIX + "%"))):
            try:
                if float(value) <= epoch:
                    expired_timers.append(key)
            except (TypeError, ValueError):
                continue
    removed = 0
    for start in range(0, len(expired_timers), BATCH):
        chunk = expired_timers[start:start + BATCH]
        with flow.lock, flow.sessions.begin() as session:
            # Recheck inside the transaction: a send may have just renewed a timer.
            for item in session.scalars(select(FlowSetting).where(FlowSetting.key.in_(chunk))):
                try:
                    if float(item.value) <= epoch:
                        session.delete(item)
                        removed += 1
                except (TypeError, ValueError):
                    continue
    counts["sendTimers"] = removed

    with flow.lock, flow.sessions.begin() as session:
        counts["previews"] = session.execute(delete(FlowPreview).where(
            FlowPreview.created_at <= now - PREVIEW_TTL)).rowcount or 0
    return counts

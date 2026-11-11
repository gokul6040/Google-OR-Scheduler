"""Queue and offer state used only by the isolated test database.

Index names are fixed so that ``flow_test.performance_schema`` can add the same
indexes to databases created before they existed (``CREATE INDEX IF NOT EXISTS``).
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Keep test-only tables out of the production metadata and migrations."""

    pass


class FlowSetting(Base):
    __tablename__ = "flow_test_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class FlowContactWindow(Base):
    """Verified inbound time for one coworker and receiving business number."""

    __tablename__ = "flow_test_contact_windows"
    recipient: Mapped[str] = mapped_column(String(24), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_inbound: Mapped[datetime] = mapped_column(DateTime)


class FlowOffer(Base):
    __tablename__ = "flow_test_offers"
    __table_args__ = (
        Index("ix_flow_offers_visit_date", "visit_date"),
        Index("ix_flow_offers_state_expires", "state", "expires_at"),
        Index("ix_flow_offers_recipient", "recipient"),
        Index("ix_flow_offers_patient", "patient_code", "state"),
    )
    token: Mapped[str] = mapped_column(String(40), primary_key=True)
    appointment_ids: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[str] = mapped_column(Text)
    patient_code: Mapped[str] = mapped_column(String(40))
    recipient: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(30), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # The single visit day an offer covers. NULL only for rows written before the
    # column existed and not yet backfilled; readers fall back to the snapshot.
    visit_date: Mapped[date | None] = mapped_column(Date)


class FlowOfferAppointment(Base):
    """Which appointments an offer covers (mirrors ``FlowOffer.appointment_ids``).

    Lets an export or attendance update find the offers for given appointments
    with an index, even after a visit was moved to another date.
    """

    __tablename__ = "flow_test_offer_appointments"
    token: Mapped[str] = mapped_column(String(40), primary_key=True)
    appointment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, index=True)


class FlowJob(Base):
    __tablename__ = "flow_test_jobs"
    __table_args__ = (
        Index("ix_flow_jobs_kind_status", "kind", "status", "id"),
        Index("ix_flow_jobs_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[str | None] = mapped_column(String(200), index=True)
    delivery: Mapped[str | None] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FlowInbox(Base):
    __tablename__ = "flow_test_inbox"
    __table_args__ = (
        Index("ix_flow_inbox_resolved", "resolved", "id"),
        Index("ix_flow_inbox_patient", "patient_code"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    patient_code: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    resolved: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FlowExportItem(Base):
    """The Zoho Scheduler row last saved for one appointment.

    Replaces one settings row per exported appointment, so an export reads only
    the target date's rows instead of scanning every record ever pushed.
    """

    __tablename__ = "flow_test_export_items"
    appointment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    therapy_date: Mapped[date] = mapped_column(Date, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    record_id: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FlowPreview(Base):
    """A reviewed schedule preview, stored so any app copy can save it."""

    __tablename__ = "flow_test_previews"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    data: Mapped[str] = mapped_column(Text)

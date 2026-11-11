"""Additive performance migration for workflow storage (review items #18, #23, #26).

Callers must already have validated namespace ownership. Every step is idempotent
and preserves existing rows:

* adds ``flow_test_offers.visit_date`` and backfills it from each offer's snapshot;
* moves per-appointment Zoho export tracking out of settings rows into
  ``flow_test_export_items`` (created beforehand by ``create_all``);
* records a contact-list version so readers can cache parent phone numbers;
* adds the query indexes declared on the models (and on ``audit_logs``).

Plain ``CREATE INDEX`` briefly blocks writes to that table; the tables are small
before go-live. On a large live table, build the index ``CONCURRENTLY`` by hand
first, using the same name, and this step becomes a no-op.
"""

from datetime import date, datetime
import hashlib
import json
from uuid import uuid4

from sqlalchemy import inspect, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from flow_test.models import FlowExportItem, FlowOffer, FlowOfferAppointment, FlowSetting

# Tables this release adds to existing stores (created by ``create_all``).
ADDED_TABLES = ("flow_test_export_items", "flow_test_previews", "flow_test_offer_appointments")

EXPORT_ITEM_PREFIX = "zoho_scheduler_export:item:"
SOURCE_KEY = "zoho_test_scenario"
SOURCE_VERSION_KEY = "zoho_test_scenario_version"

# (index name, table, columns). Names match the models' declared indexes.
INDEXES = (
    ("ix_flow_offers_visit_date", "flow_test_offers", ("visit_date",)),
    ("ix_flow_offers_state_expires", "flow_test_offers", ("state", "expires_at")),
    ("ix_flow_offers_recipient", "flow_test_offers", ("recipient",)),
    ("ix_flow_offers_patient", "flow_test_offers", ("patient_code", "state")),
    ("ix_flow_jobs_kind_status", "flow_test_jobs", ("kind", "status", "id")),
    ("ix_flow_jobs_created", "flow_test_jobs", ("created_at",)),
    ("ix_flow_inbox_resolved", "flow_test_inbox", ("resolved", "id")),
    ("ix_flow_inbox_patient", "flow_test_inbox", ("patient_code",)),
    ("ix_audit_logs_entity_action", "audit_logs", ("entity", "action")),
)


def source_version_for(scenario: dict) -> str:
    """Content version of a source roster: unchanged imports keep caches valid."""
    encoded = json.dumps(scenario, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def snapshot_date(snapshot: str) -> date | None:
    """The visit day recorded in an offer snapshot (each row: [id, iso_date, ...])."""
    try:
        days = {row[1] for row in json.loads(snapshot)}
    except (ValueError, TypeError, IndexError):
        return None
    if len(days) != 1:
        return None
    try:
        return date.fromisoformat(next(iter(days)))
    except (TypeError, ValueError):
        return None


def missing_performance_storage(engine: Engine, schema: str | None = None) -> list[str]:
    """Report storage this release needs and that only the migration adds."""
    inspector = inspect(engine)
    missing = []
    if "visit_date" not in {c["name"] for c in inspector.get_columns(FlowOffer.__tablename__, schema=schema)}:
        missing.append("flow_test_offers.visit_date")
    names = set(inspector.get_table_names(schema=schema))
    missing.extend(table for table in ADDED_TABLES if table not in names)
    return missing


def _qualified(connection: Connection, table: str, schema: str | None) -> tuple[str, str]:
    """Return (index prefix, table reference) honouring the SQLite attach quirk."""
    quote = connection.dialect.identifier_preparer.quote
    if schema and connection.dialect.name == "sqlite":
        # Attached SQLite namespaces qualify the INDEX, not the ON table.
        return quote(schema) + ".", quote(table)
    return "", (quote(schema) + "." + quote(table)) if schema else quote(table)


def migrate_performance(connection: Connection, schema: str | None = None) -> dict:
    """Apply every step; safe to run on each start-up and on explicit migrations."""
    quote = connection.dialect.identifier_preparer.quote
    _, offers = _qualified(connection, FlowOffer.__tablename__, schema)
    columns = {c["name"] for c in inspect(connection).get_columns(FlowOffer.__tablename__, schema=schema)}
    if "visit_date" not in columns:
        connection.exec_driver_sql(f"ALTER TABLE {offers} ADD COLUMN {quote('visit_date')} DATE")

    session = Session(bind=connection, join_transaction_mode="rollback_only", autoflush=False)
    backfilled = moved = 0
    try:
        # Backfill offer visit dates in bounded chunks. Offers whose snapshot has no
        # single readable date stay NULL; readers then use the snapshot directly.
        skipped: set[str] = set()
        while True:
            query = select(FlowOffer.token, FlowOffer.snapshot).where(FlowOffer.visit_date.is_(None))
            if skipped:
                query = query.where(FlowOffer.token.notin_(skipped))
            rows = session.execute(query.limit(500)).all()
            if not rows:
                break
            for token, snapshot in rows:
                day = snapshot_date(snapshot)
                if day is None:
                    skipped.add(token)
                    continue
                session.execute(update(FlowOffer).where(FlowOffer.token == token).values(visit_date=day))
                backfilled += 1
            session.flush()

        # Link each existing offer to its appointments (once per offer). Offers with
        # no readable appointment list are skipped so the loop always ends.
        unlinkable: set[str] = set()
        while True:
            linked = select(FlowOfferAppointment.token).where(FlowOfferAppointment.token == FlowOffer.token)
            query = select(FlowOffer.token, FlowOffer.appointment_ids).where(~linked.exists())
            if unlinkable:
                query = query.where(FlowOffer.token.notin_(unlinkable))
            rows = session.execute(query.limit(500)).all()
            if not rows:
                break
            for token, appointment_ids in rows:
                try:
                    identifiers = sorted({int(item) for item in json.loads(appointment_ids)})
                except (TypeError, ValueError):
                    identifiers = []
                if not identifiers:
                    unlinkable.add(token)
                for appointment_id in identifiers:
                    session.add(FlowOfferAppointment(token=token, appointment_id=appointment_id))
            session.flush()

        # Move export tracking rows out of the settings table.
        while True:
            # LIKE, not a key range: range bounds depend on the database collation.
            items = list(session.scalars(select(FlowSetting).where(
                FlowSetting.key.like(EXPORT_ITEM_PREFIX + "%"),
            ).limit(500)))
            if not items:
                break
            for item in items:
                value = json.loads(item.value)
                appointment_id = int(item.key.removeprefix(EXPORT_ITEM_PREFIX))
                if session.get(FlowExportItem, appointment_id) is None:
                    session.add(FlowExportItem(
                        appointment_id=appointment_id,
                        therapy_date=datetime.strptime(value["therapyDate"], "%d-%m-%Y").date(),
                        fingerprint=value["fingerprint"], record_id=str(value["recordId"]),
                    ))
                session.delete(item)
                moved += 1
            session.flush()

        # Give an existing imported contact list a version, so readers can cache it.
        if session.get(FlowSetting, SOURCE_KEY) is not None and session.get(FlowSetting, SOURCE_VERSION_KEY) is None:
            session.add(FlowSetting(key=SOURCE_VERSION_KEY, value=uuid4().hex))
        session.flush()
    finally:
        session.close()

    present = set(inspect(connection).get_table_names(schema=schema))
    created = 0
    for name, table, cols in INDEXES:
        if table not in present:
            continue
        prefix, reference = _qualified(connection, table, schema)
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {prefix}{quote(name)} ON {reference} "
            f"({', '.join(quote(column) for column in cols)})")
        created += 1
    return {"offerDatesBackfilled": backfilled, "exportItemsMoved": moved, "indexesEnsured": created}

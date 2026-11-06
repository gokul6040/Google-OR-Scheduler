"""Additive queue migration; callers must validate namespace ownership first."""

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from flow_test.models import FlowJob


RELIABILITY_COLUMNS = {
    "next_attempt_at": "TIMESTAMP", "deadline_at": "TIMESTAMP",
    "locked_by": "VARCHAR(64)", "locked_until": "TIMESTAMP",
    "send_started_at": "TIMESTAMP", "accepted_at": "TIMESTAMP", "error_code": "VARCHAR(30)",
}


def missing_columns(engine: Engine, schema: str | None = None) -> set[str]:
    return set(RELIABILITY_COLUMNS) - {c["name"] for c in inspect(engine).get_columns(FlowJob.__tablename__, schema=schema)}


def migrate_queue(connection: Connection, schema: str | None = None) -> None:
    """Preserve historical rows; NULL send markers remain conservatively unknown."""
    quote = connection.dialect.identifier_preparer.quote
    table = quote(FlowJob.__tablename__)
    if schema:
        table = quote(schema) + "." + table
    columns = {c["name"] for c in inspect(connection).get_columns(FlowJob.__tablename__, schema=schema)}
    for name, sql_type in RELIABILITY_COLUMNS.items():
        if name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {quote(name)} {sql_type}")
    index = quote("ix_flow_jobs_retry_due")
    if schema and connection.dialect.name == "sqlite":
        # Attached SQLite namespaces qualify the INDEX, not the ON table.
        index = quote(schema) + "." + index
        table = quote(FlowJob.__tablename__)
    connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {index} ON {table} (status, next_attempt_at)")

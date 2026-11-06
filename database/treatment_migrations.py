"""Add treatment-course identity without rewriting clinical records."""

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine


def has_course_identity(engine: Engine, schema: str | None = None) -> bool:
    """Report readiness without changing the selected database."""
    return "source_plan_id" in {
        column["name"] for column in inspect(engine).get_columns(
            "treatment_requirements", schema=schema,
        )
    }


def migrate_treatment_courses(connection: Connection, schema: str | None = None) -> None:
    """Add a nullable source ID and index; existing requirement IDs stay intact."""
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(87652515)")
    quote = connection.dialect.identifier_preparer.quote
    table = quote("treatment_requirements")
    qualified = f"{quote(schema)}.{table}" if schema else table
    columns = inspect(connection).get_columns("treatment_requirements", schema=schema)
    if "source_plan_id" not in {column["name"] for column in columns}:
        connection.exec_driver_sql(
            f"ALTER TABLE {qualified} ADD COLUMN source_plan_id VARCHAR(80)"
        )
    index = quote("ix_treatment_requirements_source_plan_id")
    if schema and connection.dialect.name == "sqlite":
        index = f"{quote(schema)}.{index}"
        qualified = table
    connection.exec_driver_sql(
        f"CREATE INDEX IF NOT EXISTS {index} ON {qualified} (source_plan_id)"
    )

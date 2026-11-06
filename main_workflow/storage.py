"""Add queue tables explicitly; never seed or replace main clinical records."""

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from flow_test.models import Base as WorkflowBase, FlowSetting
from flow_test.storage import TEST_TABLES
from database.db import Base


MARKER = "main-workflow-v1"


def check_storage(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    required = {table.name for table in TEST_TABLES}
    if not required <= names:
        raise ValueError("Prepare the main workflow tables explicitly with python -m main_workflow.migrate --apply")
    from database.treatment_migrations import has_course_identity
    if not has_course_identity(engine):
        raise ValueError("Upgrade treatment courses with python -m main_workflow.migrate --apply")
    with sessionmaker(bind=engine)() as session:
        marker = session.get(FlowSetting, "scenario")
        if marker is None or marker.value != MARKER:
            raise ValueError("This database is not marked for main daily operations; run the explicit migration first")
    from flow_test.reliability_schema import missing_columns
    if missing_columns(engine):
        raise ValueError("Upgrade the WhatsApp queue with python -m main_workflow.migrate --apply before starting workers")


def prepare_storage(engine: Engine, source_name: str) -> dict:
    """Upgrade only verified workflow storage under cross-process coordination."""
    from database.workflow_lock import workflow_locks

    lock, inbound_lock = workflow_locks(engine)
    with lock, inbound_lock:
        return _prepare_storage(engine, source_name)


def _prepare_storage(engine: Engine, source_name: str) -> dict:
    """Create five additive workflow tables, preserving all domain tables/rows."""
    names = set(inspect(engine).get_table_names())
    domain = {table.name for table in TEST_TABLES if table.metadata is Base.metadata}
    if not domain <= names:
        raise ValueError("Initialize the normal application's existing database before preparing its workflow")
    if FlowSetting.__tablename__ in names:
        with sessionmaker(bind=engine)() as session:
            marker = session.get(FlowSetting, "scenario")
            if marker is None or marker.value != MARKER:
                raise ValueError("Refusing to convert test storage into the main workflow")
            source = session.get(FlowSetting, "main_workflow_source")
            if source and source.value != source_name:
                raise ValueError("Changing the main source identity requires a separate reviewed migration")
    with engine.begin() as connection:
        from database.treatment_migrations import migrate_treatment_courses
        migrate_treatment_courses(connection)
        WorkflowBase.metadata.create_all(connection)
        from flow_test.reliability_schema import migrate_queue
        migrate_queue(connection)
        with sessionmaker(bind=connection, join_transaction_mode="rollback_only").begin() as session:
            for key, value in (("scenario", MARKER), ("main_workflow_source", source_name)):
                if session.get(FlowSetting, key) is None:
                    session.add(FlowSetting(key=key, value=value))
    return {"tablesAdded": sorted(set(inspect(engine).get_table_names()) - names),
            "clinicalRowsChanged": 0, "enabled": False, "messagesSent": 0}

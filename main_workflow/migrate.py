"""Explicit additive preparation of the normal database; defaults to inspection."""

import argparse
import json

from sqlalchemy import inspect

from main_workflow.config import WorkflowConfig, data_source
from main_workflow.storage import prepare_storage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if data_source() == "test":
        from database.workflow_lock import workflow_locks
        from flow_test.models import FlowSetting
        from flow_test.reliability_schema import migrate_queue, missing_columns
        from database.treatment_migrations import has_course_identity, migrate_treatment_courses
        from flow_test.storage import create_test_engine
        from main_workflow.retained_data import selected_test_target
        from sqlalchemy.orm import sessionmaker

        url, schema = selected_test_target()
        queue_engine = create_test_engine(url, schema)
        try:
            lock, inbound_lock = workflow_locks(queue_engine, schema)
            with lock, inbound_lock:
                with sessionmaker(bind=queue_engine)() as session:
                    marker = session.get(FlowSetting, "scenario")
                    if not marker or marker.value not in {"malaparamba-fictional-v1", "malaparamba-fictional-v2", "malaparamba-fictional-v3"}:
                        raise ValueError("Refusing to upgrade an unrecognized test store")
                missing = sorted(missing_columns(queue_engine, schema))
                courses_missing = not has_course_identity(queue_engine, schema)
                if args.apply:
                    with queue_engine.begin() as connection:
                        migrate_queue(connection, schema)
                        migrate_treatment_courses(connection, schema)
                print(json.dumps({"mode": "APPLIED" if args.apply else "INSPECTION ONLY",
                                  "queueColumns": missing, "treatmentCourseColumnMissing": courses_missing,
                                  "clinicalRowsChanged": 0, "messagesSent": 0}))
        finally:
            queue_engine.dispose()
        return
    from database.db import engine

    config = WorkflowConfig.from_environment()
    if args.apply:
        result = prepare_storage(engine, config.source_name)
    else:
        result = {"mode": "INSPECTION ONLY", "source": config.source_name,
                  "existingTables": sorted(inspect(engine).get_table_names()),
                  "changes": "--apply adds/upgrades workflow queue tables and treatment-course identity; no patients, plans or appointments are seeded"}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""Local release helper: backup/drain a retained test queue, then provision controls.

Used by deploy_whatsapp.ps1; main_workflow/release.py reuses its drain, backup and
upgrade steps for deploy_main.ps1. No WhatsApp requests are made here. Database
URLs remain in the process environment; diagnostic output excludes row contents.
"""

import argparse
from contextlib import ExitStack
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url

from database.workflow_lock import WorkflowLock
from services.whatsapp_dispatch import DispatchControl


def encode_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("Unsupported database value in release backup")


def read_revision_inventory(path: str) -> list[dict[str, str]]:
    """Reject malformed CLI values before any provisioning, backup or stopping."""
    items = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(items, list):
        raise ValueError("Revision inventory must be a JSON array")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each revision must contain app and revision strings")
        app, revision = item.get("app"), item.get("revision")
        if (not isinstance(app, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", app)
                or not isinstance(revision, str)
                or not re.fullmatch(re.escape(app) + r"--[a-z0-9-]+", revision)):
            raise ValueError("Invalid app/revision name; no sender was stopped")
    return items


# Same order as reset: drain both new lanes plus old embedded workflow sends.
DRAIN_LANES = ("sender", "ack_sender", "workflow", "inbound")


def queue_engine(url: str, schema: str):
    """Engine for the retained queue schema; statement parameters stay out of errors."""
    return create_engine(url, hide_parameters=True, pool_pre_ping=True,
                         connect_args={"connect_timeout": 15, "options": f"-csearch_path={schema},pg_catalog"},
                         execution_options={"schema_translate_map": {None: schema}})


def drain_backup_and_stop(engine, schema: str, backup_path: str, revisions: list[dict[str, str]],
                          az: str, resource_group: str, subscription: str,
                          lanes: tuple[str, ...] = DRAIN_LANES) -> None:
    """Hold every lane, save a consistent backup, then stop the listed revisions.

    Ownership is retained until every old cloud replica has stopped, so no old
    process is part-way through a send or an update when the caller migrates.
    """
    with ExitStack() as stack:
        for lane in lanes:
            stack.enter_context(WorkflowLock(engine, lane, schema))
        metadata = MetaData()
        metadata.reflect(bind=engine, schema=schema)
        backup = {"schema": schema, "created_at": datetime.utcnow().isoformat() + "Z", "tables": {}}
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            for table in metadata.sorted_tables:
                backup["tables"][table.name] = [dict(row) for row in connection.execute(select(table)).mappings()]
        with Path(backup_path).open("x", encoding="utf-8") as output:
            json.dump(backup, output, default=encode_value, ensure_ascii=False)
        print("Consistent retained-queue backup saved in the protected release directory.", flush=True)
        for item in revisions:
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", az,
                       "containerapp", "revision", "deactivate", "--name", item["app"],
                       "--revision", item["revision"], "--resource-group", resource_group,
                       "--subscription", subscription, "--only-show-errors", "--output", "none"]
            subprocess.run(command, check=True)
        for item in revisions:
            for _ in range(90):
                command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", az,
                           "containerapp", "replica", "list", "--name", item["app"],
                           "--revision", item["revision"], "--resource-group", resource_group,
                           "--subscription", subscription, "--only-show-errors", "--output", "json"]
                replicas = json.loads(subprocess.check_output(command, text=True, encoding="utf-8"))
                if not replicas:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Old sender replicas did not stop; deployment stopped before migration")


def migration_environment(queue_url: str, schema: str) -> dict[str, str]:
    """Select exactly this retained store for main_workflow.migrate."""
    environment = os.environ.copy()
    environment.update(MAIN_WORKFLOW_DATA_SOURCE="test", MAIN_WORKFLOW_TEST_DATABASE_URL=queue_url,
                       MAIN_WORKFLOW_TEST_DATABASE_SCHEMA=schema)
    return environment


def apply_migration(queue_url: str, schema: str) -> None:
    """Run the additive upgrade (one transaction) against the selected store."""
    subprocess.run([sys.executable, "-m", "main_workflow.migrate", "--apply"],
                   env=migration_environment(queue_url, schema), check=True)


def release(args) -> None:
    revisions = read_revision_inventory(args.revisions) if not args.check else []
    queue_url = os.environ["WHATSAPP_RELEASE_QUEUE_URL"]
    admin_url = os.environ["WHATSAPP_RELEASE_ADMIN_URL"]
    queue_target, admin_target = make_url(queue_url), make_url(admin_url)
    if (queue_target.get_backend_name() != "postgresql" or
            ((queue_target.host or "").lower(), queue_target.port or 5432, queue_target.database) !=
            ((admin_target.host or "").lower(), admin_target.port or 5432, admin_target.database)):
        raise ValueError("Queue and control provisioner must select the same PostgreSQL database")
    schema = "flow_test"
    engine = queue_engine(queue_url, schema)
    control = None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            marker = connection.execute(text("SELECT value FROM flow_test.flow_test_settings WHERE key='scenario'")).scalar()
            mode = connection.execute(text("SELECT value FROM flow_test.flow_test_settings WHERE key='mode'")).scalar()
            if marker not in {"malaparamba-fictional-v1", "malaparamba-fictional-v2", "malaparamba-fictional-v3"} or mode != "live":
                raise ValueError("Refusing to release an unrecognized or non-live retained test store")
        if args.check:
            with create_engine(admin_url, hide_parameters=True, connect_args={"connect_timeout": 15}).connect() as connection:
                connection.execute(text("SELECT 1"))
            print("Selected live retained test queue and provisioning connection verified.")
            return
        # Provision with the existing migration identity, then verify that the
        # restricted queue identity can operate the separate control table.
        os.environ["WHATSAPP_DISPATCH_DATABASE_URL"] = admin_url
        control = DispatchControl(engine)
        control.prepare()
        quote = control.engine.dialect.identifier_preparer.quote
        role = quote(queue_target.username or "")
        with control.engine.begin() as connection:
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA whatsapp_dispatch TO {role}")
            connection.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE ON whatsapp_dispatch.whatsapp_dispatch_state TO {role}")
        control.engine.dispose()
        os.environ["WHATSAPP_DISPATCH_DATABASE_URL"] = queue_url
        control = DispatchControl(engine)
        control.check()
        with control.state("release-permission-check"):
            pass
        drain_backup_and_stop(engine, schema, args.backup, revisions, args.az,
                              args.resource_group, args.subscription)
        # Migration remains explicitly scoped to existing flow_test tables.
        apply_migration(queue_url, schema)
        print("Workflow migration and shared control permissions verified. No messages sent.", flush=True)
    finally:
        if control is not None:
            control.engine.dispose()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--revisions")
    parser.add_argument("--az")
    parser.add_argument("--resource-group")
    parser.add_argument("--subscription")
    args = parser.parse_args()
    if not args.check and not all((args.backup, args.revisions, args.az, args.resource_group, args.subscription)):
        parser.error("Release requires backup, revision inventory and Azure CLI arguments")
    try:
        release(args)
    except Exception as exc:
        # DB exceptions may contain connection information or row values.
        raise SystemExit(f"Release preparation stopped ({type(exc).__name__}); no sender was started. Check the protected backup and Azure revision status.") from None


if __name__ == "__main__":
    main()

"""Local release helper for deploy_main.ps1: check, then drain, back up, stop and upgrade.

Never sends WhatsApp messages and never contacts Zoho. The deploy script puts the
app's database URL in MAIN_RELEASE_DATABASE_URL (process memory only). It is never
printed, and failures report only a safe reason, never connection details or rows.

--check   Read-only: does this store need the release's upgrade? Prints one JSON line.
--stop    Wait for running sends and Zoho pushes, save a backup, stop the listed
          revisions and, with --upgrade, apply the additive upgrade (one database
          transaction) and confirm that nothing is still pending.
"""

import argparse
import json
import os
import subprocess
import sys

from sqlalchemy.engine import make_url

from services.whatsapp_release import (DRAIN_LANES, drain_backup_and_stop, migration_environment,
                                       queue_engine, read_revision_inventory)

SCHEMA = "flow_test"
URL_VARIABLE = "MAIN_RELEASE_DATABASE_URL"
# A Zoho push takes the workflow lane inside its own lane, so its lane comes first.
MAIN_LANES = ("zoho_push",) + DRAIN_LANES
# Inspection keys that describe the run itself rather than missing storage.
REPORT_ONLY = {"mode", "clinicalRowsChanged", "messagesSent"}


class ReleaseStopped(Exception):
    """A failure whose message is safe to print (no URL, no row values)."""


def database_url() -> str:
    """The app's PostgreSQL URL, supplied by the deploy script."""
    url = os.environ.get(URL_VARIABLE, "").strip()
    if not url:
        raise ReleaseStopped("the deploy script did not supply the database connection")
    try:
        backend = make_url(url).get_backend_name()
    except Exception:  # noqa: BLE001 - the parser's message would echo the URL
        raise ReleaseStopped("the database connection is not a valid URL") from None
    if backend != "postgresql":
        raise ReleaseStopped("the main release needs the app's PostgreSQL database")
    return url


def safe_reason(stderr: str) -> str:
    """The upgrade command's final error, without connection details."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "no details"
    name, _, message = lines[-1].partition(": ")
    name = name.rsplit(".", 1)[-1]
    if name == "ValueError" and message and "://" not in message and "@" not in message:
        return message
    return name if name.isidentifier() else "see the upgrade command's output"


def _run_migrate(url: str, *extra: str, timeout: int) -> str:
    result = subprocess.run([sys.executable, "-m", "main_workflow.migrate", *extra],
                            env=migration_environment(url, SCHEMA), capture_output=True,
                            encoding="utf-8", timeout=timeout)
    if result.returncode != 0:
        step = "upgrade" if extra else "database check"
        raise ReleaseStopped(f"the {step} failed: {safe_reason(result.stderr)}")
    return result.stdout


def inspect_store(url: str) -> dict:
    """Read-only: the same inspection main_workflow.migrate prints without --apply."""
    output = _run_migrate(url, timeout=900)
    try:
        report = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ReleaseStopped("the database check returned no report") from None
    details = {key: value for key, value in report.items() if key not in REPORT_ONLY and value}
    return {"upgradeNeeded": bool(details), "pending": sorted(details), "details": details}


def stop_and_upgrade(args) -> dict:
    """Drain, back up and stop the old revisions; then upgrade and verify if asked."""
    url = database_url()
    revisions = read_revision_inventory(args.revisions)
    engine = queue_engine(url, SCHEMA)
    try:
        drain_backup_and_stop(engine, SCHEMA, args.backup, revisions, args.az,
                              args.resource_group, args.subscription, lanes=MAIN_LANES)
    finally:
        engine.dispose()
    print("Old revisions stopped: " + (", ".join(item["revision"] for item in revisions) or "none"), flush=True)
    summary = {"stopped": [item["revision"] for item in revisions], "upgraded": False, "messagesSent": 0}
    if args.upgrade:
        # main_workflow.migrate --apply makes every change in one transaction.
        _run_migrate(url, "--apply", timeout=3600)
        after = inspect_store(url)
        if after["upgradeNeeded"]:
            raise ReleaseStopped("the upgrade ran but storage is still incomplete: " + ", ".join(after["pending"]))
        summary["upgraded"] = True
        print("Database upgrade applied and verified.", flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--stop", action="store_true")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--backup")
    parser.add_argument("--revisions")
    parser.add_argument("--az")
    parser.add_argument("--resource-group")
    parser.add_argument("--subscription")
    args = parser.parse_args()
    if args.check and args.upgrade:
        parser.error("--upgrade is used only with --stop")
    if args.stop and not all((args.backup, args.revisions, args.az, args.resource_group, args.subscription)):
        parser.error("--stop requires backup, revision inventory and Azure CLI arguments")
    try:
        result = inspect_store(database_url()) if args.check else stop_and_upgrade(args)
    except (ReleaseStopped, RuntimeError) as exc:
        raise SystemExit(f"Release stopped: {exc}. No messages were sent.") from None
    except subprocess.CalledProcessError:
        raise SystemExit("Release stopped: an Azure command failed while stopping the old version. "
                         "No messages were sent. Check the Azure revision status.") from None
    except Exception as exc:  # noqa: BLE001 - database errors may contain connection details or rows
        raise SystemExit(f"Release stopped ({type(exc).__name__}). No messages were sent. "
                         "Check the release directory and the Azure revision status.") from None
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()

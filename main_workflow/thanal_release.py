"""Thanal-only image build, backed-up schema upgrade and initial source import.

Host mode uses the separate .azure-thanal profile and the existing registry
identity. Jobs run inside Thanal's environment because local PostgreSQL access
may be blocked. No WhatsApp credentials are copied into these one-time jobs.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.request import ProxyHandler, build_opener
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import zlib

SUB = "1b57e3d2-240d-45a7-9b84-9eb3e25fa405"
TENANT = "f4206d88-df65-496a-833c-54112a391124"
GROUP, APP, WORKER, REGISTRY = "thanal-rg", "thanalapp", "thanalworker", "thanalacr"
SERVER = "thanalschedulerdb"
ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIELDS = {
    "MAIN_WORKFLOW_ENABLED": "true", "MAIN_WORKFLOW_DATA_SOURCE": "main",
    "MAIN_WORKFLOW_LIVE": "false", "FLOW_TEST_LIVE": "false",
    "MAIN_WORKFLOW_REMOTE_SENDER": "false", "WHATSAPP_WORKER_MODE": "external",
    "MAIN_WORKFLOW_REQUIRE_ACCESS_KEY": "false", "MAIN_WORKFLOW_SOURCE_TIMEZONE": "Asia/Kolkata",
    "MAIN_WORKFLOW_ZOHO_OWNER": "thanal", "MAIN_WORKFLOW_ZOHO_APP": "eic",
    "MAIN_WORKFLOW_ZOHO_PLAN_REPORT": "All_Therapy_Schedule_Plans",
    "MAIN_WORKFLOW_ZOHO_CONSULTANT_REPORT": "All_Consultants",
    "MAIN_WORKFLOW_ZOHO_REGISTRATION_REPORT": "All_Registrations",
    "MAIN_WORKFLOW_ZOHO_SCHEDULE_FORM": "Schedule_Adhoc_Trial",
    "MAIN_WORKFLOW_ZOHO_SCHEDULE_REPORT": "Schedule_Adhoc_Trial_Report",
}


class ReleaseError(RuntimeError):
    """A release error whose text is safe to display."""


def validate_database_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme.split("+")[0] != "postgresql"
            or parsed.hostname != SERVER + ".postgres.database.azure.com"
            or parsed.path != "/postgres" or (parsed.port or 5432) != 5432):
        raise ReleaseError("The saved database does not match Thanal's verified PostgreSQL target.")


def verify_database(engine):
    from sqlalchemy import text
    validate_database_url(engine.url.render_as_string(hide_password=True))
    with engine.connect() as connection:
        if connection.scalar(text("select current_schema()")) != "public":
            raise ReleaseError("The database must use Thanal's verified public schema.")


class Azure:
    def __init__(self):
        cli = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft SDKs/Azure/CLI2/python.exe"
        if not cli.is_file():
            raise ReleaseError("Install the Windows Azure CLI before running this release.")
        self.command = [str(cli), "-IBm", "azure.cli"]
        self.env = {**os.environ, "AZURE_CONFIG_DIR": str(Path.home() / ".azure-thanal"),
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def run(self, *args, stream=False):
        command = self.command + list(args) + ["--subscription", SUB, "--only-show-errors"]
        command += ["--output", "none" if stream else "json"]
        result = subprocess.run(command, env=self.env, capture_output=not stream,
                                text=True, encoding="utf-8", cwd=ROOT)
        if result.returncode:
            # Azure stderr can contain request bodies; do not echo credentials.
            raise ReleaseError("Azure command failed: " + " ".join(args[:3]))
        return None if stream else (json.loads(result.stdout) if result.stdout.strip() else None)


def app_env(app):
    return {r["name"]: r for r in app["properties"]["template"]["containers"][0].get("env", [])}


def validate_target(account, app, worker, active_workers):
    """Reject the TurnB profile, test store and any enabled sender before building."""
    if account.get("id") != SUB or account.get("tenantId") != TENANT:
        raise ReleaseError("Sign in to the separate .azure-thanal profile and Thanal subscription first.")
    env = app_env(app)
    for key, value in {"MAIN_WORKFLOW_DATA_SOURCE": "main", "MAIN_WORKFLOW_ZOHO_OWNER": "thanal",
                       "MAIN_WORKFLOW_ZOHO_APP": "eic", "MAIN_WORKFLOW_LIVE": "false"}.items():
        if env.get(key, {}).get("value", "").lower() != value:
            raise ReleaseError("Unexpected Thanal app setting: " + key)
    if app_env(worker).get("MAIN_WORKFLOW_LIVE", {}).get("value", "").lower() != "false" or active_workers:
        raise ReleaseError("Stop Thanal's worker and set its live flag false before releasing.")
    if app["properties"]["configuration"].get("activeRevisionsMode") != "Single":
        raise ReleaseError("This release expects Thanal's existing Single revision mode.")


def approved_roster(path):
    """Use the approved one-time snapshot, not a permanent daily allowlist."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = [{"code": r["code"], "name": r["listed_name"] or r["name"],
             "approvedActive": bool(r["active"] and not r["contact_issues"])} for r in data if r["listed"]]
    if len(rows) != 512 or len({r["code"] for r in rows}) != 512 or sum(r["approvedActive"] for r in rows) != 354:
        raise ReleaseError("The approved snapshot must contain 512 unique listed children, 354 approved active.")
    return rows


def job_document(app, secrets, image, name, mode, roster=None):
    """Reuse the current identity; copy only credentials required for this job."""
    registry = next((r for r in app["properties"]["configuration"].get("registries", [])
                     if r["server"] == "thanalacr.azurecr.io" and r.get("identity")), None)
    if registry is None or registry["identity"] not in app.get("identity", {}).get("userAssignedIdentities", {}):
        raise ReleaseError("Thanal's existing registry identity could not be verified.")
    if not image.startswith("thanalacr.azurecr.io/therapy-scheduler:"):
        raise ReleaseError("The job image must be built in Thanal's existing registry.")
    env = dict(SOURCE_FIELDS)
    env.update(MAIN_WORKFLOW_ADMIN_ID="thanal_release", MAIN_WORKFLOW_ADMIN_TOKEN="unused-local-job-key-at-least-32-characters")
    source = app_env(app)
    wanted = {"THERAPY_SCHEDULER_DATABASE_URL"}
    if mode == "--inside-import":
        wanted |= {k for k in source if k.startswith(("ZOHO_CREATOR_", "MAIN_WORKFLOW_ZOHO_"))
                   and any(k.endswith(suffix) for suffix in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN", "API_URL", "ACCOUNTS_URL"))}
    job_secrets = []
    variables = [{"name": k, "value": v} for k, v in env.items()]
    for key in sorted(wanted):
        item = source.get(key)
        if not item:
            raise ReleaseError("Missing Thanal setting: " + key)
        value = secrets.get(item.get("secretRef")) if item.get("secretRef") else item.get("value")
        if not value:
            raise ReleaseError("Could not resolve the saved Thanal setting: " + key)
        if key == "THERAPY_SCHEDULER_DATABASE_URL":
            validate_database_url(value)
        secret_name = "release-" + key.lower().replace("_", "-")
        job_secrets.append({"name": secret_name, "value": value})
        variables.append({"name": key, "secretRef": secret_name})
    if roster is not None:
        encoded = base64.b64encode(zlib.compress(json.dumps(roster).encode())).decode()
        chunks = [encoded[i:i + 2000] for i in range(0, len(encoded), 2000)]
        variables.append({"name": "THANAL_INITIAL_ROSTER_CHUNKS", "value": str(len(chunks))})
        for i, chunk in enumerate(chunks):
            secret_name = f"initial-roster-{i:03d}"
            job_secrets.append({"name": secret_name, "value": chunk})
            variables.append({"name": f"THANAL_INITIAL_ROSTER_{i:03d}", "secretRef": secret_name})
    identity = registry["identity"]
    return {"name": name, "location": app["location"],
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity: {}}},
            "properties": {"environmentId": app["properties"]["managedEnvironmentId"],
                "workloadProfileName": app["properties"].get("workloadProfileName") or "Consumption",
                "configuration": {"triggerType": "Manual", "replicaTimeout": 3600, "replicaRetryLimit": 0,
                    "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                    "registries": [{"server": registry["server"], "identity": identity}], "secrets": job_secrets},
                "template": {"containers": [{"name": "release", "image": image,
                    "command": ["python", "-m", "main_workflow.thanal_release"], "args": [mode],
                    "env": variables, "resources": {"cpu": 0.5, "memory": "1Gi"}}]}}}


def run_job(azure, document):
    name = document["name"]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as f:
            temporary = Path(f.name)
            json.dump(document, f)
        azure.run("containerapp", "job", "create", "--name", name, "--resource-group", GROUP, "--yaml", str(temporary))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    started = azure.run("containerapp", "job", "start", "--name", name, "--resource-group", GROUP)
    execution = started["name"]
    print("Started one-time job:", name, execution, flush=True)
    until, previous = time.monotonic() + 3900, None
    while time.monotonic() < until:
        runs = azure.run("containerapp", "job", "execution", "list", "--name", name, "--resource-group", GROUP)
        run = next((r for r in runs if r["name"] == execution), None)
        state = run.get("properties", {}).get("status") if run else "Pending"
        if state != previous:
            print(name + ": " + str(state), flush=True)
            previous = state
        if state == "Succeeded":
            return
        if state in {"Failed", "Stopped", "Degraded"}:
            raise ReleaseError("Job failed: " + name + ". Inspect its Azure execution logs; the release stopped.")
        time.sleep(5)
    raise ReleaseError("Timed out waiting for job " + name)


def inside_upgrade():
    from sqlalchemy import select, func
    from database.db import engine
    from database.models import Patient, TreatmentRequirement, Appointment
    from sqlalchemy.orm import Session
    from main_workflow.storage import prepare_storage, check_storage
    if os.environ.get("MAIN_WORKFLOW_LIVE") != "false":
        raise ReleaseError("Migration requires messaging off.")
    verify_database(engine)
    def counts():
        with Session(engine) as s:
            return [s.scalar(select(func.count(m.id))) for m in (Patient, TreatmentRequirement, Appointment)]
    before = counts()
    result = prepare_storage(engine, "thanal/eic")
    check_storage(engine)
    if counts() != before:
        raise ReleaseError("Clinical row counts changed unexpectedly during schema migration.")
    print(json.dumps({"migration": "APPLIED_AND_VERIFIED", **result}), flush=True)


def inside_import():
    from sqlalchemy import select
    from database.db import engine
    from database.models import AuditLog, Patient
    from flow_test.models import FlowSetting
    from flow_test.zoho_import import CHECKPOINT_KEY
    from main_workflow.config import WorkflowConfig
    from main_workflow.provider import ZohoProvider
    from main_workflow.service import MainWorkflow
    from main_workflow.thanal_sync import apply_snapshot, prepare_snapshot
    from services.patient_roster_service import prepare_roster, apply_roster
    if any(os.environ.get(k, "false") != "false" for k in ("MAIN_WORKFLOW_LIVE", "FLOW_TEST_LIVE")):
        raise ReleaseError("Import requires messaging off.")
    verify_database(engine)
    encoded = "".join(os.environ[f"THANAL_INITIAL_ROSTER_{i:03d}"]
                      for i in range(int(os.environ["THANAL_INITIAL_ROSTER_CHUNKS"])))
    roster = json.loads(zlib.decompress(base64.b64decode(encoded)))
    config = WorkflowConfig.from_environment()
    flow = MainWorkflow(engine, config, send=lambda *_: (_ for _ in ()).throw(ReleaseError("Sending is disabled in release jobs.")))
    with flow.sessions() as session:
        checkpoint = session.get(FlowSetting, CHECKPOINT_KEY)
        previous = json.loads(checkpoint.value) if checkpoint else None
    # The sync contract rejects fractional seconds, including those from the
    # normal datetime.isoformat() default. Use the same precision as daily sync.
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    print("Fetching and validating the complete Thanal source snapshot.", flush=True)
    reports = ZohoProvider(config).fetch(previous, now)
    prepared = prepare_snapshot(reports)
    print("Source validated. Importing masters and latest treatment plans.", flush=True)
    sync = apply_snapshot(flow, reports, window={"from": previous, "to": now})
    decisions = prepare_roster(roster, prepared, reports)
    approved = {r["code"] for r in roster if r["approvedActive"]}
    print("Applying the initial patient eligibility roster once.", flush=True)
    for row in decisions:
        row["active"] = row["code"] in approved and not row["reasons"]
    ident = hashlib.sha256(json.dumps(roster, sort_keys=True).encode()).hexdigest()[:32]
    with flow.lock, flow.inbound_lock, flow.sessions.begin() as session:
        applied = session.scalar(select(AuditLog.id).where(AuditLog.action == "patient_roster_baseline", AuditLog.entity == "roster:" + ident))
        baseline = {"alreadyApplied": True} if applied else apply_roster(session, decisions, operation_id=ident,
            today=datetime.now(ZoneInfo("Asia/Kolkata")).date(), admin_user="thanal_release")
    with flow.sessions() as session:
        patients = list(session.scalars(select(Patient)))
        summary = {"patients": len(patients), "active": sum(p.active for p in patients),
                   "inactive": sum(not p.active for p in patients)}
    safe_sync = {k: sync[k] for k in ("registrations", "consultants", "downloaded_plans", "patients", "quarantined_plans", "partial")}
    print(json.dumps({"import": safe_sync, "baseline": baseline, "database": summary, "messagesSent": 0}), flush=True)


def stop_app(azure):
    revisions = azure.run("containerapp", "revision", "list", "--name", APP, "--resource-group", GROUP)
    active = [r["name"] for r in revisions if r.get("properties", {}).get("active")]
    for revision in active:
        if not re.fullmatch(r"thanalapp--[a-z0-9-]+", revision):
            raise ReleaseError("Unexpected revision name; refusing to stop anything.")
        azure.run("containerapp", "revision", "deactivate", "--revision", revision, "--resource-group", GROUP)
    for revision in active:
        for _ in range(90):
            if not azure.run("containerapp", "replica", "list", "--name", APP, "--revision", revision, "--resource-group", GROUP):
                break
            time.sleep(2)
        else:
            raise ReleaseError("Old app replicas did not stop; migration was not started.")
    return active


def deploy(args):
    azure = Azure()
    account = azure.run("account", "show")
    app = azure.run("containerapp", "show", "--name", APP, "--resource-group", GROUP)
    worker = azure.run("containerapp", "show", "--name", WORKER, "--resource-group", GROUP)
    revisions = azure.run("containerapp", "revision", "list", "--name", WORKER, "--resource-group", GROUP)
    active_workers = [r for r in revisions if r.get("properties", {}).get("active")]
    validate_target(account, app, worker, active_workers)
    registry = azure.run("acr", "show", "--name", REGISTRY)
    if registry["loginServer"] != "thanalacr.azurecr.io":
        raise ReleaseError("Unexpected registry.")
    registry_auth = next((r for r in app["properties"]["configuration"].get("registries", [])
                          if r.get("server") == registry["loginServer"]), {})
    if registry_auth.get("identity") not in app.get("identity", {}).get("userAssignedIdentities", {}):
        raise ReleaseError("Thanal's existing registry identity could not be verified.")
    server = azure.run("postgres", "flexible-server", "show", "--name", SERVER, "--resource-group", GROUP)
    if server["backup"]["backupRetentionDays"] < 1 or not server["backup"].get("earliestRestoreDate"):
        raise ReleaseError("Database backups are not enabled.")
    roster = None if args.build_only else approved_roster(args.roster)
    values = azure.run("containerapp", "secret", "list", "--name", APP, "--resource-group", GROUP, "--show-values")
    secrets = {r["name"]: r["value"] for r in values if "value" in r}
    db_setting = app_env(app)["THERAPY_SCHEDULER_DATABASE_URL"]
    db_url = secrets.get(db_setting.get("secretRef")) if db_setting.get("secretRef") else db_setting.get("value")
    if not db_url:
        raise ReleaseError("Could not resolve Thanal's database reference.")
    validate_database_url(db_url)
    print("Verified: Thanal subscription, main database, registry identity and messaging OFF.", flush=True)
    if roster is not None:
        print("Approved initial roster: 512 listed; at most 354 active after current source checks.", flush=True)
    if args.check:
        print("CHECK ONLY: no image build, database change, import or deployment performed.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = "thanal-" + stamp
    image = "thanalacr.azurecr.io/therapy-scheduler:" + tag
    print("Building and pushing:", image, flush=True)
    azure.run("acr", "build", "--registry", REGISTRY, "--platform", "linux/amd64", "--image", "therapy-scheduler:" + tag, ".", stream=True)
    if args.build_only:
        print("BUILD ONLY: pushed " + image + "; no database changes or deployment.")
        return
    job_stamp = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    migration = job_document(app, secrets, image, "thanal-db-" + job_stamp, "--inside-upgrade")
    importer = job_document(app, secrets, image, "thanal-import-" + job_stamp, "--inside-import", roster)
    restore_time = datetime.now(timezone.utc).isoformat()
    state_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TherapyScheduler/ThanalReleases" / stamp
    state_dir.mkdir(parents=True, exist_ok=False)
    (state_dir / "release.json").write_text(json.dumps({"subscription": SUB, "server": SERVER,
        "startedAtUtc": restore_time, "oldImage": app["properties"]["template"]["containers"][0]["image"],
        "newImage": image, "migrationJob": migration["name"], "importJob": importer["name"]}, indent=2), encoding="utf-8")
    if server.get("sku", {}).get("tier") != "Burstable" and server.get("storage", {}).get("type") != "PremiumV2_LRS":
        backup = "before-" + tag
        print("Creating managed PostgreSQL backup:", backup, flush=True)
        azure.run("postgres", "flexible-server", "backup", "create", "--server-name", SERVER, "--resource-group", GROUP, "--name", backup)
    else:
        print("Verified automatic point-in-time backups; this database tier does not support on-demand snapshots.", flush=True)
    print("Release/restore details saved:", str(state_dir), flush=True)
    print("Stopping old app for database upgrade and source import; the website will be unavailable until these finish.", flush=True)
    stop_app(azure)
    state_file = state_dir / "release.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["restoreTimeUtc"] = datetime.now(timezone.utc).isoformat()
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("Pre-upgrade restore time (UTC):", state["restoreTimeUtc"], flush=True)
    run_job(azure, migration)
    run_job(azure, importer)
    flags = [f"{k}={v}" for k, v in SOURCE_FIELDS.items()]
    print("Starting the upgraded app. Messaging remains OFF; worker stays stopped.", flush=True)
    azure.run("containerapp", "update", "--name", APP, "--resource-group", GROUP, "--image", image,
              "--revision-suffix", "d" + job_stamp, "--set-env-vars", *flags)
    endpoint = "https://" + app["properties"]["configuration"]["ingress"]["fqdn"]
    opener = build_opener(ProxyHandler({}))
    for _ in range(90):
        try:
            with opener.open(endpoint + "/main-workflow-config.js", timeout=15) as response:
                body = response.read().decode()
            settings = json.loads(body.split("=", 1)[1].strip().rstrip(";"))
            if settings.get("live") is False and settings.get("requiresAccessKey") is False and settings.get("dataSource") == "main":
                with opener.open(endpoint + "/api/patients", timeout=30) as response:
                    people = json.loads(response.read())["patients"]
                print(json.dumps({"deployed": image, "url": endpoint, "patients": len(people),
                                  "active": sum(p["active"] for p in people), "messaging": "OFF"}), flush=True)
                return
        except Exception:
            pass
        time.sleep(5)
    raise ReleaseError("The new app did not pass the health/access/messaging checks. Inspect its Azure revision.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for flag in ("check", "build-only", "deploy", "inside-upgrade", "inside-import"):
        modes.add_argument("--" + flag, action="store_true")
    parser.add_argument("--roster", default=str(ROOT / ".codex_tmp/iqra-roster/decisions.json"))
    args = parser.parse_args()
    try:
        if args.inside_upgrade:
            inside_upgrade()
        elif args.inside_import:
            inside_import()
        else:
            deploy(args)
    except ReleaseError as exc:
        raise SystemExit("Thanal release stopped: " + str(exc)) from None
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        location = Path(frame.filename).name + ":" + str(frame.lineno) + " in " + frame.name
        raise SystemExit("Thanal release stopped (" + type(exc).__name__ + ") at " + location + ". Connection details withheld; inspect the last completed step.") from None


if __name__ == "__main__":
    main()

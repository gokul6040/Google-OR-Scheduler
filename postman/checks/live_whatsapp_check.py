"""Consent-scoped deployed worker check; never changes appointment records.

Read-only inspection is the default. --send queues one labelled diagnostic TEXT
for WHATSAPP_TEST_RECIPIENT only, using a genuine verified service window.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from database.workflow_lock import WorkflowLock, workflow_locks
from flow_test.models import FlowContactWindow, FlowInbox, FlowJob, FlowOffer, FlowSetting
from flow_test.service import FlowTest


SUBSCRIPTION = "0488dadb-6bca-435f-bee2-0f8977fc646f"
GROUP = "TherapyScheduler"
APP = "orscheduler"


def azure(*arguments):
    executable = shutil.which("az")
    if not executable:
        raise RuntimeError("Azure CLI unavailable")
    msi_python = Path(executable).parent.parent / "python.exe"
    command = ([str(msi_python), "-IBm", "azure.cli"] if executable.lower().endswith(".cmd") and msi_python.exists()
               else [executable])
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run(command + list(arguments) + ["--subscription", SUBSCRIPTION,
                            "--only-show-errors", "--output", "json"], capture_output=True,
                            encoding="utf-8", env=environment, timeout=90)
    if result.returncode:
        raise RuntimeError("Azure inspection failed; no secret output retained")
    return json.loads(result.stdout) if result.stdout.strip() else None


def settings():
    app = azure("containerapp", "show", "--name", APP, "--resource-group", GROUP)
    if len(app["properties"]["template"]["containers"]) != 1:
        raise ValueError("Unexpected application layout")
    secret_rows = azure("containerapp", "secret", "list", "--name", APP, "--resource-group", GROUP, "--show-values")
    secret_values = {row["name"]: row.get("value") for row in secret_rows}
    environment = {item["name"]: secret_values.get(item["secretRef"]) if item.get("secretRef") else item.get("value")
                   for item in app["properties"]["template"]["containers"][0]["env"]}
    if (environment.get("MAIN_WORKFLOW_DATA_SOURCE") != "test" or
            environment.get("MAIN_WORKFLOW_TEST_DATABASE_SCHEMA") != "flow_test"):
        raise ValueError("Only the existing retained test workspace may be checked")
    host = app["properties"]["configuration"]["ingress"]["fqdn"]
    if not host.endswith(".azurecontainerapps.io"):
        raise ValueError("Unexpected deployed hostname")
    return environment, "https://" + host


def request(base, path, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with urlopen(Request(base + path, headers=headers), timeout=30) as response:
        return response.status, response.read()


def queue_check(session, recipient: str, business_id: str, *, now=None) -> str | None:
    """Caller holds the workflow lock. No fabricated contact windows/appointments."""
    now = now or datetime.utcnow()
    window = session.get(FlowContactWindow, (recipient, business_id))
    if not window or window.last_inbound > now + timedelta(minutes=5) or window.last_inbound <= now - timedelta(hours=24) + timedelta(minutes=5):
        return None
    token = secrets.token_hex(10)
    deadline = min(now + timedelta(minutes=20), window.last_inbound + timedelta(hours=24))
    session.add(FlowOffer(token=token, patient_code="SYSTEM-CHECK", recipient=recipient,
                          appointment_ids="[]", snapshot="[]", state="PENDING", created_at=now, expires_at=deadline))
    session.flush()
    FlowTest._job(session, "deployment-check:" + token, "TEXT", {
        "token": token, "recipient": recipient, "expected_state": "PENDING",
        "send_before": int(deadline.replace(tzinfo=timezone.utc).timestamp()),
        "text": "DEPLOYMENT CHECK ONLY - no appointment is being booked or changed. "
                f"Please reply with exactly: HELP {token}\nThis checks the new WhatsApp worker and reply handling.",
    })
    return token


def check_status(session, token):
    offer = session.get(FlowOffer, token)
    if not offer or offer.patient_code != "SYSTEM-CHECK" or offer.appointment_ids != "[]":
        raise ValueError("Not a diagnostic reference")
    jobs = []
    for job in session.scalars(select(FlowJob).where(FlowJob.created_at >= offer.created_at).order_by(FlowJob.id)):
        payload = json.loads(job.payload)
        if payload.get("token") == token:
            jobs.append({"id": job.id, "kind": job.kind, "status": job.status, "delivery": job.delivery,
                         "attempts": job.attempts, "accepted_at": job.accepted_at.isoformat() if job.accepted_at else None,
                         "error_code": job.error_code})
    reply_count = session.scalar(select(func.count()).select_from(FlowInbox).where(
        FlowInbox.patient_code == "SYSTEM-CHECK", FlowInbox.created_at >= offer.created_at,
        FlowInbox.reason == "Parent requested help", FlowInbox.text.ilike("HELP " + token)))
    return {"reference": token, "jobs": jobs, "help_reply_recorded": bool(reply_count)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--reference")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--restart", action="store_true", help="Restart only the worker after a completed diagnostic exchange and idle-queue check")
    args = parser.parse_args()
    environment, base = settings()
    recipient = environment.get("WHATSAPP_TEST_RECIPIENT", "").strip()
    business = environment.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    if not recipient.isdigit() or not business.isdigit():
        raise ValueError("No verified configured test recipient/business number")
    engine = create_engine(environment["MAIN_WORKFLOW_TEST_DATABASE_URL"], hide_parameters=True, pool_pre_ping=True,
                           connect_args={"connect_timeout": 15, "options": "-csearch_path=flow_test,pg_catalog"},
                           execution_options={"schema_translate_map": {None: "flow_test"}})
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    lock, _ = workflow_locks(engine, "flow_test")
    try:
        if args.send:
            with lock, sessions.begin() as session:
                token = queue_check(session, recipient, business)
            print(json.dumps({"queued": bool(token), "reference": token,
                              "needs_fresh_START": token is None, "recipient_last4": recipient[-4:]}))
            return
        if args.reference:
            restart_result = None
            if args.restart:
                with sessions() as session:
                    before = check_status(session, args.reference)
                if not before["help_reply_recorded"] or len(before["jobs"]) != 2 or any(
                        job["status"] != "SENT" or job["attempts"] != 1 for job in before["jobs"]):
                    raise ValueError("Complete the diagnostic exchange before a restart check")
                worker = "orscheduler-whatsapp-worker"
                replicas_before = azure("containerapp", "replica", "list", "--name", worker, "--resource-group", GROUP)
                old_names = {row["name"] for row in replicas_before}
                old_restarts = sum(container.get("restartCount", 0) for row in replicas_before
                                   for container in row.get("properties", {}).get("containers", []))
                with WorkflowLock(engine, "sender", "flow_test"), WorkflowLock(engine, "ack_sender", "flow_test"):
                    with lock, sessions() as session:
                        busy = session.scalar(select(func.count()).select_from(FlowJob).where(
                            FlowJob.status.in_(("QUEUED", "PROCESSING", "RETRY_WAIT"))))
                        if busy:
                            raise ValueError("Queue is busy; worker was not restarted")
                    instance = azure("containerapp", "show", "--name", worker, "--resource-group", GROUP)
                    revision = instance["properties"]["latestReadyRevisionName"]
                    # The workflow lock is released before this request, so the
                    # replacement can initialize while outbound lanes are held.
                    azure("containerapp", "revision", "restart", "--name", worker,
                          "--revision", revision, "--resource-group", GROUP)
                restart_verified = False
                for _ in range(12):
                    try:
                        current = azure("containerapp", "replica", "list", "--name", worker, "--resource-group", GROUP)
                    except RuntimeError:
                        # A replica-list request can fail during replacement;
                        # retry only inspection, never the restart operation.
                        time.sleep(2)
                        continue
                    names = {row["name"] for row in current}
                    restarted = sum(container.get("restartCount", 0) for row in current
                                    for container in row.get("properties", {}).get("containers", []))
                    ready = current and all(row.get("properties", {}).get("runningState") == "Running" and
                        all(container.get("ready") for container in row.get("properties", {}).get("containers", []))
                        for row in current)
                    if ready and (names != old_names or restarted > old_restarts):
                        restart_verified = True
                        break
                    time.sleep(2)
                if not restart_verified:
                    raise RuntimeError("Restart was requested but new ready worker was not verified; inspect without repeating restart")
                with sessions() as session:
                    after = check_status(session, args.reference)
                if [(job["id"], job["attempts"], job["status"]) for job in before["jobs"]] != [
                        (job["id"], job["attempts"], job["status"]) for job in after["jobs"]]:
                    raise RuntimeError("Diagnostic queue state changed after restart; inspect before any further sends")
                restart_result = {"worker_replaced_and_ready": True, "diagnostic_messages_not_requeued": True,
                                  "website_http_after_restart": request(base, "/")[0]}
            with lock, sessions.begin() as session:
                result = check_status(session, args.reference)
                if restart_result:
                    result["restart_check"] = restart_result
                if args.close:
                    session.get(FlowOffer, args.reference).state = "CLOSED"
                    session.get(FlowOffer, args.reference).expires_at = None
                    for item in session.scalars(select(FlowInbox).where(FlowInbox.patient_code == "SYSTEM-CHECK",
                            FlowInbox.text.ilike("HELP " + args.reference))):
                        item.resolved = 1
            print(json.dumps(result))
            return
        results = {"website_http": request(base, "/")[0], "worker_config_js_http": request(base, "/main-workflow-config.js")[0]}
        key = environment.get("MAIN_WORKFLOW_ADMIN_TOKEN") or environment.get("FLOW_TEST_ADMIN_TOKEN")
        day = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        status, body = request(base, "/test/api/state?" + urlencode({"date": day}), key)
        state = json.loads(body)
        results.update(state_http=status, mode=state.get("mode"), sending_paused=state.get("sending_paused"))
        verify_token = environment.get("WHATSAPP_VERIFY_TOKEN")
        if verify_token:
            status, body = request(base, "/webhooks/whatsapp?" + urlencode({"hub.mode": "subscribe",
                "hub.verify_token": verify_token, "hub.challenge": "deployment-health-check"}))
            results["webhook_verification"] = status == 200 and body == b"deployment-health-check"
        with sessions() as session:
            results["queue_status_counts"] = {row[0]: row[1] for row in session.execute(
                select(FlowJob.status, func.count()).group_by(FlowJob.status))}
            results["outbound_delivery_counts"] = {str(row[0]): row[1] for row in session.execute(
                select(FlowJob.delivery, func.count()).where(FlowJob.kind.in_(("OFFER", "TEXT"))).group_by(FlowJob.delivery))}
            window = session.get(FlowContactWindow, (recipient, business))
            results["test_recipient_has_open_window"] = bool(window and window.last_inbound > datetime.utcnow() - timedelta(hours=24) + timedelta(minutes=5))
            results["recipient_last4"] = recipient[-4:]
            control = session.execute(text("SELECT state FROM whatsapp_dispatch.whatsapp_dispatch_state WHERE phone_id=:phone"), {"phone": business}).scalar()
            saved = json.loads(control) if control else {}
            results["provider_circuit_open"] = saved.get("open_until", 0) > time.time()
        print(json.dumps(results))
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("Live check stopped (" + type(exc).__name__ + "); no private response or credentials printed") from None

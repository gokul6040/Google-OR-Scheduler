"""Staff "Refresh from Zoho": run the nightly Thanal import now, in the background.

Same fetch and same patient rules as the 00:00 job (main_workflow.thanal_sync):
new complete children start active, inactive children are never switched on, an
active child with a broken latest plan keeps the previous plan. It never sends a
message or changes bookings. One refresh at a time, a short cool-down because
every run uses Zoho API calls, and none around midnight when the nightly job runs.
"""

from datetime import datetime, time as clock, timedelta, timezone
import json
import threading
from typing import Any
from zoneinfo import ZoneInfo

STATUS_KEY = "zoho_manual_refresh"
COOLDOWN = timedelta(minutes=5)
STALE = timedelta(minutes=20)  # A run older than this (e.g. app restarted mid-run) no longer blocks.
IST = ZoneInfo("Asia/Kolkata")
_start_lock = threading.Lock()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def near_nightly_import(now: datetime) -> bool:
    """The 00:00 IST import must not race a manual refresh for the same checkpoint."""
    local = now.astimezone(IST).time()
    return local >= clock(23, 45) or local < clock(0, 30)


def read_status(flow: Any) -> dict:
    from flow_test.models import FlowSetting

    with flow.sessions() as session:
        row = session.get(FlowSetting, STATUS_KEY)
        return json.loads(row.value) if row else {"state": "NEVER"}


def _write(flow: Any, value: dict) -> None:
    from flow_test.models import FlowSetting

    with flow.sessions.begin() as session:
        row = session.get(FlowSetting, STATUS_KEY)
        if row is None:
            session.add(FlowSetting(key=STATUS_KEY, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)


def start_refresh(instance: Any, *, now: datetime | None = None, run: bool = True) -> dict:
    """Validate and record a new refresh, then run it on a background thread."""
    from main_workflow.thanal_sync import enabled

    flow = instance.flow
    if flow.is_test or not enabled(instance.config):
        raise ValueError("Refresh from Zoho is available only for the Thanal source")
    now = now or datetime.now(timezone.utc)
    if near_nightly_import(now):
        raise ValueError("The nightly import runs around midnight. Try again after 12:30 AM.")
    with _start_lock:
        status = read_status(flow)
        started = _parse(status.get("startedAt"))
        if status.get("state") == "RUNNING" and started and now - started < STALE:
            raise ValueError("A refresh is already running. Please wait for it to finish.")
        if started and now - started < COOLDOWN:
            minutes = max(1, int((COOLDOWN - (now - started)).total_seconds() // 60) + 1)
            raise ValueError(f"Data was refreshed just now. Please wait {minutes} more minute(s) before refreshing again.")
        record = {"state": "RUNNING", "startedAt": now.isoformat(timespec="seconds"), "by": flow.admin_id}
        _write(flow, record)
    if run:
        threading.Thread(target=run_refresh, args=(instance,), name="zoho-refresh", daemon=True).start()
    return record


def run_refresh(instance: Any) -> dict:
    """Fetch every report and apply it exactly like the nightly import; record the outcome."""
    from sqlalchemy import select

    from database.models import Patient
    from flow_test.models import FlowSetting
    from flow_test.zoho_import import CHECKPOINT_KEY
    from main_workflow.thanal_sync import apply_snapshot
    from services.patient_activation_service import activation_review

    flow = instance.flow
    record = read_status(flow)
    try:
        with flow.sessions() as session:
            checkpoint = session.get(FlowSetting, CHECKPOINT_KEY)
            previous = json.loads(checkpoint.value) if checkpoint else None
            before = {p.patient_code: p.active for p in session.scalars(select(Patient))}
        until = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        reports = instance.provider.fetch(previous, until)
        result = apply_snapshot(flow, reports, window={"from": previous, "to": until})
        with flow.sessions() as session:
            patients = list(session.scalars(select(Patient)))
            new_active = sorted(p.patient_code for p in patients if p.patient_code not in before and p.active)
            ready = sorted(p.patient_code for p in patients
                           if not p.active and activation_review(session, p)["canActivate"])
        record.update(state="DONE", finishedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"), summary={
            "status": result.get("status"), "newActive": new_active, "newInactive": result.get("newInactive", []),
            "readyToActivate": ready, "stillNeedFixing": len(result.get("quarantined") or []),
            "keptOnPreviousPlan": result.get("keptOnPreviousPlan", []), "messagesSent": 0})
    except Exception as exc:  # Report safely; Zoho/database details can contain credentials.
        message = str(exc) if isinstance(exc, ValueError) else "The refresh could not finish. Nothing was changed; try again later."
        record.update(state="FAILED", finishedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"), error=message)
    _write(flow, record)
    return record

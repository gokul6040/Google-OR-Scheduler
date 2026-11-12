"""Unattended night step: plan tomorrow, check it and save it. Never sends a message.

Run as ``python -m main_workflow.daily_plan`` from the 02:00 IST job. Saving queues
tomorrow's offers exactly like the staff Save button; the worker sends them from
09:00 IST only in timed-template mode, so live messaging without it is refused.
Sunday is closed, so the Saturday-night run (planning Sunday) does nothing.
A run that already saved tomorrow does nothing; a failed check saves nothing and
leaves a staff alert.
"""

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from typing import Any

MARKER = "auto_daily_plan"


def _alert_key(target: date) -> str:
    return f"auto-plan:{target.isoformat()}"


def already_saved(flow: Any, target: date) -> dict | None:
    from flow_test.models import FlowSetting

    with flow.sessions() as session:
        marker = session.get(FlowSetting, MARKER)
        value = json.loads(marker.value) if marker else {}
    return value if value.get("date") == target.isoformat() else None


def plan_and_save(flow: Any, target: date, *, settings=None, attempts: int = 2) -> dict:
    """Plan and save one day like the staff Save button; safe to run again."""
    from flow_test.models import FlowSetting
    from services.therapy_schedule_template import scheduled_template_enabled

    if target.weekday() == 6:
        return {"status": "SKIPPED", "date": target.isoformat(), "reason": "Sunday is closed"}
    if target != flow.next_offer_date():
        raise ValueError("The night plan only saves tomorrow (Asia/Kolkata)")
    if flow.live and not scheduled_template_enabled(flow):
        raise ValueError("Live messaging is on without timed templates; saving now would send "
                         "offers immediately instead of at 09:00. Nothing was saved.")
    previous = already_saved(flow, target)
    if previous:
        return {"status": "ALREADY_SAVED", **previous}
    reason = "The plan did not pass its checks"
    for _ in range(attempts):
        result, _pinned = flow.preview_daily(target, None, settings=settings or flow.solver_settings)
        day = replace(result, appointments=[item for item in result.appointments if item.date == target])
        try:
            saved = flow.save_calendar(day, target, expected_offer_on_save=True)
        except ValueError as exc:
            # Usually a parent reply or import changed the inputs mid-search: plan again once.
            reason = str(exc)
            continue
        record = {"date": target.isoformat(), "savedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "added": saved["added"], "offers": saved["offers"], "complete": saved["complete"]}
        with flow.lock, flow.sessions.begin() as session:
            marker = session.get(FlowSetting, MARKER)
            if marker is None:
                session.add(FlowSetting(key=MARKER, value=json.dumps(record)))
            else:
                marker.value = json.dumps(record)
            if not saved["complete"]:
                flow._job_warning(session, _alert_key(target), "Night plan saved — needs review", saved["message"])
        return {"status": "SAVED", **record, "message": saved["message"]}
    raise ValueError(reason)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Plan tomorrow and report, without saving")
    args = parser.parse_args()
    from database.db import engine
    from main_workflow.config import WorkflowConfig
    from main_workflow.service import MainWorkflow

    flow = MainWorkflow(engine, WorkflowConfig.from_environment())

    def refuse(*_args, **_kwargs):
        raise RuntimeError("The night plan never sends messages")

    flow.send = refuse  # Offers are only queued; the worker sends them at 09:00 IST.
    target = flow.next_offer_date()
    try:
        if args.check:
            result, _ = flow.preview_daily(target, None)
            visits = [item for item in result.appointments if item.date == target]
            print(json.dumps({"mode": "CHECK ONLY", "date": target.isoformat(), "status": result.status,
                              "visits": len(visits), "children": len({item.patient_id for item in visits}),
                              "passedChecks": not any(result.validation.values())}))
            return
        print(json.dumps(plan_and_save(flow, target)))
    except ValueError as exc:
        with flow.lock, flow.sessions.begin() as session:
            flow._job_warning(session, _alert_key(target), "Night plan failed — nothing saved",
                              f"{target.isoformat()}: {exc} Generate and save tomorrow's plan in the app before 09:00.")
        print(f"Night plan stopped for {target.isoformat()}: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

"""Review and save the main workflow's rolling schedule as one operation."""

from datetime import datetime, timedelta
from dataclasses import replace
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from database.models import Appointment, AppointmentStatus
from database.repository import load_scheduling_problem, save_proposed_schedule
from services.rolling_schedule_service import generate_rolling_schedule, monday


def _fingerprint(flow, session, start, end):
    """Capture the saved inputs and released demand for every planned week."""
    weeks = []
    week = start
    while week <= end:
        problem = load_scheduling_problem(session, week, horizon_days=6)
        weeks.append(flow._planning_fingerprint(session, problem))
        week += timedelta(days=7)
    released = [tuple(row) for row in session.execute(select(
        Appointment.id, Appointment.patient_id, Appointment.treatment_requirement_id,
        Appointment.date, Appointment.slot_index, Appointment.status,
    ).where(Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING,
            Appointment.date <= end).order_by(Appointment.id))]
    return hashlib.sha256(json.dumps({"weeks": weeks, "released": released}, default=str, sort_keys=True).encode()).hexdigest()


def generate_preview(instance, selected_date, seconds):
    """Prepare a rolling preview without saving appointments or sending messages."""
    import web_app

    flow = instance.flow
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    run_date = max(selected_date, today)
    start = monday(run_date)
    end = start + timedelta(days=19)
    with flow.lock, flow.sessions() as session:
        before = _fingerprint(flow, session, start, end)
        result, start, end = generate_rolling_schedule(session, run_date,
            settings=replace(flow.solver_settings, max_time_seconds=seconds))
        if before != _fingerprint(flow, session, start, end):
            raise ValueError("Scheduling inputs changed while planning. Generate a fresh preview.")
    tomorrow = flow.next_offer_date()
    offer_on_save = run_date <= tomorrow <= end and tomorrow.weekday() != 6
    preview_id = str(uuid4())
    instance.previews[preview_id] = {"result": result, "start": start,
        "horizon_days": (end - start).days + 1, "created": datetime.utcnow(),
        "planning_mode": "rolling", "planning_start": run_date,
        "fingerprint": before, "offer_on_save": offer_on_save}
    payload = web_app.serialize_preview(result, start, end)
    payload.update({"previewId": preview_id, "planningStart": run_date.isoformat(),
        "planningMode": "rolling", "autoOfferOnSave": offer_on_save,
        "liveMessaging": getattr(flow, "messaging_enabled", flow.live),
        "messagingPaused": flow.live and not getattr(flow, "messaging_enabled", flow.live)})
    return payload


def save_preview(instance, preview):
    """Save the reviewed horizon and tomorrow's offers atomically."""
    flow, result, start = instance.flow, preview["result"], preview["start"]
    if result.status not in {"OPTIMAL", "FEASIBLE"} or any(result.validation.values()):
        raise ValueError("This preview did not pass validation")
    end = start + timedelta(days=preview["horizon_days"] - 1)
    tomorrow = flow.next_offer_date()
    offer_on_save = preview["planning_start"] <= tomorrow <= end and tomorrow.weekday() != 6
    if offer_on_save != preview["offer_on_save"]:
        raise ValueError("The local date changed. Generate a fresh preview before saving.")
    with flow.lock, flow.sessions.begin() as session:
        if _fingerprint(flow, session, start, end) != preview["fingerprint"]:
            raise ValueError("Bookings or scheduling inputs changed. Generate and review a fresh preview.")
        with sessionmaker(bind=session.connection(), join_transaction_mode="rollback_only",
                          expire_on_commit=False)() as saving_session:
            added = save_proposed_schedule(saving_session, result, start, preview["horizon_days"])
        offers = flow._publish(session, tomorrow) if offer_on_save else 0
    return {"message": f"Saved {added} proposed appointments across the rolling plan. "
            + (f"Queued {offers} WhatsApp visit offer(s) for tomorrow." if offer_on_save else "No WhatsApp offers were queued."),
            "added": added, "offers": offers, "offerDate": tomorrow.isoformat() if offers else None}

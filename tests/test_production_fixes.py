"""Production regression cases use only fictional records and disposable stores."""

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from config import SolverSettings
from database.models import (Appointment, AppointmentStatus, AuditLog, Department, Location,
                             Patient, Therapist, TreatmentRequirement)
from database.repository import (limit_new_bookings_to_day, load_daily_scheduling_problem,
                                 record_daily_planning_start, save_proposed_schedule)
from flow_test.models import FlowContactWindow, FlowInbox, FlowJob, FlowOffer
from flow_test.service import FlowTest
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.types import RequirementInput, ScheduledAppointment, SchedulingProblem
from services.whatsapp_dispatch import DispatchControl
from services import therapy_schedule_template as template


def test_first_week_never_invents_history_and_distributes_scarce_visits(db_session):
    start = date(2026, 11, 9)
    department, location = Department(name="Speech"), Location(name="Clinic", capacity=1)
    therapist = Therapist(therapist_code="T", name="Fictional therapist", department=department, location=location)
    db_session.add(therapist)
    db_session.flush()
    for index in range(100):
        patient = Patient(patient_code=f"FAKE-{index}", name="Fictional child")
        db_session.add(patient)
        db_session.flush()
        db_session.add(TreatmentRequirement(patient_id=patient.id, department_id=department.id,
            assigned_therapist_id=therapist.id, sessions_per_week=4, sequence_order=0))
    db_session.commit()
    for offset in range(6):
        target = start + timedelta(days=offset)
        problem = load_daily_scheduling_problem(db_session, target)
        assert {r.sessions_per_week for r in problem.requirements} == {4}
        limit_new_bookings_to_day(problem, target)
        result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=5))
        assert not any(result.validation.values())
        save_proposed_schedule(db_session, replace(result, appointments=[a for a in result.appointments if a.date == target]), target, 1)
        record_daily_planning_start(db_session, target)
        db_session.commit()
    counts = Counter(db_session.scalars(select(Appointment.patient_id)))
    assert len(counts) == 60
    assert max(counts.values()) == 1  # Previously the same ten children could win every day.
    assert db_session.scalar(select(AuditLog.entity).where(AuditLog.action == "daily_planning_started")) == start.isoformat()


def test_invalid_saved_booking_isolated_from_independent_centre():
    start = date(2026, 11, 9)
    problem = SchedulingProblem(start,
        [RequirementInput(1, 1, 1, 1, 1, 1, 0), RequirementInput(2, 2, 1, 2, 2, 1, 0)],
        {(t, day, slot) for t in (1, 2, 3) for day in range(6) for slot in range(10)},
        {(p, day, slot) for p in (1, 2) for day in range(6) for slot in range(10)},
        location_capacities={1: 1, 2: 1}, active_therapists={1, 2, 3},
        pinned=[ScheduledAppointment(1, 1, 3, 1, 1, start, 0, "invalid")],
        isolate_saved_failures=True)
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=2))
    assert result.status in {"FEASIBLE", "OPTIMAL"}
    assert result.statistics["blocked_locations"] == [1]
    assert any(a.patient_id == 2 for a in result.appointments)
    assert [a for a in result.appointments if a.location_id == 1] == problem.pinned


def test_decline_commits_before_repair_and_survives_repair_failure(tmp_path, legacy_flow_scenario, monkeypatch):
    flow = FlowTest(f"sqlite:///{tmp_path / 'decline.sqlite'}", scenario=legacy_flow_scenario)
    flow.fill_released_after_decline = True  # Opt-in automatic fill; this test covers its failure path.
    target = date(2026, 10, 19)
    try:
        flow.generate(target)
        flow.publish(target)
        flow.drain()
        offer = next(o for o in flow.state(target)["offers"] if o["patient"] == "TEST-B")
        flow.simulate(offer["token"], "NO")
        assert flow.process_events() == 1
        with flow.sessions() as session:
            assert session.get(FlowOffer, offer["token"]).state == "DECLINED"
            assert session.scalar(select(FlowJob).where(FlowJob.kind == "REPAIR")).status == "QUEUED"
        acquired = []
        def repair_failure(*_):
            from threading import Thread
            def check():
                locked = flow.lock.acquire(timeout=0.5)
                acquired.append(locked)
                if locked:
                    flow.lock.release()
            thread = Thread(target=check)
            thread.start()
            thread.join(timeout=1)
            raise RuntimeError("fictional solver failure")
        monkeypatch.setattr("flow_test.service.fill_released_openings", repair_failure)
        assert flow.process_repairs()
        assert acquired == [True]  # Slow repair leaves sending/replies free.
        with flow.sessions() as session:
            assert session.get(FlowOffer, offer["token"]).state == "DECLINED"
            assert all(a.status == AppointmentStatus.NEEDS_RESCHEDULING for a in session.scalars(
                select(Appointment).join(Patient).where(Patient.patient_code == "TEST-B")))
            assert session.scalar(select(FlowInbox.id).where(FlowInbox.reason == "Replacement search needs review"))
    finally:
        flow.engine.dispose()


def test_rolling_budget_shared_and_acknowledgments_not_blocked(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    monkeypatch.setenv("WHATSAPP_UNIQUE_PARENTS_PER_24H", "1")
    monkeypatch.setenv("WHATSAPP_DISPATCH_DATABASE_URL", f"sqlite:///{tmp_path / 'budget.sqlite'}")
    engine = create_engine("sqlite:///:memory:")
    first, second = DispatchControl(engine), DispatchControl(engine)
    now = [1000.0]
    monkeypatch.setattr("services.whatsapp_dispatch.time.time", lambda: now[0])
    try:
        ticket = first.reserve("business", "parent-a", "one")
        assert ticket
        first.outcome("business", "parent-a", ticket, None)
        now[0] += 7
        assert second.reserve("business", "parent-b", "two") is None
        assert second.summary("business")["budget_remaining"] == 0
        assert second.reserve("business", "parent-a", "ack", priority=True)
        now[0] += 86401
        assert first.reserve("business", "parent-b", "three")
    finally:
        first.engine.dispose()
        second.engine.dispose()
        engine.dispose()


@pytest.mark.parametrize("window_open", [True, False])
def test_same_day_template_uses_dated_session_or_visible_contact_hold(monkeypatch, window_open):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 9, 8, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(tz)
    monkeypatch.setattr(template, "datetime", Clock)
    warnings = []
    flow = SimpleNamespace(transport=SimpleNamespace(mode="template", profile=template.PROFILE, timed_schedule=False),
        _session_deadline=lambda *_: 9999999999 if window_open else 0,
        _attention=lambda *args: warnings.append(args))
    job = SimpleNamespace(attempts=1)
    payload = {"template_profile": template.PROFILE, "recipient": "fictional", "fields": {"date": "2026-11-09"}}
    assert template.prepare_template_job(flow, None, job, payload) == window_open
    if window_open:
        assert payload["session_override"]
    else:
        assert job.status == "WAITING_FOR_CONTACT" and warnings


def test_zoho_budget_shared_across_reads_and_writes_and_restart(tmp_path, monkeypatch):
    from services.zoho_budget import call_budget
    monkeypatch.setenv("ZOHO_CALL_BUDGET_FILE", str(tmp_path / "budget.json"))
    now, starts = [1000.0], []
    monkeypatch.setattr("services.zoho_budget.time.time", lambda: now[0])
    monkeypatch.setattr("services.zoho_budget.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    for suffix in ("report/Plan_Report", "form/Scheduler", "report/Scheduler_Report"):
        with call_budget("http://localhost/creator/v2.1/data/owner/app/" + suffix):
            starts.append(now[0])
    assert starts == [1000.0, 1001.25, 1002.5]


def test_completed_history_survives_a_later_therapist_absence(base_problem):
    from dataclasses import replace
    from scheduling.schedule_validator import validate_schedule
    start = date(2026, 9, 7)
    problem = base_problem([RequirementInput(1, 1, 1, 1, 1, 2, 0)])
    completed = ScheduledAppointment(1, 1, 1, 1, 1, start, 0, "completed", status="COMPLETED")
    problem.pinned = [completed]
    problem.absences = {(1, start, 0)}
    assert not validate_schedule(problem, [completed])[0]
    # A new booking in that same absence is still rejected.
    assert validate_schedule(problem, [replace(completed, status="PROPOSED")])[1]["absence_conflicts"] == 1


def test_excess_offers_wait_for_staff_and_only_resolution_requeues(tmp_path, monkeypatch, workflow_clock, legacy_flow_scenario):
    monkeypatch.setenv("WHATSAPP_UNIQUE_PARENTS_PER_24H", "1")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "fictional-business")
    monkeypatch.setenv("FLOW_TEST_MESSAGE_MODE", "session")
    monkeypatch.setenv("WHATSAPP_DISPATCH_DATABASE_URL", f"sqlite:///{tmp_path / 'dispatch.sqlite'}")
    target = date(2030, 1, 7)
    workflow_clock(target - timedelta(days=1), 8)
    calls = []
    recipients = {f"TEST-{letter}": f"91999999999{index}" for index, letter in enumerate("ABC", 1)}
    flow = FlowTest(f"sqlite:///{tmp_path / 'offers.sqlite'}", scenario=legacy_flow_scenario,
                    recipients=recipients, send=lambda kind, payload: calls.append(payload) or "dummy-message")
    try:
        flow.generate(target)
        flow.publish(target)
        with flow.sessions.begin() as session:
            for recipient in recipients.values():
                session.add(FlowContactWindow(recipient=recipient, business_id="fictional-business",
                                              last_inbound=datetime(2030, 1, 6, 2, 30)))
        flow.live = True
        flow._dispatch_batch(5)
        assert len(calls) == 1
        with flow.sessions() as session:
            held = list(session.scalars(select(FlowJob).where(FlowJob.status == "NEEDS_ATTENTION")))
            assert len(held) == 2
            inbox = session.scalar(select(FlowInbox).where(FlowInbox.reason == "WhatsApp allowance reached"))
            inbox_id, job_id = inbox.id, held[0].id
        with pytest.raises(ValueError, match="allowance is still exhausted"):
            flow.resolve(inbox_id)
        workflow_clock(target, 8)
        # Budget expiry alone does not release offers. Staff must act.
        with flow.sessions() as session:
            assert session.get(FlowJob, job_id).status == "NEEDS_ATTENTION"
        flow.resolve(inbox_id)
        with flow.sessions() as session:
            resolved_job = session.scalar(select(FlowJob).where(FlowJob.event_key == inbox.event_key.split(":", 2)[1]))
            assert resolved_job.status == "QUEUED"
    finally:
        flow.engine.dispose()
        flow.dispatch_control.engine.dispose()

"""Centre budgets and background previews; fictional SQLite only, no providers."""

from datetime import date
import json
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from config import SOLVER_SETTINGS, SolverSettings
from database.models import Appointment
from flow_test.models import FlowJob, FlowOffer
from flow_test.planning_tasks import preview_status, runner, submit_preview
from flow_test.preview_store import PreviewStore
from flow_test.service import FlowTest
from scheduling import cp_sat_scheduler as planner
from tests.conftest import req


def test_independent_centres_each_receive_five_minutes_and_return_early(base_problem, monkeypatch):
    problem = base_problem([req(1, 1, 1, 1, frequency=1, location=1),
                            req(2, 2, 2, 2, frequency=3, location=2)], capacities={1: 1, 2: 1})
    real_solve = planner.cp_model.CpSolver.Solve
    limits = []

    def solve(solver, model):
        limits.append(solver.parameters.max_time_in_seconds)
        return real_solve(solver, model)

    monkeypatch.setattr(planner.cp_model.CpSolver, "Solve", solve)
    result = planner.generate_weekly_schedule(problem)
    assert limits == [300, 300]  # Unequal demand must not divide either budget.
    assert result.status == "OPTIMAL"
    assert len(result.appointments) == 4
    assert result.statistics["wall_time_seconds"] < 10
    assert [c["search_limit_seconds"] for c in result.statistics["centres"]] == [300, 300]


@pytest.mark.parametrize("shared", ["patient", "therapist"])
def test_shared_resources_are_solved_together(base_problem, monkeypatch, shared):
    second = req(2, 1 if shared == "patient" else 2, 2,
                 1 if shared == "therapist" else 2, location=2)
    problem = base_problem([req(1, 1, 1, 1, location=1), second], capacities={1: 1, 2: 1})
    assert planner.location_partitions(problem) is None
    result = planner.generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=1))
    assert result.statistics["strategy"] == "cp_sat"
    assert not any(result.validation.values())
    assert len(result.appointments) == 2


def test_large_short_search_still_optimizes_patient_preference(base_problem):
    requirements = [req(i, i, 1, i) for i in range(1, 101)]
    problem = base_problem(requirements, capacities={1: 100})
    # Many requirements still exercise the old size threshold, but only the
    # first consultant is active, keeping the preference optimum deterministic.
    problem.active_therapists = {1}
    problem.patient_preferences[(1, 4, 9)] = 100
    result = planner.generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=2))
    assert result.statistics["strategy"] == "cp_sat"
    assert len(result.appointments) == 1
    preferred = next(a for a in result.appointments if a.patient_id == 1)
    assert (preferred.date.weekday(), preferred.slot_index) == (4, 9)
    assert not any(result.validation.values())


def test_timeout_keeps_valid_incumbent_and_unknown_is_not_saveable(base_problem, monkeypatch):
    problem = base_problem([req(1, 1, 1, 1)])
    real_solve = planner.cp_model.CpSolver.Solve

    def feasible(solver, model):
        real_solve(solver, model)
        return planner.cp_model.FEASIBLE

    monkeypatch.setattr(planner.cp_model.CpSolver, "Solve", feasible)
    result = planner.generate_weekly_schedule(problem)
    assert result.status == "FEASIBLE" and len(result.appointments) == 1
    assert not any(result.validation.values())
    monkeypatch.setattr(planner.cp_model.CpSolver, "Solve", lambda *_: planner.cp_model.UNKNOWN)
    result = planner.generate_weekly_schedule(problem)
    assert result.status == "UNKNOWN" and not result.appointments


@pytest.fixture
def planning_instance(tmp_path, legacy_flow_scenario):
    flow = FlowTest(f"sqlite:///{tmp_path / 'planning.sqlite'}", scenario=legacy_flow_scenario)
    flow.next_offer_date = lambda: date(2026, 10, 22)
    instance = SimpleNamespace(flow=flow, token="test-key")
    instance.previews = PreviewStore(flow)
    yield instance
    if hasattr(instance, "planning_runner"):
        thread = instance.planning_runner.thread
        if thread:
            thread.join(timeout=10)
            assert not thread.is_alive()
    flow.engine.dispose()


def test_background_preview_frees_lock_and_stale_save_is_rejected(planning_instance, monkeypatch):
    import flow_test.service as service
    from database.models import Patient

    instance, entered, release = planning_instance, Event(), Event()
    original = service.generate_weekly_schedule

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "generate_weekly_schedule", blocked)
    job = submit_preview(instance, instance.flow.next_offer_date(), None, SolverSettings(max_time_seconds=1))
    try:
        assert entered.wait(3)
        # Read status using a second app copy while the solver is still busy.
        other_flow = FlowTest(str(instance.flow.engine.url), scenario=instance.flow.scenario)
        other = SimpleNamespace(flow=other_flow, previews=PreviewStore(other_flow))
        assert preview_status(other, job["jobId"])["status"] == "PROCESSING"
        assert instance.flow.lock.acquire(timeout=0.5)
        try:
            with instance.flow.sessions.begin() as session:
                patient = session.scalar(select(Patient).order_by(Patient.id))
                patient.active = False
            instance.flow.state(instance.flow.next_offer_date())
        finally:
            instance.flow.lock.release()
    finally:
        release.set()
    runner(instance).thread.join(5)
    result = preview_status(instance, job["jobId"])
    assert result["status"] == "DONE", result
    assert all(j["kind"] != "PLANNING" for j in instance.flow.state(instance.flow.next_offer_date())["jobs"])
    preview = instance.previews[result["preview"]["previewId"]]
    with pytest.raises(ValueError, match="changed after this preview"):
        instance.flow.save_calendar(preview["result"], preview["start"], expected_offer_on_save=True)
    with instance.flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Appointment)) == 0
        assert session.scalar(select(func.count()).select_from(FlowOffer)) == 0
    if hasattr(other, "planning_runner"):
        other.planning_runner.thread.join(5)
    other_flow.engine.dispose()


def test_interrupted_planning_job_is_recomputed_not_marked_unknown(planning_instance):
    from dataclasses import asdict
    instance = planning_instance
    with instance.flow.sessions.begin() as session:
        job = FlowJob(event_key="planning:interrupted", kind="PLANNING", status="PROCESSING",
                      locked_by="old-process", payload=json.dumps({"date": "2026-10-22", "patients": None,
                          "settings": asdict(SolverSettings(max_time_seconds=1))}))
        session.add(job)
        session.flush()
        ident = job.id
    with instance.flow.lock, instance.flow.sessions.begin() as session:
        instance.flow._recover_abandoned(session)
    assert preview_status(instance, ident)["status"] == "PROCESSING"
    runner(instance).thread.join(5)
    assert preview_status(instance, ident)["status"] == "DONE"


def test_background_api_requires_auth_and_production_uses_300_seconds(planning_instance, monkeypatch):
    from flow_test import mount
    from tests.test_flow_test_mount import request
    import flow_test.planning_tasks as tasks

    monkeypatch.setattr(mount, "runtime", lambda: planning_instance)
    planning_instance.flow.is_test = False
    captured = {}
    monkeypatch.setattr(tasks, "submit_preview", lambda instance, day, patients, settings:
                        captured.update(seconds=settings.max_time_seconds) or {"jobId": 123, "status": "QUEUED"})
    body = {"start": "2026-10-22", "searchSeconds": 20}
    assert request("/test/api/schedules/generate", body, token="wrong")[0] == 401
    status, raw = request("/test/api/schedules/generate", body)
    assert status == 202 and json.loads(raw)["jobId"] == 123
    assert captured["seconds"] == SOLVER_SETTINGS.max_time_seconds == 300
    assert request("/test/api/schedules/jobs/123", token="wrong")[0] == 401
    assert request("/test/api/schedules/jobs/123")[0] == 404


def test_pending_preview_deduplicates_only_unchanged_inputs(planning_instance, monkeypatch):
    from database.models import Patient
    instance = planning_instance
    consumer = runner(instance)
    monkeypatch.setattr(consumer, "start", lambda: None)
    settings = SolverSettings(max_time_seconds=1)
    first = submit_preview(instance, instance.flow.next_offer_date(), None, settings)
    assert submit_preview(instance, instance.flow.next_offer_date(), None, settings)["jobId"] == first["jobId"]
    with instance.flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).order_by(Patient.id))
        patient.active = False
    second = submit_preview(instance, instance.flow.next_offer_date(), None, settings)
    assert second["jobId"] != first["jobId"]


def test_preview_failure_is_visible_without_leaking_internal_errors(planning_instance, monkeypatch):
    import flow_test.shared_ui as ui
    def fail(*_):
        raise RuntimeError("sensitive internal connection detail")
    monkeypatch.setattr(ui, "build_daily_preview", fail)
    task = submit_preview(planning_instance, planning_instance.flow.next_offer_date(), None,
                          SolverSettings(max_time_seconds=1))
    runner(planning_instance).thread.join(5)
    result = preview_status(planning_instance, task["jobId"])
    assert result["status"] == "FAILED"
    assert "sensitive" not in result["error"]
    assert "fresh preview" in result["error"]

"""Unattended night jobs: 02:00 plan-and-save and the 23:00 / 07:00 push targets.

Fictional data only. No message is sent and no external system is called.
"""

from datetime import date, datetime as real_datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from database.models import Appointment
from flow_test.models import FlowInbox, FlowJob, FlowOffer, FlowSetting
from flow_test.service import FlowTest
from main_workflow import daily_plan

MONDAY = date(2026, 10, 19)
SUNDAY = date(2026, 10, 25)


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'night.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    flow.next_offer_date = lambda: MONDAY
    yield flow
    flow.engine.dispose()


def counts(flow):
    with flow.sessions() as session:
        return (session.scalar(select(func.count(Appointment.id))),
                session.scalar(select(func.count(FlowOffer.token))))


def test_night_plan_saves_tomorrow_once_and_only_queues_offers(flow):
    sent = []
    flow.send = lambda *args: sent.append(args)
    first = daily_plan.plan_and_save(flow, MONDAY)
    assert first["status"] == "SAVED" and first["added"] > 0 and first["offers"] > 0
    saved = counts(flow)
    again = daily_plan.plan_and_save(flow, MONDAY)
    assert again["status"] == "ALREADY_SAVED" and again["date"] == MONDAY.isoformat()
    assert counts(flow) == saved  # A rerun never books or offers twice.
    assert sent == []  # Offers are only queued; the worker sends them later.
    with flow.sessions() as session:
        assert session.scalar(select(func.count(FlowJob.id)).where(FlowJob.kind == "OFFER")) == first["offers"]
        assert session.get(FlowSetting, daily_plan.MARKER) is not None


def test_saturday_night_run_does_nothing_because_sunday_is_closed(flow):
    flow.next_offer_date = lambda: SUNDAY
    assert daily_plan.plan_and_save(flow, SUNDAY)["status"] == "SKIPPED"
    assert counts(flow) == (0, 0)


def test_live_messaging_without_timed_templates_is_refused_before_saving(flow):
    flow.live = True  # Session mode: saving would send at 02:00 instead of 09:00.
    with pytest.raises(ValueError, match="timed templates"):
        daily_plan.plan_and_save(flow, MONDAY)
    assert counts(flow) == (0, 0)


def test_only_tomorrow_can_be_saved(flow):
    with pytest.raises(ValueError, match="tomorrow"):
        daily_plan.plan_and_save(flow, date(2026, 10, 20))


def test_changed_inputs_are_planned_again_once(flow):
    original, calls = flow.save_calendar, []

    def changed_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("Bookings or scheduling inputs changed after this preview.")
        return original(*args, **kwargs)

    flow.save_calendar = changed_once
    assert daily_plan.plan_and_save(flow, MONDAY)["status"] == "SAVED"
    assert len(calls) == 2


def test_failed_checks_save_nothing(flow):
    def always_fails(*_args, **_kwargs):
        raise ValueError("This preview did not pass validation")

    flow.save_calendar = always_fails
    with pytest.raises(ValueError, match="did not pass"):
        daily_plan.plan_and_save(flow, MONDAY)
    assert counts(flow) == (0, 0)
    assert daily_plan.already_saved(flow, MONDAY) is None


def test_incomplete_save_alerts_staff(flow):
    flow.save_calendar = lambda *a, **k: {"added": 3, "offers": 0, "complete": False,
                                          "message": "INCOMPLETE — staff review needed."}
    assert daily_plan.plan_and_save(flow, MONDAY)["complete"] is False
    with flow.sessions() as session:
        alert = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"auto-plan:{MONDAY}"))
    assert alert.reason == "Night plan saved — needs review" and not alert.resolved


def test_command_failure_leaves_a_staff_alert_and_exits_nonzero(flow, monkeypatch):
    monkeypatch.setattr("main_workflow.service.MainWorkflow", lambda *_args, **_kwargs: flow)
    monkeypatch.setattr("main_workflow.config.WorkflowConfig.from_environment", lambda: None)
    monkeypatch.setattr(flow, "preview_daily", lambda *a, **k: (_ for _ in ()).throw(ValueError("Search failed")))
    monkeypatch.setattr("sys.argv", ["daily_plan"])
    with pytest.raises(SystemExit) as stopped:
        daily_plan.main()
    assert stopped.value.code == 1
    with flow.sessions() as session:
        alert = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"auto-plan:{MONDAY}"))
    assert alert.reason == "Night plan failed — nothing saved" and "Search failed" in alert.text
    assert counts(flow) == (0, 0)


@pytest.mark.parametrize("now,day,expected", [
    (real_datetime(2026, 11, 11, 23, 0), "tomorrow", "2026-11-12"),   # 23:00 push: tomorrow's visits
    (real_datetime(2026, 11, 12, 7, 0), "today", "2026-11-12"),       # 07:00 re-push: today's late changes
    (real_datetime(2026, 11, 14, 23, 0), "tomorrow", None),           # Saturday night: Sunday is closed
    (real_datetime(2026, 11, 15, 7, 0), "today", None),               # Sunday morning: nothing to push
])
def test_scheduled_push_targets_the_right_day(monkeypatch, capsys, now, day, expected):
    import main_workflow.push as cli

    class Clock:
        @staticmethod
        def now(tz=None):
            return now.replace(tzinfo=tz)

    calls = []
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "workflow_connection", lambda: (SimpleNamespace(admin_token="fictional"), None))
    monkeypatch.setattr(cli, "api", lambda config, operation, body=None, timeout=180:
                        calls.append((operation, body)) or {"pushed": 0})
    monkeypatch.setattr("sys.argv", ["push", "--day", day, "--apply"])
    cli.main()
    if expected is None:
        assert calls == [] and "SKIPPED" in capsys.readouterr().out
    else:
        assert calls == [("export/push", {"date": expected})]

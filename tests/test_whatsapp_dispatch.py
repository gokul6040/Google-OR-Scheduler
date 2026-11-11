"""Shared control/reply-lane tests use disposable storage and fictional messages."""

from datetime import date, datetime, timedelta
import json
from threading import Event, Thread
import time

import pytest
from sqlalchemy import create_engine, select

from flow_test.models import FlowJob
from flow_test.service import FlowTest
from services.whatsapp_dispatch import DispatchControl
from services.whatsapp_service import WhatsAppDeliveryError
from services.whatsapp_worker import embedded_worker_enabled, run_worker


@pytest.fixture
def controls(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_DISPATCH_DATABASE_URL", f"sqlite:///{tmp_path / 'control.sqlite'}")
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.sqlite'}")
    first, second = DispatchControl(engine), DispatchControl(engine)
    now = [1000.0]
    monkeypatch.setattr("services.whatsapp_dispatch.time.time", lambda: now[0])
    yield first, second, now
    first.engine.dispose()
    second.engine.dispose()
    engine.dispose()


def test_rate_is_shared_across_instances_and_survives_restart(controls):
    first, second, now = controls
    for index in range(5):
        assert (first if index % 2 else second).reserve("business", f"parent-{index}", str(index))
    assert second.reserve("business", "sixth-parent", "six") is None
    replacement = DispatchControl(first.engine)
    try:
        assert replacement.reserve("business", "seventh-parent", "seven") is None
        # Another business number has its own allowance.
        assert replacement.reserve("another-business", "sixth-parent", "six")
        now[0] += 1.01
        assert second.reserve("business", "sixth-parent", "six")
    finally:
        replacement.engine.dispose()


def test_parent_cooldown_and_inflight_are_shared(controls):
    first, second, now = controls
    ticket = first.reserve("business", "parent", "one")
    now[0] += 10
    assert second.reserve("business", "parent", "two") is None
    first.outcome("business", "parent", ticket, None)
    now[0] += 5.9
    assert second.reserve("business", "parent", "two") is None
    now[0] += 0.2
    assert second.reserve("business", "parent", "two")


def test_waiting_acknowledgment_gets_next_available_rate_slot(controls):
    first, second, now = controls
    for index in range(5):
        assert first.reserve("business", str(index), str(index))
    assert second.reserve("business", "reply", "reply", priority=True) is None
    now[0] += 1.001
    # Renew the pending-priority intent as the acknowledgment lane does.
    assert second.reserve("business", "reply", "reply", priority=True)
    assert first.reserve("business", "offer", "offer")


def fail_distinct(control, now):
    for index in range(3):
        ticket = control.reserve("business", f"parent-{index}", str(index))
        assert ticket
        control.outcome("business", f"parent-{index}", ticket,
                        WhatsAppDeliveryError("temporary", code=131016, rejected=True))


def test_outage_is_shared_then_one_probe_recovers(controls):
    first, second, now = controls
    fail_distinct(first, now)
    assert second.reserve("business", "waiting", "waiting") is None
    assert second.summary("business")["open_until"] == now[0] + 30
    now[0] += 30
    probe = second.reserve("business", "probe-parent", "probe")
    assert probe["probe"]
    assert first.reserve("business", "other-parent", "other") is None
    second.outcome("business", "probe-parent", probe, None)
    assert first.reserve("business", "other-parent", "other")
    assert first.summary("business")["open_until"] is None


def test_failed_probe_reopens_and_late_success_cannot_close_it(controls):
    first, second, now = controls
    late = first.reserve("business", "late-parent", "late")
    fail_distinct(first, now)
    first.outcome("business", "late-parent", late, None)
    assert second.reserve("business", "blocked", "blocked") is None
    now[0] += 30
    probe = second.reserve("business", "probe-parent", "probe")
    second.outcome("business", "probe-parent", probe, TimeoutError())
    assert first.reserve("business", "blocked", "blocked") is None
    # A crashed probe holds only its probe interval, never causes job resending.
    now[0] += 30
    assert second.reserve("business", "new-probe", "new-probe")["probe"]
    now[0] += 121
    assert first.reserve("business", "replacement-probe", "replacement")["probe"]


@pytest.mark.parametrize("code", [131026, 131056, 190, 132001])
def test_recipient_and_configuration_errors_do_not_trip_provider_outage(controls, code):
    first, second, now = controls
    for index in range(3):
        ticket = first.reserve("business", f"parent-{index}", str(index))
        first.outcome("business", f"parent-{index}", ticket,
                      WhatsAppDeliveryError("rejected", code=code, rejected=True))
    assert second.reserve("business", "another-parent", "another")
    assert first.summary("business")["open_until"] is None


def test_same_parent_temporary_errors_cannot_pause_everyone(controls):
    first, second, now = controls
    for index in range(4):
        ticket = first.reserve("business", "same-parent", str(index))
        first.outcome("business", "same-parent", ticket, TimeoutError())
        now[0] += 7
    assert second.reserve("business", "another-parent", "another")


def test_web_defaults_to_external_worker(monkeypatch):
    monkeypatch.delenv("WHATSAPP_WORKER_MODE", raising=False)
    assert not embedded_worker_enabled()
    monkeypatch.setenv("WHATSAPP_WORKER_MODE", "embedded")
    assert embedded_worker_enabled()
    monkeypatch.setenv("WHATSAPP_WORKER_MODE", "typo")
    with pytest.raises(ValueError):
        embedded_worker_enabled()


def test_replies_calendar_and_ack_lane_continue_during_slow_offers(tmp_path, legacy_flow_scenario):
    entered, release, acknowledged = Event(), Event(), Event()
    def send(kind, payload):
        if kind == "OFFER":
            entered.set()
            assert release.wait(10)
        else:
            acknowledged.set()
        return "dummy-" + payload["token"]
    flow = FlowTest(f"sqlite:///{tmp_path / 'lanes.sqlite'}", scenario=legacy_flow_scenario, send=send)
    day = date(2026, 10, 19)
    flow.generate(day)
    flow.publish(day)
    worker = Thread(target=lambda: flow._dispatch_batch(3, kinds=("OFFER",)))
    try:
        worker.start()
        assert entered.wait(5)
        # Calendar state and reply processing are no longer blocked by HTTP.
        state = flow.state(day)
        token = state["offers"][0]["token"]
        flow.simulate(token, "HELP")
        assert flow.process_events() == 1
        assert flow._dispatch_batch(1, kinds=("TEXT",), acknowledgments=True) == 1
        assert acknowledged.is_set() and not release.is_set()
        with flow.sessions() as session:
            assert session.scalar(select(FlowJob).where(FlowJob.kind == "INBOUND")).status == "DONE"
    finally:
        release.set()
        worker.join(10)
        flow.engine.dispose()


def test_early_delivery_receipt_waits_for_saved_provider_id(tmp_path, legacy_flow_scenario):
    flow = FlowTest(f"sqlite:///{tmp_path / 'early.sqlite'}", scenario=legacy_flow_scenario)
    try:
        day = date(2026, 10, 19)
        flow.generate(day)
        flow.publish(day)
        with flow.sessions.begin() as session:
            original = session.scalar(select(FlowJob).order_by(FlowJob.id))
            original.status, original.send_started_at = "PROCESSING", datetime.utcnow()
            recipient = json.loads(original.payload)["recipient"]
            original_id = original.id
            status = flow._job(session, "early-receipt", "STATUS",
                               {"id": "dummy-meta-id", "status": "delivered", "recipient_id": recipient})
            status_id = status.id
        assert flow.process_events() == 1
        with flow.sessions.begin() as session:
            assert session.get(FlowJob, status_id).status == "QUEUED"
            original = session.get(FlowJob, original_id)
            original.status, original.message_id = "SENT", "dummy-meta-id"
            session.get(FlowJob, status_id).next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        assert flow.process_events() == 1
        with flow.sessions() as session:
            assert session.get(FlowJob, original_id).delivery == "delivered"
    finally:
        flow.engine.dispose()


def test_acknowledgment_precedes_older_offer_backlog(tmp_path, legacy_flow_scenario):
    calls = []
    flow = FlowTest(f"sqlite:///{tmp_path / 'priority.sqlite'}", scenario=legacy_flow_scenario,
                    send=lambda kind, payload: calls.append(kind) or "dummy")
    try:
        day = date(2026, 10, 19)
        flow.generate(day)
        flow.publish(day)
        token = flow.state(day)["offers"][0]["token"]
        flow.simulate(token, "HELP")
        assert flow.process_one()  # The newer inbound job wins over old offers.
        assert flow.process_one()  # Its newer acknowledgment wins next.
        assert calls == ["TEXT"]
    finally:
        flow.engine.dispose()


def test_separate_worker_runs_all_lanes_and_stops_cleanly(tmp_path, legacy_flow_scenario, workflow_clock):
    day = date(2030, 1, 7)
    workflow_clock(day, 8)
    stopped, sent = Event(), Event()
    calls = []
    def send(kind, payload):
        calls.append(kind)
        if kind == "TEXT":
            sent.set()
        return "dummy-" + payload["token"]
    flow = FlowTest(f"sqlite:///{tmp_path / 'worker.sqlite'}", scenario=legacy_flow_scenario, send=send,
                    recipients={"TEST-A": "919999999991", "TEST-B": "919999999992", "TEST-C": "919999999993"})
    flow.generate(day)
    flow.publish(day)
    token = flow.state(day)["offers"][0]["token"]
    # Keep this parent's offer out of the bulk lane so the fixed test clock
    # does not intentionally hold its acknowledgment behind a six-second cooldown.
    with flow.sessions.begin() as session:
        for job in session.scalars(select(FlowJob)):
            if json.loads(job.payload).get("token") == token:
                job.status = "OBSOLETE"
    flow.simulate(token, "HELP")
    flow.live, flow.message_mode = True, "template"
    worker = Thread(target=run_worker, args=(flow, stopped))
    try:
        worker.start()
        assert sent.wait(10)
        assert "TEXT" in calls
        with flow.sessions() as session:
            assert session.scalar(select(FlowJob).where(FlowJob.kind == "INBOUND")).status == "DONE"
    finally:
        stopped.set()
        worker.join(10)
        assert not worker.is_alive()
        flow.dispatch_control.engine.dispose()
        flow.engine.dispose()


def test_control_unavailable_holds_unsent_job_without_attempt(tmp_path, legacy_flow_scenario, workflow_clock, monkeypatch):
    day = date(2030, 1, 7)
    workflow_clock(day, 8)
    calls = []
    flow = FlowTest(f"sqlite:///{tmp_path / 'unavailable.sqlite'}", scenario=legacy_flow_scenario,
                    send=lambda *_: calls.append(True) or "dummy")
    try:
        flow.generate(day)
        flow.publish(day)
        flow.live, flow.message_mode = True, "template"
        def unavailable(*args, **kwargs):
            raise OSError("fictional control outage")
        monkeypatch.setattr(flow.dispatch_control, "reserve", unavailable)
        monkeypatch.setattr(flow.dispatch_control, "reserve_with_reason", unavailable)
        assert flow.process_batch() == 0
        assert not calls
        with flow.sessions() as session:
            assert all(job.status == "QUEUED" and job.attempts == 0 and job.send_started_at is None
                       for job in session.scalars(select(FlowJob)))
    finally:
        flow.dispatch_control.engine.dispose()
        flow.engine.dispose()

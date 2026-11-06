"""The live check creates diagnostic queue rows, never clinical records."""

from datetime import date, datetime, timedelta
import json
import time

from sqlalchemy import select

from database.models import Appointment
from flow_test.models import FlowContactWindow, FlowOffer
from flow_test.service import FlowTest
from postman.checks.live_whatsapp_check import queue_check, check_status


def test_live_diagnostic_help_roundtrip_leaves_appointments_unchanged(tmp_path, legacy_flow_scenario, monkeypatch):
    calls = []
    flow = FlowTest(f"sqlite:///{tmp_path / 'diagnostic.sqlite'}", scenario=legacy_flow_scenario,
                    send=lambda kind, payload: calls.append((kind, payload["text"])) or f"dummy-{len(calls)}")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    try:
        flow.generate(date(2030, 1, 7))
        with flow.sessions.begin() as session:
            before = [(row.id, row.status) for row in session.scalars(select(Appointment))]
            session.add(FlowContactWindow(recipient="919999999991", business_id="123456789", last_inbound=datetime.utcnow()))
        with flow.lock, flow.sessions.begin() as session:
            token = queue_check(session, "919999999991", "123456789")
        assert token and flow.process_one()
        assert len(calls) == 1 and calls[0][0] == "TEXT" and "DEPLOYMENT CHECK ONLY" in calls[0][1]
        flow.receive({"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123456789"}, "messages": [{"id": "diagnostic-help",
                "from": "919999999991", "timestamp": str(int(time.time())), "text": {"body": "HELP " + token}}],
        }}]}]})
        assert flow.process_events() == 1
        assert flow.process_one()
        with flow.sessions() as session:
            assert [(row.id, row.status) for row in session.scalars(select(Appointment))] == before
            status = check_status(session, token)
            assert status["help_reply_recorded"]
            assert len(status["jobs"]) == 2 and all(job["attempts"] == 1 for job in status["jobs"])
        assert len(calls) == 2
        flow.state(date(2030, 1, 7))  # Empty diagnostic snapshot must not break the calendar state.
    finally:
        flow.engine.dispose()


def test_live_diagnostic_requires_genuine_recent_contact_window(tmp_path, legacy_flow_scenario):
    flow = FlowTest(f"sqlite:///{tmp_path / 'closed-window.sqlite'}", scenario=legacy_flow_scenario)
    try:
        with flow.sessions.begin() as session:
            assert queue_check(session, "919999999991", "123456789") is None
            session.add(FlowContactWindow(recipient="919999999991", business_id="123456789",
                                          last_inbound=datetime.utcnow() - timedelta(days=2)))
        with flow.sessions.begin() as session:
            assert queue_check(session, "919999999991", "123456789") is None
            assert not list(session.scalars(select(FlowOffer)))
    finally:
        flow.engine.dispose()

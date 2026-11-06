"""Opt-in local Postman HTTP fault verification, with only fake credentials.

Run the saved simulation, then set WHATSAPP_SIMULATION_PORT to its port.
Production code keeps the Meta endpoint fixed; this test-only adapter sends
the same request body through urllib to the loopback fixture.
"""

from datetime import date
import os
from urllib.request import Request, ProxyHandler, build_opener

import pytest
from sqlalchemy import select

from flow_test.models import FlowJob
from flow_test.service import FlowTest
from services import whatsapp_service


PORT = os.getenv("WHATSAPP_SIMULATION_PORT")
pytestmark = pytest.mark.skipif(not PORT, reason="Local Postman simulation not selected")


@pytest.mark.parametrize("mode, expected", [("temporary", "RETRY_WAIT"), ("expired", "BLOCKED"),
    ("invalid-template", "BLOCKED"), ("permanent", "NEEDS_ATTENTION"),
    ("gateway", "UNKNOWN"), ("timeout", "UNKNOWN"), ("success", "SIMULATED")])
def test_real_transport_failure_reaches_durable_workflow(tmp_path, legacy_flow_scenario, monkeypatch, mode, expected):
    assert PORT and PORT.isdigit()
    settings = whatsapp_service.WhatsAppSenderSettings("simulation-only", "123456789", "v26.0")
    opener = build_opener(ProxyHandler({}))
    def local_http(request, timeout):
        assert request.full_url == "https://graph.facebook.com/v26.0/123456789/messages"
        assert request.get_header("Authorization") == "Bearer simulation-only"
        local = Request(f"http://127.0.0.1:{PORT}/whatsapp/v26.0/123456789/messages",
                        data=request.data, headers={"Content-Type": "application/json"}, method="POST")
        return opener.open(local, timeout=0.25 if mode == "timeout" else timeout)
    monkeypatch.setattr(whatsapp_service, "urlopen", local_http)
    calls = []
    def send(*_):
        calls.append(True)
        return whatsapp_service.send_text_message(settings, "919999999999", mode)["messages"][0]["id"]
    flow = FlowTest(f"sqlite:///{tmp_path / 'http.sqlite'}", scenario=legacy_flow_scenario, send=send)
    try:
        day = date(2026, 10, 19)
        flow.generate(day)
        flow.publish(day)
        before = flow.state(day)["appointments"]
        assert flow.process_one()
        with flow.sessions() as session:
            job = session.scalar(select(FlowJob).order_by(FlowJob.id))
            assert job.status == expected
            if mode == "temporary":
                assert job.error_code == "130429" and job.next_attempt_at
        assert len(calls) == 1 and flow.state(day)["appointments"] == before
    finally:
        flow.engine.dispose()


@pytest.fixture
def live_http_queue(tmp_path, legacy_flow_scenario, monkeypatch, workflow_clock):
    """Real urllib transport, fictional future appointments and isolated controls."""
    assert PORT and PORT.isdigit()
    settings = whatsapp_service.WhatsAppSenderSettings("simulation-only", "123456789", "v26.0")
    opener = build_opener(ProxyHandler({}))
    def local_http(request, timeout):
        assert request.full_url == "https://graph.facebook.com/v26.0/123456789/messages"
        assert request.get_header("Authorization") == "Bearer simulation-only"
        local = Request(f"http://127.0.0.1:{PORT}/whatsapp/v26.0/123456789/messages",
                        data=request.data, headers={"Content-Type": "application/json"}, method="POST")
        return opener.open(local, timeout=timeout)
    monkeypatch.setattr(whatsapp_service, "urlopen", local_http)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", settings.phone_number_id)
    monkeypatch.setenv("WHATSAPP_DISPATCH_DATABASE_URL", f"sqlite:///{tmp_path / 'controls.sqlite'}")
    monkeypatch.setenv("WHATSAPP_SENDS_PER_SECOND", "5")
    mode, calls = ["success"], []
    def send(_, payload):
        calls.append(mode[0])
        return whatsapp_service.send_text_message(settings, payload["recipient"], mode[0])["messages"][0]["id"]
    flow = FlowTest(f"sqlite:///{tmp_path / 'queue.sqlite'}", scenario=legacy_flow_scenario,
                    recipients={"TEST-A": "919999999991", "TEST-B": "919999999992", "TEST-C": "919999999993"},
                    send=send)
    day = date(2030, 1, 7)
    workflow_clock(day, 8)
    flow.generate(day)
    flow.publish(day)
    before = flow.state(day)["appointments"]
    flow.live, flow.message_mode = True, "template"
    yield flow, mode, calls, day
    assert flow.state(day)["appointments"] == before
    flow.dispatch_control.engine.dispose()
    flow.engine.dispose()


def test_http_sends_obey_shared_rate_limit(live_http_queue, monkeypatch, workflow_clock):
    flow, mode, calls, day = live_http_queue
    monkeypatch.setenv("WHATSAPP_SENDS_PER_SECOND", "2")
    assert flow.process_batch() == 2
    assert len(calls) == 2
    assert flow.process_batch() == 0
    workflow_clock(day, 8, minute=1)
    assert flow.process_batch() == 1
    assert len(calls) == 3


def test_http_outage_pauses_unsent_jobs_and_single_probe_recovers(live_http_queue, workflow_clock):
    import json
    flow, mode, calls, day = live_http_queue
    mode[0] = "gateway"
    assert flow.process_batch() == 3
    assert len(calls) == 3
    with flow.sessions.begin() as session:
        original = session.scalar(select(FlowJob).order_by(FlowJob.id))
        payload = json.loads(original.payload)
        waiting = flow._job(session, "additional-fictional-offer", "OFFER", payload)
        waiting_id = waiting.id
    assert flow.process_one() is False
    with flow.sessions() as session:
        assert session.get(FlowJob, waiting_id).attempts == 0
        assert len(list(session.scalars(select(FlowJob).where(FlowJob.status == "UNKNOWN")))) == 3
    mode[0] = "success"
    workflow_clock(day, 8, minute=1)
    assert flow.process_one()
    assert len(calls) == 4
    assert flow.dispatch_control.summary(flow._business_number())["open_until"] is None
    with flow.sessions() as session:
        assert session.get(FlowJob, waiting_id).status == "SENT"
        assert len(list(session.scalars(select(FlowJob).where(FlowJob.status == "UNKNOWN")))) == 3

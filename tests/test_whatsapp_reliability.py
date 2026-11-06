"""Durable retry decisions never change appointment state or resend ambiguity."""

from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import create_engine, inspect, select

from flow_test.models import FlowInbox, FlowJob, FlowSetting
from flow_test.reliability import PAUSE_KEY
from flow_test.reliability_schema import migrate_queue, missing_columns
from flow_test.service import FlowTest
from services.whatsapp_service import WhatsAppConfigurationError, WhatsAppDeliveryError


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    app = FlowTest(f"sqlite:///{(tmp_path / 'reliability.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    yield app
    app.engine.dispose()


def queued(flow):
    with flow.sessions.begin() as session:
        job = flow._job(session, "reliable-test", "TEXT", {"recipient": "preview:TEST-A"})
        job.attempts = 1
        job.send_started_at = datetime.utcnow()
        return job.id


def saved(flow, job_id):
    with flow.sessions() as session:
        return session.get(FlowJob, job_id)


def test_temporary_rejection_survives_restart_and_waits_until_due(flow):
    job_id = queued(flow)
    flow._failed_job(job_id, "TEXT", WhatsAppDeliveryError("temporary", code=130429, rejected=True, retry_after=90))
    job = saved(flow, job_id)
    assert job.status == "RETRY_WAIT" and job.send_started_at is None
    assert job.next_attempt_at >= datetime.utcnow() + timedelta(seconds=89)
    restored = FlowTest(str(flow.engine.url), scenario=flow.scenario)
    try:
        with restored.lock, restored.sessions.begin() as session:
            restored._maintain_delivery(session)
            assert session.get(FlowJob, job_id).status == "RETRY_WAIT"
            session.get(FlowJob, job_id).next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        with restored.lock, restored.sessions.begin() as session:
            restored._maintain_delivery(session)
        assert saved(restored, job_id).status == "QUEUED"
    finally:
        restored.engine.dispose()


@pytest.mark.parametrize("exc", [TimeoutError("private"), OSError("private"),
    WhatsAppDeliveryError("invalid JSON"), WhatsAppDeliveryError("gateway 503")])
def test_uncertain_send_is_never_retried_or_logged_with_private_details(flow, exc):
    job_id = queued(flow)
    flow._failed_job(job_id, "TEXT", exc)
    flow._failed_job(job_id, "TEXT", exc)
    assert saved(flow, job_id).status == "UNKNOWN"
    with flow.lock, flow.sessions.begin() as session:
        flow._recover_processing(session)
    with flow.sessions() as session:
        warnings = list(session.scalars(select(FlowInbox)))
        assert len(warnings) == 1 and "private" not in warnings[0].text
    assert flow.drain() == 0


@pytest.mark.parametrize("exc", [WhatsAppConfigurationError("private"),
    WhatsAppDeliveryError("expired", code=190, rejected=True),
    WhatsAppDeliveryError("template", code=132001, rejected=True)])
def test_configuration_failure_pauses_outbound_but_processes_inbound(flow, exc):
    job_id = queued(flow)
    flow._failed_job(job_id, "TEXT", exc)
    with flow.sessions.begin() as session:
        other = flow._job(session, "second", "TEXT", {"recipient": "preview:TEST-B"})
        other_id = other.id
        flow._job(session, "status", "STATUS", {"id": "unmatched", "recipient_id": "preview:TEST-A", "status": "sent"})
    assert flow.process_batch() == 1
    assert saved(flow, other_id).attempts == 0
    assert saved(flow, job_id).status == "BLOCKED"
    assert flow.state(date(2026, 10, 19))["sending_paused"]
    assert flow.resume_sending()["requeued"] == 1
    assert saved(flow, job_id).status == "QUEUED"
    with flow.sessions() as session:
        assert session.get(FlowSetting, PAUSE_KEY) is None


@pytest.mark.parametrize("deadline", [False, True])
def test_retry_limit_and_deadline_end_in_staff_attention(flow, deadline):
    job_id = queued(flow)
    with flow.sessions.begin() as session:
        job = session.get(FlowJob, job_id)
        if deadline:
            job.deadline_at = datetime.utcnow() + timedelta(seconds=1)
        else:
            job.attempts = flow.max_send_attempts
    flow._failed_job(job_id, "TEXT", WhatsAppDeliveryError("temporary", code=131016, rejected=True))
    job = saved(flow, job_id)
    assert job.status == "NEEDS_ATTENTION" and job.next_attempt_at is None
    with flow.sessions() as session:
        assert "Booking unchanged" in session.scalar(select(FlowInbox)).text


def test_crash_requeues_only_owned_jobs_that_never_started_sending(flow):
    with flow.sessions.begin() as session:
        jobs = []
        for index in range(3):
            job = flow._job(session, f"crash-{index}", "TEXT", {})
            job.status = "PROCESSING"
            if index < 2:
                job.locked_by = "crashed-worker"
                job.locked_until = datetime.utcnow() - timedelta(seconds=1)
            if index == 1:
                job.send_started_at = datetime.utcnow()
            jobs.append(job.id)
    with flow.lock, flow.sessions.begin() as session:
        flow._recover_processing(session)
    assert [saved(flow, ident).status for ident in jobs] == ["QUEUED", "UNKNOWN", "UNKNOWN"]


def test_overdue_delivery_alert_is_deduplicated_and_never_resends(flow):
    job_id = queued(flow)
    with flow.sessions.begin() as session:
        job = session.get(FlowJob, job_id)
        job.status, job.message_id = "SENT", "wamid.fake"
        job.accepted_at = datetime.utcnow() - timedelta(minutes=20)
        job.delivery = "sent"
    for _ in range(2):
        with flow.lock, flow.sessions.begin() as session:
            flow._maintain_delivery(session)
    assert flow.drain() == 0 and saved(flow, job_id).status == "SENT"
    with flow.sessions() as session:
        assert len(list(session.scalars(select(FlowInbox)))) == 1


def test_unsent_deadline_expires_without_a_send(flow):
    job_id = queued(flow)
    with flow.sessions.begin() as session:
        job = session.get(FlowJob, job_id)
        job.status, job.send_started_at = "WAITING_FOR_CONTACT", None
        job.deadline_at = datetime.utcnow() - timedelta(seconds=1)
    assert flow.drain() == 0
    assert saved(flow, job_id).status == "NEEDS_ATTENTION"


def test_delivery_receipt_resolves_overdue_warning_without_resending(flow):
    job_id = queued(flow)
    with flow.sessions.begin() as session:
        job = session.get(FlowJob, job_id)
        job.status, job.message_id = "SENT", "wamid.receipt"
        job.accepted_at = datetime.utcnow() - timedelta(minutes=20)
    with flow.lock, flow.sessions.begin() as session:
        flow._maintain_delivery(session)
        flow._job(session, "delivered", "STATUS", {"id": "wamid.receipt", "status": "delivered",
                                                  "recipient_id": "preview:TEST-A"})
    assert flow.process_one()
    with flow.sessions() as session:
        assert session.get(FlowJob, job_id).delivery == "delivered"
        assert session.scalar(select(FlowInbox)).resolved == 1


def test_queue_migration_preserves_legacy_rows_and_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE flow_test_jobs (id INTEGER PRIMARY KEY, status VARCHAR(30), payload TEXT)")
            connection.exec_driver_sql("INSERT INTO flow_test_jobs VALUES (1, 'UNKNOWN', '{}')")
            migrate_queue(connection)
            migrate_queue(connection)
        assert not missing_columns(engine)
        with engine.connect() as connection:
            assert tuple(connection.exec_driver_sql("SELECT id, status, payload, send_started_at FROM flow_test_jobs").one()) == (1, "UNKNOWN", "{}", None)
        assert "ix_flow_jobs_retry_due" in {i["name"] for i in inspect(engine).get_indexes("flow_test_jobs")}
    finally:
        engine.dispose()


def test_real_workflow_retry_does_not_change_bookings(flow):
    day = date(2026, 10, 19)
    flow.generate(day)
    flow.publish(day)
    before = flow.state(day)["appointments"]
    calls = []
    def send(*_):
        calls.append(True)
        if len(calls) == 1:
            raise WhatsAppDeliveryError("busy", code=130429, rejected=True)
        return f"wamid.fake-{len(calls)}"
    flow.send = send
    assert flow.process_one()
    with flow.sessions.begin() as session:
        job = session.scalar(select(FlowJob).where(FlowJob.status == "RETRY_WAIT"))
        job.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
    flow.drain()
    assert len(calls) == 4
    assert flow.state(day)["appointments"] == before
    assert all(j["status"] == "SIMULATED" for j in flow.state(day)["jobs"])


def test_resume_api_requires_authentication_and_preserves_unknown_sends(flow):
    from tests.test_flow_test import http_call

    job_id = queued(flow)
    flow._failed_job(job_id, "TEXT", TimeoutError())
    status, _ = http_call(flow, "/api/resume-sending", body=b"{}")
    assert status == 401
    status, result = http_call(flow, "/api/resume-sending", body=b"{}",
                              headers={"Authorization": "Bearer test-admin-token"})
    # The helper binds this token; authenticated resume has no resend-unknown path.
    assert status == 200 and json.loads(result) == {"requeued": 0}
    assert saved(flow, job_id).status == "UNKNOWN"

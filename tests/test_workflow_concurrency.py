"""Real process races against disposable SQLite and opt-in local PostgreSQL."""

from dataclasses import replace
from datetime import date, datetime
import json
import multiprocessing as mp
import os
from queue import Empty
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from database.models import Appointment, Patient
from flow_test.models import FlowInbox, FlowJob, FlowSetting
from flow_test.service import FlowTest


DAY = date(2026, 10, 19)
CTX = mp.get_context("spawn")


@pytest.fixture(params=["sqlite", "postgresql"])
def target(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{(tmp_path / 'race.sqlite').as_posix()}"
        return
    configured = os.getenv("WORKFLOW_CONCURRENCY_POSTGRES_URL")
    if not configured:
        pytest.skip("Set WORKFLOW_CONCURRENCY_POSTGRES_URL to a disposable local PostgreSQL server")
    url = make_url(configured)
    if url.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Concurrency tests only create databases on a local PostgreSQL server")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    database = f"codex_{uuid4().hex}_flow_test"
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        target_url = url.set(database=database).render_as_string(hide_password=False)
        from services.whatsapp_dispatch import DispatchControl
        queue_engine = create_engine(target_url)
        control = DispatchControl(queue_engine)
        try:
            control.prepare()
        finally:
            control.engine.dispose()
            queue_engine.dispose()
        yield target_url
    finally:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        engine.dispose()


def _batch_worker(url, scenario, ready, start, results, calls):
    try:
        def send(_, payload):
            calls.put(payload["token"])
            time.sleep(0.05)
            return "dummy-" + payload["token"]

        flow = FlowTest(url, scenario=scenario, send=send)
        ready.put(True)
        assert start.wait(20)
        results.put(("ok", flow.process_batch()))
        flow.engine.dispose()
    except BaseException as exc:
        results.put(("error", repr(exc)))


def _save_worker(url, scenario, ready, start, results):
    try:
        flow = FlowTest(url, scenario=scenario)
        flow.next_offer_date = lambda: DAY
        preview, _ = flow.preview_daily(DAY)
        preview = replace(preview, appointments=[a for a in preview.appointments if a.date == DAY])
        ready.put(True)
        assert start.wait(20)
        try:
            saved = flow.save_calendar(preview, DAY, expected_offer_on_save=True)
            results.put(("saved", saved["added"]))
        except ValueError as exc:
            results.put(("rejected", str(exc)))
        flow.engine.dispose()
    except BaseException as exc:
        results.put(("error", repr(exc)))


def _blocked_send_worker(url, scenario, entered, release, results):
    try:
        def send(_, payload):
            entered.set()
            assert release.wait(30)
            return "dummy-" + payload["token"]

        flow = FlowTest(url, scenario=scenario, send=send)
        results.put(flow.process_one())
        flow.engine.dispose()
    except BaseException as exc:
        results.put(repr(exc))


def _claim_worker(url, scenario, job_id, ready, start, results):
    try:
        flow = FlowTest(url, scenario=scenario)
        ready.put(True)
        assert start.wait(20)
        # Intentionally bypass the outer mutex: the conditional UPDATE must
        # itself prevent two claims, even when callers have a stale queue view.
        with flow.sessions.begin() as session:
            won = flow._claim_job(session, job_id)
            time.sleep(0.1)
        results.put(won)
        flow.engine.dispose()
    except BaseException as exc:
        results.put(repr(exc))


def _rate_worker(url, index, ready, start, results):
    from services.whatsapp_dispatch import DispatchControl
    engine = create_engine(url)
    control = DispatchControl(engine)
    try:
        control.check()
        ready.put(True)
        assert start.wait(20)
        granted = sum(control.reserve("shared-business", f"parent-{index}-{item}", f"{index}-{item}")
                      is not None for item in range(3))
        results.put(granted)
    except BaseException as exc:
        results.put(repr(exc))
    finally:
        control.engine.dispose()
        engine.dispose()


def test_business_rate_limit_is_atomic_across_real_processes(target, monkeypatch):
    from services.whatsapp_dispatch import DispatchControl
    monkeypatch.delenv("WHATSAPP_DISPATCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("WHATSAPP_SENDS_PER_SECOND", "5")
    engine = create_engine(target)
    control = DispatchControl(engine)
    control.prepare()
    ready, start, results = CTX.Queue(), CTX.Event(), CTX.Queue()
    workers = [CTX.Process(target=_rate_worker, args=(target, index, ready, start, results)) for index in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=25)
        start.set()
        counts = [results.get(timeout=25) for _ in workers]
        _join(workers)
        assert all(isinstance(count, int) for count in counts), counts
        assert sum(counts) == 5, "Two processes must share the same five-send allowance"
    finally:
        _stop(workers)
        control.engine.dispose()
        engine.dispose()


def _join(processes):
    for process in processes:
        process.join(25)
        assert not process.is_alive(), "Competing workflow processes deadlocked"
        assert process.exitcode == 0


def _stop(processes):
    for process in processes:
        if process.is_alive():
            process.terminate()
        process.join(10)


def test_two_processes_send_each_queued_offer_once(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    flow.generate(DAY)
    expected = flow.publish(DAY)
    ready, results, calls, start = CTX.Queue(), CTX.Queue(), CTX.Queue(), CTX.Event()
    workers = [CTX.Process(target=_batch_worker,
                          args=(target, legacy_flow_scenario, ready, start, results, calls))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=25)
        start.set()
        outcomes = [results.get(timeout=25) for _ in workers]
        _join(workers)
        assert all(status == "ok" for status, _ in outcomes), outcomes
        assert sum(count for _, count in outcomes) == expected
        tokens = [calls.get(timeout=5) for _ in range(expected)]
        assert len(set(tokens)) == expected
        with pytest.raises(Empty):
            calls.get(timeout=0.1)
        with flow.sessions() as session:
            jobs = list(session.scalars(select(FlowJob)))
            assert all(job.status == "SIMULATED" and job.attempts == 1 for job in jobs)
    finally:
        _stop(workers)
        flow.engine.dispose()


def test_conditional_job_claim_has_only_one_winner(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    flow.generate(DAY)
    flow.publish(DAY)
    with flow.sessions() as session:
        job_id = session.scalar(select(FlowJob.id).order_by(FlowJob.id))
    ready, results, start = CTX.Queue(), CTX.Queue(), CTX.Event()
    workers = [CTX.Process(target=_claim_worker,
                          args=(target, legacy_flow_scenario, job_id, ready, start, results)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=25)
        start.set()
        outcomes = [results.get(timeout=25) for _ in workers]
        _join(workers)
        assert sorted(outcomes) == [False, True], outcomes
        with flow.sessions() as session:
            assert session.get(FlowJob, job_id).attempts == 1
    finally:
        _stop(workers)
        flow.engine.dispose()


def test_two_processes_cannot_save_the_same_reviewed_calendar(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    ready, results, start = CTX.Queue(), CTX.Queue(), CTX.Event()
    workers = [CTX.Process(target=_save_worker,
                          args=(target, legacy_flow_scenario, ready, start, results)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=25)
        start.set()
        outcomes = [results.get(timeout=25) for _ in workers]
        _join(workers)
        assert sorted(status for status, _ in outcomes) == ["rejected", "saved"], outcomes
        assert "changed after this preview" in next(value for status, value in outcomes if status == "rejected")
        with flow.sessions() as session:
            appointments = list(session.scalars(select(Appointment)))
            assert len(appointments) == next(value for status, value in outcomes if status == "saved")
            keys = [(a.therapist_id, a.date, a.slot_index) for a in appointments]
            assert len(keys) == len(set(keys))
    finally:
        _stop(workers)
        flow.engine.dispose()


def test_starting_another_instance_does_not_recover_an_active_send(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    flow.generate(DAY)
    flow.publish(DAY)
    entered, release, results = CTX.Event(), CTX.Event(), CTX.Queue()
    worker = CTX.Process(target=_blocked_send_worker,
                         args=(target, legacy_flow_scenario, entered, release, results))
    opened, instances, errors = threading.Event(), [], []

    def open_second():
        try:
            instances.append(FlowTest(target, scenario=legacy_flow_scenario))
        except BaseException as exc:
            errors.append(exc)
        finally:
            opened.set()

    second = threading.Thread(target=open_second)
    try:
        worker.start()
        assert entered.wait(25)
        second.start()
        assert opened.wait(5), "Opening the calendar must not wait for WhatsApp HTTP"
        with flow.sessions() as session:
            assert session.scalar(select(FlowJob.id).where(FlowJob.status == "PROCESSING"))
        release.set()
        assert results.get(timeout=25) is True
        _join([worker])
        second.join(25)
        assert opened.is_set() and not errors
        with flow.sessions() as session:
            assert not list(session.scalars(select(FlowJob).where(FlowJob.status == "UNKNOWN")))
            assert not list(session.scalars(select(FlowInbox)))
    finally:
        release.set()
        _stop([worker])
        if second.ident is not None:
            second.join(25)
        for instance in instances:
            instance.engine.dispose()
        flow.engine.dispose()


def test_crashed_sender_is_recovered_without_resending(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    flow.generate(DAY)
    flow.publish(DAY)
    entered, release, results = CTX.Event(), CTX.Event(), CTX.Queue()
    worker = CTX.Process(target=_blocked_send_worker,
                         args=(target, legacy_flow_scenario, entered, release, results))
    try:
        worker.start()
        assert entered.wait(25)
        with flow.sessions() as session:
            job_id = session.scalar(select(FlowJob.id).where(FlowJob.status == "PROCESSING"))
        worker.terminate()
        worker.join(10)
        # An already-running survivor recovers the orphan too; no restart needed.
        assert flow.process_one(job_id=job_id) is False
        with flow.sessions() as session:
            assert session.get(FlowJob, job_id).status == "UNKNOWN"
            assert session.get(FlowJob, job_id).attempts == 1
            assert len(list(session.scalars(select(FlowInbox)))) == 1
        assert flow.process_one(job_id=job_id) is False
    finally:
        _stop([worker])
        flow.engine.dispose()


def test_shared_lock_is_reentrant_timed_and_keeps_inbound_independent(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    other = FlowTest(target, scenario=legacy_flow_scenario)
    try:
        with flow.lock:
            with flow.lock:
                assert other.lock.acquire(blocking=False) is False
                assert other.lock.acquire(timeout=0.05) is False
                assert other.inbound_lock.acquire(timeout=1) is True
                other.inbound_lock.release()
        assert other.lock.acquire(timeout=1) is True
        other.lock.release()
        with pytest.raises(RuntimeError, match="unowned"):
            other.lock.release()
        with pytest.raises(ValueError):
            with flow.lock:
                raise ValueError("Exercise exception cleanup")
        with other.lock:
            pass
    finally:
        flow.engine.dispose()
        other.engine.dispose()


def test_recipient_cooldown_is_shared_by_batch_and_single_job_workers(target, legacy_flow_scenario, monkeypatch):
    class Clock(datetime):
        now = staticmethod(lambda *args: datetime(2026, 10, 19))
        utcnow = staticmethod(lambda: datetime(2026, 10, 19))

    calls = []
    contacts = {code: "919999999999" for code in ("TEST-A", "TEST-B", "TEST-C")}
    flow = FlowTest(target, scenario=legacy_flow_scenario, recipients=contacts,
                    send=lambda _, payload: calls.append(payload["token"]) or "dummy")
    other = FlowTest(target, scenario=legacy_flow_scenario, recipients=contacts,
                     send=lambda _, payload: calls.append(payload["token"]) or "dummy")
    try:
        flow.generate(DAY)
        flow.publish(DAY)
        flow.live = other.live = True
        flow.message_mode = other.message_mode = "template"
        now = [100.0]
        monkeypatch.setattr("flow_test.service.datetime", Clock)
        monkeypatch.setattr("flow_test.service.time.time", lambda: now[0])
        assert flow.process_batch() == 1
        assert other.process_batch() == 0
        assert other.process_one() is False
        if flow.engine.dialect.name == "postgresql":
            # Shared controls deliberately use the DB clock. Advancing Python's
            # test epoch cannot bypass the production parent cooldown.
            time.sleep(6.05)
        now[0] += 6
        assert other.process_one() is True
        assert flow.process_batch() == 0
        if flow.engine.dialect.name == "postgresql":
            time.sleep(6.05)
        now[0] += 6
        assert flow.process_batch() == 1
        assert len(calls) == len(set(calls)) == 3
    finally:
        flow.engine.dispose()
        other.engine.dispose()


def test_forwarded_import_does_not_hold_the_remote_workers_mutex(target, legacy_flow_scenario):
    from flow_test.shared_ui import dispatch_shared_api

    flow = FlowTest(target, scenario=legacy_flow_scenario)
    remote = FlowTest(target, scenario=legacy_flow_scenario)
    flow.remote_worker = True
    forwarded = []

    def remote_dispatch(request, instance, path):
        assert remote.lock.acquire(timeout=0.1), "Forwarder held the remote import's mutex"
        remote.lock.release()
        forwarded.append(path)

    instance = SimpleNamespace(flow=flow, token="dummy", zoho_dispatch=remote_dispatch)
    request = SimpleNamespace(path="/api/zoho/import", command="POST",
                              headers={"Authorization": "Bearer dummy"})
    try:
        assert dispatch_shared_api(request, instance, prefix="") is True
        assert forwarded == ["/api/zoho/import"]
    finally:
        flow.engine.dispose()
        remote.engine.dispose()


def test_replica_refreshes_roster_after_another_workers_source_update(target, legacy_flow_scenario):
    flow = FlowTest(target, scenario=legacy_flow_scenario)
    replica = FlowTest(target, scenario=legacy_flow_scenario)
    try:
        source = json.loads(json.dumps(legacy_flow_scenario))
        for index, patient in enumerate(source["patients"]):
            patient["phone"] = f"91999999999{index}"
        source["patients"].append({**source["patients"][0], "code": "NEW-CHILD", "phone": "919999999999"})
        with flow.lock, flow.inbound_lock, flow.sessions.begin() as session:
            session.add(Patient(patient_code="NEW-CHILD", name="New fictional child", active=True))
            session.add(FlowSetting(key="zoho_test_scenario", value=json.dumps(source)))
        assert "NEW-CHILD" not in {p["code"] for p in replica.scenario["patients"]}
        assert "919999999999" in replica.contact_numbers()
        preview, _ = replica.preview_daily(DAY, ["TEST-A", "NEW-CHILD"])
        assert preview.appointments
        assert "NEW-CHILD" in {p["code"] for p in replica.state(DAY)["scenario"]["patients"]}
    finally:
        flow.engine.dispose()
        replica.engine.dispose()

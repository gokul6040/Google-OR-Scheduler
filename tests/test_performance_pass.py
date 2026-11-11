"""Performance pass (review items #18, #22-#28). Fictional data and disposable stores only."""

from datetime import date, datetime, timedelta
import json
import os
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text

from database.models import Appointment, AuditLog
from database.workflow_lock import WorkflowLock
from flow_test.cleanup import cleanup_operational_data
from flow_test.models import (Base as FlowBase, FlowExportItem, FlowInbox, FlowJob, FlowOffer,
                              FlowOfferAppointment, FlowPreview, FlowSetting)
from flow_test.performance_schema import SOURCE_VERSION_KEY, migrate_performance
from flow_test.preview_store import PreviewStore
from flow_test.service import FlowTest
from flow_test.zoho_calendar import push_guard
from flow_test.zoho_export import acknowledge_export, export_rows, finish_export, prepare_export
from scheduling.types import ScheduledAppointment, ScheduleResult, UnmetRequirement

TARGET = date(2026, 10, 19)


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    item = FlowTest(f"sqlite:///{(tmp_path / 'perf.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    yield item
    item.engine.dispose()


def published(flow):
    flow.generate(TARGET)
    flow.publish(TARGET)
    flow.drain()
    return flow


# --- #26 / #18 migration of existing stores --------------------------------------------------

def test_migration_upgrades_a_store_written_before_this_release(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.sqlite').as_posix()}")
    FlowBase.metadata.create_all(engine, tables=[t.__table__ for t in (
        FlowSetting, FlowExportItem, FlowOfferAppointment, FlowPreview)])
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE flow_test_offers (token VARCHAR(40) PRIMARY KEY, appointment_ids TEXT, snapshot TEXT, "
            "patient_code VARCHAR(40), recipient VARCHAR(24), state VARCHAR(30), created_at TIMESTAMP, expires_at TIMESTAMP)")
        connection.execute(text("INSERT INTO flow_test_offers VALUES ('a1', '[7, 8]', :snap, 'FIC1', '919999999991', "
                                "'PENDING', '2026-10-18 03:30:00', NULL)"),
                           {"snap": json.dumps([[7, "2026-10-19", 0], [8, "2026-10-19", 1]])})
        connection.execute(text("INSERT INTO flow_test_settings VALUES ('zoho_scheduler_export:item:7', :value)"),
                           {"value": json.dumps({"fingerprint": "f" * 64, "recordId": "555", "therapyDate": "19-10-2026"})})
        connection.execute(text("INSERT INTO flow_test_settings VALUES ('zoho_test_scenario', '{\"patients\": []}')"))
    with engine.begin() as connection:
        result = migrate_performance(connection)
    assert result["offerDatesBackfilled"] == 1 and result["exportItemsMoved"] == 1
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT visit_date FROM flow_test_offers")) == "2026-10-19"
        assert {tuple(row) for row in connection.execute(text(
            "SELECT token, appointment_id FROM flow_test_offer_appointments"))} == {("a1", 7), ("a1", 8)}
        moved = connection.execute(text("SELECT therapy_date, record_id FROM flow_test_export_items")).one()
        assert tuple(moved) == ("2026-10-19", "555")
        assert connection.scalar(text(
            "SELECT count(*) FROM flow_test_settings WHERE key LIKE 'zoho_scheduler_export:item:%'")) == 0
        assert connection.scalar(text(f"SELECT value FROM flow_test_settings WHERE key = '{SOURCE_VERSION_KEY}'"))
    names = {index["name"] for index in inspect(engine).get_indexes("flow_test_offers")}
    assert {"ix_flow_offers_visit_date", "ix_flow_offers_state_expires", "ix_flow_offers_recipient",
            "ix_flow_offers_patient"} <= names
    with engine.begin() as connection:  # Idempotent on every start-up.
        again = migrate_performance(connection)
    assert again["offerDatesBackfilled"] == 0 and again["exportItemsMoved"] == 0
    engine.dispose()


def test_new_offers_record_their_day_and_appointments(flow):
    published(flow)
    with flow.sessions() as session:
        offers = list(session.scalars(select(FlowOffer)))
        assert offers and all(offer.visit_date == TARGET for offer in offers)
        for offer in offers:
            linked = set(session.scalars(select(FlowOfferAppointment.appointment_id).where(
                FlowOfferAppointment.token == offer.token)))
            assert linked == set(json.loads(offer.appointment_ids))
    names = {index["name"] for index in inspect(flow.engine).get_indexes("audit_logs")}
    assert "ix_audit_logs_entity_action" in names


# --- #18 export preparation reads only the target date -----------------------------------------

def test_export_uses_tracking_table_and_still_finds_a_moved_visit(flow):
    published(flow)
    for offer in flow.state(TARGET)["offers"]:
        flow.simulate(offer["token"], "YES")
    flow.process_events()
    batch = prepare_export(flow, TARGET)
    assert batch["changes"]
    for row in batch["changes"]:
        acknowledge_export(flow, batch["id"], row["appointmentId"], row["fingerprint"], str(900000 + row["appointmentId"]))
    finish_export(flow, batch["id"])
    with flow.sessions() as session:
        tracked = {item.appointment_id: item for item in session.scalars(select(FlowExportItem))}
        assert set(tracked) == {row["appointmentId"] for row in batch["changes"]}
        assert all(item.therapy_date == TARGET for item in tracked.values())
        assert not session.scalar(select(FlowSetting.key).where(FlowSetting.key.like("zoho_scheduler_export:item:%")))
    moved_id = batch["changes"][0]["appointmentId"]
    with flow.sessions.begin() as session:
        session.get(Appointment, moved_id).date = TARGET + timedelta(days=2)
    with flow.sessions() as session:
        namespace = json.loads(session.get(FlowSetting, "zoho_scheduler_export:namespace").value)
        rows, _ = export_rows(flow, session, TARGET, namespace)
    # The visit pushed under TARGET is updated with its new date; nothing else changed.
    assert [row["appointmentId"] for row in rows] == [moved_id]
    assert rows[0]["fields"]["Therapy_Date"] == (TARGET + timedelta(days=2)).strftime("%d-%m-%Y")


# --- #23 webhooks / #22 sends check only the numbers involved -----------------------------------

def test_contact_check_is_targeted_and_cached_by_source_version(flow):
    published(flow)
    with flow.sessions.begin() as session:
        session.add(FlowSetting(key="zoho_test_scenario", value=json.dumps(
            {"patients": [{"code": "FIC1", "phone": "917000000001"}]})))
        session.add(FlowSetting(key=SOURCE_VERSION_KEY, value="v1"))
    with flow.sessions() as session:
        assert flow.contacts_among(session, ["917000000001", "919000000000"]) == {"917000000001"}
    with flow.sessions.begin() as session:
        # Same version: the cached roster is used (the large roster is not re-parsed).
        session.get(FlowSetting, "zoho_test_scenario").value = json.dumps(
            {"patients": [{"code": "FIC1", "phone": "917000000002"}]})
    with flow.sessions() as session:
        assert flow.contacts_among(session, ["917000000002"]) == set()
    with flow.sessions.begin() as session:
        session.get(FlowSetting, SOURCE_VERSION_KEY).value = "v2"  # A real import changes the version.
    with flow.sessions() as session:
        assert flow.contacts_among(session, ["917000000002"]) == {"917000000002"}
        # Past offer recipients still count, found by an indexed lookup.
        offer_recipient = session.scalar(select(FlowOffer.recipient))
        assert offer_recipient in flow.contacts_among(session, [offer_recipient])


def test_webhook_queues_only_registered_numbers(flow, monkeypatch):
    published(flow)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "fictional-business")
    with flow.sessions() as session:
        recipient = session.scalar(select(FlowOffer.recipient))
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "fictional-business"}, "messages": [
        {"from": recipient, "id": "wamid.fictional.1", "timestamp": "1", "text": {"body": "hi"}},
        {"from": "910000000000", "id": "wamid.fictional.2", "timestamp": "1", "text": {"body": "hi"}}]}}]}]}
    assert flow.receive(payload) == 1


# --- #24 dashboards ---------------------------------------------------------------------------

def test_compact_state_sends_only_the_days_children_and_never_waits_for_the_lock(flow):
    published(flow)
    flow.compact_state = True
    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with flow.lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    result = {}
    try:
        assert held.wait(5)
        reader = threading.Thread(target=lambda: result.update(flow.state(TARGET)))
        reader.start()
        reader.join(5)
        assert not reader.is_alive(), "state() must not wait for the workflow lock"
    finally:
        release.set()
        holder.join(10)
    shown = {item["patient"] for item in result["appointments"]} | {item["patient"] for item in result["offers"]}
    assert result["appointments"] and set(result["recipients"]) <= shown
    assert result["scenario"]["patients"] == []
    assert all(value.startswith("••••") for value in result["recipients"].values())


# --- #28 shared previews and push lock -----------------------------------------------------------

def test_preview_saved_on_one_app_copy_can_be_read_by_another(tmp_path, legacy_flow_scenario):
    url = f"sqlite:///{(tmp_path / 'shared.sqlite').as_posix()}"
    first = FlowTest(url, scenario=legacy_flow_scenario)
    second = FlowTest(url, scenario=legacy_flow_scenario)
    try:
        result = ScheduleResult(
            status="FEASIBLE",
            appointments=[ScheduledAppointment(1, 2, 3, 4, 5, TARGET, 6, "b1", overridden_warnings=("W1",))],
            unmet=[UnmetRequirement(1, 2, 4, 1, "SAVED_BOOKING_NEEDS_REVIEW", ("X",))],
            requested_sessions=2, scheduled_sessions=1, validation={"absence_conflicts": 0},
            statistics={"blocked_locations": [5], "held_centres": ["Fictional"], "codes": {"a", "b"}})
        PreviewStore(first)["p1"] = {"result": result, "start": TARGET, "horizon_days": 1,
                                     "created": datetime(2026, 10, 18, 9), "offer_on_save": True}
        loaded = PreviewStore(second)["p1"]
        assert loaded["result"] == result and loaded["start"] == TARGET and loaded["offer_on_save"] is True
        assert loaded["result"].appointments[0].overridden_warnings == ("W1",)
        assert loaded["result"].statistics["codes"] == {"a", "b"}
        loaded["saved_response"] = {"message": "Saved"}
        PreviewStore(second)["p1"] = loaded
        assert PreviewStore(first)["p1"]["saved_response"] == {"message": "Saved"}
        with first.sessions.begin() as session:
            session.get(FlowPreview, "p1").created_at = datetime.utcnow() - timedelta(hours=25)
        assert PreviewStore(first).get("p1") is None  # Expired drafts are never served.
        PreviewStore(first)["p2"] = {"start": TARGET}
        PreviewStore(second).clear()
        assert PreviewStore(first) == {}
    finally:
        first.engine.dispose()
        second.engine.dispose()


def test_push_lock_is_shared_between_app_copies(tmp_path, legacy_flow_scenario):
    url = f"sqlite:///{(tmp_path / 'push.sqlite').as_posix()}"
    first = FlowTest(url, scenario=legacy_flow_scenario)
    second = FlowTest(url, scenario=legacy_flow_scenario)
    try:
        assert first.push_lock.acquire(blocking=False)
        try:
            result = []

            def attempt():
                try:
                    with push_guard(second):
                        result.append("ran")
                except ValueError as exc:
                    result.append(str(exc))

            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(10)
            assert result and "already running" in result[0]
        finally:
            first.push_lock.release()
        with push_guard(second):
            pass  # Free again once the first copy finished.
    finally:
        first.engine.dispose()
        second.engine.dispose()


def test_command_line_apply_runs_the_push_inside_the_app(monkeypatch):
    import main_workflow.push as cli

    calls = []
    monkeypatch.setattr(cli, "workflow_connection", lambda: (SimpleNamespace(admin_token="fictional"), None))
    monkeypatch.setattr(cli, "api", lambda config, operation, body=None, timeout=180:
                        calls.append((operation, body, timeout)) or {"pushed": 0, "mode": "APPLIED"})
    monkeypatch.setattr(cli, "run_push", lambda *a, **k: pytest.fail("A command-line apply must not push locally"))
    monkeypatch.setattr("sys.argv", ["push", "--date", "2026-10-19", "--apply"])
    cli.main()
    assert calls == [("export/push", {"date": "2026-10-19"}, 3600)]


# --- #27 retention clean-up ---------------------------------------------------------------------

def test_cleanup_removes_only_expired_operational_rows(flow):
    published(flow)  # Its own jobs and offers are recent, so they must all be kept.
    now = datetime.utcnow()
    old = now - timedelta(days=181)
    with flow.sessions.begin() as session:
        appointments_before = sorted(session.scalars(select(Appointment.id)))
        audit_before = len(list(session.scalars(select(AuditLog.id))))
        for status in ("DONE", "SENT", "QUEUED", "FAILED"):
            session.add(FlowJob(event_key=f"old-{status}", kind="OFFER", payload="{}", status=status, created_at=old))
        session.add(FlowJob(event_key="recent-done", kind="INBOUND", payload="{}", status="DONE",
                            created_at=now - timedelta(days=10)))
        session.add(FlowOffer(token="oldoffer", appointment_ids="[1]", snapshot="[]", patient_code="FIC",
                              recipient="910000000001", state="CLOSED", created_at=old, visit_date=old.date()))
        session.add(FlowOfferAppointment(token="oldoffer", appointment_id=1))
        session.add(FlowInbox(event_key="old-resolved", reason="x", text="x", resolved=1, created_at=old))
        session.add(FlowInbox(event_key="old-open", reason="x", text="x", resolved=0, created_at=old))
        session.add(FlowExportItem(appointment_id=999999, therapy_date=old.date(), fingerprint="f", record_id="1"))
        session.add(FlowExportItem(appointment_id=999998, therapy_date=now.date(), fingerprint="f", record_id="2"))
        session.add(FlowSetting(key="send_after:expired", value=str((old - datetime(1970, 1, 1)).total_seconds())))
        session.add(FlowSetting(key="send_after:active",
                                value=str((now + timedelta(minutes=1) - datetime(1970, 1, 1)).total_seconds())))
    removed = cleanup_operational_data(flow, now=now, days=180)
    assert removed["jobs"] == 2 and removed["inbox"] == 1
    assert removed["exportTracking"] == 1 and removed["sendTimers"] == 1 and removed["offers"] >= 1
    with flow.sessions() as session:
        keys = set(session.scalars(select(FlowJob.event_key)))
        assert {"old-QUEUED", "old-FAILED", "recent-done"} <= keys and not {"old-DONE", "old-SENT"} & keys
        assert session.get(FlowOffer, "oldoffer") is None
        assert not session.scalar(select(FlowOfferAppointment.token).where(FlowOfferAppointment.token == "oldoffer"))
        assert session.scalar(select(FlowInbox.id).where(FlowInbox.event_key == "old-open"))
        assert session.get(FlowExportItem, 999998) and not session.get(FlowExportItem, 999999)
        assert session.get(FlowSetting, "send_after:active") and not session.get(FlowSetting, "send_after:expired")
        # Clinical records are never touched.
        assert sorted(session.scalars(select(Appointment.id))) == appointments_before
        assert len(list(session.scalars(select(AuditLog.id)))) == audit_before


def test_retention_period_is_bounded(monkeypatch):
    from flow_test.cleanup import retention_days

    monkeypatch.delenv("WORKFLOW_RETENTION_DAYS", raising=False)
    assert retention_days() == 180
    monkeypatch.setenv("WORKFLOW_RETENTION_DAYS", "5")
    with pytest.raises(ValueError):
        retention_days()


# --- #25 one reusable lock connection (PostgreSQL) ----------------------------------------------

def test_postgres_lock_reuses_one_connection_and_recovers_from_a_dropped_one():
    configured = os.getenv("WORKFLOW_CONCURRENCY_POSTGRES_URL")
    if not configured:
        pytest.skip("Set WORKFLOW_CONCURRENCY_POSTGRES_URL to a disposable local PostgreSQL server")
    engine = create_engine(configured)
    lock = WorkflowLock(engine, "perf-test")
    other = WorkflowLock(engine, "perf-test")
    try:
        for _ in range(50):
            with lock:
                result = []
                probe = threading.Thread(target=lambda: result.append(other.acquire(blocking=False)))
                probe.start()
                probe.join(10)
                assert result == [False]  # Still a real cross-session lock.
        assert lock.connections_opened == 1
        # Simulate a dropped idle session: the next acquisition reconnects once.
        with engine.connect() as admin:
            pid = lock._connection.connection.dbapi_connection.info.backend_pid
            admin.execute(text("select pg_terminate_backend(:pid)"), {"pid": pid})
        with lock:
            pass
        assert lock.connections_opened == 2
        assert other.acquire(blocking=False)
        other.release()
    finally:
        lock.close()
        other.close()
        engine.dispose()

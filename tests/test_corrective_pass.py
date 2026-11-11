"""Corrective pass: truthful saves, durable warnings, bounded repairs, allowance reasons,
named Zoho failures, unsubscribe/subscribe and parent phone format. Fictional data only."""

from datetime import date
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from database.models import Appointment, AppointmentStatus, AuditLog, Location
from flow_test.models import FlowInbox, FlowJob, FlowOffer
from flow_test.service import FlowTest
from flow_test.zoho_export import acknowledge_export, prepare_export, record_export_failures
from flow_test.zoho_import import phone_number
from scheduling.types import UnmetRequirement
from services.whatsapp_dispatch import DispatchControl

TARGET = date(2026, 10, 19)
RECIPIENTS = {f"TEST-{letter}": f"91999999999{index}" for index, letter in enumerate("ABCD", 1)}


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    calls = []
    item = FlowTest(f"sqlite:///{(tmp_path / 'corrective.sqlite').as_posix()}", scenario=legacy_flow_scenario,
                    recipients=RECIPIENTS, send=lambda kind, payload: calls.append((kind, payload)) or f"dummy-{len(calls)}")
    item.calls = calls
    yield item
    item.engine.dispose()


_MESSAGE_IDS = iter(range(1, 10**6))


def inbound(flow, sender, text):
    number = next(_MESSAGE_IDS)  # Unique even under a frozen test clock.
    with flow.sessions.begin() as session:
        flow._job(session, f"in:{sender}:{text}:{number}", "INBOUND",
                  {"from": sender, "id": f"wamid.fictional.{number}", "timestamp": str(int(time.time())), "text": {"body": text}})
    flow.process_events()


def declined_repair_job(flow):
    # The automatic fill after a No is opt-in now; these tests cover its robustness.
    flow.fill_released_after_decline = True
    flow.generate(TARGET)
    flow.publish(TARGET)
    flow.drain()
    offer = next(o for o in flow.state(TARGET)["offers"] if o["patient"] == "TEST-B")
    flow.simulate(offer["token"], "NO")
    assert flow.process_events() == 1
    with flow.sessions() as session:
        return session.scalar(select(FlowJob.id).where(FlowJob.kind == "REPAIR"))


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "919876543210"), ("09876543210", "919876543210"), ("+91 98765 43210", "919876543210"),
    ("0091 9876543210", "919876543210"), ("919876543210", "919876543210"), ("(+91) 98765-43210", "919876543210"),
])
def test_parent_phone_numbers_become_91_plus_ten_digits(raw, expected):
    assert phone_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "98765", "1234567890", "04712345678", "+971501234567", "98765abc10", "9198765432100",
                                 # Explicit foreign codes must never become an Indian number.
                                 "+65 8123 4567", "0065 8123 4567", "+1 650 555 0100", "+9876543210"])
def test_unusual_parent_numbers_are_never_guessed(raw):
    with pytest.raises(ValueError):
        phone_number(raw)


def test_allowance_refusal_is_told_apart_from_a_short_wait(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    monkeypatch.setenv("WHATSAPP_UNIQUE_PARENTS_PER_24H", "1")
    monkeypatch.setenv("WHATSAPP_DISPATCH_DATABASE_URL", f"sqlite:///{(tmp_path / 'dispatch.sqlite').as_posix()}")
    engine = create_engine("sqlite:///:memory:")
    control = DispatchControl(engine)
    now = [1000.0]
    monkeypatch.setattr("services.whatsapp_dispatch.time.time", lambda: now[0])
    try:
        ticket, reason = control.reserve_with_reason("business", "parent-a", "one")
        assert ticket and reason is None
        control.outcome("business", "parent-a", ticket, None)
        # A sibling's offer to the same, already counted parent only waits for pacing.
        assert control.reserve_with_reason("business", "parent-a", "two") == (None, "recipient_busy")
        assert control.reserve_with_reason("business", "parent-b", "three") == (None, "allowance")
        now[0] += 7
        assert control.reserve_with_reason("business", "parent-a", "four")[0]
    finally:
        control.engine.dispose()
        engine.dispose()


def test_a_resolved_warning_reopens_when_the_failure_repeats(flow):
    with flow.sessions.begin() as session:
        flow._job_warning(session, "failed:example", "Parent reply needs review", "first failure")
    with flow.sessions.begin() as session:
        session.scalar(select(FlowInbox).where(FlowInbox.event_key == "failed:example")).resolved = 1
    with flow.sessions.begin() as session:
        flow._job_warning(session, "failed:example", "Parent reply needs review", "second failure")
    with flow.sessions() as session:
        item = session.scalar(select(FlowInbox).where(FlowInbox.event_key == "failed:example"))
        assert (item.resolved, item.text) == (0, "second failure")


def test_held_centre_is_reported_in_preview_and_save(flow, monkeypatch):
    import flow_test.service as service

    real = service.generate_weekly_schedule

    def with_held_centre(problem, settings=None):
        result = real(problem, settings=settings)
        result.statistics["blocked_locations"] = [min(problem.location_capacities)]
        result.unmet.append(UnmetRequirement(problem.requirements[0].patient_id, problem.requirements[0].id,
                                             problem.requirements[0].department_id, 1, "SAVED_BOOKING_NEEDS_REVIEW"))
        return result

    monkeypatch.setattr(service, "generate_weekly_schedule", with_held_centre)
    flow.next_offer_date = lambda: date(2026, 10, 30)
    preview, _pinned = flow.preview_daily(TARGET)
    with flow.sessions() as session:
        warning = session.scalar(select(FlowInbox).where(FlowInbox.reason == "Centre planning needs review"))
        centre = session.scalar(select(Location.name).order_by(Location.id).limit(1))
    assert warning is not None and warning.resolved == 0  # Not lost with the unsaved preview.
    assert preview.statistics["held_centres"] == [centre] and preview.statistics["held_children"]
    saved = flow.save_calendar(preview, TARGET, expected_offer_on_save=False)
    assert saved["complete"] is False and saved["heldCentres"] == [centre]
    assert saved["message"].startswith("INCOMPLETE") and saved["childrenNotPlanned"]


def test_repair_failure_while_loading_is_recorded_and_stops_after_three_attempts(flow, monkeypatch):
    job_id = declined_repair_job(flow)
    monkeypatch.setattr("flow_test.service.load_scheduling_problem", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fictional loader failure")))
    for attempt in (1, 2, 3):
        assert flow.process_repairs()
        with flow.sessions.begin() as session:
            job = session.get(FlowJob, job_id)
            assert job.status == ("FAILED" if attempt == 3 else "QUEUED")
            job.next_attempt_at = None
            warning = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"failed:{job.event_key}"))
            assert warning.reason == "Replacement search needs review" and "fictional loader failure" in warning.text
    assert not flow.process_repairs()  # A failed job is never picked up again on its own.


def test_repair_with_no_replacement_tells_staff(flow, monkeypatch):
    declined_repair_job(flow)
    monkeypatch.setattr("flow_test.service.fill_released_openings",
                        lambda problem, openings, settings: SimpleNamespace(status="FEASIBLE", validation={}, appointments=list(problem.pinned)))
    assert flow.process_repairs()
    with flow.sessions() as session:
        assert session.scalar(select(FlowJob.status).where(FlowJob.kind == "REPAIR")) == "DONE"
        assert session.scalar(select(FlowInbox.id).where(FlowInbox.reason == "No replacement found"))


def test_repair_that_lost_its_lease_saves_nothing(flow, monkeypatch):
    job_id = declined_repair_job(flow)
    with flow.sessions() as session:
        before = len(list(session.scalars(select(Appointment.id))))

    def taken_over(problem, openings, settings):
        with flow.sessions.begin() as session:
            session.execute(update(FlowJob).where(FlowJob.id == job_id).values(locked_by="another-worker"))
        extra = openings[0]
        return SimpleNamespace(status="FEASIBLE", validation={}, appointments=list(problem.pinned) + [extra])

    monkeypatch.setattr("flow_test.service.fill_released_openings", taken_over)
    assert flow.process_repairs()
    with flow.sessions() as session:
        assert len(list(session.scalars(select(Appointment.id)))) == before


def test_failure_from_a_stale_repair_worker_leaves_the_new_owner_alone(flow, monkeypatch):
    job_id = declined_repair_job(flow)

    def taken_over_then_fails(problem, openings, settings):
        with flow.sessions.begin() as session:
            session.execute(update(FlowJob).where(FlowJob.id == job_id).values(locked_by="worker-b"))
        raise RuntimeError("fictional slow failure")

    monkeypatch.setattr("flow_test.service.fill_released_openings", taken_over_then_fails)
    assert flow.process_repairs()
    with flow.sessions() as session:
        job = session.get(FlowJob, job_id)
        assert (job.status, job.locked_by) == ("PROCESSING", "worker-b")


def test_sending_warning_reopens_but_delivery_overdue_stays_dismissed(flow):
    flow.generate(TARGET)
    flow.publish(TARGET)
    with flow.sessions.begin() as session:
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER").limit(1))
        flow._attention(session, job, "WhatsApp sending paused", "first pause.")
        flow._attention(session, job, "WhatsApp delivery overdue", "overdue.", reopen=False)
    with flow.sessions.begin() as session:
        for item in session.scalars(select(FlowInbox).where(FlowInbox.event_key.like("attention:%"))):
            item.resolved = 1
    with flow.sessions.begin() as session:
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER").limit(1))
        flow._attention(session, job, "WhatsApp sending paused", "second pause.")
        flow._attention(session, job, "WhatsApp delivery overdue", "overdue.", reopen=False)
    with flow.sessions() as session:
        paused = session.scalar(select(FlowInbox).where(FlowInbox.reason == "WhatsApp sending paused"))
        overdue = session.scalar(select(FlowInbox).where(FlowInbox.reason == "WhatsApp delivery overdue"))
        assert (paused.resolved, paused.text) == (0, "second pause. Booking unchanged.")
        assert overdue.resolved == 1


def test_subscribe_again_closes_an_offer_that_is_no_longer_valid(flow, workflow_clock):
    workflow_clock(date(2026, 10, 18), 10)  # The day before the visits.
    phone = RECIPIENTS["TEST-A"]
    flow.generate(TARGET)
    inbound(flow, phone, "unsubscribe")
    flow.publish(TARGET)
    flow.drain()
    token = next(o["token"] for o in flow.state(TARGET)["offers"] if o["patient"] == "TEST-A")
    with flow.sessions.begin() as session:
        session.get(FlowOffer, token).state = "CLOSED"
    inbound(flow, phone, "subscribe")
    flow.drain()
    assert phone not in {payload["recipient"] for kind, payload in flow.calls if kind == "OFFER"}
    assert next(o for o in flow.state(TARGET)["offers"] if o["patient"] == "TEST-A")["send_status"] == "OBSOLETE"


def test_unsubscribe_holds_messages_and_subscribe_restarts_them(flow, workflow_clock):
    workflow_clock(date(2026, 10, 18), 10)  # The day before the visits.
    phone = RECIPIENTS["TEST-A"]
    flow.generate(TARGET)
    inbound(flow, phone, "  UnSubscribe ")
    inbound(flow, RECIPIENTS["TEST-B"], "please unsubscribe me")  # Not the exact word: no change.
    with flow.sessions() as session:
        listed = flow.unsubscribed_numbers(session)
        assert list(listed) == [phone] and listed[phone]["patients"] == ["TEST-A"]
        assert session.scalar(select(FlowInbox.id).where(FlowInbox.reason == "Parent unsubscribed from WhatsApp"))
        assert session.scalar(select(AuditLog.id).where(AuditLog.action == "whatsapp_unsubscribed"))
    flow.publish(TARGET)
    flow.drain()
    assert phone not in {payload["recipient"] for kind, payload in flow.calls if kind == "OFFER"}
    state = flow.state(TARGET)
    held = next(o for o in state["offers"] if o["patient"] == "TEST-A")
    assert held["send_status"] == "UNSUBSCRIBED"
    assert state["unsubscribed"][0]["patients"] == ["TEST-A"] and state["unsubscribed"][0]["phone"].endswith(phone[-4:])
    inbound(flow, phone, "Subscribe")
    with flow.sessions() as session:
        assert flow.unsubscribed_numbers(session) == {}
        assert session.scalar(select(FlowInbox.resolved).where(FlowInbox.reason == "Parent unsubscribed from WhatsApp")) == 1
        assert session.scalar(select(AuditLog.id).where(AuditLog.action == "whatsapp_subscribed"))
    # The offer held while unsubscribed is still valid, so it is sent now.
    flow.drain()
    assert phone in {payload["recipient"] for kind, payload in flow.calls if kind == "OFFER"}


@pytest.mark.parametrize("module,operation", [("main_workflow.client", "export/failures"),
                                              ("flow_test.zoho_daily", "export/failures")])
def test_command_line_clients_allow_the_failure_report(monkeypatch, module, operation):
    import importlib
    from contextlib import contextmanager
    from io import BytesIO

    target = importlib.import_module(module)
    sent = []

    class Opener:
        @contextmanager
        def open(self, request, timeout):
            sent.append(request.full_url)
            yield BytesIO(b'{"recorded": 1}')

    monkeypatch.setattr(target, "build_opener", lambda *a: Opener())
    monkeypatch.setenv("MAIN_WORKFLOW_APP_URL", "http://127.0.0.1:9")
    if module == "main_workflow.client":
        result = target.api(SimpleNamespace(admin_token="fictional"), operation, {"batchId": "b", "failures": []})
    else:
        result = target.test_api("http://127.0.0.1:9", "fictional", operation, {"batchId": "b", "failures": []})
    assert result == {"recorded": 1} and sent[0].endswith("/" + operation)


def test_push_says_so_when_staff_tasks_could_not_be_created(monkeypatch):
    import flow_test.zoho_push as push

    rows = [{"appointmentId": 1, "fingerprint": "fp1", "fields": {"Scheduler_Key": "test-ns:1", "Reg_ID": "FIC1",
             "Therapy_Date": "10-11-2026"}}]

    def exchange(operation, body):
        if operation == "preview":
            return {"target": "fictional", "changes": rows}
        if operation == "prepare":
            return {"id": "batch-1", "changes": rows, "acknowledged": {}, "assumedAcceptedVisits": 0}
        if operation == "failures":
            raise ValueError("Unsupported operations request")
        return {}

    monkeypatch.setattr(push, "validate_schema", lambda *a, **k: None)
    monkeypatch.setattr(push, "read_owned_keys", lambda settings, token, keys: {})
    monkeypatch.setattr(push, "write_records", lambda *a, errors, **k: errors.update({"test-ns:1": "Zoho code 3002: x"}) or {})
    with pytest.raises(ValueError, match=r"FIC1 10-11-2026.*staff tasks could NOT be created"):
        push.run_push(SimpleNamespace(refresh_token=None), "token", "", "", date(2026, 11, 10), apply=True,
                      exchange=exchange, verify=lambda s: None)


def test_single_update_keeps_zohos_explanation(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    import flow_test.zoho_push as push

    body = json.dumps({"code": 3002, "message": "Fictional: Doctor is inactive"}).encode()

    def rejected(request, timeout):
        raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body))

    monkeypatch.setattr(push, "open_zoho_request", rejected)
    settings = SimpleNamespace(api_url="http://127.0.0.1", account_owner="o", app_link_name="a",
                               form_link_name="f", report_link_name="r")
    fields = {name: "fictional" for name in push.REQUIRED_FIELDS} | {"Scheduler_Key": "test-ns:1"}
    with pytest.raises(ValueError, match="Doctor is inactive"):
        push.write_record(settings, "token", fields, "123", verify=lambda s: None)


def test_bulk_rejection_is_matched_only_when_the_order_is_proven():
    from flow_test.zoho_push import record_create_rejections

    fields = [{"Scheduler_Key": f"test-ns:{index}"} for index in (1, 2, 3)]
    accepted = lambda key, ident: {"code": 3000, "data": {"Scheduler_Key": key, "ID": ident}}
    rejected = {"code": 3002, "message": "Fictional: Doctor is inactive"}
    # Same order as sent: the reason belongs to row 2.
    errors = {}
    record_create_rejections(fields, {"result": [accepted("test-ns:1", "1"), rejected, accepted("test-ns:3", "3")]},
                             {"test-ns:1": "1", "test-ns:3": "3"}, errors)
    assert errors == {"test-ns:2": "Zoho code 3002: Fictional: Doctor is inactive"}
    # Shuffled response with two missing rows: never guess, keep the reasons for staff.
    errors = {}
    record_create_rejections(fields, {"result": [accepted("test-ns:3", "3"), rejected,
                                                 {"code": 3002, "message": "Fictional: Date invalid"}]},
                             {"test-ns:3": "3"}, errors)
    assert set(errors) == {"test-ns:1", "test-ns:2"}
    assert all("without naming them" in text and "Doctor is inactive" in text and "Date invalid" in text
               for text in errors.values())


def test_zoho_push_names_each_failed_record_and_saves_the_rest(monkeypatch):
    import flow_test.zoho_push as push

    rows = [{"appointmentId": index, "fingerprint": f"fp{index}", "fields": {
        "Scheduler_Key": f"test-ns:{index}", "Reg_ID": f"FIC{index}", "PatientName": "Fictional", "Doctor": "Fictional",
        "Therapy_Date": "10-11-2026", "TherapyTime": "09:00 AM", "Status": "CONFIRMED"}} for index in (1, 2, 3)]
    calls = []

    def exchange(operation, body):
        calls.append((operation, body))
        if operation == "preview":
            return {"target": "fictional", "changes": rows, "wouldAssumeAcceptedVisits": 0, "awaitingFinalisation": [], "lastPush": None}
        if operation == "prepare":
            return {"id": "batch-1", "changes": rows, "acknowledged": {}, "assumedAcceptedVisits": 0}
        return {}

    def write_records(settings, token, fields, *, verify, key_prefix, errors):
        errors["test-ns:2"] = "Zoho code 3002: Therapy_Date is invalid"
        return {"test-ns:1": "101", "test-ns:3": "103"}

    monkeypatch.setattr(push, "validate_schema", lambda *a, **k: None)
    monkeypatch.setattr(push, "read_owned_keys", lambda settings, token, keys: {})
    monkeypatch.setattr(push, "write_records", write_records)
    settings = SimpleNamespace(refresh_token=None)
    with pytest.raises(ValueError, match=r"FIC2 10-11-2026: Zoho code 3002: Therapy_Date is invalid"):
        push.run_push(settings, "token", "", "", date(2026, 11, 10), apply=True, exchange=exchange, verify=lambda s: None)
    acked = next(body for operation, body in calls if operation == "ack")
    assert {r["appointmentId"] for r in acked["records"]} == {1, 3}
    failed = next(body for operation, body in calls if operation == "failures")
    assert failed["failures"] == [{"appointmentId": 2, "key": "test-ns:2", "patient": "FIC2", "date": "10-11-2026",
                                   "reason": "Zoho code 3002: Therapy_Date is invalid"}]
    assert "finish" not in {operation for operation, _ in calls}


def test_failed_export_record_becomes_a_staff_task_that_closes_once_saved(flow):
    flow.generate(TARGET)
    flow.publish(TARGET)
    flow.drain()
    for offer in flow.state(TARGET)["offers"]:
        flow.simulate(offer["token"], "YES")
    flow.process_events()
    batch = prepare_export(flow, TARGET)
    row = batch["changes"][0]
    record_export_failures(flow, batch["id"], [{"appointmentId": row["appointmentId"], "reason": "Zoho code 3002: invalid"}])
    with flow.sessions() as session:
        task = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"zoho-export:{row['appointmentId']}"))
        assert task.resolved == 0 and task.patient_code == row["fields"]["Reg_ID"] and "Zoho code 3002" in task.text
    acknowledge_export(flow, batch["id"], row["appointmentId"], row["fingerprint"], "555")
    with flow.sessions() as session:
        assert session.scalar(select(FlowInbox.resolved).where(FlowInbox.event_key == f"zoho-export:{row['appointmentId']}")) == 1

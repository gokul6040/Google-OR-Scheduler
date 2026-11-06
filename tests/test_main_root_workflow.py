"""The full main / app uses retained test records without touching main storage."""

from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from config import SOLVER_SETTINGS
from database.models import Appointment, AuditLog, Patient
from flow_test.models import FlowJob, FlowOffer, FlowSetting
from flow_test.service import FlowTest
from flow_test.zoho_import import apply_reports
from main_workflow import mount
from main_workflow.retained_data import ExistingTestWorkflow, TestDataRuntime as RootTestRuntime
from tests.test_flow_test_mount import request


DAY = date(2026, 10, 23)
KEY = "dummy-root-main-access-key-at-least-32-characters"


@pytest.fixture
def root_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_TEST_RECIPIENTS_B64", "e30=")
    monkeypatch.setenv("MAIN_WORKFLOW_LIVE", "false")
    path = tmp_path / "existing-test.sqlite"
    original = FlowTest("sqlite:///" + path.as_posix())
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    apply_reports(original, reports)
    original.generate(DAY, ["TEST-H"])
    original.engine.dispose()
    monkeypatch.setenv("MAIN_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("MAIN_WORKFLOW_REQUIRE_ACCESS_KEY", "true")
    monkeypatch.setenv("MAIN_WORKFLOW_DATA_SOURCE", "test")
    monkeypatch.setenv("MAIN_WORKFLOW_ADMIN_TOKEN", KEY)
    monkeypatch.setenv("MAIN_WORKFLOW_TEST_DATABASE_URL", "sqlite:///" + path.as_posix())
    monkeypatch.delenv("MAIN_WORKFLOW_TEST_DATABASE_SCHEMA", raising=False)
    yield path


@pytest.fixture
def root_main(root_store, monkeypatch):
    instance = RootTestRuntime()
    instance.flow.next_offer_date = lambda: DAY
    monkeypatch.setattr(mount, "_runtime", instance)
    yield instance
    instance.close()


def payload(path, body=None, token=KEY):
    status, raw = request(path, body, token=token)
    return status, json.loads(raw)


def test_main_root_serves_full_app_and_existing_test_database(root_main, monkeypatch):
    import web_app

    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: pytest.fail("Main storage was opened"))
    status, html = request("/")
    assert status == 200 and b'/main-workflow-config.js' in html
    assert b'src="/app.js?v=' in html and b'/test/shared.js' not in html
    status, config = request("/main-workflow-config.js")
    assert status == 200 and b'"apiPrefix": ""' in config and b'"dataSource": "test"' in config
    assert payload("/api/overview")[1]["planningMode"] == "day"
    patients = payload("/api/patients")[1]["patients"]
    assert {p["code"] for p in patients} == {f"TEST-{c}" for c in "ABCDEFGH"}
    state = payload(f"/api/state?date={DAY}")[1]
    assert state["appointments"][0]["patient"] == "TEST-H"
    assert state["test_data"] and state["allow_simulation"]
    assert root_main.flow.solver_settings == SOLVER_SETTINGS
    assert payload(f"/test/api/state?date={DAY}")[1] == state


def test_patient_status_requires_reason_and_survives_sync(root_main):
    patient = payload("/api/patients?q=TEST-A")[1]["patients"][0]
    path = f"/api/patients/{patient['id']}/status"
    assert payload(path, {"active": False, "reason": "   "})[0] == 400
    assert payload(path, {"active": "false", "reason": "Paused"})[0] == 400
    assert payload(path, {"active": False, "reason": "Paused"}, token="wrong")[0] == 401

    generate = {"start": str(DAY), "patients": ["TEST-A"], "searchSeconds": 20}
    status, draft = payload("/api/schedules/generate", generate)
    assert status == 201 and draft["newSessions"] > 0
    inactive = {"active": False, "reason": "  Paused by family  "}
    assert payload(path, inactive) == (200, {"active": False})
    assert payload(path, inactive) == (200, {"active": False})
    assert payload(f"/api/schedules/{draft['previewId']}/save", {})[0] == 409
    assert payload("/api/schedules/generate", generate)[1]["newSessions"] == 0

    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    apply_reports(root_main.flow, reports)
    assert not payload("/api/patients?q=TEST-A")[1]["patients"][0]["active"]
    assert payload(path, {"active": True}) == (200, {"active": True})
    assert payload("/api/schedules/generate", generate)[1]["newSessions"] > 0
    with root_main.flow.sessions() as session:
        history = list(session.scalars(select(AuditLog).where(
            AuditLog.entity == f"patient:{patient['id']}", AuditLog.action == "patient_status_changed",
        ).order_by(AuditLog.id)))
        assert [json.loads(item.new_value) for item in history] == [
            {"active": False, "reason": "Paused by family"}, {"active": True, "reason": None},
        ]
        assert all(item.timestamp and item.user == root_main.flow.admin_id for item in history)


@pytest.mark.parametrize("sent", [False, True])
def test_deactivation_withdraws_unsent_offers_and_flags_sent_ones(root_main, monkeypatch, sent):
    import web_app

    class ClinicClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(DAY.year, DAY.month, DAY.day, tzinfo=tz)

    monkeypatch.setattr(web_app, "datetime", ClinicClock)
    _, draft = payload("/api/schedules/generate", {
        "start": str(DAY), "patients": ["TEST-A"], "searchSeconds": 20,
    })
    assert payload(f"/api/schedules/{draft['previewId']}/save", {})[0] == 200
    if sent:
        assert payload("/api/process", {})[0] == 200
        before = payload("/api/zoho/export/preview", {"date": str(DAY)})[1]["wouldAssumeAcceptedVisits"]
    patient = payload("/api/patients?q=TEST-A")[1]["patients"][0]
    assert payload(f"/api/patients/{patient['id']}/status", {
        "active": False, "reason": "Paused by family",
    })[0] == 200

    state = payload(f"/api/state?date={DAY}")[1]
    offer = next(item for item in state["offers"] if item["patient"] == "TEST-A")
    bookings = {item["status"] for item in state["appointments"] if item["id"] in offer["appointment_ids"]}
    if sent:
        assert offer["send_status"] == "SIMULATED"
        assert bookings == {"PENDING_CONFIRMATION"}
        assert any(item["reason"] == "Inactive patient — review existing offer" for item in state["inbox"])
        preview = payload("/api/zoho/export/preview", {"date": str(DAY)})[1]
        assert preview["wouldAssumeAcceptedVisits"] == before - 1
        assert set(offer["appointment_ids"]) <= set(preview["awaitingFinalisation"])
    else:
        assert (offer["send_status"], offer["state"]) == ("OBSOLETE", "CLOSED")
        assert bookings == {"CANCELLED_OTHER"}
    assert next(item for item in state["appointments"] if item["patient"] == "TEST-H")["status"] in {
        "PROPOSED", "PENDING_CONFIRMATION",
    }


@pytest.mark.parametrize("path", ["/webhooks", "/webhooks/", "/calendar/next", "/missing-page?view=schedule"])
def test_main_fallback_pages_include_workflow_login_and_test_database_config(root_main, monkeypatch, path):
    import web_app

    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: pytest.fail("Main storage was opened"))
    status, html = request(path, token="wrong")
    assert status == 200
    assert b'/main-workflow-config.js' in html
    assert b'/main-workspace.js' in html
    assert payload("/api/overview", token="wrong")[0] == 401


def test_main_fallback_keeps_whatsapp_verification_endpoint(root_main, monkeypatch):
    import web_app
    from services.whatsapp_service import WhatsAppWebhookSettings

    monkeypatch.setattr(web_app, "webhook_settings_from_environment",
                        lambda: WhatsAppWebhookSettings("verify-token", "app-secret"))
    status, body = request("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=challenge-value")
    assert status == 200 and body == b"challenge-value"
    assert request("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=challenge-value")[0] == 403


def test_hosted_main_needs_no_key_while_test_and_workflow_remain_keyed(root_main, monkeypatch):
    import web_app

    monkeypatch.setenv("MAIN_WORKFLOW_REQUIRE_ACCESS_KEY", "false")
    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: pytest.fail("Main storage was opened"))
    # Hosted access must not accidentally retain the loopback-only restriction.
    monkeypatch.setattr(mount, "local_request_allowed", lambda _request: False)
    assert payload("/api/overview", token="")[0] == 200
    status, patients = payload("/api/patients", token="")
    assert status == 200 and {p["code"] for p in patients["patients"]} == {f"TEST-{c}" for c in "ABCDEFGH"}
    assert payload(f"/api/state?date={DAY}", token="")[0] == 200
    assert payload("/api/schedules/generate", {"start": str(DAY), "patients": ["TEST-A"], "searchSeconds": 20}, token="")[0] == 201
    assert payload("/test/api/state", token="")[0] == 401
    assert payload("/workflow/api/patients", token="")[0] == 401
    assert payload("/api/reset", {}, token="")[0] == 404
    status, config = request("/main-workflow-config.js", token="")
    assert status == 200 and b'"requiresAccessKey": false' in config
    assert b'"dataSource": "test"' in config


@pytest.mark.parametrize("headers", [
    {"Host": "scheduler.example", "Origin": "https://elsewhere.example"},
    {"Host": "scheduler.example", "Sec-Fetch-Site": "cross-site"},
])
def test_hosted_main_rejects_cross_origin_mutations(root_main, monkeypatch, headers):
    monkeypatch.setenv("MAIN_WORKFLOW_REQUIRE_ACCESS_KEY", "false")
    responses = []
    handler = SimpleNamespace(command="POST", path="/api/schedules/generate", headers=headers,
                              close_connection=False, send_json=lambda body, status: responses.append((status, body)))
    mount.dispatch(handler, prefix="")
    assert responses[0][0] == 403


def test_root_api_auth_and_unknown_actions_never_fall_through(root_main, monkeypatch):
    import web_app

    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: pytest.fail("Main storage was opened"))
    assert payload("/api/patients", token="wrong")[0] == 401
    assert payload("/api/source/initialize", {}, token="wrong")[0] == 401
    for path in ("source/initialize", "reset", "generate", "assistant/messages", "zoho/sync"):
        assert payload("/api/" + path, {})[0] == 404
    assert payload("/api/zoho/status")[1]["source"] == "nidhin.kumar_turnb/test-gokul"
    assert len(payload("/api/patients")[1]["patients"]) == 8


def test_local_test_app_needs_no_key_but_rejects_cross_origin_and_nonlocal_hosts(root_main, monkeypatch):
    from http.server import ThreadingHTTPServer
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, Request, build_opener
    from web_app import TherapySchedulerHandler

    monkeypatch.setenv("MAIN_WORKFLOW_LOCAL_NO_AUTH", "true")
    server = ThreadingHTTPServer(("127.0.0.1", 0), TherapySchedulerHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    base = f"http://127.0.0.1:{server.server_port}"

    def fetch(path, *, headers=None, body=None):
        request = Request(base + path, headers=headers or {}, data=body)
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    try:
        assert fetch("/api/overview")[0] == 200
        assert fetch("/api/patients")[0] == 200
        assert fetch(f"/api/state?date={DAY}")[0] == 200
        assert fetch("/api/reset", body=b"{}", headers={"Content-Type": "application/json"})[0] == 404
        assert fetch("/api/patients", headers={"Origin": "https://example.com"})[0] == 403
        assert fetch("/api/patients", headers={"Host": f"example.com:{server.server_port}"})[0] == 403
        assert fetch("/api/patients", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert fetch("/workflow/api/patients")[0] == 401
        assert b'"localNoAuth": true' in fetch("/main-workflow-config.js")[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_password_free_mode_refuses_non_loopback_binding(monkeypatch):
    import web_app

    monkeypatch.setenv("MAIN_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("MAIN_WORKFLOW_DATA_SOURCE", "test")
    monkeypatch.setenv("MAIN_WORKFLOW_LOCAL_NO_AUTH", "true")
    with pytest.raises(ValueError, match="loopback"):
        web_app.run(host="0.0.0.0", port=0)


def test_root_main_review_save_offer_replies_and_attendance(root_main):
    status, draft = payload("/api/schedules/generate", {"start": str(DAY), "patients": ["TEST-A"], "searchSeconds": 20})
    assert status == 201 and draft["appointments"]
    assert draft["autoOfferOnSave"] and not draft["liveMessaging"]
    path = f"/api/schedules/{draft['previewId']}/save"
    status, saved = payload(path, {})
    assert status == 200 and saved["offers"] >= 1
    assert payload(path, {})[1] == saved
    assert payload("/api/process", {})[0] == 200
    state = payload(f"/api/state?date={DAY}")[1]
    offer = next(item for item in state["offers"] if item["patient"] == "TEST-A")
    assert offer["send_status"] == "SIMULATED"
    assert payload("/api/simulate", {"token": offer["token"], "action": "HELP"})[0] == 200
    payload("/api/process", {})
    assert payload(f"/api/state?date={DAY}")[1]["inbox"]
    payload("/api/simulate", {"token": offer["token"], "action": "YES"})
    payload("/api/process", {})
    confirmed = next(a for a in payload(f"/api/state?date={DAY}")[1]["appointments"] if a["patient"] == "TEST-A")
    assert confirmed["status"] == "CONFIRMED"
    assert payload("/api/attendance", {"id": confirmed["id"], "status": "COMPLETED"})[0] == 200
    assert next(a for a in payload(f"/api/state?date={DAY}")[1]["appointments"] if a["id"] == confirmed["id"])["status"] == "COMPLETED"


def test_main_workflow_rejects_multiweek_planning_without_writes(root_main):
    with root_main.flow.sessions() as session:
        before = session.scalar(select(func.count()).select_from(Appointment))
    for mode in ("rolling", "week", None):
        status, result = payload("/api/schedules/generate", {
            "start": str(DAY), "planningMode": mode, "searchSeconds": 20,
        })
        assert status == 400 and "one selected day" in result["error"]
    assert root_main.previews == {}
    with root_main.flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Appointment)) == before
        assert session.scalar(select(func.count()).select_from(FlowJob)) == 0


def test_main_workflow_does_not_expose_pull_forward_chain(root_main):
    for body in (None, {"previewToken": "obsolete", "requestId": "obsolete"}):
        assert payload("/api/appointments/1/replacement-chain", body)[0] == 404
    with root_main.flow.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Appointment)) == 1
        assert session.scalar(select(func.count()).select_from(FlowJob)) == 0


def test_suggestion_api_uses_selected_visit_scope(root_main, monkeypatch):
    import web_app
    from database.models import AppointmentStatus

    with root_main.flow.sessions.begin() as session:
        row = session.scalar(select(Appointment))
        row.status = AppointmentStatus.CANCELLED_PATIENT
        identifier = row.id
    scopes = []
    monkeypatch.setattr(web_app, "find_replacement_candidates",
                        lambda *_args, **kwargs: scopes.append(kwargs.get("whole_visit")) or [])
    path = f"/api/appointments/{identifier}/replacement-candidates"
    assert payload(path)[0] == 200
    assert payload(path + "?wholeVisit=false")[0] == 200
    assert scopes == [True, False]
    assert payload(path + "?wholeVisit=invalid")[0] == 400


def test_retained_schema_suggestions_do_not_require_main_waitlist_table(root_main):
    from database.models import AppointmentStatus
    from sqlalchemy import inspect

    with root_main.flow.sessions.begin() as session:
        assert not inspect(session.connection()).has_table("waitlist_entries")
        row = session.scalar(select(Appointment))
        row.status = AppointmentStatus.CANCELLED_PATIENT
        identifier = row.id
    status, result = payload(f"/api/appointments/{identifier}/replacement-candidates?wholeVisit=false")
    assert status == 200 and result["candidates"]
    with root_main.flow.sessions() as session:
        assert not inspect(session.connection()).has_table("waitlist_entries")


def test_existing_store_startup_has_no_seed_ddl_or_mutations(root_store):
    from sqlalchemy.engine import Engine

    statements = []

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")):
            statements.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        flow = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
        assert len(flow.state(DAY)["appointments"]) == 1
        with flow.sessions() as session:
            assert session.scalar(select(func.count(Patient.id))) == 8
        flow.engine.dispose()
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    assert statements == []


def test_paused_live_test_store_never_consumes_outbound_jobs(root_store):
    flow = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    flow.next_offer_date = lambda: DAY
    flow.publish(DAY)
    with flow.sessions.begin() as session:
        session.get(FlowSetting, "mode").value = "live"
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        job.status = "QUEUED"
        before = job.id, job.status, job.attempts, job.payload
    flow.engine.dispose()
    calls = []
    paused = ExistingTestWorkflow("sqlite:///" + root_store.as_posix(), send=lambda *args: calls.append(args))
    assert paused.live and not paused.messaging_enabled
    assert paused.process_batch() == 0 and not paused.process_one() and paused.drain() == 0
    with paused.sessions() as session:
        job = session.get(FlowJob, before[0])
        assert (job.id, job.status, job.attempts, job.payload) == before
    assert paused.state(DAY)["messaging_paused"] and not paused.state(DAY)["allow_simulation"]
    with pytest.raises(ValueError, match="preview database"):
        paused.simulate("dummy", "YES")
    assert calls == []
    paused.engine.dispose()


def test_remote_sender_keeps_local_worker_idle_and_refreshes_new_source(root_store, monkeypatch):
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    initial = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    with initial.sessions.begin() as session:
        session.get(FlowSetting, "mode").value = "live"
    initial.engine.dispose()
    monkeypatch.setenv("MAIN_WORKFLOW_REMOTE_SENDER", "true")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    sent = []
    local = ExistingTestWorkflow("sqlite:///" + root_store.as_posix(), send=lambda *args: sent.append(args))
    remote = ExistingTestWorkflow("sqlite:///" + root_store.as_posix(), send=lambda *_args: "dummy")
    try:
        assert local.remote_worker and local.messaging_enabled
        assert local.publish(DAY) == 1
        assert local.process_one() is False and local.process_batch() == 0 and local.drain() == 0
        with local.sessions() as session:
            job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
            assert job.status == "WAITING_FOR_CONTACT" and job.attempts == 0
        changed = deepcopy(reports)
        row = next(item for item in changed["Plan_Report"] if item["RegistrationID"] == "TEST-H")
        row["PatientName"] = "Updated fictional child"
        row["Modified_Time"] = "24-Oct-2026 04:50:00"
        apply_reports(remote, changed)
        assert next(p for p in local.scenario["patients"] if p["code"] == "TEST-H")["name"] != row["PatientName"]
        local.refresh_source()
        assert next(p for p in local.scenario["patients"] if p["code"] == "TEST-H")["name"] == row["PatientName"]
        assert sent == []
    finally:
        local.engine.dispose()
        remote.engine.dispose()


def test_remote_source_import_is_forwarded_to_the_existing_sender(root_store, monkeypatch):
    import flow_test.zoho_daily
    from flow_test.reset_live import BASE_URL

    initial = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    with initial.sessions.begin() as session:
        session.get(FlowSetting, "mode").value = "live"
    initial.engine.dispose()
    monkeypatch.setenv("MAIN_WORKFLOW_REMOTE_SENDER", "true")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow = ExistingTestWorkflow("sqlite:///" + root_store.as_posix(), send=lambda *_args: "dummy")
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    calls = []

    def remote_api(base, token, path, body):
        calls.append((base, token, path))
        assert body["source"] == flow.source_name
        apply_reports(flow, body["reports"])
        return {"patients": 8, "messages_sent": 0}

    monkeypatch.setattr(flow_test.zoho_daily, "test_api", remote_api)
    instance = SimpleNamespace(flow=flow, token="test-key", previews={"old": {}})
    output = []
    request = SimpleNamespace(read_json=lambda: {"source": flow.source_name, "reports": reports},
                              send_json=output.append)
    try:
        RootTestRuntime.zoho_dispatch(request, instance, "/api/zoho/import")
        assert calls == [(BASE_URL, "test-key", "import")]
        assert output == [{"patients": 8, "messages_sent": 0}]
        assert instance.previews == {}
    finally:
        flow.engine.dispose()


def test_main_startup_with_test_data_skips_legacy_initialization(root_main, monkeypatch):
    import web_app

    called = []

    class Server:
        def __init__(self, *_args):
            pass

        def serve_forever(self):
            called.append("served")

        def server_close(self):
            called.append("closed")

    def forbidden(*_args):
        pytest.fail("Main startup touched legacy initialization or sender")

    for name in ("init_db", "initialize_source_database", "init_admin_booking_tables", "start_help_reply_worker"):
        monkeypatch.setattr(web_app, name, forbidden)
    monkeypatch.setattr(web_app, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(mount, "close", lambda: None)
    web_app.run()
    assert called == ["served", "closed"]


def test_missing_test_file_is_not_created(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="does not exist"):
        ExistingTestWorkflow("sqlite:///" + missing.as_posix())
    assert not missing.exists()


def test_root_webhook_never_opens_legacy_main_storage(root_main, monkeypatch):
    import hashlib
    import hmac
    import web_app

    raw = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    secret = "dummy-root-webhook-secret"
    monkeypatch.setenv("FLOW_TEST_LIVE", "false")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "dummy-business")
    monkeypatch.setattr(web_app, "webhook_settings_from_environment", lambda: SimpleNamespace(app_secret=secret))

    def forbidden(*_args):
        pytest.fail("Workflow webhook touched the legacy main queue")

    monkeypatch.setattr(web_app, "ProductionSessionLocal", forbidden)
    monkeypatch.setattr(web_app, "record_button_clicks", forbidden)
    monkeypatch.setattr(web_app, "queue_help_replies", forbidden)
    handler = web_app.TherapySchedulerHandler.__new__(web_app.TherapySchedulerHandler)
    handler.read_raw_body = lambda: raw
    handler.headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}
    responses = []
    handler.send_json = responses.append
    handler.handle_whatsapp_webhook()
    assert responses == [{"ok": True}]


def test_paused_main_processes_received_receipts_without_touching_sends(root_store, monkeypatch):
    initial = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    with initial.sessions.begin() as session:
        session.get(FlowSetting, "mode").value = "live"
        outgoing = initial._job(session, "retained-outbound", "TEXT", {"recipient": "919999999999", "text": "Retained"})
        outbound_id = outgoing.id
        initial._job(session, "received-status", "STATUS", {"id": "dummy-provider-id", "recipient_id": "919999999999", "status": "delivered"})
    initial.engine.dispose()
    instance = RootTestRuntime()
    monkeypatch.setattr(mount, "_runtime", instance)
    try:
        code, result = payload("/api/process", {})
        assert code == 200 and result == {"processed": 1, "outboundMessaging": "OFF"}
        with instance.flow.sessions() as session:
            outgoing = session.get(FlowJob, outbound_id)
            assert outgoing.status == "QUEUED" and outgoing.attempts == 0
    finally:
        instance.close()


def test_main_using_test_data_rejects_another_zoho_source(root_main):
    code, _result = payload("/api/zoho/import", {"source": "another-owner/another-app", "reports": {}})
    assert code == 409
    assert len(payload("/api/patients")[1]["patients"]) == 8


def test_new_paused_main_offers_are_not_visible_to_a_running_test_sender(root_store):
    initial = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    with initial.sessions.begin() as session:
        session.get(FlowSetting, "mode").value = "live"
    initial.engine.dispose()
    paused = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    try:
        assert paused.publish(DAY) == 1
        with paused.sessions.begin() as session:
            offer = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
            assert offer.status == "WAITING_FOR_ENABLE" and offer.attempts == 0
            acknowledgement = paused._job(session, "new-paused-ack", "TEXT", {"recipient": "919999999999", "text": "Ack"})
            assert acknowledgement.status == "WAITING_FOR_ENABLE"
            # Both old and new senders claim only QUEUED jobs.
            assert not list(session.scalars(select(FlowJob).where(FlowJob.status == "QUEUED")))
        assert paused.drain() == 0
    finally:
        paused.engine.dispose()


def test_paused_offer_uses_updated_contact_and_stays_held_during_import(root_store, monkeypatch, workflow_clock):
    workflow_clock(DAY)
    initial = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    with initial.sessions.begin() as session:
        session.get(FlowSetting, "mode").value = "live"
    initial.engine.dispose()
    paused = ExistingTestWorkflow("sqlite:///" + root_store.as_posix())
    assert paused.publish(DAY) == 1
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    row = next(row for row in reports["Plan_Report"] if row["RegistrationID"] == "TEST-H")
    row["Parent_No"] = "919999999901"
    row["Modified_Time"] = "24-Oct-2026 04:50:00"
    apply_reports(paused, reports)
    with paused.sessions() as session:
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        assert job.status == "WAITING_FOR_ENABLE" and job.attempts == 0
        assert json.loads(job.payload)["recipient"] == "919999999901"
    paused.engine.dispose()
    # No network sender: verify explicit later activation rechecks current binding.
    monkeypatch.setenv("MAIN_WORKFLOW_LIVE", "true")
    enabled = ExistingTestWorkflow("sqlite:///" + root_store.as_posix(), send=lambda *_args: "dummy-only")
    try:
        with enabled.sessions() as session:
            job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
            assert job.status == "QUEUED" and job.attempts == 0
            assert json.loads(job.payload)["recipient"] == "919999999901"
    finally:
        enabled.engine.dispose()

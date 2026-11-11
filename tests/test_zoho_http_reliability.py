"""Opt-in real HTTP checks against the saved fictional Postman Zoho simulation."""

from dataclasses import replace
from datetime import date
import json
import os
import time
from urllib.request import Request

import pytest
from sqlalchemy import select

from database.models import Appointment, AppointmentStatus
from flow_test.models import FlowSetting
from flow_test.service import FlowTest
from flow_test.zoho_export import acknowledge_export, export_preview, finish_export, prepare_export
from flow_test.zoho_import import CHECKPOINT_KEY, apply_reports
from flow_test.zoho_push import export_settings, run_push
from services.zoho_creator_service import get_report_records, open_zoho_request, refresh_access_token


PORT = os.getenv("ZOHO_SIMULATION_PORT")
pytestmark = pytest.mark.skipif(not PORT, reason="Local Postman Zoho simulation not selected")
DAY = date(2026, 10, 22)


@pytest.fixture
def provider():
    assert PORT and PORT.isdigit()
    base = f"http://127.0.0.1:{PORT}/zoho"
    settings = replace(export_settings(), api_url=base, accounts_url=base,
                       client_id="dummy", client_secret="dummy", refresh_token="dummy")
    control(base, {"count": 0})
    yield settings
    control(base, {"count": 0})


def control(base, fault):
    request = Request(base + "/__test__/fault", data=json.dumps(fault).encode(),
                      headers={"Content-Type": "application/json"})
    with open_zoho_request(request, timeout=5) as response:
        return json.load(response)


@pytest.mark.parametrize("status", [429, 503])
def test_later_import_page_retries_without_duplicates(provider, status):
    token = refresh_access_token(provider)["access_token"]
    expected = get_report_records(provider, token, "Plan_Report")
    control(provider.api_url, {"path": "report/Plan_Report", "cursor": "next-page",
                               "count": 1, "status": status, "retryAfter": 2})
    start = time.monotonic()
    actual = get_report_records(provider, token, "Plan_Report")
    assert time.monotonic() - start >= 2
    assert actual == expected
    assert len({row["ID"] for row in actual}) == len(actual)


def test_refresh_retries_temporary_http_failure(provider):
    control(provider.api_url, {"path": "/oauth/v2/token", "count": 1, "status": 503})
    assert refresh_access_token(provider)["access_token"] == "simulation-token"


@pytest.mark.parametrize("payload", [[], {"code": 3000}, {"code": 3000, "data": None},
                                    {"code": 3000, "data": ""}, {"code": 3000, "data": {}}])
def test_invalid_report_response_is_not_an_empty_success(provider, payload):
    control(provider.api_url, {"path": "report/Plan_Report", "count": 1, "status": 200, "payload": payload})
    with pytest.raises(ValueError, match="invalid report response"):
        get_report_records(provider, "simulation-token", "Plan_Report")


def test_exhausted_read_preserves_source_checkpoint_and_bookings(provider, tmp_path):
    token = refresh_access_token(provider)["access_token"]
    reports = {name: get_report_records(provider, token, name) for name in ("Plan_Report", "All_Consultants")}
    flow = FlowTest(f"sqlite:///{tmp_path / 'import.sqlite'}")
    try:
        apply_reports(flow, reports, window={"from": None, "to": "2026-10-22T05:30:00Z"})
        flow.generate(DAY)
        before = flow.state(DAY)["appointments"]
        with flow.sessions() as session:
            cache = session.get(FlowSetting, "zoho_test_source").value
        control(provider.api_url, {"path": "report/Plan_Report", "cursor": "next-page", "count": 4, "status": 503})
        with pytest.raises(ValueError, match="report read"):
            get_report_records(provider, token, "Plan_Report")
        with flow.sessions() as session:
            assert json.loads(session.get(FlowSetting, CHECKPOINT_KEY).value) == "2026-10-22T05:30:00Z"
            assert session.get(FlowSetting, "zoho_test_source").value == cache
        assert flow.state(DAY)["appointments"] == before
    finally:
        flow.engine.dispose()


def exchange_for(flow):
    def exchange(operation, body):
        if operation == "preview":
            return export_preview(flow, DAY)
        if operation == "prepare":
            return prepare_export(flow, DAY)
        if operation == "ack":
            return acknowledge_export(flow, body["batchId"], body["appointmentId"],
                                      body["fingerprint"], body["recordId"], records=body.get("records"))
        return finish_export(flow, body["batchId"])
    return exchange


@pytest.fixture
def booked(tmp_path, legacy_flow_scenario):
    flow = FlowTest(f"sqlite:///{tmp_path / 'export.sqlite'}", scenario=legacy_flow_scenario)
    flow.generate(DAY)
    with flow.sessions.begin() as session:
        for item in session.scalars(select(Appointment)):
            item.status = AppointmentStatus.CONFIRMED
    yield flow
    flow.engine.dispose()


def push(provider, flow, exchange=None):
    return run_push(provider, "simulation-token", "", "", DAY, apply=True,
                    exchange=exchange or exchange_for(flow))


@pytest.mark.parametrize("disconnect", [True, False])
def test_commit_with_lost_response_reconciles_without_another_create(provider, booked, disconnect):
    control(provider.api_url, {"path": "form/Scheduler", "count": 1, "afterWrite": True,
                               "disconnect": disconnect})
    result = push(provider, booked)
    assert result["reconciled"] == result["pushed"] and result["created"] == 0
    assert push(provider, booked)["pushed"] == 0
    # Subsequent changes update the same key instead of creating another row.
    with booked.sessions.begin() as session:
        first = session.scalar(select(Appointment).order_by(Appointment.id))
        first.status = AppointmentStatus.COMPLETED
    control(provider.api_url, {"path": "report/Scheduler_Report/", "count": 0})
    result = push(provider, booked)
    assert result["updated"] == 1 and result["created"] == 0


def test_restart_after_write_before_ack_recovers_same_key(provider, booked):
    exchange = exchange_for(booked)

    def interrupt(operation, body):
        if operation == "ack":
            raise ValueError("Simulated process exit before acknowledgement")
        return exchange(operation, body)

    with pytest.raises(ValueError, match="process exit"):
        push(provider, booked, interrupt)
    pending = export_preview(booked, DAY)
    batch = pending["openBatch"]
    assert batch and pending["lastPush"] is None and batch["acknowledged"] == {}
    first_key = batch["changes"][0]["fields"]["Scheduler_Key"]
    restarted = FlowTest(str(booked.engine.url), scenario=booked.scenario)
    try:
        result = push(provider, restarted)
        assert result["reconciled"] == len(batch["changes"]) and result["resumedBatch"] == batch["id"]
        rows = get_report_records(provider, "simulation-token", "Scheduler_Report", criteria="ID != 0")
        assert len([row for row in rows if row.get("Scheduler_Key") == first_key]) == 1
        assert export_preview(restarted, DAY)["openBatch"] is None
    finally:
        restarted.engine.dispose()


def test_uncommitted_export_failure_retains_batch_for_explicit_resume(provider, booked):
    control(provider.api_url, {"path": "form/Scheduler", "count": 1, "status": 503})
    with pytest.raises(ValueError, match="batch retained"):
        push(provider, booked)
    preview = export_preview(booked, DAY)
    batch = preview["openBatch"]
    assert batch and not batch["acknowledged"] and preview["lastPush"] is None
    result = push(provider, booked)
    assert result["resumedBatch"] == batch["id"] and result["created"] == len(batch["changes"])


@pytest.mark.parametrize("update", [False, True])
def test_actual_write_timeout_is_reconciled_before_another_write(provider, booked, monkeypatch, update):
    from flow_test import zoho_push

    if update:
        push(provider, booked)
        with booked.sessions.begin() as session:
            first = session.scalar(select(Appointment).order_by(Appointment.id))
            first.status = AppointmentStatus.COMPLETED
        previous = get_report_records(provider, "simulation-token", "Scheduler_Report", criteria="ID != 0")
        first_key = export_preview(booked, DAY)["changes"][0]["fields"]["Scheduler_Key"]
        record_id = next(row["ID"] for row in previous if row.get("Scheduler_Key") == first_key)
        path = "report/Scheduler_Report/" + record_id
    else:
        path = "form/Scheduler"
    control(provider.api_url, {"path": path, "count": 1, "afterWrite": True, "delayMs": 700})

    def shorter_timeout(request, timeout):
        return open_zoho_request(request, timeout=0.15 if request.get_method() in {"POST", "PATCH"} else timeout)

    monkeypatch.setattr(zoho_push, "open_zoho_request", shorter_timeout)
    result = push(provider, booked)
    assert result["reconciled"] == (1 if update else result["pushed"])
    assert result["created"] == 0
    assert push(provider, booked)["pushed"] == 0


def test_bulk_partial_success_saves_good_rows_and_resumes_rejected_row(provider, booked):
    request = Request(provider.api_url + "/__test__/reject-scheduler-row", data=b'{"index":1}',
                      headers={"Content-Type": "application/json"})
    with open_zoho_request(request, timeout=5) as response:
        assert json.load(response)["armed"]
    with pytest.raises(ValueError, match="batch retained"):
        push(provider, booked)
    batch = export_preview(booked, DAY)["openBatch"]
    assert len(batch["acknowledged"]) == len(batch["changes"]) - 1
    assert export_preview(booked, DAY)["lastPush"] is None
    result = push(provider, booked)
    assert result["created"] == 1 and result["resumedBatch"] == batch["id"]
    rows = get_report_records(provider, "simulation-token", "Scheduler_Report", criteria="ID != 0")
    keys = {r["fields"]["Scheduler_Key"] for r in batch["changes"]}
    assert len([row for row in rows if row["Scheduler_Key"] in keys]) == len(keys)


def test_bulk_attendance_update_preserves_owned_ids(provider, booked):
    push(provider, booked)
    before = get_report_records(provider, "simulation-token", "Scheduler_Report", criteria="ID != 0")
    with booked.sessions.begin() as session:
        for appointment in session.scalars(select(Appointment)):
            appointment.status = AppointmentStatus.COMPLETED
    result = push(provider, booked)
    assert result["updated"] == 4 and result["created"] == 0
    after = get_report_records(provider, "simulation-token", "Scheduler_Report", criteria="ID != 0")
    assert {r["Scheduler_Key"]: r["ID"] for r in before} == {r["Scheduler_Key"]: r["ID"] for r in after}
    assert push(provider, booked)["pushed"] == 0

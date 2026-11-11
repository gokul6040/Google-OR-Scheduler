"""Integration boundaries must not publish stale contacts or hide delivery failures."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import time

import pytest
from sqlalchemy import select

from flow_test.models import FlowJob, FlowSetting
from flow_test.service import FlowTest
from flow_test.zoho_export import export_preview, prepare_export
from flow_test.zoho_import import TIMEZONE_KEY, apply_reports


DAY = date(2026, 10, 23)


@pytest.fixture
def imported_flow(tmp_path):
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    calls = []
    flow = FlowTest(f"sqlite:///{(tmp_path / 'pipeline.sqlite').as_posix()}",
                    send=lambda kind, payload: calls.append((kind, deepcopy(payload))) or f"dummy-{len(calls)}")
    flow.next_offer_date = lambda: DAY
    apply_reports(flow, reports)
    yield flow, reports, calls
    flow.engine.dispose()


def mark_old_source(flow):
    with flow.sessions.begin() as session:
        session.get(FlowSetting, TIMEZONE_KEY).value = json.dumps(
            {"Added_Time": "Asia/Kolkata", "Modified_Time": "UTC"})


def test_reconciliation_required_blocks_save_and_offer_atomically(imported_flow):
    flow, _reports, calls = imported_flow
    result, _ = flow.preview_daily(DAY)
    result = replace(result, appointments=[a for a in result.appointments if a.date == DAY])
    mark_old_source(flow)
    with pytest.raises(ValueError, match="--apply --reconcile"):
        flow.save_calendar(result, DAY, expected_offer_on_save=True)
    assert not flow.state(DAY)["appointments"]
    assert not flow.state(DAY)["offers"]
    assert calls == []


@pytest.mark.parametrize("change_phone", [False, True])
def test_queued_offer_waits_for_reconciliation_and_resumes_once(imported_flow, change_phone):
    flow, reports, calls = imported_flow
    flow.generate(DAY, ["TEST-H"])
    flow.publish(DAY)
    before = flow.state(DAY)["appointments"]
    mark_old_source(flow)
    assert flow.process_one()
    assert calls == []
    assert flow.state(DAY)["offers"][0]["send_status"] == "WAITING_FOR_SYNC"
    assert not flow.process_one()

    corrected = deepcopy(reports)
    if change_phone:
        corrected["Plan_Report"][0]["Parent_No"] = "917777777777"
    window = {"from": None, "to": "2026-10-24T00:00:00Z"}
    invalid = deepcopy(corrected)
    invalid["Plan_Report"][0]["Schedule"] = "invalid"
    result = apply_reports(flow, invalid, window=window, full_snapshot=True)
    assert result["partial"] and "TEST-H" in result["quarantined"]
    assert flow.state(DAY)["offers"][0]["send_status"] == "WAITING_FOR_SYNC"
    assert calls == []

    apply_reports(flow, corrected, window=window, full_snapshot=True)
    assert flow.state(DAY)["appointments"] == before
    assert flow.state(DAY)["offers"][0]["send_status"] == "QUEUED"
    assert flow.process_one()
    assert len(calls) == 1
    assert calls[0][1]["recipient"] == corrected["Plan_Report"][0]["Parent_No"]
    assert not flow.process_one()


def test_reconciliation_required_blocks_manual_cutoff_without_confirming(imported_flow):
    flow, _reports, _calls = imported_flow
    flow.generate(DAY, ["TEST-H"])
    flow.publish(DAY)
    flow.drain()
    before = flow.state(DAY)["appointments"]
    mark_old_source(flow)
    with pytest.raises(ValueError, match="--apply --reconcile"):
        prepare_export(flow, DAY)
    assert flow.state(DAY)["appointments"] == before
    assert export_preview(flow, DAY)["openBatch"] is None


def test_failed_delivery_creates_one_staff_warning_without_resending(imported_flow, monkeypatch):
    flow, _reports, calls = imported_flow
    flow.generate(DAY, ["TEST-H"])
    flow.publish(DAY)
    flow.drain()
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "dummy-business")
    with flow.sessions() as session:
        original = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        recipient = json.loads(original.payload)["recipient"]
        message_id = original.message_id

    def callback(status, stamp, phone=recipient):
        return {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "dummy-business"},
            "statuses": [{"id": message_id, "recipient_id": phone, "status": status,
                          "timestamp": str(stamp), "errors": [{"code": 131042,
                          "message": "sensitive provider body must not appear"}]}],
        }}]}]}

    stamp = int(time.time())
    # A different registered sender cannot attach a failure to this offer.
    flow.recipients["OTHER"] = "918888888888"
    flow.receive(callback("failed", stamp, phone="918888888888"))
    flow.drain()
    assert not flow.state(DAY)["inbox"]
    for offset in (0, 0, 1):
        flow.receive(callback("failed", stamp + offset))
        flow.drain()
    state = flow.state(DAY)
    assert len(state["inbox"]) == 1
    warning = state["inbox"][0]
    assert warning["patient"] == "TEST-H"
    assert "delivery failed" in warning["reason"].lower()
    assert "131042" in warning["text"]
    assert "sensitive" not in warning["text"]
    assert state["offers"][0]["delivery"] == "failed"
    assert state["appointments"][0]["status"] == "PENDING_CONFIRMATION"
    assert len(calls) == 1
    # An out-of-order receipt cannot erase a known failure or repeat its warning.
    flow.receive(callback("sent", stamp + 2))
    flow.drain()
    assert flow.state(DAY)["offers"][0]["delivery"] == "failed"
    assert len(flow.state(DAY)["inbox"]) == 1
    assert prepare_export(flow, DAY)["assumedAcceptedVisits"] == 0


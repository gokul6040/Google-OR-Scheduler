"""Real main importer against fictional paginated Zoho registration reports."""

from datetime import date
import json
import os
from urllib.request import Request

import pytest
from sqlalchemy import create_engine, select

from database.db import Base
from database.models import Appointment
from flow_test.models import FlowSetting
from flow_test.zoho_import import CHECKPOINT_KEY, apply_reports
from main_workflow.config import WorkflowConfig
from main_workflow.provider import ZohoProvider
from main_workflow.service import MainWorkflow
from main_workflow.storage import prepare_storage
from services.zoho_creator_service import open_zoho_request


PORT = os.getenv("ZOHO_SIMULATION_PORT")
pytestmark = pytest.mark.skipif(not PORT, reason="Local Postman Zoho simulation not selected")
FIRST = "2026-10-22T05:30:00Z"
SECOND = "2026-10-23T05:30:00Z"


def fixture_control(base, route, body):
    request = Request(base + "/__test__/" + route, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    with open_zoho_request(request, timeout=5) as response:
        return json.load(response)


@pytest.fixture
def main_source(tmp_path, monkeypatch):
    assert PORT and PORT.isdigit()
    base = f"http://127.0.0.1:{PORT}/zoho"
    for name, value in {"API_URL": base, "ACCOUNTS_URL": base, "CLIENT_ID": "dummy",
                        "CLIENT_SECRET": "dummy", "REFRESH_TOKEN": "dummy"}.items():
        monkeypatch.setenv("MAIN_WORKFLOW_ZOHO_" + name, value)
    config = WorkflowConfig("x" * 32, "operator", "fictional-clinic", "eic-pilot",
                            "Mapped_Plans", "Mapped_Consultants", "Scheduler", "Scheduler_Report",
                            registration_report="All_Registrations")
    engine = create_engine(f"sqlite:///{(tmp_path / 'main.sqlite').as_posix()}")
    Base.metadata.create_all(engine)
    prepare_storage(engine, config.source_name)
    flow = MainWorkflow(engine, config, send=lambda *_: pytest.fail("Import sent a message"))
    fixture_control(base, "fault", {"count": 0})
    fixture_control(base, "registration-phone", {"registration": "TEST-H", "phone": "919999999999"})
    try:
        yield flow, ZohoProvider(config), base
    finally:
        fixture_control(base, "fault", {"count": 0})
        fixture_control(base, "registration-phone", {"registration": "TEST-H", "phone": "919999999999"})
        engine.dispose()


def test_paginated_reports_join_and_refresh_phone_without_a_plan_edit(main_source):
    flow, provider, base = main_source
    reports = provider.fetch(None, FIRST)
    assert len(reports["Plan_Report"]) == len(reports["All_Registrations"]) == 8
    assert all("Parent_No" not in row and "Modified_Time" not in row for row in reports["Plan_Report"])
    assert apply_reports(flow, reports, window={"from": None, "to": FIRST})["patients"] == 8
    flow.generate(date(2026, 10, 23))
    with flow.sessions() as session:
        before = [(a.id, a.date, a.slot_index, a.status) for a in session.scalars(select(Appointment))]
    fixture_control(base, "registration-phone", {"registration": "TEST-H", "phone": "918888888888"})
    updated = provider.fetch(FIRST, SECOND)
    assert updated["Plan_Report"] == reports["Plan_Report"]
    assert apply_reports(flow, updated, window={"from": FIRST, "to": SECOND})["changed"]
    assert flow.recipients["TEST-H"] == "918888888888"
    with flow.sessions() as session:
        assert [(a.id, a.date, a.slot_index, a.status) for a in session.scalars(select(Appointment))] == before
        cache = session.get(FlowSetting, "zoho_test_source").value
        assert "Extra_Plan_Column" not in cache
        assert "Extra_Registration_Column" not in cache
    assert not apply_reports(flow, provider.fetch(SECOND, "2026-10-24T05:30:00Z"),
                             window={"from": SECOND, "to": "2026-10-24T05:30:00Z"})["changed"]


def test_registration_page_failure_keeps_the_last_successful_import(main_source):
    flow, provider, base = main_source
    apply_reports(flow, provider.fetch(None, FIRST), window={"from": None, "to": FIRST})
    with flow.sessions() as session:
        before = session.get(FlowSetting, "zoho_test_source").value
    fixture_control(base, "fault", {"path": "report/All_Registrations", "cursor": "next-page",
                                   "count": 4, "status": 503})
    with pytest.raises(ValueError, match="source fetch failed"):
        provider.fetch(FIRST, SECOND)
    with flow.sessions() as session:
        assert session.get(FlowSetting, "zoho_test_source").value == before
        assert json.loads(session.get(FlowSetting, CHECKPOINT_KEY).value) == FIRST

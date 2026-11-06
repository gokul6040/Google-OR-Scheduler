"""Flexible source columns and authoritative registration contact joins."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from database.models import Appointment
from flow_test.models import FlowInbox, FlowSetting
from flow_test.service import FlowTest
from flow_test.zoho_import import CHECKPOINT_KEY, apply_reports, convert_reports
from main_workflow.config import WorkflowConfig
from services.zoho_source_mapping import REGISTRATION_REPORT
from tests.test_flow_test_mount import request
from tests.test_main_workflow import KEY, import_source, main_operations


FIRST = "2026-10-22T00:00:00Z"
SECOND = "2026-10-23T00:00:00Z"


@pytest.fixture
def joined_reports():
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    reports[REGISTRATION_REPORT] = []
    for index, plan in enumerate(reports["Plan_Report"]):
        reports[REGISTRATION_REPORT].append({
            "ID": str(900000 + index), "Registration Id": plan["RegistrationID"],
            "Phone": plan.pop("Parent_No"), "Added Time": "21-Oct-2026 09:00:00",
            "Unused medical field": {"ignored": ["fictional"]},
        })
    return reports


@pytest.fixture
def source_flow(tmp_path):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'mapped.sqlite').as_posix()}")
    yield flow
    flow.engine.dispose()


def test_display_names_and_extra_columns_build_the_existing_scheduler_inputs(joined_reports):
    expected = convert_reports(joined_reports)
    for plan in joined_reports["Plan_Report"]:
        for old, new in (("RegistrationID", "Registration Id"), ("PatientName", "Name"),
                         ("Added_Time", "Added Time"), ("Modified_Time", "Modified Time")):
            plan[new] = plan.pop(old)
        plan["Unrelated column"] = [1, {"nested": True}]
    assert convert_reports(joined_reports) == expected


def test_changing_column_order_and_unused_columns_does_not_change_the_projection(joined_reports, source_flow):
    assert apply_reports(source_flow, joined_reports)["changed"]
    for rows in joined_reports.values():
        for index, row in enumerate(rows):
            row.pop("Unused medical field", None)
            row["New unused column"] = {"ignored": True}
            rows[index] = dict(reversed(list(row.items())))
    assert not apply_reports(source_flow, joined_reports)["changed"]
    with source_flow.sessions() as session:
        cached = session.get(FlowSetting, "zoho_test_source").value
        assert "New unused column" not in cached
        assert "Unused medical field" not in cached


def test_registration_phone_overrides_an_old_phone_inside_the_plan(joined_reports):
    joined_reports["Plan_Report"][0]["Parent_No"] = "917777777777"
    scenario = convert_reports(joined_reports)
    assert next(p for p in scenario["patients"] if p["code"] == "TEST-H")["phone"] == "919999999999"


def test_latest_created_registration_wins_even_when_an_old_record_was_edited_later(joined_reports):
    first = joined_reports[REGISTRATION_REPORT][0]
    first["Modified Time"] = "23-Oct-2026 09:00:00"
    newer = {**first, "ID": "990000", "Phone": "918888888888",
             "Added Time": "22-Oct-2026 09:00:00", "Modified Time": "22-Oct-2026 09:00:00"}
    joined_reports[REGISTRATION_REPORT].append(newer)
    assert next(p for p in convert_reports(joined_reports)["patients"] if p["code"] == "TEST-H")["phone"] == "918888888888"


def test_modified_time_breaks_a_registration_added_time_tie(joined_reports):
    first = joined_reports[REGISTRATION_REPORT][0]
    joined_reports[REGISTRATION_REPORT].append({
        **first, "ID": "990000", "Phone": "918888888888", "Modified Time": "22-Oct-2026 09:00:00",
    })
    assert next(p for p in convert_reports(joined_reports)["patients"] if p["code"] == "TEST-H")["phone"] == "918888888888"


def test_ties_in_superseded_registrations_do_not_hide_a_unique_latest_record(joined_reports):
    first = joined_reports[REGISTRATION_REPORT][0]
    joined_reports[REGISTRATION_REPORT].extend([
        {**first, "ID": "990000"},
        {**first, "ID": "990001", "Phone": "918888888888", "Added Time": "22-Oct-2026 09:00:00"},
    ])
    assert next(p for p in convert_reports(joined_reports)["patients"] if p["code"] == "TEST-H")["phone"] == "918888888888"


@pytest.mark.parametrize("failure", ["missing_match", "missing_phone", "bad_phone", "timestamp_tie",
                                      "conflicting_alias", "missing_plan_field", "lookup_object"])
def test_bad_mapping_rolls_back_contacts_cache_checkpoint_and_bookings(joined_reports, source_flow, failure):
    apply_reports(source_flow, joined_reports, window={"from": None, "to": FIRST})
    source_flow.generate(date(2026, 10, 23))
    before = dict(source_flow.recipients)
    with source_flow.sessions() as session:
        cache = session.get(FlowSetting, "zoho_test_source").value
        bookings = [(a.id, a.status) for a in session.scalars(select(Appointment))]
    broken = deepcopy(joined_reports)
    row = broken[REGISTRATION_REPORT][0]
    if failure == "missing_match":
        broken[REGISTRATION_REPORT].pop(0)
    elif failure == "missing_phone":
        row.pop("Phone")
    elif failure == "bad_phone":
        row["Phone"] = "invalid"
    elif failure == "timestamp_tie":
        broken[REGISTRATION_REPORT].append({**row, "ID": "990000"})
    elif failure == "conflicting_alias":
        row["RegistrationID"] = "SOMEONE-ELSE"
    elif failure == "missing_plan_field":
        broken["Plan_Report"][0].pop("Schedule")
    else:
        row["Registration Id"] = {"ID": "123", "display_value": "TEST-H"}
    with pytest.raises(ValueError):
        apply_reports(source_flow, broken, window={"from": FIRST, "to": SECOND})
    assert source_flow.recipients == before
    with source_flow.sessions() as session:
        assert session.get(FlowSetting, "zoho_test_source").value == cache
        assert json.loads(session.get(FlowSetting, CHECKPOINT_KEY).value) == FIRST
        assert [(a.id, a.status) for a in session.scalars(select(Appointment))] == bookings
        assert session.scalar(select(FlowInbox).where(FlowInbox.reason.like("Zoho import blocked%"))) is not None


def test_phone_edit_without_modified_time_updates_an_unchanged_plan_and_survives_restart(joined_reports, source_flow):
    for rows in joined_reports.values():
        for row in rows:
            row.pop("Modified_Time", None)
    apply_reports(source_flow, joined_reports, window={"from": None, "to": FIRST})
    source_flow.generate(date(2026, 10, 23))
    with source_flow.sessions() as session:
        before = [(a.id, a.date, a.slot_index, a.status) for a in session.scalars(select(Appointment))]
    joined_reports[REGISTRATION_REPORT][0]["Phone"] = "918888888888"
    result = apply_reports(source_flow, joined_reports, window={"from": FIRST, "to": SECOND})
    assert result["changed"]
    assert source_flow.recipients["TEST-H"] == "918888888888"
    with source_flow.sessions() as session:
        assert [(a.id, a.date, a.slot_index, a.status) for a in session.scalars(select(Appointment))] == before
    restarted = FlowTest(str(source_flow.engine.url))
    try:
        assert restarted.recipients["TEST-H"] == "918888888888"
    finally:
        restarted.engine.dispose()


def test_unused_registration_does_not_require_a_phone(joined_reports):
    joined_reports[REGISTRATION_REPORT].append({
        "ID": "990000", "Registration Id": "NOT-IN-A-PLAN", "Added Time": "21-Oct-2026 09:00:00",
    })
    assert len(convert_reports(joined_reports)["patients"]) == 8


def test_registration_lookup_cannot_silently_fall_back_to_plan_phones(joined_reports, source_flow):
    apply_reports(source_flow, joined_reports, window={"from": None, "to": FIRST})
    joined_reports.pop(REGISTRATION_REPORT)
    with pytest.raises(ValueError, match="Keep All_Registrations"):
        apply_reports(source_flow, joined_reports, window={"from": FIRST, "to": SECOND})


def test_registration_record_cannot_be_reassigned_to_another_patient(joined_reports, source_flow):
    apply_reports(source_flow, joined_reports, window={"from": None, "to": FIRST})
    joined_reports[REGISTRATION_REPORT][0]["Registration Id"] = "SOMEONE-ELSE"
    with pytest.raises(ValueError, match="Changing an existing source Registration ID"):
        apply_reports(source_flow, joined_reports, window={"from": FIRST, "to": SECOND})


def test_old_unused_cached_columns_are_removed_on_the_next_sync(joined_reports, source_flow):
    apply_reports(source_flow, joined_reports, window={"from": None, "to": FIRST})
    with source_flow.sessions.begin() as session:
        item = session.get(FlowSetting, "zoho_test_source")
        cache = json.loads(item.value)
        cache["Plan_Report"][0]["Old unused column"] = "fictional"
        item.value = json.dumps(cache)
    # Plans may be absent from a pull without implying source deletion. The
    # retained row still needs the same projection and extra-column filtering.
    joined_reports["Plan_Report"] = []
    apply_reports(source_flow, joined_reports, window={"from": FIRST, "to": SECOND})
    with source_flow.sessions() as session:
        assert "Old unused column" not in session.get(FlowSetting, "zoho_test_source").value


def test_legacy_source_can_adopt_registration_snapshot_without_fake_edit_times(joined_reports, source_flow):
    legacy = deepcopy(joined_reports)
    legacy.pop(REGISTRATION_REPORT)
    for row in legacy["Plan_Report"]:
        row["Parent_No"] = "919999999999"
    apply_reports(source_flow, legacy, window={"from": None, "to": FIRST})
    for rows in joined_reports.values():
        for row in rows:
            row.pop("Modified_Time", None)
    assert not apply_reports(source_flow, joined_reports, window={"from": FIRST, "to": SECOND})["changed"]
    with source_flow.sessions() as session:
        cached = json.loads(session.get(FlowSetting, "zoho_test_source").value)
        assert all("Modified_Time" not in row for rows in cached.values() for row in rows)


def test_legacy_two_report_input_still_requires_real_modification_timestamps(joined_reports):
    joined_reports.pop(REGISTRATION_REPORT)
    joined_reports["Plan_Report"][0].pop("Modified_Time")
    with pytest.raises(ValueError, match="Added_Time/Modified_Time"):
        convert_reports(joined_reports)


def test_main_provider_fetches_complete_three_report_snapshots(monkeypatch):
    from main_workflow.provider import ZohoProvider
    from services.zoho_creator_service import settings_from_environment

    config = WorkflowConfig("x" * 32, "operator", "fictional-clinic", "eic-pilot", "Plans",
                            "Consultants", "Scheduler", "Scheduler_Report", registration_report="Registrations")
    provider = ZohoProvider(config)
    settings = settings_from_environment()
    monkeypatch.setattr(provider, "settings", lambda: settings)
    monkeypatch.setattr(provider, "verify", lambda _settings: None)
    monkeypatch.setattr("main_workflow.provider.refresh_access_token", lambda _: {"access_token": "dummy"})
    calls = []

    def read(_settings, _token, report, *, criteria):
        calls.append((report, criteria))
        return []

    monkeypatch.setattr("main_workflow.provider.get_report_records", read)
    assert set(provider.fetch(FIRST, SECOND)) == {"Plan_Report", "All_Consultants", REGISTRATION_REPORT}
    assert [name for name, _ in calls] == ["Plans", "Consultants", "Registrations"]
    assert all(criteria == "Added_Time < '23-Oct-2026 05:30:00'" for _, criteria in calls)


def test_registration_report_configuration_is_optional_and_validated(monkeypatch):
    values = {"ADMIN_TOKEN": "x" * 32, "ADMIN_ID": "operator", "ZOHO_OWNER": "fictional-clinic",
              "ZOHO_APP": "eic-pilot", "ZOHO_PLAN_REPORT": "Plans", "ZOHO_CONSULTANT_REPORT": "Consultants",
              "ZOHO_SCHEDULE_FORM": "Scheduler", "ZOHO_SCHEDULE_REPORT": "Scheduler_Report",
              "SOURCE_TIMEZONE": "Asia/Kolkata"}
    for name, value in values.items():
        monkeypatch.setenv("MAIN_WORKFLOW_" + name, value)
    monkeypatch.delenv("MAIN_WORKFLOW_ZOHO_REGISTRATION_REPORT", raising=False)
    assert WorkflowConfig.from_environment().registration_report == ""
    monkeypatch.setenv("MAIN_WORKFLOW_ZOHO_REGISTRATION_REPORT", "Registrations")
    assert WorkflowConfig.from_environment().registration_report == "Registrations"
    for invalid in ("../Registrations", "Plans", "Consultants"):
        monkeypatch.setenv("MAIN_WORKFLOW_ZOHO_REGISTRATION_REPORT", invalid)
        with pytest.raises(ValueError, match="registration report"):
            WorkflowConfig.from_environment()


def test_configured_main_import_joins_contacts_and_requires_registrations(main_operations, joined_reports):
    instance, _legacy, sends = main_operations
    instance.config = replace(instance.config, registration_report="Registrations")
    instance.flow.config = instance.config
    status, body = import_source(instance, joined_reports)
    assert status == 200, body
    assert json.loads(body)["patients"] == 8
    assert instance.flow.recipients["TEST-H"] == "919999999999"
    joined_reports.pop(REGISTRATION_REPORT)
    status, body = request("/workflow/api/zoho/import", {
        "source": instance.config.source_name, "reports": joined_reports,
    }, token=KEY)
    assert status == 409
    assert "requires All_Registrations" in json.loads(body)["error"]
    assert sends == []

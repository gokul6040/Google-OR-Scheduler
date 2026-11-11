"""Integrated Thanal snapshots using fictional records and disposable SQLite."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select

from config import SolverSettings
from database.db import Base
from database.models import Appointment, Patient, Therapist, TherapistCapability, TreatmentRequirement
from flow_test.models import FlowInbox, FlowJob, FlowOffer, FlowSetting
from flow_test.zoho_import import CHECKPOINT_KEY
from main_workflow.config import WorkflowConfig
from main_workflow.service import MainWorkflow
from main_workflow.storage import prepare_storage
from main_workflow.thanal_sync import BRANCH, PLANS, apply_snapshot, prepare_snapshot

FIRST = {"from": None, "to": "2026-11-08T00:00:00Z"}
SECOND = {"from": FIRST["to"], "to": "2026-11-09T00:00:00Z"}


def test_one_time_roster_keeps_source_issues_visible_and_future_new_patients_active(flow, reports):
    from services.patient_roster_service import apply_roster, prepare_roster
    bad_reg = deepcopy(reports["All_Registrations"][0])
    bad_reg.update(ID="102", Registration_Id="BAD-PLAN", Name2="Invalid plan child")
    reports["All_Registrations"].append(bad_reg)
    bad_plan = deepcopy(reports[PLANS][0])
    bad_plan.update(ID="302", Name={"ID": "102"})
    bad_plan["Schedule"][0]["Consultant"] = {}
    reports[PLANS].append(bad_plan)
    decisions = prepare_roster([{"code": "BAD-PLAN", "name": "Invalid plan child"},
                               {"code": "NO-PLAN", "name": "Listed without a plan"}],
                              prepare_snapshot(reports), reports)
    with flow.sessions.begin() as session:
        session.add(Patient(patient_code="OLDER", name="Earlier import", active=True))
        result = apply_roster(session, decisions, operation_id="fictional-baseline", today=date(2026, 11, 10))
    assert result["listed_blocked"] == 2 and result["active"] == 0
    with flow.sessions() as session:
        assert {p.patient_code for p in session.scalars(select(Patient))} == {
            "FAKE-1", "BAD-PLAN", "NO-PLAN", "OLDER"}
        assert all(not p.active for p in session.scalars(select(Patient)))
    new_reg = deepcopy(reports["All_Registrations"][0])
    new_reg.update(ID="103", Registration_Id="TOMORROW", Name2="New child")
    reports["All_Registrations"].append(new_reg)
    new_plan = deepcopy(reports[PLANS][0])
    new_plan.update(ID="303", Name={"ID": "103"})
    reports[PLANS].append(new_plan)
    apply_snapshot(flow, reports, window=FIRST)
    with flow.sessions() as session:
        patients = {p.patient_code: p.active for p in session.scalars(select(Patient))}
        assert patients["TOMORROW"] and not patients["FAKE-1"] and not patients["BAD-PLAN"]
    with pytest.raises(ValueError, match="already been applied"):
        with flow.sessions.begin() as session:
            apply_roster(session, decisions, operation_id="fictional-baseline", today=date(2026, 11, 11))


def test_roster_phone_and_inactive_doctor_both_prevent_activation(reports):
    from services.patient_roster_service import prepare_roster
    entries = [{"code": "FAKE-1", "name": "Fictional child"}]
    reports["All_Registrations"][0]["Phone_Number"] = "123"
    decision, = prepare_roster(entries, prepare_snapshot(reports), reports)
    assert not decision["active"] and decision["contact_issues"] == ["invalid_phone"]
    reports["All_Registrations"][0]["Phone_Number"] = "919999999999"
    reports["All_Consultants"][0]["Status"] = "Inactive"
    decision, = prepare_roster(entries, prepare_snapshot(reports), reports)
    assert not decision["active"] and decision["reasons"] == ["assigned_consultant_inactive"]


def test_roster_rejects_duplicate_codes_and_never_matches_by_name(reports):
    from services.patient_roster_service import prepare_roster
    entries = [{"code": "UNKNOWN", "name": "Fictional child"}]
    decisions = prepare_roster(entries, prepare_snapshot(reports), reports)
    unknown = next(r for r in decisions if r["listed"])
    assert not unknown["active"] and "registration_not_found" in unknown["reasons"]
    with pytest.raises(ValueError, match="unique registration"):
        prepare_roster(entries * 2, prepare_snapshot(reports), reports)


def test_roster_bulk_inactivation_preserves_confirmed_and_past_visits(flow, reports):
    from datetime import timedelta
    from database.models import AppointmentStatus, AuditLog
    from services.patient_status_service import change_patient_statuses
    apply_snapshot(flow, reports, window=FIRST)
    today = date(2026, 11, 10)
    with flow.sessions.begin() as session:
        patient = session.scalar(select(Patient))
        req = session.scalar(select(TreatmentRequirement))
        ids = []
        for day, status in [(today, AppointmentStatus.PROPOSED),
                            (today, AppointmentStatus.CONFIRMED),
                            (today - timedelta(days=1), AppointmentStatus.PROPOSED)]:
            appt = Appointment(patient_id=patient.id, treatment_requirement_id=req.id,
                               therapist_id=req.assigned_therapist_id, department_id=req.department_id,
                               location_id=req.location_id, date=day, slot_index=len(ids), status=status)
            session.add(appt)
            session.flush()
            ids.append(appt.id)
        assert change_patient_statuses(session, [(patient, False, "Roster baseline")], today, "tester") == 1
    with flow.sessions() as session:
        assert [session.get(Appointment, i).status for i in ids] == [
            AppointmentStatus.CANCELLED_OTHER, AppointmentStatus.CONFIRMED, AppointmentStatus.PROPOSED]
        assert session.scalar(select(AuditLog).where(AuditLog.action == "patient_status_changed"))


def test_activation_review_reports_all_problems_and_refreshes_after_correction(flow, reports):
    from services.patient_activation_service import activation_review
    reports["All_Registrations"][0]["Phone_Number"] = "123"
    reports["All_Consultants"][0]["Status"] = "Inactive"
    reports[PLANS][0]["Schedule"][0]["Per_Week"] = "0"
    with flow.sessions.begin() as session:
        session.add(Patient(patient_code="FAKE-1", name="Fictional child", active=False))
    apply_snapshot(flow, reports, window=FIRST)
    with flow.sessions() as session:
        patient = session.scalar(select(Patient))
        result = activation_review(session, patient)
        assert not result["canActivate"]
        message = " ".join(result["issues"])
        assert "phone" in message and "inactive" in message and "weekly sessions" in message
    reports["All_Registrations"][0]["Phone_Number"] = "919999999999"
    reports["All_Consultants"][0]["Status"] = "Active"
    reports[PLANS][0]["Schedule"][0]["Per_Week"] = "2"
    apply_snapshot(flow, reports, window=FIRST)
    with flow.sessions() as session:
        patient = session.scalar(select(Patient))
        assert not patient.active
        assert activation_review(session, patient)["canActivate"]


@pytest.fixture
def reports():
    return {"All_Registrations": [
        {"ID": "100", "Registration_Id": "FAKE-1", "Name2": "Fictional child",
         "Phone_Number": "919999999999", "Added_Time": "01/11/2026 08:00:00",
         "Branch": {"ID": "10", "Branch_Name": BRANCH}},
        {"ID": "101", "Registration_Id": "TEST-UNUSED", "Name2": "Unused fictional child",
         "Phone_Number": "123", "Added_Time": "01/11/2026 08:00:00"}],
        "All_Consultants": [{"ID": "200", "Name": "Fictional clinician", "Status": "Active",
            "Branch": {"ID": "10", "Branch_Name": BRANCH}, "Consultation_Type": "Full-Time",
            "Consultations": [{"ID": "20", "Value": "Speech"}, {"ID": "21", "Value": "Occupational"}]}],
        PLANS: [{"ID": "300", "Branch": {"ID": "10", "Branch_Name": BRANCH},
                 "Name": {"ID": "100", "Name2": "display value ignored"}, "Status": "Draft",
                 "Added_Time": "01/11/2026 09:00:00", "Schedule": [
                     {"ID": "400", "Therapy": {"ID": "20", "Value": "Speech"},
                      "Consultant": {"ID": "200"}, "Per_Week": "2", "No_Of_Week": "3"},
                     {"ID": "401", "Therapy": {"ID": "21", "Value": "Occupational"},
                      "Consultant": {"ID": "200"}, "Per_Week": "2", "No_Of_Week": "3"}]}]}


@pytest.fixture
def flow(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'daily.sqlite'}")
    Base.metadata.create_all(engine)
    prepare_storage(engine, "thanal/eic")
    config = WorkflowConfig("fictional-key-at-least-32-characters", "tester", "thanal", "eic",
                            PLANS, "All_Consultants", "Schedule_Adhoc_Trial", "Schedule_Adhoc_Trial_Report",
                            registration_report="All_Registrations")
    flow = MainWorkflow(engine, config, send=lambda *_: pytest.fail("Sync attempted a send"))
    flow.next_offer_date = lambda: date(2026, 11, 11)
    flow.solver_settings = SolverSettings(max_time_seconds=1)
    yield flow
    engine.dispose()


def count(flow, model):
    with flow.sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_daily_import_updates_all_sources_and_is_idempotent(flow, reports):
    result = apply_snapshot(flow, reports, window=FIRST)
    assert not result["partial"]
    assert count(flow, Therapist) == 1 and count(flow, TherapistCapability) == 2
    assert count(flow, Patient) == 1 and count(flow, TreatmentRequirement) == 2
    assert count(flow, Appointment) == count(flow, FlowOffer) == 0
    with flow.sessions() as session:
        assert len(json.loads(session.get(FlowSetting, "thanal_registration_snapshot").value)) == 2
        assert not list(session.scalars(select(FlowInbox)))  # Unused invalid phone is NOT a staff alert.
    reports["All_Registrations"][0].update(Name2="Renamed fictional child", Phone_Number="918888888888")
    reports["All_Consultants"][0]["Status"] = "Inactive"
    reports[PLANS][0]["Schedule"][0]["Per_Week"] = "3"
    apply_snapshot(flow, reports, window=SECOND)
    with flow.sessions() as session:
        assert session.scalar(select(Patient.name)) == "Renamed fictional child"
        assert not session.scalar(select(Therapist.active))
        assert len(list(session.scalars(select(TreatmentRequirement)))) == 2
        assert {r.sessions_per_week for r in session.scalars(select(TreatmentRequirement))} == {2, 3}
    assert flow.recipients["FAKE-1"] == "918888888888"


def test_wire_snapshot_discards_unrelated_clinical_data_without_mutating_source(reports):
    from main_workflow.thanal_sync import select_fields
    reports["All_Registrations"][0]["Aadhaar_No"] = "discard identity document"
    reports["All_Registrations"][0]["Current_Address"] = {"private": "discard"}
    original = deepcopy(reports)
    selected = select_fields(reports)
    assert reports == original
    assert "Aadhaar_No" not in selected["All_Registrations"][0]
    assert "Current_Address" not in selected["All_Registrations"][0]
    assert prepare_snapshot(selected)["patients"]


def test_invalid_phone_still_schedules_and_only_warns_when_offering_saved_visit(flow, reports):
    reports["All_Registrations"][0]["Phone_Number"] = "123"
    # Preserve the existing staff-controlled status on a source contact edit.
    # The revised activation rule prevents creating/reactivating such a child.
    with flow.sessions.begin() as session:
        session.add(Patient(patient_code="FAKE-1", name="Existing active child", active=True))
    apply_snapshot(flow, reports, window=FIRST)
    assert count(flow, FlowInbox) == 0
    result, _ = flow.preview_daily(flow.next_offer_date())
    assert result.appointments
    response = flow.save_calendar(replace(result, appointments=[a for a in result.appointments
                                 if a.date == flow.next_offer_date()]), flow.next_offer_date(), expected_offer_on_save=True)
    assert response["heldForReview"] and not response["offers"]
    assert count(flow, Appointment) > 0 and count(flow, FlowInbox) == 1


@pytest.mark.parametrize("bad_contact", [True, False])
def test_new_source_patient_with_invalid_phone_or_inactive_doctor_starts_inactive(flow, reports, bad_contact):
    if bad_contact:
        reports["All_Registrations"][0]["Phone_Number"] = "123"
    else:
        reports["All_Consultants"][0]["Status"] = "Inactive"
    apply_snapshot(flow, reports, window=FIRST)
    with flow.sessions() as session:
        assert not session.scalar(select(Patient)).active
    assert not flow.preview_daily(flow.next_offer_date())[0].appointments


def test_renewal_preserves_original_course_and_bookings(flow, reports):
    apply_snapshot(flow, reports, window=FIRST)
    flow.generate(flow.next_offer_date())
    before = count(flow, Appointment)
    old = deepcopy(reports[PLANS][0])
    reports[PLANS][0].update(ID="301", Added_Time="02/11/2026 09:00:00", Status="")
    reports[PLANS].append(old)
    apply_snapshot(flow, reports, window=SECOND)
    with flow.sessions() as session:
        assert {r.source_plan_id for r in session.scalars(select(TreatmentRequirement).where(
            TreatmentRequirement.active.is_(True)))} == {"301"}
    assert count(flow, TreatmentRequirement) == 4 and count(flow, Appointment) == before


def test_latest_bad_plan_never_falls_back_to_old_prescription_and_recovers(flow, reports):
    old = deepcopy(reports[PLANS][0])
    old.update(ID="299", Added_Time="30/10/2026 09:00:00")
    reports[PLANS].append(old)
    reports[PLANS][0]["Schedule"][0]["Consultant"] = {}
    result = apply_snapshot(flow, reports, window=FIRST)
    assert result["partial"] and not result["checkpoint"]
    assert count(flow, TreatmentRequirement) == 0
    assert count(flow, FlowInbox) == 1
    reports[PLANS][0]["Schedule"][0]["Consultant"] = {"ID": "200"}
    assert not apply_snapshot(flow, reports, window=FIRST)["partial"]
    with flow.sessions() as session:
        assert all(r.resolved for r in session.scalars(select(FlowInbox)))


def test_unrelated_invalid_consultant_does_not_block_valid_current_plan(flow, reports):
    reports["All_Consultants"].append({"ID": "201", "Name": "Unrelated fictional clinician", "Status": ""})
    assert not apply_snapshot(flow, reports, window=FIRST)["partial"]
    assert count(flow, Therapist) == 1 and count(flow, TreatmentRequirement) == 2


def test_identity_failure_rolls_back_masters_and_checkpoint(flow, reports):
    apply_snapshot(flow, reports, window=FIRST)
    with flow.sessions.begin() as session:
        mapping = session.get(FlowSetting, "thanal_registration_identity_map")
        mapping.value = json.dumps({"100": "OTHER-CODE"})
    reports["All_Consultants"][0]["Status"] = "Inactive"
    with pytest.raises(ValueError, match="identity review"):
        apply_snapshot(flow, reports, window=SECOND)
    with flow.sessions() as session:
        assert session.scalar(select(Therapist.active))
        assert json.loads(session.get(FlowSetting, CHECKPOINT_KEY).value) == FIRST["to"]


def test_stale_checkpoint_and_out_of_scope_branch_are_rejected(flow, reports):
    apply_snapshot(flow, reports, window=FIRST)
    with pytest.raises(ValueError, match="checkpoint"):
        apply_snapshot(flow, reports, window=FIRST)
    reports[PLANS][0]["Branch"] = {"ID": "11", "Branch_Name": "Different centre"}
    with pytest.raises(ValueError, match="outside"):
        prepare_snapshot(reports)


def test_corrected_phone_rebinds_only_unattempted_offers(flow, reports):
    apply_snapshot(flow, reports, window=FIRST)
    flow.generate(flow.next_offer_date())
    flow.publish(flow.next_offer_date())
    assert count(flow, FlowOffer) == 1
    reports["All_Registrations"][0]["Phone_Number"] = "918888888888"
    apply_snapshot(flow, reports, window=SECOND)
    with flow.sessions() as session:
        assert session.scalar(select(FlowOffer.recipient)) == "918888888888"
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        assert json.loads(job.payload)["recipient"] == "918888888888"


def test_new_registration_and_plan_are_adopted_on_the_next_run(flow, reports):
    apply_snapshot(flow, reports, window=FIRST)
    registration = deepcopy(reports["All_Registrations"][0])
    registration.update(ID="102", Registration_Id="FAKE-2", Name2="New fictional child")
    reports["All_Registrations"].append(registration)
    plan = deepcopy(reports[PLANS][0])
    plan.update(ID="302", Name={"ID": "102"})
    reports[PLANS].append(plan)
    apply_snapshot(flow, reports, window=SECOND)
    assert count(flow, Patient) == 2 and count(flow, TreatmentRequirement) == 4
    assert count(flow, Appointment) == count(flow, FlowOffer) == 0


def test_inactive_consultant_holds_an_existing_unsent_scheduled_offer(flow, reports):
    apply_snapshot(flow, reports, window=FIRST)
    flow.generate(flow.next_offer_date())
    flow.publish(flow.next_offer_date())
    reports["All_Consultants"][0]["Status"] = "Inactive"
    apply_snapshot(flow, reports, window=SECOND)
    assert flow.process_one()
    with flow.sessions() as session:
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        assert job.status == "NEEDS_ATTENTION" and job.send_started_at is None


def test_sent_offer_stays_bound_to_original_phone_and_reminder_cannot_use_it(flow, reports):
    from datetime import datetime
    apply_snapshot(flow, reports, window=FIRST)
    flow.generate(flow.next_offer_date())
    flow.publish(flow.next_offer_date())
    with flow.sessions.begin() as session:
        original = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        original.status, original.send_started_at, original.message_id = "SENT", datetime.utcnow(), "fake-provider-id"
        payload = json.loads(original.payload)
    reports["All_Registrations"][0]["Phone_Number"] = "918888888888"
    apply_snapshot(flow, reports, window=SECOND)
    with flow.sessions.begin() as session:
        assert session.scalar(select(FlowOffer.recipient)) == "919999999999"
        payload["reminder"] = True
        reminder = flow._job(session, "fake-reminder", "OFFER", payload)
        session.flush()
        ident = reminder.id
    assert flow.process_one(job_id=ident)
    with flow.sessions() as session:
        assert session.get(FlowJob, ident).status == "WAITING_FOR_SYNC"


@pytest.mark.skipif(not os.getenv("ZOHO_SIMULATION_PORT"), reason="Local Postman simulation not selected")
def test_actual_http_provider_fetches_full_snapshots_and_filters_plans(flow, monkeypatch, tmp_path):
    from main_workflow.provider import ZohoProvider
    from flow_test.zoho_push import export_settings
    base = "http://127.0.0.1:" + os.environ["ZOHO_SIMULATION_PORT"] + "/zoho"
    monkeypatch.setenv("ZOHO_CALL_BUDGET_FILE", str(tmp_path / "budget.json"))
    settings = replace(export_settings(), account_owner="thanal", app_link_name="eic",
                       form_link_name="Schedule_Adhoc_Trial", report_link_name="Schedule_Adhoc_Trial_Report",
                       api_url=base, accounts_url=base, client_id="dummy", client_secret="dummy", refresh_token="dummy")
    provider = ZohoProvider(flow.config)
    monkeypatch.setattr(provider, "settings", lambda: settings)
    data = provider.fetch(None, FIRST["to"])
    assert len(data["All_Registrations"]) == 2  # Follows cursor, including unused test registration.
    assert len(data["All_Consultants"]) == 2
    assert len(data[PLANS]) == 1 and data[PLANS][0]["Branch"]["ID"] == "10"
    result = apply_snapshot(flow, data, window=FIRST)
    assert not result["partial"] and count(flow, Patient) == 1
    assert count(flow, Appointment) == count(flow, FlowOffer) == 0
    assert count(flow, FlowInbox) == 0
    # A later source failure must stop before import, retaining the prior checkpoint.
    from urllib.request import Request
    from services.zoho_creator_service import open_zoho_request
    fault = Request(base + "/__test__/fault", data=json.dumps({
        "path": "report/All_Therapy_Schedule_Plans", "count": 1, "status": 403}).encode(),
        headers={"Content-Type": "application/json"})
    with open_zoho_request(fault, timeout=5) as response:
        json.load(response)
    with pytest.raises(ValueError, match="fetch failed"):
        provider.fetch(FIRST["to"], SECOND["to"])
    with flow.sessions() as session:
        assert json.loads(session.get(FlowSetting, CHECKPOINT_KEY).value) == FIRST["to"]


@pytest.mark.skipif(not os.getenv("ZOHO_SIMULATION_PORT"), reason="Local Postman simulation not selected")
def test_release_import_accepts_real_clock_microseconds_and_applies_roster(flow, monkeypatch, tmp_path, capsys):
    """The actual cloud entry point must honour the sync checkpoint precision."""
    import base64
    from datetime import datetime, timezone
    import zlib
    from main_workflow import thanal_release as release
    from main_workflow.provider import ZohoProvider
    from flow_test.zoho_push import export_settings
    base = "http://127.0.0.1:" + os.environ["ZOHO_SIMULATION_PORT"] + "/zoho"
    for key, value in release.SOURCE_FIELDS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MAIN_WORKFLOW_ADMIN_TOKEN", "fictional-release-key-at-least-32-characters")
    monkeypatch.setenv("MAIN_WORKFLOW_ADMIN_ID", "tester")
    monkeypatch.setenv("ZOHO_BUDGET_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ZOHO_CALL_BUDGET_FILE", str(tmp_path / "release-budget.json"))
    roster = [{"code": "FAKE-1", "name": "Fictional child", "approvedActive": True},
              {"code": "TEST-UNUSED", "name": "Unused fictional child", "approvedActive": False}]
    encoded = base64.b64encode(zlib.compress(json.dumps(roster).encode())).decode()
    monkeypatch.setenv("THANAL_INITIAL_ROSTER_CHUNKS", "1")
    monkeypatch.setenv("THANAL_INITIAL_ROSTER_000", encoded)
    monkeypatch.setattr("database.db.engine", flow.engine)
    monkeypatch.setattr(release, "verify_database", lambda _: None)
    class FractionalClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return super().now(tz).replace(microsecond=123456)
    monkeypatch.setattr(release, "datetime", FractionalClock)
    settings = replace(export_settings(), account_owner="thanal", app_link_name="eic",
                       form_link_name="Schedule_Adhoc_Trial", report_link_name="Schedule_Adhoc_Trial_Report",
                       api_url=base, accounts_url=base, client_id="dummy", client_secret="dummy", refresh_token="dummy")
    monkeypatch.setattr(ZohoProvider, "settings", lambda _: settings)
    release.inside_import()
    with flow.sessions() as session:
        patient_status = {p.patient_code: p.active for p in session.scalars(select(Patient))}
        assert patient_status == {"FAKE-1": True, "TEST-UNUSED": False}
        boundary = json.loads(session.get(FlowSetting, CHECKPOINT_KEY).value)
        assert datetime.fromisoformat(boundary).microsecond == 0
    assert count(flow, Appointment) == count(flow, FlowOffer) == 0
    output = capsys.readouterr().out
    assert "919999999999" not in output
    assert json.loads(output.strip().splitlines()[-1])["messagesSent"] == 0


def test_daily_cli_import_route_queues_only_an_unsaved_draft(flow, reports, monkeypatch, capsys):
    import sys
    from main_workflow import mount, sync
    from tests.test_flow_test_mount import request
    from flow_test.preview_store import PreviewStore
    instance = SimpleNamespace(flow=flow, config=flow.config, token=flow.config.admin_token,
        previews=PreviewStore(flow), zoho_dispatch=mount.MainRuntime.zoho_dispatch)
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    monkeypatch.setenv("MAIN_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("MAIN_WORKFLOW_DATA_SOURCE", "main")
    monkeypatch.setenv("MAIN_WORKFLOW_ADMIN_TOKEN", flow.config.admin_token)
    provider = SimpleNamespace(fetch=lambda *_: reports)
    monkeypatch.setattr(sync, "workflow_connection", lambda: (flow.config, provider))
    def exchange(config, operation, body=None, **kwargs):
        status, raw = request("/api/zoho/" + operation, body, token=config.admin_token)
        assert status == 200, raw
        return json.loads(raw)
    monkeypatch.setattr(sync, "api", exchange)
    monkeypatch.setattr(sys, "argv", ["sync", "--apply"])
    try:
        sync.main()
        printed = capsys.readouterr().out
        output = json.loads(printed[printed.index("{"):])
    finally:
        if hasattr(instance, "planning_runner"):
            instance.planning_runner.thread.join(5)
    assert count(flow, Patient) == 1
    assert count(flow, Appointment) == count(flow, FlowOffer) == 0
    assert output["draftPending"] and output["downloaded"][PLANS] == 1
    from flow_test.planning_tasks import preview_status
    assert preview_status(instance, output["planning"]["jobId"])["status"] == "DONE"

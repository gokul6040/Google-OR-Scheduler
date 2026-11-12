"""Fictional Thanal exports: lookup identities, display names and safe retries."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
import os

import pytest
from sqlalchemy import select

from database.models import Appointment, AppointmentStatus, Patient, TreatmentRequirement
from flow_test.models import FlowSetting
from flow_test.zoho_export import acknowledge_export, export_preview, finish_export, prepare_export
from flow_test.zoho_push import export_settings, fields_match, run_push
from main_workflow import thanal_export
from main_workflow.thanal_sync import apply_snapshot
from tests.test_thanal_daily_sync import FIRST, flow as thanal_flow, reports

DAY = date(2026, 11, 12)
CATALOG = {"registration_display": "Name2", "approved_branch_ids": ["10"],
           "department_bindings": [
               {"branch_id": "10", "consultation_id": "20", "department_id": "900"},
               {"branch_id": "10", "consultation_id": "21", "department_id": "901"}],
           "slot_bindings": [
               {"branch_id": "10", "ID": "700", "Slot_Number": "1", "appointment_slot_number": "1",
                "Start_Time": "09:00:00", "End_Time": "09:40:00"},
               {"branch_id": "10", "ID": "701", "Slot_Number": "2", "appointment_slot_number": "2",
                "Start_Time": "09:40:00", "End_Time": "10:20:00"}]}


@pytest.fixture
def booked(thanal_flow, reports, monkeypatch):
    apply_snapshot(thanal_flow, reports, window=FIRST)
    monkeypatch.setattr(thanal_export, "default_catalog", lambda: deepcopy(CATALOG))
    with thanal_flow.sessions.begin() as session:
        patient = session.scalar(select(Patient))
        for slot, req in enumerate(session.scalars(select(TreatmentRequirement).order_by(TreatmentRequirement.id))):
            session.add(Appointment(patient_id=patient.id, treatment_requirement_id=req.id,
                therapist_id=req.assigned_therapist_id, department_id=req.department_id,
                location_id=req.location_id, date=DAY, slot_index=slot, status=AppointmentStatus.CONFIRMED))
    return thanal_flow


def exchange_for(flow):
    def exchange(operation, body):
        if operation == "preview":
            return export_preview(flow, DAY)
        if operation == "prepare":
            return prepare_export(flow, DAY)
        if operation == "ack":
            return acknowledge_export(flow, body["batchId"], body["appointmentId"],
                body["fingerprint"], body["recordId"], records=body.get("records"))
        if operation == "failures":
            from flow_test.zoho_export import record_export_failures
            return record_export_failures(flow, body["batchId"], body["failures"])
        return finish_export(flow, body["batchId"])
    return exchange


def test_exact_output_uses_child_identity_and_name_without_guessing(booked):
    preview = export_preview(booked, DAY)
    assert not preview["blockedReasons"] and len(preview["changes"]) == 2
    for index, row in enumerate(preview["changes"]):
        assert set(row["fields"]) == thanal_export.FIELDS
        assert row["fields"]["Registration"] == "100"
        assert row["display"]["Registration"] == "Fictional child"
        assert row["fields"]["For_Parent"] is False
        assert row["fields"]["Status"] == "Scheduled"
        assert row["fields"]["Slot_Number"] == index + 1
        assert row["fields"]["Appointment_Time_Slot"] == str(700 + index)
        assert row["fields"]["Date_field"] == "12/11/2026"
        assert row["fields"]["Date_Time"] == ("12/11/2026 09:00", "12/11/2026 09:40")[index]
        assert row["fields"]["End_Time"] == ("12/11/2026 09:40:00", "12/11/2026 10:20:00")[index]
        assert row["fields"]["Duration"] == 40
    assert preview["openBatch"] is None


def test_missing_or_ambiguous_lookup_blocks_before_export_preparation(booked, monkeypatch):
    catalog = deepcopy(CATALOG)
    catalog["slot_bindings"].append({**catalog["slot_bindings"][0], "ID": "799"})
    monkeypatch.setattr(thanal_export, "default_catalog", lambda: catalog)
    result = export_preview(booked, DAY)
    assert len(result["changes"]) == 1
    assert "Slot Number" in result["blockedReasons"][0]["reason"]
    with pytest.raises(ValueError, match="lookup/status"):
        prepare_export(booked, DAY)
    assert export_preview(booked, DAY)["openBatch"] is None


def test_registration_display_must_be_verified_as_child_name(booked, monkeypatch):
    monkeypatch.setattr(thanal_export, "default_catalog", lambda: {**CATALOG,"registration_display":"Registration_Id"})
    result = export_preview(booked, DAY)
    assert not result["changes"]
    assert all("child's name" in row["reason"] for row in result["blockedReasons"])


def test_no_show_is_not_silently_exported_as_scheduled(booked):
    with booked.sessions.begin() as session:
        session.scalar(select(Appointment)).status = AppointmentStatus.NO_SHOW
    result = export_preview(booked, DAY)
    assert len(result["changes"]) == 1
    assert "Status" in result["blockedReasons"][0]["reason"]


def test_acknowledgement_uses_thanal_date_and_keeps_same_key(booked):
    batch = prepare_export(booked, DAY)
    for row in batch["changes"]:
        acknowledge_export(booked, batch["id"], row["appointmentId"],row["fingerprint"],str(8000+row["appointmentId"]))
    assert finish_export(booked, batch["id"])["pushed"] == 2
    assert not export_preview(booked, DAY)["changes"]
    with booked.sessions.begin() as session:
        session.scalar(select(Appointment)).status = AppointmentStatus.CANCELLED_PATIENT
    changed, = export_preview(booked, DAY)["changes"]
    assert changed["fields"]["Status"] == "Cancelled"
    assert changed["fields"]["Scheduler_Key"] == batch["changes"][0]["fields"]["Scheduler_Key"]


def test_lookup_objects_match_ids_and_checkbox_strings_for_reconciliation():
    assert fields_match({"Registration":{"ID":"100","Name2":"Changed label"},"For_Parent":"false"},
                        {"Registration":"100","For_Parent":False})
    assert not fields_match({"Registration":{"ID":"101","Name2":"Same label"}}, {"Registration":"100"})
    assert fields_match({"Date_Time":"12/11/2026 09:00:00", "End_Time":"12/11/2026 09:40"},
                        {"Date_Time":"12/11/2026 09:00", "End_Time":"12/11/2026 09:40:00"})
    assert not fields_match({"Date_Time":"12/11/2026 09:00:01"}, {"Date_Time":"12/11/2026 09:00"})


@pytest.mark.parametrize("field,value", [("Date_Time", "13/11/2026 09:00"),
    ("End_Time", "12/11/2026 09:41:00"), ("Duration", 41), ("Duration", True)])
def test_invalid_workflow_times_are_rejected_before_write(booked, field, value):
    fields = export_preview(booked, DAY)["changes"][0]["fields"]
    fields[field] = value
    with pytest.raises(ValueError, match="Appointment time"):
        thanal_export.validate_fields(fields)


def test_all_slots_use_clinic_local_time_and_exact_duration():
    from scheduling.slot_config import SLOTS
    for index, slot in enumerate(SLOTS):
        values = thanal_export.timing_fields(date(2026, 12, 31), index)
        assert values["Date_Time"] == f"31/12/2026 {slot.start_time:%H:%M}"
        assert values["End_Time"] == f"31/12/2026 {slot.end_time:%H:%M:%S}"
        assert values["Duration"] == 40


def _mark_pushed(flow):
    batch = prepare_export(flow, DAY)
    records = [{"appointmentId": row["appointmentId"], "fingerprint": row["fingerprint"],
                "recordId": f"9{index:05d}"} for index, row in enumerate(batch["changes"])]
    acknowledge_export(flow, batch["id"], records[0]["appointmentId"], None, None, records=records)
    finish_export(flow, batch["id"])


def test_a_pushed_visit_declined_later_is_updated_to_cancelled_not_blocked(booked):
    _mark_pushed(booked)
    with booked.sessions.begin() as session:
        declined = session.scalar(select(Appointment).order_by(Appointment.id))
        declined.status = AppointmentStatus.NEEDS_RESCHEDULING
        declined_id = declined.id
    preview = export_preview(booked, DAY)
    assert not preview["blockedReasons"]
    assert [(row["appointmentId"], row["fields"]["Status"]) for row in preview["changes"]] == [(declined_id, "Cancelled")]


def test_a_visit_released_before_any_push_is_never_exported(booked):
    with booked.sessions.begin() as session:
        session.scalar(select(Appointment).order_by(Appointment.id)).status = AppointmentStatus.NEEDS_RESCHEDULING
    preview = export_preview(booked, DAY)
    assert not preview["blockedReasons"]
    assert [row["fields"]["Status"] for row in preview["changes"]] == ["Scheduled"]


def test_schema_rejects_text_registration_and_additional_required_fields():
    fields = {name:{"type":12,"is_lookup_field":True} for name in thanal_export.LOOKUPS}
    fields.update(For_Parent={"type":16},Date_field={"type":10},Slot_Number={"type":5},
        Date_Time={"type":11},End_Time={"type":11},Duration={"type":5},
        Scheduler_Key={"type":1,"unique":True,"max_char":255},
        Status={"type":12,"choices":[{"value":v} for v in ("Scheduled","Completed","Cancelled","Recreate")]})
    thanal_export.validate_schema(fields)
    broken=deepcopy(fields)
    broken["Registration"]={"type":1}
    with pytest.raises(ValueError,match="Registration"):
        thanal_export.validate_schema(broken)
    fields["Unexpected"]={"type":1,"mandatory":True}
    with pytest.raises(ValueError,match="additional required"):
        thanal_export.validate_schema(fields)


@pytest.mark.skipif(not os.getenv("ZOHO_SIMULATION_PORT"), reason="Local Postman simulation not selected")
def test_real_http_create_retry_update_and_uncertain_write(booked, tmp_path, monkeypatch):
    from services.zoho_creator_service import open_zoho_request
    from urllib.request import Request

    port = os.environ["ZOHO_SIMULATION_PORT"]
    base = f"http://127.0.0.1:{port}/zoho"
    monkeypatch.setenv("ZOHO_CALL_BUDGET_FILE", str(tmp_path / "budget.json"))
    settings = replace(export_settings(), account_owner="thanal",app_link_name="eic",
        form_link_name=thanal_export.FORM,report_link_name=thanal_export.REPORT,api_url=base,accounts_url=base)
    from main_workflow.provider import ZohoProvider
    verify = ZohoProvider(booked.config).verify
    def push():
        return run_push(settings,"fictional-token","","",DAY,apply=True,
            exchange=exchange_for(booked),verify=verify,key_prefix="main-")
    fault = Request(base+"/__test__/fault",data=json.dumps({"path":"form/"+thanal_export.FORM,
        "count":1,"afterWrite":True,"disconnect":True}).encode(),headers={"Content-Type":"application/json"})
    with open_zoho_request(fault, timeout=10) as response:
        json.load(response)
    first = push()
    assert first["reconciled"] == 2
    assert push()["created"] == 0
    with booked.sessions.begin() as session:
        for item in session.scalars(select(Appointment)):
            item.status = AppointmentStatus.COMPLETED
    result = push()
    assert result["updated"] == 2 and result["created"] == 0
    with booked.sessions.begin() as session:
        session.scalar(select(Appointment)).status=AppointmentStatus.CANCELLED_PATIENT
    result=push()
    assert result["updated"]==1 and result["created"]==0


@pytest.mark.skipif(not os.getenv("ZOHO_SIMULATION_PORT"), reason="Local Postman simulation not selected")
def test_real_http_partial_rejection_keeps_success_and_retries_only_missing(booked,tmp_path,monkeypatch):
    from flow_test.models import FlowInbox
    from main_workflow.provider import ZohoProvider
    from services.zoho_creator_service import open_zoho_request
    from urllib.request import Request

    base=f"http://127.0.0.1:{os.environ['ZOHO_SIMULATION_PORT']}/zoho"
    monkeypatch.setenv("ZOHO_CALL_BUDGET_FILE",str(tmp_path/'budget.json'))
    settings=replace(export_settings(),account_owner="thanal",app_link_name="eic",
        form_link_name=thanal_export.FORM,report_link_name=thanal_export.REPORT,api_url=base,accounts_url=base)
    request=Request(base+'/__test__/reject-scheduler-row',data=json.dumps({'index':1}).encode(),
        headers={'Content-Type':'application/json'})
    with open_zoho_request(request,timeout=10) as response:
        json.load(response)
    def push():
        return run_push(settings,'fictional-token','','',DAY,apply=True,exchange=exchange_for(booked),
            verify=ZohoProvider(booked.config).verify,key_prefix='main-')
    with pytest.raises(ValueError,match='Fictional form validation rejection'):
        push()
    batch=export_preview(booked,DAY)['openBatch']
    assert len(batch['acknowledged'])==1
    with booked.sessions() as session:
        warning=session.scalar(select(FlowInbox).where(FlowInbox.event_key.like('zoho-export:%')))
        assert warning.patient_code=='FAKE-1'
    assert push()['created']==1
    assert export_preview(booked,DAY)['openBatch'] is None

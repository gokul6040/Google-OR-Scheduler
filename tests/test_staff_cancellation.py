"""Staff cancellations use fictional storage and never call a real provider."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from database.models import Appointment, AppointmentStatus, AuditLog, Patient, TreatmentRequirement
from flow_test import mount
from flow_test.models import FlowInbox, FlowJob, FlowOffer
from flow_test.service import FlowTest
from services.cancellation_service import cancel_saved_visit
from services.parent_response_service import weekly_progress
from services.admin_reservation_service import preview_admin_reservation, reserve_admin_slot
from tests.test_flow_test_mount import request

DAY = date(2026, 10, 19)


@pytest.fixture
def staff_flow(tmp_path, monkeypatch, legacy_flow_scenario):
    legacy_flow_scenario["patients"][1]["slots"] = [0, 1]
    flow = FlowTest("sqlite:///" + (tmp_path / "staff.sqlite").as_posix(), scenario=legacy_flow_scenario)
    flow.admin_id = "test_staff"
    with flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-A"))
        reqs = list(session.scalars(select(TreatmentRequirement).where(
            TreatmentRequirement.patient_id == patient.id).order_by(TreatmentRequirement.sequence_order)))
        items = []
        for slot, req in enumerate(reqs):
            item = Appointment(patient_id=patient.id, treatment_requirement_id=req.id,
                therapist_id=req.assigned_therapist_id, department_id=req.department_id,
                location_id=req.location_id, date=DAY, slot_index=slot, bundle_id="visit-a",
                status=AppointmentStatus.PENDING_CONFIRMATION, source="staff-test")
            session.add(item)
            items.append(item)
        req = reqs[0]
        session.add(Appointment(patient_id=patient.id, treatment_requirement_id=req.id,
            therapist_id=req.assigned_therapist_id, department_id=req.department_id,
            location_id=req.location_id, date=DAY + timedelta(days=1), slot_index=0,
            bundle_id="another-visit", status=AppointmentStatus.CONFIRMED, source="staff-test"))
        session.flush()
        flow.test_ids = [item.id for item in items]
        session.add(FlowOffer(token="staff-offer", patient_code=patient.patient_code, recipient="919999999999",
            appointment_ids=json.dumps(flow.test_ids), snapshot=flow._snapshot(items),
            state="PENDING", visit_date=DAY, expires_at=datetime.utcnow() + timedelta(days=1)))
        for kind, payload in [("OFFER", {}), ("OFFER", {"reminder": True}), ("TEXT", {"expected_state": "PENDING"})]:
            session.add(FlowJob(event_key="staff-" + str(payload) + kind, kind=kind,
                payload=json.dumps({"token": "staff-offer", **payload}), status="QUEUED"))
        session.add(FlowJob(event_key="other-offer", kind="OFFER", payload='{"token":"unrelated"}', status="QUEUED"))
    instance = SimpleNamespace(flow=flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    yield flow
    flow.engine.dispose()


def cancel(flow, scope="session", reason="Family called staff"):
    status, raw = request(f"/test/api/appointments/{flow.test_ids[0]}/staff-cancel", {"scope": scope, "reason": reason})
    return status, json.loads(raw)


def test_session_cancel_releases_count_keeps_other_therapy_and_closes_old_offer(staff_flow):
    flow = staff_flow
    with flow.sessions() as session:
        before = {p.requirement_id: p for p in weekly_progress(session, DAY)}
    status, result = cancel(flow)
    assert status == 200 and result["cancelledIds"] == [flow.test_ids[0]]
    assert len(result["replacementGroups"]) == 1
    with flow.sessions() as session:
        first, other = [session.get(Appointment, i) for i in flow.test_ids]
        assert first.status == AppointmentStatus.CANCELLED_PATIENT
        assert other.status == AppointmentStatus.PENDING_CONFIRMATION
        after = {p.requirement_id: p for p in weekly_progress(session, DAY)}
        old, new = before[first.treatment_requirement_id], after[first.treatment_requirement_id]
        assert new.required == old.required and new.booked == old.booked - 1 and new.remaining == old.remaining + 1
        assert session.get(FlowOffer, "staff-offer").state == "CLOSED"
        assert session.scalar(select(func.count(Appointment.id))) == 3
        assert session.scalar(select(func.count(FlowOffer.token))) == 1
        assert all(j.status == "OBSOLETE" for j in session.scalars(select(FlowJob).where(FlowJob.event_key != "other-offer")))
        assert session.scalar(select(FlowJob).where(FlowJob.event_key == "other-offer")).status == "QUEUED"
        assert session.scalar(select(FlowInbox).where(FlowInbox.reason == "Visit changed — review remaining sessions"))
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "staff_appointment_cancelled"))
        assert audit.user == flow.admin_id and json.loads(audit.new_value)["reason"] == "Family called staff"
    flow.simulate("staff-offer", "YES")
    flow.process_events()
    with flow.sessions() as session:
        assert session.get(Appointment, flow.test_ids[0]).status == AppointmentStatus.CANCELLED_PATIENT


def test_visit_cancel_is_idempotent_and_preserves_another_day(staff_flow):
    assert cancel(staff_flow, "visit")[1]["cancelledIds"] == staff_flow.test_ids
    assert cancel(staff_flow, "visit")[1]["alreadyCancelled"]
    with staff_flow.sessions() as session:
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "staff_appointment_cancelled")) == 2
        assert session.scalar(select(Appointment).where(Appointment.date != DAY)).status == AppointmentStatus.CONFIRMED
    status, raw = request(f"/test/api/schedule?start={DAY}&days=1")
    payload = json.loads(raw)
    assert status == 200 and payload["staffCancellationEnabled"] is True
    assert {a["status"] for a in payload["appointments"]} == {"CANCELLED_PATIENT"}


@pytest.mark.parametrize("sent_status", ["SENT", "SIMULATED"])
def test_cancel_preserves_sent_job_history(staff_flow, sent_status):
    with staff_flow.sessions.begin() as session:
        job = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        job.status, job.message_id = sent_status, "retained-provider-reference"
        identifier = job.id
    assert cancel(staff_flow)[0] == 200
    with staff_flow.sessions() as session:
        job = session.get(FlowJob, identifier)
        assert job.status == sent_status and job.message_id == "retained-provider-reference"
        assert session.get(FlowOffer, "staff-offer").state == "CLOSED"


def test_cancel_rejects_unauthenticated_request(staff_flow):
    status, _ = request(f"/test/api/appointments/{staff_flow.test_ids[0]}/staff-cancel",
                        {"scope": "session", "reason": "Reason"}, token="wrong-key")
    assert status == 401
    with staff_flow.sessions() as session:
        assert session.get(Appointment, staff_flow.test_ids[0]).status == AppointmentStatus.PENDING_CONFIRMATION


def test_older_operational_cancel_route_cannot_bypass_reason_or_queue_cleanup(staff_flow):
    status, _ = request(f"/test/api/appointments/{staff_flow.test_ids[0]}/cancel", {})
    assert status == 409
    with staff_flow.sessions() as session:
        assert session.get(Appointment, staff_flow.test_ids[0]).status == AppointmentStatus.PENDING_CONFIRMATION
        assert session.get(FlowOffer, "staff-offer").state == "PENDING"


@pytest.mark.parametrize("scope,reason", [("day", "Reason"), ("session", ""), ("session", 5), ("visit", "x" * 1001)])
def test_invalid_cancellation_changes_nothing(staff_flow, scope, reason):
    assert cancel(staff_flow, scope, reason)[0] == 409
    with staff_flow.sessions() as session:
        assert all(session.get(Appointment, i).status == AppointmentStatus.PENDING_CONFIRMATION for i in staff_flow.test_ids)
        assert session.get(FlowOffer, "staff-offer").state == "PENDING"


def test_entire_visit_with_completed_therapy_is_rejected_atomically(staff_flow):
    with staff_flow.sessions.begin() as session:
        session.get(Appointment, staff_flow.test_ids[1]).status = AppointmentStatus.COMPLETED
    assert cancel(staff_flow, "visit")[0] == 409
    with staff_flow.sessions() as session:
        assert session.get(Appointment, staff_flow.test_ids[0]).status == AppointmentStatus.PENDING_CONFIRMATION
        assert session.get(Appointment, staff_flow.test_ids[1]).status == AppointmentStatus.COMPLETED


def test_transaction_failure_does_not_leave_a_partial_cancellation(staff_flow, monkeypatch):
    def fail(*_args):
        raise RuntimeError("Injected failure")
    monkeypatch.setattr(staff_flow, "_job_warning", fail)
    with pytest.raises(RuntimeError), staff_flow.sessions.begin() as session:
        cancel_saved_visit(session, staff_flow.test_ids[0], scope="session", reason="Reason", actor="staff", flow=staff_flow)
    with staff_flow.sessions() as session:
        assert session.get(Appointment, staff_flow.test_ids[0]).status == AppointmentStatus.PENDING_CONFIRMATION
        assert session.get(FlowOffer, "staff-offer").state == "PENDING"


def test_released_session_uses_existing_suggestions_and_manual_reservation(staff_flow):
    assert cancel(staff_flow)[0] == 200
    source = staff_flow.test_ids[0]
    status, raw = request(f"/test/api/appointments/{source}/reservation-context")
    context = json.loads(raw)
    assert status == 200 and any(p["patient_code"] == "TEST-B" for p in context["candidates"])
    with staff_flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-B"))
        preview = preview_admin_reservation(session, source, patient.id)
        result = reserve_admin_slot(session, source_id=source, patient_id=patient.id,
            request_id="staff-replacement", preview_token=preview["preview_token"],
            override_warnings=bool(preview["warnings"]))
        assert result["appointment_ids"]
    status, raw = request(f"/test/api/schedule?start={DAY}&days=1")
    appointments = json.loads(raw)["appointments"]
    assert source not in [a["id"] for a in appointments]
    assert any(a["patientCode"] == "TEST-B" and a["status"] == "CONFIRMED" for a in appointments)
    repeated = cancel(staff_flow)[1]
    assert repeated["alreadyCancelled"] and repeated["replacementGroups"] == []


def test_old_preview_cannot_restore_a_cancelled_booking(staff_flow, monkeypatch):
    monkeypatch.setattr(staff_flow, "next_offer_date", lambda: DAY)
    status, raw = request("/test/api/schedules/generate", {"start": DAY.isoformat(), "searchSeconds": 20})
    assert status == 201
    preview = json.loads(raw)
    assert cancel(staff_flow)[0] == 200
    status, _ = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 409
    with staff_flow.sessions() as session:
        assert session.get(Appointment, staff_flow.test_ids[0]).status == AppointmentStatus.CANCELLED_PATIENT


def test_two_staff_requests_cannot_cancel_or_audit_twice(staff_flow):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cancel(staff_flow), range(2)))
    assert all(status == 200 for status, _ in results)
    assert sorted(len(result["cancelledIds"]) for _, result in results) == [0, 1]

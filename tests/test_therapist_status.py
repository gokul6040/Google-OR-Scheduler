"""Staff-owned clinician Active/Inactive status from the Clinical team page."""

import json
from pathlib import Path

from sqlalchemy import func, select

from database.models import AuditLog, Location, Patient, Therapist, TreatmentRequirement
from flow_test.zoho_import import apply_reports
from tests.test_main_root_workflow import DAY, payload, root_main, root_store  # noqa: F401 - fixtures


def assigned_doctors(flow, code):
    with flow.sessions() as session:
        return sorted(set(session.scalars(
            select(TreatmentRequirement.assigned_therapist_id).join(Patient)
            .where(Patient.patient_code == code, TreatmentRequirement.active.is_(True)))))


def test_clinician_status_controls_scheduling_and_survives_zoho_sync(root_main):
    flow = root_main.flow
    doctors = assigned_doctors(flow, "TEST-A")
    assert doctors
    first = f"/api/therapists/{doctors[0]}/status"
    assert payload(first, {"active": False, "reason": "   "})[0] == 400
    assert payload(first, {"active": "false", "reason": "Leave"})[0] == 400
    assert payload(first, {"active": False, "reason": "Leave"}, token="wrong")[0] == 401
    assert payload("/api/therapists/999999/status", {"active": True})[0] == 404

    generate = {"start": str(DAY), "patients": ["TEST-A"], "searchSeconds": 20}
    assert payload("/api/schedules/generate", generate)[1]["newSessions"] > 0
    for doctor in doctors:
        status, result = payload(f"/api/therapists/{doctor}/status", {"active": False, "reason": " On leave "})
        assert status == 200 and result["active"] is False and result["upcomingBookings"] >= 0
    listed = {row["id"]: row["active"] for row in payload(f"/api/therapists?start={DAY}")[1]["therapists"]}
    assert not any(listed[doctor] for doctor in doctors)
    # Inactive clinicians are not taken for new sessions.
    assert payload("/api/schedules/generate", generate)[1]["newSessions"] == 0

    # The Zoho sync keeps the status staff chose.
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text())
    apply_reports(flow, reports)
    with flow.sessions() as session:
        assert not any(session.get(Therapist, doctor).active for doctor in doctors)

    for doctor in doctors:
        assert payload(f"/api/therapists/{doctor}/status", {"active": True})[1]["active"] is True
    assert payload("/api/schedules/generate", generate)[1]["newSessions"] > 0
    with flow.sessions() as session:
        history = [json.loads(item.new_value) for item in session.scalars(select(AuditLog).where(
            AuditLog.entity == f"therapist:{doctors[0]}", AuditLog.action == "therapist_status_changed",
        ).order_by(AuditLog.id))]
        assert history == [{"active": False, "reason": "On leave"}, {"active": True, "reason": None}]
        for doctor in doctors:
            location = session.get(Location, session.get(Therapist, doctor).location_id)
            active_here = session.scalar(select(func.count(Therapist.id)).where(
                Therapist.location_id == location.id, Therapist.active.is_(True)))
            assert location.capacity >= active_here


def test_repeating_the_same_status_writes_no_history(root_main):
    doctor = assigned_doctors(root_main.flow, "TEST-A")[0]
    path = f"/api/therapists/{doctor}/status"
    assert payload(path, {"active": True})[0] == 200
    with root_main.flow.sessions() as session:
        assert session.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.action == "therapist_status_changed")) == 0


def test_inactive_assigned_clinician_blocks_patient_activation(root_main):
    from services.patient_activation_service import store_activation_review
    flow = root_main.flow
    patient = payload("/api/patients?q=TEST-A")[1]["patients"][0]
    path = f"/api/patients/{patient['id']}"
    assert payload(path + "/status", {"active": False, "reason": "Paused"})[0] == 200
    with flow.sessions.begin() as session:
        store_activation_review(session, {"source": "thanal/eic", "checkedAt": "2026-11-16T08:00:00Z",
                                          "planCodes": ["TEST-A"], "issues": {}})
    doctor = assigned_doctors(flow, "TEST-A")[0]
    assert payload(f"/api/therapists/{doctor}/status", {"active": False, "reason": "Left"})[0] == 200
    check = payload(path + "/activation")[1]
    assert not check["canActivate"] and "Clinical team" in " ".join(check["issues"])
    assert payload(path + "/status", {"active": True})[0] == 409
    assert payload(f"/api/therapists/{doctor}/status", {"active": True})[0] == 200
    remaining = [d for d in assigned_doctors(flow, "TEST-A") if d != doctor]
    with flow.sessions() as session:
        if all(session.get(Therapist, d).active for d in remaining):
            assert payload(path + "/activation")[1]["canActivate"]

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from database.models import (
    AdminBookingDecision, AdminSlotReservation, Appointment, AppointmentStatus,
    AuditLog, Department, Patient, PatientAvailability, Therapist, TreatmentRequirement,
)
from database.repository import limit_new_bookings_to_day, load_daily_scheduling_problem
from importers.parent_response_demo_importer import load_parent_response_demo
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from services.admin_reservation_service import preview_admin_reservation, reserve_admin_slot
from services.appointment_service import move_appointment
from services.parent_response_service import process_daily_responses, weekly_progress


@pytest.fixture
def booking_demo(db_session):
    target = load_parent_response_demo(db_session)
    result = process_daily_responses(db_session, target)
    patients = {item.patient_code: item for item in db_session.scalars(select(Patient))}
    return target, result.released_appointment_ids[0], patients, result.proposed_appointment_ids[0]


def reserve(session, preview, request_id="test-reservation", override=False, **kwargs):
    result = reserve_admin_slot(
        session, source_id=preview["source_id"], patient_id=preview["patient_id"],
        whole_visit=preview["whole_visit"], request_id=request_id,
        preview_token=preview["preview_token"], override_warnings=override, **kwargs,
    )
    session.commit()
    return result


def test_confirm_suggestion_reserves_same_appointment_without_duplicates(db_session, booking_demo):
    _, source, patients, proposal_id = booking_demo
    preview = preview_admin_reservation(db_session, source, patients["DEMO-D"].id)
    assert not preview["warnings"]
    result = reserve(db_session, preview)
    assert result["appointment_ids"] == [proposal_id]
    assert db_session.get(Appointment, proposal_id).status == AppointmentStatus.CONFIRMED
    assert db_session.get(AdminSlotReservation, source).appointment_id == proposal_id
    assert db_session.scalar(select(func.count()).select_from(Appointment)) == 6
    assert reserve(db_session, preview) == result
    assert db_session.scalar(select(func.count()).select_from(AdminBookingDecision)) == 1


def test_search_any_patient_override_warns_and_replaces_only_tentative_suggestion(db_session, booking_demo):
    target, source, patients, proposal_id = booking_demo
    before = {item.id: (item.date, item.slot_index, item.status) for item in db_session.scalars(select(Appointment))}
    preview = preview_admin_reservation(db_session, source, patients["DEMO-E"].id)
    assert "WEEKLY_TARGET" in {item["code"] for item in preview["warnings"]}
    with pytest.raises(ValueError, match="Reserve anyway"):
        reserve(db_session, preview)
    result = reserve(db_session, preview, override=True)
    appointment = db_session.get(Appointment, result["appointment_ids"][0])
    assert (appointment.patient_id, appointment.date, appointment.slot_index) == (patients["DEMO-E"].id, target, 1)
    assert appointment.status == AppointmentStatus.CONFIRMED
    assert db_session.get(Appointment, proposal_id).status == AppointmentStatus.CANCELLED_OTHER
    for identifier, values in before.items():
        if identifier != proposal_id:
            item = db_session.get(Appointment, identifier)
            assert (item.date, item.slot_index, item.status) == values
    decision = db_session.get(AdminBookingDecision, "test-reservation")
    assert decision.admin_user == "local_demo_admin"
    assert json.loads(decision.warnings) == preview["warnings"]
    assert db_session.scalar(select(AuditLog).where(AuditLog.action == "admin_slot_reserved")) is not None


def test_admin_can_override_parent_absence(db_session, booking_demo):
    _, source, patients, _ = booking_demo
    preview = preview_admin_reservation(db_session, source, patients["DEMO-B"].id)
    assert "PATIENT_ABSENCE" in {item["code"] for item in preview["warnings"]}
    result = reserve(db_session, preview, override=True)
    assert db_session.get(Appointment, result["appointment_ids"][0]).status == AppointmentStatus.CONFIRMED


def test_admin_can_override_a_patient_conflict_without_changing_the_other_booking(db_session, booking_demo):
    target, source, patients, _ = booking_demo
    requirement = db_session.scalar(select(TreatmentRequirement).where(
        TreatmentRequirement.patient_id == patients["DEMO-F"].id,
    ))
    existing = Appointment(
        patient_id=patients["DEMO-F"].id, treatment_requirement_id=requirement.id,
        therapist_id=requirement.assigned_therapist_id,
        department_id=requirement.department_id, location_id=requirement.location_id,
        date=target, slot_index=1, status=AppointmentStatus.CONFIRMED,
        bundle_id="EXISTING-F",
    )
    db_session.add(existing)
    db_session.commit()
    preview = preview_admin_reservation(db_session, source, patients["DEMO-F"].id)
    assert "PATIENT_CONFLICT" in {item["code"] for item in preview["warnings"]}
    result = reserve(db_session, preview, override=True)
    assert db_session.get(Appointment, result["appointment_ids"][0]).status == AppointmentStatus.CONFIRMED
    assert (existing.date, existing.slot_index, existing.status) == (target, 1, AppointmentStatus.CONFIRMED)


def test_admin_can_override_therapist_mismatch_and_unavailability(db_session, booking_demo):
    target, source, patients, _ = booking_demo
    db_session.add(PatientAvailability(
        patient_id=patients["DEMO-F"].id, day_of_week=target.weekday(), slot_index=1, available=False,
    ))
    db_session.commit()
    preview = preview_admin_reservation(db_session, source, patients["DEMO-F"].id)
    codes = {item["code"] for item in preview["warnings"]}
    assert {"THERAPIST_MISMATCH", "PATIENT_UNAVAILABLE"} <= codes
    result = reserve(db_session, preview, override=True)
    assert db_session.get(Appointment, result["appointment_ids"][0]).therapist_id == db_session.get(Appointment, source).therapist_id


def test_patient_without_a_plan_can_be_reserved_without_creating_clinical_demand(db_session, booking_demo):
    target, source, _, _ = booking_demo
    patient = Patient(patient_code="DEMO-NO-PLAN", name="Demo No Plan", active=True)
    db_session.add(patient)
    db_session.commit()
    preview = preview_admin_reservation(db_session, source, patient.id)
    assert {"NO_ACTIVE_PLAN", "THERAPY_NOT_IN_PLAN"} <= {item["code"] for item in preview["warnings"]}
    result = reserve(db_session, preview, override=True)
    appointment = db_session.get(Appointment, result["appointment_ids"][0])
    assert appointment.patient_id == patient.id
    assert not db_session.get(AdminSlotReservation, source).counts_toward_requirement
    assert not any(item.patient_id == patient.id for item in weekly_progress(db_session, target))
    next_day = target + timedelta(days=1)
    problem = load_daily_scheduling_problem(db_session, next_day)
    moved, issues = move_appointment(db_session, problem, problem.pinned, appointment, next_day, 9)
    assert moved, issues
    assert appointment.date == next_day


def test_off_plan_reservation_occupies_its_slot_without_fulfilling_another_therapy(db_session, booking_demo):
    target, source, _, _ = booking_demo
    opening = db_session.get(Appointment, source)
    department = Department(name="Off-plan test therapy")
    therapist = Therapist(therapist_code="OFF-PLAN-T", name="Other therapist", department=department,
                          location_id=opening.location_id)
    patient = Patient(patient_code="OFF-PLAN-P")
    db_session.add_all([therapist, patient])
    db_session.flush()
    requirement = TreatmentRequirement(patient_id=patient.id, department_id=department.id,
                                       assigned_therapist_id=therapist.id, sessions_per_week=1,
                                       sequence_order=0, plan_start_date=target)
    db_session.add(requirement)
    db_session.commit()
    preview = preview_admin_reservation(db_session, source, patient.id)
    assert "THERAPY_NOT_IN_PLAN" in {item["code"] for item in preview["warnings"]}
    reserve(db_session, preview, override=True)
    assert not db_session.get(AdminSlotReservation, source).counts_toward_requirement

    next_day = target + timedelta(days=1)
    problem = load_daily_scheduling_problem(db_session, next_day)
    limit_new_bookings_to_day(problem, next_day)
    result = generate_weekly_schedule(problem)

    assert not any(result.validation.values())
    assert {(item.date, item.department_id, item.therapist_id) for item in result.appointments
            if item.patient_id == patient.id} == {
        (target, opening.department_id, opening.therapist_id),
        (next_day, department.id, therapist.id),
    }


def test_stale_preview_cannot_silently_skip_new_warnings(db_session, booking_demo):
    target, source, patients, _ = booking_demo
    preview = preview_admin_reservation(db_session, source, patients["DEMO-D"].id)
    db_session.add(PatientAvailability(
        patient_id=patients["DEMO-D"].id, day_of_week=target.weekday(), slot_index=1, available=False,
    ))
    db_session.commit()
    with pytest.raises(ValueError, match="details changed"):
        reserve(db_session, preview)
    assert db_session.get(AdminSlotReservation, source) is None


def test_explicit_same_therapy_different_doctor_reservation_remains_valid(db_session, booking_demo):
    target, source, _, _ = booking_demo
    opening = db_session.get(Appointment, source)
    doctor = Therapist(therapist_code="OVERRIDE-DOCTOR", name="Fictional assigned doctor",
                      department_id=opening.department_id, location_id=opening.location_id)
    patient = Patient(patient_code="OVERRIDE-CHILD", name="Fictional child")
    db_session.add_all([doctor, patient])
    db_session.flush()
    db_session.add(TreatmentRequirement(patient_id=patient.id, department_id=opening.department_id,
        assigned_therapist_id=doctor.id, location_id=opening.location_id, sessions_per_week=1, sequence_order=0))
    db_session.commit()
    preview = preview_admin_reservation(db_session, source, patient.id)
    assert "THERAPIST_MISMATCH" in {item["code"] for item in preview["warnings"]}
    reserve(db_session, preview, override=True)
    problem = load_daily_scheduling_problem(db_session, target + timedelta(days=1))
    limit_new_bookings_to_day(problem, target + timedelta(days=1))
    result = generate_weekly_schedule(problem)
    assert not any(result.validation.values())
    assert not result.statistics.get("blocked_locations")
    assert any(item.patient_id == patient.id and item.assignment_override for item in problem.pinned)


def test_second_booking_for_reserved_slot_is_rejected_but_replay_is_safe(db_session, booking_demo):
    _, source, patients, _ = booking_demo
    preview = preview_admin_reservation(db_session, source, patients["DEMO-D"].id)
    first = reserve(db_session, preview)
    assert reserve(db_session, preview) == first
    with pytest.raises(ValueError, match="already reserved"):
        preview_admin_reservation(db_session, source, patients["DEMO-E"].id)
    with pytest.raises(ValueError, match="different reservation"):
        reserve_admin_slot(
            db_session, source_id=source, patient_id=patients["DEMO-E"].id,
            request_id="test-reservation", preview_token=preview["preview_token"],
        )


def test_messaging_placeholder_never_reserves_or_sends(db_session, booking_demo):
    _, source, patients, _ = booking_demo
    preview = preview_admin_reservation(db_session, source, patients["DEMO-D"].id)
    with pytest.raises(ValueError, match="display-only"):
        reserve(db_session, preview, mode="with_messaging")
    assert db_session.get(AdminSlotReservation, source) is None

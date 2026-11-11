"""Replacement advice must obey the daily scheduler's existing constraints."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from database.models import (
    Appointment, AppointmentStatus, Department, Location, Patient, PatientAbsence,
    PatientAvailability, Therapist, TherapistAbsence, TherapistAvailability,
    TreatmentRequirement,
)
from services.replacement_service import find_replacement_candidates


DAY = date(2026, 11, 2)


@pytest.fixture
def opening(db_session):
    therapy = Department(name="Fictional Speech")
    branch = Location(name="Fictional Centre", capacity=2)
    people = [Patient(patient_code=code) for code in ("RELEASED", "ELIGIBLE")]
    db_session.add_all([therapy, branch, *people])
    db_session.flush()
    doctor = Therapist(therapist_code="DOC", name="Fictional Doctor",
                       department_id=therapy.id, location_id=branch.id)
    db_session.add(doctor)
    db_session.flush()
    requirements = [TreatmentRequirement(
        patient_id=person.id, department_id=therapy.id, assigned_therapist_id=doctor.id,
        location_id=branch.id, sessions_per_week=1, sequence_order=0,
    ) for person in people]
    db_session.add_all(requirements)
    db_session.flush()
    vacant = Appointment(
        patient_id=people[0].id, treatment_requirement_id=requirements[0].id,
        therapist_id=doctor.id, department_id=therapy.id, location_id=branch.id,
        date=DAY, slot_index=2, status=AppointmentStatus.CANCELLED_PATIENT,
        bundle_id="released-visit", source="test",
    )
    db_session.add(vacant)
    db_session.commit()
    return vacant, people[1], requirements[1], doctor, branch, therapy


def candidates(session, opening):
    return find_replacement_candidates(session, opening[0], DAY)


def booking(session, requirement, *, target=DAY + timedelta(days=1), slot=0,
            status=AppointmentStatus.PENDING_CONFIRMATION):
    row = Appointment(
        patient_id=requirement.patient_id, treatment_requirement_id=requirement.id,
        therapist_id=requirement.assigned_therapist_id, department_id=requirement.department_id,
        location_id=requirement.location_id, date=target, slot_index=slot, status=status,
        bundle_id=f"existing-{target}-{slot}", source="test",
    )
    session.add(row)
    session.flush()
    return row


def test_default_availability_and_advisory_search_preserve_saved_visits(db_session, opening):
    requirement = opening[2]
    requirement.sessions_per_week = 2
    db_session.flush()
    saved = booking(db_session, requirement)
    requirement.location_id = None  # The scheduler resolves the assigned doctor's branch.
    db_session.commit()
    before = db_session.scalar(select(func.count()).select_from(Appointment))
    result = candidates(db_session, opening)
    assert [item["patient_code"] for item in result] == ["ELIGIBLE"]
    assert result[0]["weekly_progress"] == "1/2"
    assert db_session.scalar(select(func.count()).select_from(Appointment)) == before
    assert saved.date == DAY + timedelta(days=1) and saved.status == AppointmentStatus.PENDING_CONFIRMATION


@pytest.mark.parametrize("status", [AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION,
                                  AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED])
def test_all_reserved_and_completed_sessions_count_toward_weekly_target(db_session, opening, status):
    booking(db_session, opening[2], status=status)
    assert candidates(db_session, opening) == []


@pytest.mark.parametrize("status", [AppointmentStatus.CANCELLED_PATIENT, AppointmentStatus.CANCELLED_OTHER,
                                  AppointmentStatus.NEEDS_RESCHEDULING, AppointmentStatus.NO_SHOW])
def test_released_and_no_show_records_leave_demand_available(db_session, opening, status):
    booking(db_session, opening[2], target=DAY, slot=6, status=status)
    assert len(candidates(db_session, opening)) == 1


@pytest.mark.parametrize("change", ["patient", "doctor", "branch", "therapy", "requirement",
                                  "not_started", "ended", "sunday"])
def test_inactive_records_and_invalid_plan_dates_are_not_suggested(db_session, opening, change):
    vacant, patient, requirement, doctor, branch, therapy = opening
    if change in {"patient", "doctor", "branch", "therapy", "requirement"}:
        {"patient": patient, "doctor": doctor, "branch": branch,
         "therapy": therapy, "requirement": requirement}[change].active = False
    elif change == "not_started":
        requirement.plan_start_date = DAY + timedelta(days=1)
    elif change == "ended":
        requirement.plan_start_date = date(2026, 9, 1)
        requirement.duration_months = 1
    else:
        vacant.date = DAY + timedelta(days=6)
    db_session.flush()
    assert candidates(db_session, opening) == []


@pytest.mark.parametrize("model,field", [(PatientAvailability, "patient_id"),
                                       (TherapistAvailability, "therapist_id")])
def test_explicit_unavailability_blocks_the_exact_slot(db_session, opening, model, field):
    person = opening[1] if field == "patient_id" else opening[3]
    db_session.add(model(**{field: person.id, "day_of_week": 0, "slot_index": 2, "available": False}))
    db_session.flush()
    assert candidates(db_session, opening) == []


@pytest.mark.parametrize("model,field", [(PatientAbsence, "patient_id"), (TherapistAbsence, "therapist_id")])
def test_partial_absence_only_blocks_covered_slots(db_session, opening, model, field):
    person = opening[1] if field == "patient_id" else opening[3]
    absence = model(**{field: person.id, "date": DAY, "start_slot": 6, "end_slot": 7, "full_day": False})
    db_session.add(absence)
    db_session.flush()
    assert len(candidates(db_session, opening)) == 1
    absence.start_slot = 2
    db_session.flush()
    assert candidates(db_session, opening) == []


def test_patient_with_another_saved_visit_that_day_is_not_suggested(db_session, opening):
    opening[2].sessions_per_week = 3
    booking(db_session, opening[2], target=DAY, slot=6)
    assert candidates(db_session, opening) == []


@pytest.mark.parametrize("same_doctor", [True, False])
def test_occupied_doctor_or_full_branch_is_not_suggested(db_session, opening, same_doctor):
    requirement = db_session.get(TreatmentRequirement, opening[0].treatment_requirement_id)
    if not same_doctor:
        doctor = Therapist(therapist_code="OTHER", name="Other Fictional Doctor",
                          department_id=opening[5].id, location_id=opening[4].id)
        db_session.add(doctor)
        db_session.flush()
        requirement.assigned_therapist_id = doctor.id
        opening[4].capacity = 1
    booking(db_session, requirement, target=DAY, slot=2)
    assert candidates(db_session, opening) == []


def test_replacement_preserves_remaining_therapy_sequence_and_continuity(db_session, opening):
    vacant, patient, requirement, doctor, branch, _ = opening
    therapy = Department(name="Fictional OT")
    db_session.add(therapy)
    db_session.flush()
    other = TreatmentRequirement(patient_id=patient.id, department_id=therapy.id,
        assigned_therapist_id=doctor.id, location_id=branch.id, sessions_per_week=1, sequence_order=1)
    db_session.add(other)
    db_session.flush()
    # A substitute may take one of their therapies (user decision: substitutes only).
    single = candidates(db_session, opening)
    assert [item["patient_code"] for item in single] == ["ELIGIBLE"]
    assert single[0]["fit"] == "complete_visit" and single[0]["sessions_covered"] == 1
    source = Appointment(patient_id=vacant.patient_id, treatment_requirement_id=vacant.treatment_requirement_id,
        therapist_id=doctor.id, department_id=therapy.id, location_id=branch.id,
        date=DAY, slot_index=3, status=AppointmentStatus.CANCELLED_PATIENT,
        bundle_id=vacant.bundle_id, source="test")
    db_session.add(source)
    db_session.flush()
    assert len(candidates(db_session, opening)) == 1
    other.sequence_order = -1
    db_session.flush()
    assert candidates(db_session, opening) == []  # Wrong therapy order for these openings.
    other.sequence_order = 1
    vacant.slot_index, source.slot_index = 4, 5  # Lunch interrupts the visit.
    db_session.flush()
    assert candidates(db_session, opening) == []


def test_weekly_target_ignores_bookings_from_another_week(db_session, opening):
    booking(db_session, opening[2], target=DAY + timedelta(days=7))
    assert len(candidates(db_session, opening)) == 1


def _two_session_opening(db_session, opening):
    """Release a Speech 2 + OT 3 visit; return (speech_vacancy, ot_therapy)."""
    vacant, _, _, doctor, branch, _ = opening
    ot = Department(name="Fictional OT")
    db_session.add(ot)
    db_session.flush()
    ot_requirement = TreatmentRequirement(patient_id=vacant.patient_id, department_id=ot.id,
        assigned_therapist_id=doctor.id, location_id=branch.id, sessions_per_week=1, sequence_order=1)
    db_session.add(ot_requirement)
    db_session.flush()
    db_session.add(Appointment(patient_id=vacant.patient_id, treatment_requirement_id=ot_requirement.id,
        therapist_id=doctor.id, department_id=ot.id, location_id=branch.id, date=DAY, slot_index=3,
        status=AppointmentStatus.CANCELLED_PATIENT, bundle_id=vacant.bundle_id, source="test"))
    db_session.flush()
    return vacant, ot


def test_children_who_fill_the_whole_visit_rank_before_single_session_fits(db_session, opening):
    vacant, ot = _two_session_opening(db_session, opening)
    _, eligible, _, doctor, branch, _ = opening
    db_session.add(TreatmentRequirement(patient_id=eligible.id, department_id=ot.id,
        assigned_therapist_id=doctor.id, location_id=branch.id, sessions_per_week=1, sequence_order=1))
    speech_only = Patient(patient_code="A-SPEECH-ONLY")  # Sorts first by code, so ranking must reorder.
    db_session.add(speech_only)
    db_session.flush()
    db_session.add(TreatmentRequirement(patient_id=speech_only.id, department_id=vacant.department_id,
        assigned_therapist_id=doctor.id, location_id=branch.id, sessions_per_week=1, sequence_order=0))
    db_session.flush()
    whole = find_replacement_candidates(db_session, vacant, DAY)
    assert [item["patient_code"] for item in whole] == ["ELIGIBLE"]
    assert whole[0]["fit"] == "complete_visit" and whole[0]["sessions_covered"] == 2
    ranked = find_replacement_candidates(db_session, vacant, DAY, whole_visit=False)
    assert [(item["patient_code"], item["sessions_covered"], item["fit"]) for item in ranked] == [
        ("ELIGIBLE", 2, "complete_visit"), ("A-SPEECH-ONLY", 1, "this_session")]


def test_substitute_may_need_other_therapies_with_other_doctors(db_session, opening):
    vacant, _, _, _, branch, _ = opening
    physio = Department(name="Fictional Physio")
    db_session.add(physio)
    db_session.flush()
    other_doctor = Therapist(therapist_code="PHYSIO", name="Fictional Physio Doctor",
                             department_id=physio.id, location_id=branch.id)
    db_session.add(other_doctor)
    db_session.flush()
    db_session.add(TreatmentRequirement(patient_id=opening[1].id, department_id=physio.id,
        assigned_therapist_id=other_doctor.id, location_id=branch.id, sessions_per_week=2, sequence_order=1))
    db_session.flush()
    result = find_replacement_candidates(db_session, vacant, DAY, whole_visit=False)
    assert [item["patient_code"] for item in result] == ["ELIGIBLE"]


@pytest.mark.parametrize("slot,listed", [(1, True), (3, True), (0, False), (6, False)])
def test_child_already_coming_may_take_an_adjacent_opening_only(db_session, opening, slot, listed):
    opening[2].sessions_per_week = 2
    booking(db_session, opening[2], target=DAY, slot=slot)
    result = candidates(db_session, opening)
    assert [item["patient_code"] for item in result] == (["ELIGIBLE"] if listed else [])
    if listed:
        assert result[0]["joins_existing_visit"] is True
        assert "Joins the child's existing visit" in result[0]["reason"]


@pytest.mark.parametrize("slot,warned", [(3, False), (6, True)])
def test_reservation_warns_only_when_the_opening_is_a_second_visit(db_session, opening, slot, warned):
    from services.admin_reservation_service import preview_admin_reservation
    opening[2].sessions_per_week = 2
    booking(db_session, opening[2], target=DAY, slot=slot)
    db_session.commit()
    preview = preview_admin_reservation(db_session, opening[0].id, opening[1].id, whole_visit=False)
    assert ("ANOTHER_VISIT" in {item["code"] for item in preview["warnings"]}) is warned


def test_single_slot_selection_can_fill_part_of_a_released_visit(db_session, opening):
    vacant = opening[0]
    db_session.add(Appointment(
        patient_id=vacant.patient_id, treatment_requirement_id=vacant.treatment_requirement_id,
        therapist_id=vacant.therapist_id, department_id=vacant.department_id,
        location_id=vacant.location_id, date=DAY, slot_index=3,
        status=AppointmentStatus.CANCELLED_PATIENT, bundle_id=vacant.bundle_id, source="test",
    ))
    db_session.flush()
    assert candidates(db_session, opening) == []
    assert len(find_replacement_candidates(db_session, vacant, DAY, whole_visit=False)) == 1

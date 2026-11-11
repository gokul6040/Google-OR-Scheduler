from dataclasses import replace
from datetime import date, timedelta

import pytest

from config import SolverSettings
from database.models import (
    Appointment, AppointmentStatus, Department, Location, Patient, Therapist, TreatmentRequirement,
)
from database.repository import limit_new_bookings_to_day, load_daily_scheduling_problem
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.schedule_validator import validate_schedule
from services.absence_service import mark_patient_absent, mark_therapist_absent
from services.cancellation_service import cancel_patient_appointment


def test_absence_marks_only_affected_therapist_appointments(db_session):
    department = Department(name="OT")
    location = Location(name="A", capacity=2)
    patient = Patient(patient_code="P1")
    db_session.add_all([department, location, patient])
    db_session.flush()
    therapists = [
        Therapist(therapist_code=code, name=code, department_id=department.id, location_id=location.id)
        for code in ("T1", "T2")
    ]
    db_session.add_all(therapists)
    db_session.flush()
    requirements = [
        TreatmentRequirement(patient_id=patient.id, department_id=department.id, assigned_therapist_id=t.id, sessions_per_week=1, sequence_order=i)
        for i, t in enumerate(therapists)
    ]
    db_session.add_all(requirements)
    db_session.flush()
    target = date(2026, 9, 7)
    appointments = [
        Appointment(
            patient_id=patient.id, treatment_requirement_id=requirements[i].id,
            therapist_id=therapists[i].id, department_id=department.id, location_id=location.id,
            date=target, slot_index=i, status=AppointmentStatus.CONFIRMED,
            bundle_id=f"B{i}", source="test",
        ) for i in range(2)
    ]
    db_session.add_all(appointments)
    db_session.commit()

    affected = mark_therapist_absent(db_session, therapists[0].id, target, "Leave")
    assert [item.id for item in affected] == [appointments[0].id]
    assert appointments[0].status == AppointmentStatus.NEEDS_RESCHEDULING
    assert appointments[1].status == AppointmentStatus.CONFIRMED


@pytest.mark.parametrize("release", ["patient absence", "therapist absence", "cancellation", "no-show"])
def test_releasing_middle_session_keeps_remaining_bookings_and_allows_next_day_draft(db_session, release):
    visit_date = date(2026, 9, 7)
    target = visit_date + timedelta(days=1)
    department = Department(name="Speech")
    location = Location(name="A", capacity=1)
    therapist = Therapist(therapist_code="T1", name="T1", department=department, location=location)
    patient = Patient(patient_code="P1")
    db_session.add_all([therapist, patient])
    db_session.flush()
    requirements = [TreatmentRequirement(
        patient_id=patient.id, department_id=department.id, assigned_therapist_id=therapist.id,
        sessions_per_week=1, sequence_order=slot, plan_start_date=visit_date,
    ) for slot in range(3)]
    db_session.add_all(requirements)
    db_session.flush()
    appointments = [Appointment(
        patient_id=patient.id, treatment_requirement_id=requirement.id, therapist_id=therapist.id,
        department_id=department.id, location_id=location.id, date=visit_date, slot_index=slot,
        status=AppointmentStatus.CONFIRMED, bundle_id="original-visit",
    ) for slot, requirement in enumerate(requirements)]
    db_session.add_all(appointments)
    db_session.commit()

    if release == "patient absence":
        mark_patient_absent(db_session, patient.id, visit_date, "Sick", start_slot=1, end_slot=1)
    elif release == "therapist absence":
        mark_therapist_absent(db_session, therapist.id, visit_date, "Leave", start_slot=1, end_slot=1)
    elif release == "cancellation":
        cancel_patient_appointment(db_session, appointments[1])
    else:
        appointments[1].status = AppointmentStatus.NO_SHOW
        db_session.commit()

    problem = load_daily_scheduling_problem(db_session, target)
    limit_new_bookings_to_day(problem, target)
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=2))

    assert not any(result.validation.values())
    assert {(item.date, item.slot_index, item.status) for item in problem.pinned} == {
        (visit_date, 0, "CONFIRMED"), (visit_date, 2, "CONFIRMED"),
    }
    assert set(problem.pinned) <= set(result.appointments)
    assert [(item.date, item.requirement_id) for item in result.appointments if item not in problem.pinned] == [
        (target, requirements[1].id),
    ]
    _, report = validate_schedule(replace(problem, pinned=[]), result.appointments)
    assert report["broken_consecutive_bundles"] == 1


def test_patient_absence_releases_only_that_patients_appointments(db_session):
    department = Department(name="Speech")
    location = Location(name="A", capacity=2)
    therapist = Therapist(
        therapist_code="T1", name="T1", department=department, location=location,
    )
    patients = [Patient(patient_code=code) for code in ("P1", "P2")]
    db_session.add_all([department, location, therapist, *patients])
    db_session.flush()
    requirements = [
        TreatmentRequirement(
            patient_id=patient.id,
            department_id=department.id,
            assigned_therapist_id=therapist.id,
            sessions_per_week=1,
            sequence_order=0,
        )
        for patient in patients
    ]
    db_session.add_all(requirements)
    db_session.flush()
    target = date(2026, 9, 7)
    appointments = [
        Appointment(
            patient_id=patient.id,
            treatment_requirement_id=requirement.id,
            therapist_id=therapist.id,
            department_id=department.id,
            location_id=location.id,
            date=target,
            slot_index=index,
            status=AppointmentStatus.CONFIRMED,
            bundle_id=f"B{index}",
            source="test",
        )
        for index, (patient, requirement) in enumerate(zip(patients, requirements))
    ]
    db_session.add_all(appointments)
    db_session.commit()

    affected = mark_patient_absent(db_session, patients[0].id, target, "Sick")

    assert [item.id for item in affected] == [appointments[0].id]
    assert appointments[0].status == AppointmentStatus.NEEDS_RESCHEDULING
    assert appointments[1].status == AppointmentStatus.CONFIRMED

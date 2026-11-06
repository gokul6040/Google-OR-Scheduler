"""Staff-reviewed replacement chains preserve the scheduler's existing rules."""

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import (AdminSlotReservation, Appointment, AppointmentStatus, Department,
                             Location, Patient, PatientAbsence, Therapist, TreatmentRequirement)
from flow_test.models import Base as FlowBase, FlowOffer
from flow_test.service import FlowTest
from services.replacement_chain import preview_chain, save_chain


@pytest.fixture
def chain_data(db_session):
    FlowBase.metadata.create_all(db_session.bind)
    department = Department(name="Speech")
    location = Location(name="Clinic", capacity=1)
    therapist = Therapist(therapist_code="T", name="Clinician", department=department, location=location)
    patients = [Patient(patient_code=f"P{i}", name=f"Child {i}", active=True) for i in range(5)]
    db_session.add_all([department, location, therapist, *patients])
    db_session.flush()
    requirements = []
    for patient in patients:
        requirement = TreatmentRequirement(patient_id=patient.id, department_id=department.id,
            assigned_therapist_id=therapist.id, location_id=location.id,
            sessions_per_week=1, sequence_order=0, active=True)
        db_session.add(requirement)
        requirements.append(requirement)
    db_session.flush()
    days = [19, 20, 21, 22, 26]
    statuses = [AppointmentStatus.CANCELLED_PATIENT, AppointmentStatus.PROPOSED,
                AppointmentStatus.PROPOSED, AppointmentStatus.CONFIRMED, AppointmentStatus.PROPOSED]
    appointments = []
    for index, (patient, requirement) in enumerate(zip(patients, requirements)):
        appointment = Appointment(patient_id=patient.id, treatment_requirement_id=requirement.id,
            therapist_id=therapist.id, department_id=department.id, location_id=location.id,
            date=date(2026, 10, days[index]), slot_index=2, status=statuses[index],
            bundle_id=f"VISIT-{index}", source="solver")
        db_session.add(appointment)
        appointments.append(appointment)
    db_session.commit()
    flow = SimpleNamespace(_planning_fingerprint=FlowTest._planning_fingerprint)
    return db_session, flow, patients, appointments


def test_chain_is_reviewed_then_saved_atomically_without_moving_confirmed_or_next_week(chain_data):
    session, flow, _patients, appointments = chain_data
    source, first, second, confirmed, next_week = appointments
    preview = preview_chain(session, flow, source.id)
    assert [m["appointmentIds"] for m in preview["moves"]] == [[first.id], [second.id]]
    assert [(m["fromDate"], m["toDate"]) for m in preview["moves"]] == [
        ("2026-10-20", "2026-10-19"), ("2026-10-21", "2026-10-20")]
    assert (first.date, second.date) == (date(2026, 10, 20), date(2026, 10, 21))
    result = save_chain(session, flow, source.id, preview["previewToken"], "chain-1", "staff")
    session.commit()
    assert result["moved"] == 2
    assert (first.date, second.date) == (date(2026, 10, 19), date(2026, 10, 20))
    assert confirmed.date == date(2026, 10, 22) and next_week.date == date(2026, 10, 26)
    assert first.status == second.status == AppointmentStatus.PROPOSED
    assert session.get(AdminSlotReservation, source.id).appointment_id == first.id
    assert save_chain(session, flow, source.id, preview["previewToken"], "chain-1", "staff")["moved"] == 2


def test_changed_availability_rejects_review_without_partial_moves(chain_data):
    session, flow, patients, appointments = chain_data
    source, first, second, *_ = appointments
    preview = preview_chain(session, flow, source.id)
    session.add(PatientAbsence(patient_id=patients[1].id, date=source.date, full_day=True))
    session.commit()
    with pytest.raises(ValueError, match="changed"):
        save_chain(session, flow, source.id, preview["previewToken"], "chain-2", "staff")
    session.rollback()
    assert (first.date, second.date) == (date(2026, 10, 20), date(2026, 10, 21))
    assert session.get(AdminSlotReservation, source.id) is None


def test_offered_or_confirmed_visits_are_never_proposed_as_moves(chain_data):
    session, flow, _patients, appointments = chain_data
    source, first, second, *_ = appointments
    session.add(FlowOffer(token="offered-visit", appointment_ids=f"[{first.id}]", snapshot="[]",
        patient_code="P1", recipient="919999999999", state="PENDING"))
    session.commit()
    preview = preview_chain(session, flow, source.id)
    assert [m["appointmentIds"] for m in preview["moves"]] == [[second.id]]


def test_continuous_visit_moves_together_and_preserves_its_other_opening(chain_data):
    session, flow, patients, appointments = chain_data
    source, first, second, *_ = appointments
    location = session.get(Location, first.location_id)
    department = Department(name="Occupational therapy")
    therapist = Therapist(therapist_code="T2", name="Second clinician", department=department, location=location)
    session.add_all([department, therapist])
    session.flush()
    requirement = TreatmentRequirement(patient_id=patients[1].id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id, sessions_per_week=1, sequence_order=1, active=True)
    session.add(requirement)
    session.flush()
    extra = Appointment(patient_id=patients[1].id, treatment_requirement_id=requirement.id,
        therapist_id=therapist.id, department_id=department.id, location_id=location.id,
        date=first.date, slot_index=3, status=AppointmentStatus.PROPOSED,
        bundle_id=first.bundle_id, source="solver")
    session.add(extra)
    session.commit()
    preview = preview_chain(session, flow, source.id)
    assert [move["appointmentIds"] for move in preview["moves"]] == [[first.id, extra.id], [second.id]]
    save_chain(session, flow, source.id, preview["previewToken"], "multi-visit", "staff")
    session.commit()
    assert first.date == extra.date == date(2026, 10, 19)
    assert (first.slot_index, extra.slot_index) == (2, 3)
    assert second.date == date(2026, 10, 20)

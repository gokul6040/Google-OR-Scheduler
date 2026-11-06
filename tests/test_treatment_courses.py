"""Renewals retain prescriptions and bookings without sharing course progress."""

from datetime import date
import sqlite3

import pytest
from sqlalchemy import create_engine, func, inspect, select

from database.models import Appointment, AppointmentStatus, Patient, TreatmentRequirement
from database.repository import load_scheduling_problem
from database.treatment_migrations import has_course_identity, migrate_treatment_courses
from flow_test.service import FlowTest
from flow_test.zoho_import import apply_reports
from services.parent_response_service import weekly_progress
from tests.test_flow_test_source_rules import reports  # noqa: F401


@pytest.fixture
def course_flow(tmp_path, reports):
    reports["Plan_Report"] = [next(r for r in reports["Plan_Report"] if r["RegistrationID"] == "TEST-B")]
    flow = FlowTest(f"sqlite:///{(tmp_path / 'courses.sqlite').as_posix()}")
    apply_reports(flow, reports)
    yield flow
    flow.engine.dispose()


def current_requirement(session):
    return session.scalar(select(TreatmentRequirement).join(Patient).where(
        Patient.patient_code == "TEST-B", TreatmentRequirement.active.is_(True),
    ))


def book(session, requirement, day, status=AppointmentStatus.COMPLETED):
    appointment = Appointment(
        patient_id=requirement.patient_id, treatment_requirement_id=requirement.id,
        department_id=requirement.department_id, therapist_id=requirement.assigned_therapist_id,
        location_id=requirement.location_id, date=day, slot_index=0, status=status,
    )
    session.add(appointment)
    session.flush()
    return appointment.id


def renewal(reports, *, start="23-Oct-2026 11:30:00"):
    row = dict(reports["Plan_Report"][0], ID="900003", Added_Time=start,
               Modified_Time=start, Schedule="Psychology Therapy Session, 3, 3, TEST CONSULTANT P")
    reports["Plan_Report"].append(row)
    return row


@pytest.mark.parametrize("start", ["23-Oct-2026 11:30:00", "23-Feb-2027 11:30:00"])
def test_new_course_retains_old_prescription_and_starts_own_progress(course_flow, reports, start):
    with course_flow.sessions.begin() as session:
        old = current_requirement(session)
        old_id, patient_id = old.id, old.patient_id
        old_values = (old.plan_start_date, old.duration_months, old.sessions_per_week, old.source_plan_id)
        appointment_id = book(session, old, date(2026, 10, 22))
    row = renewal(reports, start=start)
    apply_reports(course_flow, reports)
    assert not apply_reports(course_flow, reports)["changed"]
    with course_flow.sessions() as session:
        new = current_requirement(session)
        old = session.get(TreatmentRequirement, old_id)
        assert new.id != old_id and new.patient_id == patient_id
        assert new.source_plan_id == row["ID"] and new.sessions_per_week == 3
        assert not old.active
        assert (old.plan_start_date, old.duration_months, old.sessions_per_week, old.source_plan_id) == old_values
        appointment = session.get(Appointment, appointment_id)
        assert appointment.treatment_requirement_id == old_id
        assert appointment.status == AppointmentStatus.COMPLETED
        assert session.scalar(select(func.count(Patient.id)).where(Patient.patient_code == "TEST-B")) == 1
        assert session.scalar(select(func.count(TreatmentRequirement.id)).where(
            TreatmentRequirement.patient_id == patient_id, TreatmentRequirement.source_plan_id == row["ID"])) == 1
        progress = next(p for p in weekly_progress(session, date(2026, 10, 23)) if p.requirement_id == new.id)
        assert (progress.completed, progress.booked, progress.remaining) == (0, 0, 3)
        historical = weekly_progress(session, date(2026, 10, 23), source_plan_id=old.source_plan_id)
        assert len(historical) == 1 and historical[0].completed == 1


@pytest.mark.parametrize("completed_day, review_day", [(22, 23), (29, 30)])
def test_same_course_edit_preserves_progress_and_identity(course_flow, reports, completed_day, review_day):
    with course_flow.sessions.begin() as session:
        old = current_requirement(session)
        identifier = old.id
        book(session, old, date(2026, 10, completed_day))
    reports["Plan_Report"][0].update(
        Modified_Time=f"{review_day}-Oct-2026 11:30:00",
        Schedule="Psychology Therapy Session, 3, 4, TEST CONSULTANT P",
    )
    apply_reports(course_flow, reports)
    with course_flow.sessions() as session:
        current = current_requirement(session)
        assert current.id == identifier and current.duration_months == 4
        progress = next(p for p in weekly_progress(session, date(2026, 10, review_day)) if p.requirement_id == identifier)
        assert (progress.completed, progress.remaining) == (1, 2)


def test_editing_old_course_never_reactivates_it_or_resets_current_progress(course_flow, reports):
    with course_flow.sessions() as session:
        old_id = current_requirement(session).id
    renewal(reports)
    apply_reports(course_flow, reports)
    with course_flow.sessions.begin() as session:
        current = current_requirement(session)
        current_id = current.id
        book(session, current, date(2026, 10, 29))
    # A week later an edit to the old source record is newer than the renewal's
    # Modified Time, but the renewal is still the newest-created course.
    reports["Plan_Report"][0]["Modified_Time"] = "30-Oct-2026 11:30:00"
    apply_reports(course_flow, reports)
    with course_flow.sessions() as session:
        current = current_requirement(session)
        assert current.id == current_id and current.source_plan_id == "900003"
        assert not session.get(TreatmentRequirement, old_id).active
        progress = next(p for p in weekly_progress(session, date(2026, 10, 30))
                        if p.requirement_id == current_id)
        assert (progress.completed, progress.remaining) == (1, 2)


def test_old_booking_stays_pinned_without_creating_old_course_demand(course_flow, reports):
    with course_flow.sessions.begin() as session:
        old = current_requirement(session)
        old_id = old.id
        appointment_id = book(session, old, date(2026, 10, 23), AppointmentStatus.CONFIRMED)
    renewal(reports)
    result = apply_reports(course_flow, reports)
    assert any("previous course" in w["message"] for w in result["warnings"])
    with course_flow.sessions() as session:
        problem = load_scheduling_problem(session, date(2026, 10, 19),
                                          carryover_by_requirement={old_id: 10})
        retired = next(r for r in problem.requirements if r.id == old_id)
        assert retired.sessions_per_week == 0
        assert any(a.requirement_id == old_id for a in problem.pinned)
    assert not any(course_flow.preview_daily(date(2026, 10, 24))[0].validation.values())
    with course_flow.sessions() as session:
        assert session.get(Appointment, appointment_id).status == AppointmentStatus.CONFIRMED


def test_legacy_projection_binds_previous_id_before_importing_renewal(course_flow, reports):
    with course_flow.sessions.begin() as session:
        old = current_requirement(session)
        old_id = old.id
        old.source_plan_id = None
        book(session, old, date(2026, 10, 22))
    original_id = reports["Plan_Report"][0]["ID"]
    renewal(reports)
    apply_reports(course_flow, reports)
    with course_flow.sessions() as session:
        old = session.get(TreatmentRequirement, old_id)
        assert old.source_plan_id == original_id and not old.active
        assert current_requirement(session).id != old_id


def test_course_identity_and_progress_survive_restart(course_flow, reports):
    renewal(reports)
    apply_reports(course_flow, reports)
    before = course_flow.state(date(2026, 10, 23))["progress"]
    reopened = FlowTest(str(course_flow.engine.url))
    try:
        assert reopened.state(date(2026, 10, 23))["progress"] == before
        assert not apply_reports(reopened, reports)["changed"]
    finally:
        reopened.engine.dispose()


def test_restored_copy_retains_course_links_without_sending(course_flow, reports, tmp_path):
    with course_flow.sessions.begin() as session:
        old = current_requirement(session)
        old_id = old.id
        appointment_id = book(session, old, date(2026, 10, 22))
    renewal(reports)
    apply_reports(course_flow, reports)
    destination = tmp_path / "restored.sqlite"
    with sqlite3.connect(course_flow.engine.url.database) as source, sqlite3.connect(destination) as restored:
        source.backup(restored)
    calls = []
    copy = FlowTest(f"sqlite:///{destination.as_posix()}", send=lambda *args: calls.append(args))
    try:
        with copy.sessions() as session:
            assert session.get(Appointment, appointment_id).treatment_requirement_id == old_id
            assert not session.get(TreatmentRequirement, old_id).active
            assert current_requirement(session).source_plan_id == "900003"
        assert calls == []
    finally:
        copy.engine.dispose()


def test_readding_therapy_in_same_course_reuses_its_requirement(course_flow, reports):
    original = reports["Plan_Report"][0]
    original["Schedule"] += "; IEP Session, 2, 3, TEST CONSULTANT I"
    apply_reports(course_flow, reports)
    with course_flow.sessions() as session:
        ids = set(session.scalars(select(TreatmentRequirement.id).join(Patient).where(
            Patient.patient_code == "TEST-B", TreatmentRequirement.source_plan_id == original["ID"])))
    original["Schedule"] = "Psychology Therapy Session, 2, 3, TEST CONSULTANT P"
    apply_reports(course_flow, reports)
    original["Schedule"] += "; IEP Session, 2, 3, TEST CONSULTANT I"
    apply_reports(course_flow, reports)
    with course_flow.sessions() as session:
        assert set(session.scalars(select(TreatmentRequirement.id).join(Patient).where(
            Patient.patient_code == "TEST-B", TreatmentRequirement.source_plan_id == original["ID"]))) == ids


def test_additive_course_migration_preserves_legacy_rows_and_is_repeatable():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE treatment_requirements (id INTEGER PRIMARY KEY, sessions_per_week INTEGER)")
            connection.exec_driver_sql("INSERT INTO treatment_requirements VALUES (7, 2)")
            migrate_treatment_courses(connection)
            migrate_treatment_courses(connection)
            assert tuple(connection.exec_driver_sql("SELECT * FROM treatment_requirements").one()) == (7, 2, None)
        assert has_course_identity(engine)
        assert "ix_treatment_requirements_source_plan_id" in {
            i["name"] for i in inspect(engine).get_indexes("treatment_requirements")
        }
    finally:
        engine.dispose()


def test_course_migration_is_confined_to_selected_schema():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS flow_test")
            for namespace in ("main", "flow_test"):
                connection.exec_driver_sql(
                    f"CREATE TABLE {namespace}.treatment_requirements (id INTEGER PRIMARY KEY)"
                )
            migrate_treatment_courses(connection, "flow_test")
            migrate_treatment_courses(connection, "flow_test")
        assert has_course_identity(engine, "flow_test")
        assert not has_course_identity(engine, "main")
    finally:
        engine.dispose()

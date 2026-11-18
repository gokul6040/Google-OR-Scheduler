"""Courses end at sessions/week x months x 4 sessions, not on a calendar date (fictional data)."""
from contextlib import nullcontext
from datetime import date, timedelta

from database.models import (
    Appointment, AppointmentStatus, AuditLog, Department, Location, Patient, PatientAbsence,
    Therapist, TreatmentRequirement,
)
from database.repository import (
    course_target, load_daily_scheduling_problem, load_scheduling_problem,
)
from scheduling.rules import placement_rejection_reason
from scheduling.types import BundleItem, TherapyBundle
from services.admin_reservation_service import preview_admin_reservation
from services.cancellation_service import cancel_patient_appointment
from services.course_progress import cancellation_streaks, course_progress

MONDAY = date(2026, 9, 7)
# Distinct earlier Monday/Wednesday dates, newest first, all before MONDAY's week.
PAST_DAYS = [MONDAY - timedelta(days=7 * (week + 1)) + timedelta(days=day)
             for week in range(30) for day in (0, 2)]


def _setup(db, *, weekly=2, months=1, start=date(2026, 6, 1), code="P1", therapist=None):
    if therapist is None:
        department = Department(name="Speech")
        location = Location(name="Centre", capacity=3)
        therapist = Therapist(therapist_code="T1", name="Dr T", department=department, location=location)
        db.add(therapist)
        db.flush()
    patient = Patient(patient_code=code, name=code, active=True)
    db.add(patient)
    db.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id, department_id=therapist.department_id, assigned_therapist_id=therapist.id,
        location_id=therapist.location_id, sessions_per_week=weekly, sequence_order=0,
        duration_months=months, plan_start_date=start, source_plan_id=f"plan-{code}")
    db.add(requirement)
    db.flush()
    return therapist, patient, requirement


def _book(db, therapist, patient, requirement, day, slot=0, status=AppointmentStatus.CONFIRMED):
    item = Appointment(
        patient_id=patient.id, treatment_requirement_id=requirement.id, therapist_id=therapist.id,
        department_id=therapist.department_id, location_id=therapist.location_id, date=day,
        slot_index=slot, status=status, bundle_id=f"B-{patient.id}-{day}-{slot}", source="test")
    db.add(item)
    db.flush()
    return item


def _past(db, therapist, patient, requirement, days, status=AppointmentStatus.CONFIRMED):
    return [_book(db, therapist, patient, requirement, day, status=status) for day in days]


def _demand(db, requirement, week=MONDAY):
    problem = load_scheduling_problem(db, week, horizon_days=6)
    return next(item for item in problem.requirements if item.id == requirement.id)


def test_course_total_is_weekly_times_months_times_four():
    assert course_target(2, 2) == 16
    assert course_target(1, 3) == 12
    assert course_target(2, None) is None and course_target(0, 2) is None


def test_weekly_request_shrinks_to_what_the_course_still_needs(db_session):
    therapist, patient, requirement = _setup(db_session, weekly=2, months=1)  # 8 sessions
    _past(db_session, therapist, patient, requirement, PAST_DAYS[:6])
    assert _demand(db_session, requirement).sessions_per_week == 2
    _past(db_session, therapist, patient, requirement, PAST_DAYS[6:7])  # 7/8
    assert _demand(db_session, requirement).sessions_per_week == 1
    _past(db_session, therapist, patient, requirement, PAST_DAYS[7:8])  # 8/8
    assert _demand(db_session, requirement).sessions_per_week == 0
    assert course_progress(db_session)[patient.id] == [{
        "requirementId": requirement.id, "therapy": "Speech", "sessions": 8, "target": 8, "complete": True}]


def test_course_continues_after_its_calendar_end_until_the_total(db_session):
    # Started 1 June for 1 month: the calendar end passed long before September.
    therapist, patient, requirement = _setup(db_session, weekly=2, months=1, start=date(2026, 6, 1))
    _past(db_session, therapist, patient, requirement, PAST_DAYS[:4])
    item = _demand(db_session, requirement)
    assert item.sessions_per_week == 2 and item.plan_end_date is None and item.course_target == 8
    problem = load_scheduling_problem(db_session, MONDAY, horizon_days=6)
    bundle = TherapyBundle("b", patient.id, 0, (BundleItem(
        requirement.id, requirement.department_id, therapist.id, therapist.location_id,
        item.plan_start_date, item.plan_end_date),))
    assert placement_rejection_reason(problem, bundle, 0, 0) is None


def test_plan_without_duration_keeps_weekly_only_demand(db_session):
    therapist, patient, requirement = _setup(db_session, weekly=2, months=None)
    item = _demand(db_session, requirement)
    assert item.course_target is None and item.sessions_per_week == 2


def test_cancelled_declined_or_no_show_sessions_stop_counting(db_session):
    therapist, patient, requirement = _setup(db_session, weekly=2, months=1)
    sessions = _past(db_session, therapist, patient, requirement, PAST_DAYS[:8])
    assert _demand(db_session, requirement).sessions_per_week == 0
    cancel_patient_appointment(db_session, sessions[0])
    sessions[1].status = AppointmentStatus.NO_SHOW
    sessions[2].status = AppointmentStatus.NEEDS_RESCHEDULING
    db_session.flush()
    progress = course_progress(db_session)[patient.id][0]
    assert (progress["sessions"], progress["target"], progress["complete"]) == (5, 8, False)
    assert _demand(db_session, requirement).sessions_per_week == 2


def test_upcoming_saved_sessions_count_and_a_new_plan_starts_at_zero(db_session):
    therapist, patient, requirement = _setup(db_session, weekly=2, months=1)
    _book(db_session, therapist, patient, requirement, MONDAY + timedelta(days=1), status=AppointmentStatus.PROPOSED)
    assert course_progress(db_session)[patient.id][0]["sessions"] == 1
    requirement.active = False
    renewed = TreatmentRequirement(
        patient_id=patient.id, department_id=requirement.department_id, assigned_therapist_id=therapist.id,
        location_id=therapist.location_id, sessions_per_week=1, sequence_order=0, duration_months=2,
        plan_start_date=MONDAY, source_plan_id="plan-renewed")
    db_session.add(renewed)
    db_session.flush()
    assert course_progress(db_session)[patient.id] == [{
        "requirementId": renewed.id, "therapy": "Speech", "sessions": 0, "target": 8, "complete": False}]


def test_missed_week_gives_priority_but_never_doubles_the_weekly_number(db_session):
    therapist, _, course = _setup(db_session, weekly=2, months=3)
    _, _, legacy = _setup(db_session, weekly=2, months=None, code="P2", therapist=therapist)
    db_session.add(AuditLog(action="daily_planning_started", entity=(MONDAY - timedelta(days=21)).isoformat()))
    db_session.flush()
    problem = load_daily_scheduling_problem(db_session, MONDAY + timedelta(days=1))
    by_id = {item.id: item for item in problem.requirements}
    assert by_id[course.id].sessions_per_week == 2  # the course finishes later instead
    assert by_id[legacy.id].sessions_per_week == 4  # legacy carry-over unchanged
    assert {course.id, legacy.id} <= problem.priority_requirement_ids


def test_unsaved_sessions_from_earlier_preview_weeks_are_counted(db_session):
    therapist, patient, requirement = _setup(db_session, weekly=2, months=1)
    problem = load_scheduling_problem(db_session, MONDAY, horizon_days=6, planned_elsewhere={requirement.id: 7})
    assert next(item for item in problem.requirements if item.id == requirement.id).sessions_per_week == 1


def test_manual_reservation_warns_when_the_course_is_already_complete(db_session):
    therapist, owner, owner_req = _setup(db_session, weekly=1, months=1)
    _, child, child_req = _setup(db_session, weekly=1, months=1, code="P2", therapist=therapist)
    _past(db_session, therapist, child, child_req, PAST_DAYS[:4])  # 4/4
    opening = _book(db_session, therapist, owner, owner_req, MONDAY + timedelta(days=1),
                    status=AppointmentStatus.CANCELLED_PATIENT)
    db_session.commit()
    codes = {item["code"] for item in preview_admin_reservation(db_session, opening.id, child.id)["warnings"]}
    assert "COURSE_COMPLETE" in codes and "PLAN_ENDED" not in codes


def test_four_cancellations_in_a_row_raise_a_warning(db_session):
    therapist, patient, requirement = _setup(db_session, weekly=2, months=3)
    today = MONDAY + timedelta(days=14)
    _book(db_session, therapist, patient, requirement, MONDAY - timedelta(days=7))  # attended
    _book(db_session, therapist, patient, requirement, MONDAY, status=AppointmentStatus.CANCELLED_PATIENT)
    _book(db_session, therapist, patient, requirement, MONDAY + timedelta(days=2), status=AppointmentStatus.NO_SHOW)
    # Parent No: released with the child's absence recorded.
    no_day = MONDAY + timedelta(days=7)
    _book(db_session, therapist, patient, requirement, no_day, status=AppointmentStatus.NEEDS_RESCHEDULING)
    db_session.add(PatientAbsence(patient_id=patient.id, date=no_day, full_day=True, reason="Parent said no"))
    # A doctor's absence (no patient absence) and a replaced proposal do not count.
    _book(db_session, therapist, patient, requirement, MONDAY + timedelta(days=8),
          status=AppointmentStatus.NEEDS_RESCHEDULING)
    _book(db_session, therapist, patient, requirement, MONDAY + timedelta(days=9),
          status=AppointmentStatus.CANCELLED_OTHER)
    db_session.flush()
    assert cancellation_streaks(db_session, today) == {patient.id: 3}
    _book(db_session, therapist, patient, requirement, MONDAY + timedelta(days=10),
          status=AppointmentStatus.CANCELLED_PATIENT)
    # An upcoming booking does not clear the warning; a session that took place does.
    _book(db_session, therapist, patient, requirement, today + timedelta(days=1), status=AppointmentStatus.PROPOSED)
    assert cancellation_streaks(db_session, today) == {patient.id: 4}
    _book(db_session, therapist, patient, requirement, MONDAY + timedelta(days=11))
    assert cancellation_streaks(db_session, today) == {}


def test_patients_tab_shows_sessions_and_the_warning(db_session, monkeypatch):
    import web_app
    therapist, patient, requirement = _setup(db_session, weekly=2, months=2)
    _past(db_session, therapist, patient, requirement, PAST_DAYS[:2])
    for offset in range(4):
        _book(db_session, therapist, patient, requirement, date(2026, 10, 5) + timedelta(days=offset),
              status=AppointmentStatus.CANCELLED_PATIENT)
    db_session.commit()
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    row = web_app.patients_payload()["patients"][0]
    # Session counts appear only in the patient side panel, not in the list (user, 12 Nov 2026).
    assert "courses" not in row
    assert row["cancelStreak"] == 4
    plan = web_app.serialize_patient_preferences(db_session, patient)
    assert (plan["plan"][0]["sessions"], plan["plan"][0]["target"], plan["cancelStreak"]) == (2, 16, 4)

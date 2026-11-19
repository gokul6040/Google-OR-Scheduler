"""Centre shifts (IQRAA example, 18 Nov 2026): timetables, warnings, scheduling and changes."""

from datetime import date, time

import pytest
from sqlalchemy import select

from config import SolverSettings
from database.models import (Appointment, AppointmentStatus, Department, Location, Patient, Therapist,
                             TreatmentRequirement)
from database.repository import load_scheduling_problem
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import DEFAULT_TIMETABLE, slot_by_times, slot_def, timetable_for
from scheduling.types import ScheduledAppointment
from services.shift_service import (ShiftActionError, assign_shift, load_shift_context, save_shift,
                                    shifts_overview)

MONDAY = date(2027, 1, 4)


def rows(*spec):
    return [{"kind": "break", "breakKind": kind[6:], "start": start, "end": end} if kind.startswith("break")
            else {"kind": "session", "start": start, "end": end} for kind, start, end in spec]


SHIFT_2 = rows(("session", "09:40", "10:20"), ("break:short", "10:20", "10:35"), ("session", "10:35", "11:15"),
               ("session", "11:15", "11:55"), ("session", "11:55", "12:35"), ("break:lunch", "12:35", "13:15"),
               ("session", "13:15", "13:55"), ("session", "13:55", "14:35"), ("session", "14:35", "15:15"),
               ("break:short", "15:15", "15:30"), ("session", "15:30", "16:10"), ("session", "16:10", "16:50"),
               ("session", "16:50", "17:30"))
SHIFT_3 = rows(("session", "12:35", "13:15"), ("session", "13:15", "13:55"), ("session", "13:55", "14:35"),
               ("session", "14:35", "15:15"), ("break:short", "15:15", "15:30"), ("session", "15:30", "16:10"),
               ("session", "16:10", "16:50"), ("session", "16:50", "17:30"), ("session", "17:30", "18:10"),
               ("break:short", "18:10", "18:20"), ("session", "18:20", "19:00"), ("session", "19:00", "19:40"))


@pytest.fixture
def centre(db_session):
    location = Location(name="IQRAA Thanal EIC", capacity=6, active=True)
    ot, psy = Department(name="OT", active=True), Department(name="Psychology", active=True)
    db_session.add_all([location, ot, psy])
    db_session.flush()
    doctors = {code: Therapist(therapist_code=code, name=f"Doctor {code}", department_id=dept.id,
                               location_id=location.id, active=True)
               for code, dept in (("S1", ot), ("S2", ot), ("S3", psy))}
    db_session.add_all(doctors.values())
    db_session.flush()
    return db_session, location, doctors


def patient_with(session, code, plan):
    patient = Patient(patient_code=code, name=code, active=True)
    session.add(patient)
    session.flush()
    for order, (doctor, weekly) in enumerate(plan):
        session.add(TreatmentRequirement(patient_id=patient.id, department_id=doctor.department_id,
                                         assigned_therapist_id=doctor.id, location_id=doctor.location_id,
                                         sessions_per_week=weekly, sequence_order=order, active=True))
    session.flush()
    return patient


def setup_iqraa(session, location, doctors):
    two = save_shift(session, location_id=location.id, name="Shift 2", items=SHIFT_2, actor="t", activate=True)
    three = save_shift(session, location_id=location.id, name="Shift 3", items=SHIFT_3, actor="t", activate=True)
    assign_shift(session, [doctors["S2"].id], two["shift"]["id"], "t")
    assign_shift(session, [doctors["S3"].id], three["shift"]["id"], "t")
    session.commit()
    return two, three


def session_at(start: str) -> int:
    hours, mins = map(int, start.split(":"))
    total = hours * 60 + mins + 40
    found = slot_by_times(time(hours, mins), time(total // 60, total % 60))
    assert found is not None, start
    return found.slot_index


def available(problem, doctor, weekday=0):
    return {slot for (therapist, day, slot) in problem.therapist_availability if therapist == doctor and day == weekday}


def test_centre_without_shifts_keeps_the_original_timetable(centre):
    session, location, doctors = centre
    problem = load_scheduling_problem(session, MONDAY)
    assert timetable_for(problem, location.id) == DEFAULT_TIMETABLE
    assert available(problem, doctors["S1"].id) == set(range(10))
    overview = shifts_overview(session, location.id)
    assert [item["name"] for item in overview["shifts"]] == ["Shift 1"] and overview["shifts"][0]["implicit"]
    assert overview["shifts"][0]["doctors"] == 3


def test_iqraa_shifts_make_one_fifteen_session_day(centre):
    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    problem = load_scheduling_problem(session, MONDAY)
    timetable = timetable_for(problem, location.id)
    starts = [f"{slot_def(slot).start_time:%H:%M}" for slot in timetable.order]
    assert starts == ["09:00", "09:40", "10:35", "11:15", "11:55", "12:35", "13:15", "13:55", "14:35",
                      "15:30", "16:10", "16:50", "17:30", "18:20", "19:00"]
    # Original sessions keep their numbers, so saved bookings never move.
    assert session_at("09:00") == 0 and session_at("16:10") == 9 and session_at("12:35") >= 10
    # Short breaks chain a visit; 11:55 -> 12:35 -> 13:15 has no gap at all.
    for first, second in (("09:40", "10:35"), ("11:55", "12:35"), ("12:35", "13:15"), ("17:30", "18:20")):
        assert (session_at(first), session_at(second)) in timetable.links
    s1, s3 = available(problem, doctors["S1"].id), available(problem, doctors["S3"].id)
    assert session_at("12:35") not in s1 and session_at("17:30") not in s1  # Shift 1 lunch / off shift
    assert session_at("12:35") in s3 and session_at("09:00") not in s3
    assert len(s3) == 10
    names = [item["name"] for item in shifts_overview(session, location.id)["shifts"]]
    assert names == ["Shift 1", "Shift 2", "Shift 3"]


def test_partial_overlap_is_a_warning_and_blocks_activation(centre):
    session, location, _ = centre
    staggered = rows(("session", "09:20", "10:00"), ("session", "10:00", "10:40"))
    with pytest.raises(ShiftActionError) as error:
        save_shift(session, location_id=location.id, name="Late start", items=staggered, actor="t", activate=True)
    assert error.value.warnings and "overlaps 9:00 AM" in error.value.warnings[0]
    draft = save_shift(session, location_id=location.id, name="Late start", items=staggered, actor="t")
    assert draft["shift"]["status"] == "draft" and draft["warnings"]
    with pytest.raises(ShiftActionError, match="40 minutes"):
        save_shift(session, location_id=location.id, name="Hour", items=rows(("session", "09:00", "10:00")), actor="t")
    with pytest.raises(ShiftActionError, match="cannot cut through"):
        save_shift(session, location_id=location.id, name="Cut", actor="t",
                   items=rows(("session", "09:00", "09:40"), ("break:short", "09:20", "09:30")))
    with pytest.raises(ShiftActionError, match="between 7:00 AM and 9:00 PM"):
        save_shift(session, location_id=location.id, name="Night", items=rows(("session", "20:40", "21:20")), actor="t")


def test_scheduler_books_each_doctor_inside_their_shift(centre):
    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    late = patient_with(session, "LATE", [(doctors["S3"], 2)])
    mixed = patient_with(session, "MIXED", [(doctors["S1"], 1), (doctors["S3"], 1)])
    session.commit()
    problem = load_scheduling_problem(session, MONDAY)
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=10))
    assert not any(result.validation.values()) and not result.unmet
    s3_sessions = {session_at(start) for start in ("12:35", "13:15", "13:55", "14:35", "15:30", "16:10",
                                                    "16:50", "17:30", "18:20", "19:00")}
    for item in result.appointments:
        if item.therapist_id == doctors["S3"].id:
            assert item.slot_index in s3_sessions
        if item.therapist_id == doctors["S1"].id:
            assert item.slot_index in range(10)
    visits = {}
    for item in result.appointments:
        if item.patient_id == mixed.id:
            visits.setdefault(item.date, []).append(item)
    for items in visits.values():  # Both therapies in one back-to-back visit across the two shifts.
        assert len(items) == 2 and timetable_for(problem, location.id).chained([a.slot_index for a in items])
    assert sum(1 for item in result.appointments if item.patient_id == late.id) == 2


def test_back_to_back_visit_never_crosses_lunch(centre):
    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    problem = load_scheduling_problem(session, MONDAY)
    timetable = timetable_for(problem, location.id)
    assert timetable.block(session_at("11:55"), 2) == (session_at("11:55"), session_at("12:35"))
    patient = patient_with(session, "P", [(doctors["S1"], 1), (doctors["S2"], 1)])
    session.commit()
    problem = load_scheduling_problem(session, MONDAY)
    requirements = list(session.scalars(select(TreatmentRequirement).where(
        TreatmentRequirement.patient_id == patient.id).order_by(TreatmentRequirement.sequence_order)))
    # Shift 1 and 2 doctors break for lunch at 12:35, so 11:55 then 13:15 is not one visit.
    visit = [ScheduledAppointment(patient.id, req.id, req.assigned_therapist_id, req.department_id, location.id,
                                  MONDAY, slot, "V")
             for req, slot in zip(requirements, (session_at("11:55"), session_at("13:15")))]
    _, report = validate_schedule(problem, visit)
    assert report["broken_consecutive_bundles"] == 1


def test_only_clinicians_who_arrive_later_need_a_shift_check(centre):
    from services.shift_service import calendar_shift_data

    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    newcomer = Therapist(therapist_code="ZC-NEW", name="New from Zoho", department_id=doctors["S1"].department_id,
                         location_id=location.id, active=True)
    session.add(newcomer)
    session.commit()
    flags = calendar_shift_data(session, [*doctors.values(), newcomer])["therapistShifts"]
    assert [flags[doctor.id]["shiftNeedsCheck"] for doctor in doctors.values()] == [False, False, False]
    assert flags[newcomer.id]["shiftNeedsCheck"] and flags[newcomer.id]["shiftName"] == "Shift 1"


def test_moving_doctors_lists_affected_bookings_and_keeps_them(centre):
    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    patient = patient_with(session, "BOOKED", [(doctors["S1"], 1)])
    requirement = session.scalar(select(TreatmentRequirement).where(TreatmentRequirement.patient_id == patient.id))
    session.add(Appointment(patient_id=patient.id, treatment_requirement_id=requirement.id,
                            therapist_id=doctors["S1"].id, department_id=requirement.department_id,
                            location_id=location.id, date=MONDAY, slot_index=0, status=AppointmentStatus.CONFIRMED))
    session.commit()
    shift_3 = next(shift for shift in load_shift_context(session).shifts.values() if shift.name == "Shift 3")
    with pytest.raises(ShiftActionError) as error:
        assign_shift(session, [doctors["S1"].id], shift_3.id, "t")
    assert len(error.value.affected) == 1 and error.value.affected[0]["time"] == "09:00–09:40"
    result = assign_shift(session, [doctors["S1"].id], shift_3.id, "t", acknowledge_affected=True)
    session.commit()
    assert result["affected"] == 1
    # The confirmed 9:00 booking stays as booked and stays valid for planning.
    problem = load_scheduling_problem(session, MONDAY)
    issues, _ = validate_schedule(problem, problem.pinned)
    assert not issues
    assert session.scalar(select(Appointment.slot_index).where(Appointment.patient_id == patient.id)) == 0


def test_kept_booking_blocks_an_overlapping_new_time(centre):
    """Shift 1 edited to start 9:20 while a 9:00-9:40 booking is kept: the edit warns, the
    planner skips 9:20 that day, and the validator catches a hand-made clash."""
    session, location, doctors = centre
    doctor = doctors["S1"]
    kept_child = patient_with(session, "KEPT", [(doctor, 1)])
    requirement = session.scalar(select(TreatmentRequirement).where(TreatmentRequirement.patient_id == kept_child.id))
    session.add(Appointment(patient_id=kept_child.id, treatment_requirement_id=requirement.id, therapist_id=doctor.id,
                            department_id=requirement.department_id, location_id=location.id, date=MONDAY,
                            slot_index=0, status=AppointmentStatus.CONFIRMED))
    session.commit()
    late_start = rows(("session", "09:20", "10:00"))  # The only session left, so Monday must be skipped.
    with pytest.raises(ShiftActionError) as error:
        save_shift(session, location_id=location.id, name="Shift 1", items=late_start, actor="t", edit_default=True)
    assert error.value.affected[0]["clashesWith"] == ["09:20–10:00"]
    assert "overlap the new times" in str(error.value)
    save_shift(session, location_id=location.id, name="Shift 1", items=late_start, actor="t", edit_default=True,
               acknowledge_affected=True)
    patient_with(session, "DAILY", [(doctor, 6)])
    session.commit()

    problem = load_scheduling_problem(session, MONDAY)
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=10))
    assert not any(result.validation.values())
    monday = {item.slot_index for item in result.appointments if item.therapist_id == doctor.id and item.date == MONDAY}
    assert monday == {0}
    later = [item for item in result.appointments if item.therapist_id == doctor.id and item.date != MONDAY]
    assert len(later) == 5 and {item.slot_index for item in later} == {session_at("09:20")}
    assert sum(item.missing_sessions for item in result.unmet) == 1

    kept = next(item for item in problem.pinned if item.therapist_id == doctor.id)
    clash = ScheduledAppointment(kept_child.id + 1000, requirement.id, doctor.id, requirement.department_id,
                                 location.id, MONDAY, session_at("09:20"), "X")
    _, report = validate_schedule(problem, [kept, clash])
    assert report["therapist_overlaps"] == 1


def test_validator_counts_room_use_by_clock_time(centre):
    session, location, doctors = centre
    location.capacity = 1
    session.commit()
    problem = load_scheduling_problem(session, MONDAY)
    nine, nine_twenty = 0, session_at("09:20")
    first = ScheduledAppointment(1, 1, doctors["S1"].id, doctors["S1"].department_id, location.id, MONDAY, nine, "A")
    second = ScheduledAppointment(2, 2, doctors["S3"].id, doctors["S3"].department_id, location.id, MONDAY,
                                  nine_twenty, "B")
    _, report = validate_schedule(problem, [first, second])
    assert report["location_capacity_violations"] == 1 and report["therapist_overlaps"] == 0
    back_to_back = ScheduledAppointment(2, 2, doctors["S3"].id, doctors["S3"].department_id, location.id, MONDAY,
                                        1, "B")
    _, report = validate_schedule(problem, [first, back_to_back])
    assert report["location_capacity_violations"] == 0


def book(session, patient, doctor, day, slot):
    requirement = session.scalar(select(TreatmentRequirement).where(
        TreatmentRequirement.patient_id == patient.id, TreatmentRequirement.assigned_therapist_id == doctor.id))
    appointment = Appointment(patient_id=patient.id, treatment_requirement_id=requirement.id, therapist_id=doctor.id,
                              department_id=requirement.department_id, location_id=doctor.location_id, date=day,
                              slot_index=slot, status=AppointmentStatus.CONFIRMED)
    session.add(appointment)
    session.flush()
    return appointment


def test_manual_booking_outside_the_doctors_shift_needs_an_override(centre):
    from services.admin_reservation_service import EmptySlot, preview_empty_slot

    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    late = doctors["S3"]  # Shift 3 starts at 12:35
    child = patient_with(session, "MANUAL", [(late, 1)])
    session.commit()

    def codes(slot):
        opening = EmptySlot(MONDAY, slot, late.id, late.department_id, location.id)
        return {warning["code"] for warning in preview_empty_slot(session, opening, child.id)["warnings"]}

    assert "THERAPIST_OFF_SHIFT" in codes(0)  # 9:00 AM
    assert "THERAPIST_OFF_SHIFT" not in codes(session_at("12:35"))


def test_replacement_search_never_offers_an_off_shift_opening(centre):
    from services.replacement_service import find_pull_forward_candidates

    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    late = doctors["S3"]
    child = patient_with(session, "LATER", [(late, 1)])
    book(session, child, late, date(2027, 1, 7), session_at("12:35"))
    session.commit()

    def opening(slot):
        return Appointment(patient_id=child.id + 999, therapist_id=late.id, department_id=late.department_id,
                           location_id=location.id, date=MONDAY, slot_index=slot)

    assert find_pull_forward_candidates(session, opening(0), date(2027, 1, 9)) == []  # 9:00 is off shift
    found = find_pull_forward_candidates(session, opening(session_at("13:15")), date(2027, 1, 9))
    assert [item["patient_code"] for item in found] == ["LATER"]


def test_calendar_keeps_a_booked_time_that_no_shift_uses(centre):
    from services.shift_service import calendar_shift_data
    from scheduling.slot_config import encode_slot

    session, location, doctors = centre
    setup_iqraa(session, location, doctors)
    retired = encode_slot(time(20, 0), time(20, 40))  # no shift has 8:00 PM
    without = {item["id"] for item in calendar_shift_data(session, list(doctors.values()))["sessions"]}
    shown = calendar_shift_data(session, list(doctors.values()), {retired})
    assert retired not in without
    assert retired in {item["id"] for item in shown["sessions"]} and shown["slotLabels"][str(retired)] == "20:00–20:40"


def test_turning_a_short_break_into_lunch_lists_split_visits_and_stales_previews(centre):
    from flow_test.service import FlowTest

    session, location, doctors = centre
    two, _ = setup_iqraa(session, location, doctors)
    child = patient_with(session, "PAIR", [(doctors["S1"], 1), (doctors["S2"], 1)])
    book(session, child, doctors["S1"], MONDAY, session_at("09:40"))
    book(session, child, doctors["S2"], MONDAY, session_at("10:35"))  # joined by the 10:20 short break
    session.commit()
    before = FlowTest._planning_fingerprint(session, load_scheduling_problem(session, MONDAY))
    lunch = [dict(row, breakKind="lunch") if row.get("breakKind") == "short" and row["start"] == "10:20" else row
             for row in SHIFT_2]
    with pytest.raises(ShiftActionError) as error:
        save_shift(session, location_id=location.id, name="Shift 2", items=lunch, actor="t",
                   shift_id=two["shift"]["id"])
    assert [item["patientCode"] for item in error.value.affected if item.get("splitVisit")] == ["PAIR"]
    assert "split by a lunch break" in str(error.value)
    save_shift(session, location_id=location.id, name="Shift 2", items=lunch, actor="t",
               shift_id=two["shift"]["id"], acknowledge_affected=True)
    session.commit()
    after = FlowTest._planning_fingerprint(session, load_scheduling_problem(session, MONDAY))
    assert before != after  # a preview made before the change can no longer be saved


FIFTY = rows(("session", "09:00", "09:50"), ("session", "09:50", "10:40"), ("break:short", "10:40", "10:50"),
             ("session", "10:50", "11:40"), ("break:lunch", "11:40", "12:20"), ("session", "12:20", "13:10"))


def deactivate(session, shift_id=None, location_id=None):
    from services.shift_service import archive_shift

    with pytest.raises(ShiftActionError) as error:  # always asks "are you sure" first
        archive_shift(session, shift_id, "t", location_id=location_id)
    assert error.value.confirm
    archive_shift(session, shift_id, "t", location_id=location_id, acknowledge_affected=True)
    session.commit()
    return error.value


def test_deactivating_shift_1_asks_first_and_stops_scheduling_there(centre):
    from services.shift_service import calendar_shift_data

    session, location, doctors = centre
    other = Location(name="IQRAA keeps 40", capacity=6, active=True)
    session.add(other)
    session.commit()
    asked = deactivate(session, location_id=location.id)
    assert asked.title == "Deactivate Shift 1?" and "won't be scheduled" in str(asked)
    problem = load_scheduling_problem(session, MONDAY)
    assert available(problem, doctors["S1"].id) == set()
    assert timetable_for(problem, other.id) == DEFAULT_TIMETABLE  # other centres keep 40 minutes
    assert calendar_shift_data(session, [doctors["S1"]])["therapistShifts"][doctors["S1"].id]["shiftName"] == "No shift"
    overview = shifts_overview(session, location.id)
    # Shift 1 stays in the list as Inactive (kept, not used).
    assert [(item["name"], item["status"], item["standard"]) for item in overview["shifts"]] == [
        ("Shift 1", "inactive", True)]
    assert overview["lengthLocked"] is False and overview["withoutShift"] == 3


def test_first_session_of_the_next_shift_sets_the_centre_length(centre):
    session, location, doctors = centre
    deactivate(session, location_id=location.id)
    with pytest.raises(ShiftActionError, match="set by its first session"):
        save_shift(session, location_id=location.id, name="Mixed", actor="t",
                   items=rows(("session", "09:00", "09:50"), ("session", "09:50", "10:30")))
    saved = save_shift(session, location_id=location.id, name="Morning 50", items=FIFTY, actor="t", activate=True)
    session.commit()
    assert saved["shift"]["isDefault"] and saved["shift"]["sessionMinutes"] == 50
    overview = shifts_overview(session, location.id)
    assert overview["sessionMinutes"] == 50 and overview["lengthLocked"] is True and overview["withoutShift"] == 0
    # Every later shift at this centre follows 50 minutes until all are deactivated again.
    with pytest.raises(ShiftActionError, match="are 50 minutes"):
        save_shift(session, location_id=location.id, name="Forty", items=rows(("session", "13:00", "13:40")),
                   actor="t")
    patient_with(session, "FIFTY", [(doctors["S1"], 2), (doctors["S3"], 2)])
    session.commit()
    problem = load_scheduling_problem(session, MONDAY)
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=10))
    assert not any(result.validation.values()) and not result.unmet
    for item in result.appointments:
        slot = slot_def(item.slot_index)
        assert (slot.end_time.hour * 60 + slot.end_time.minute) - (slot.start_time.hour * 60 + slot.start_time.minute) == 50


def test_default_shift_is_deactivated_last(centre):
    session, location, doctors = centre
    two, three = setup_iqraa(session, location, doctors)
    from services.shift_service import archive_shift

    with pytest.raises(ShiftActionError, match="Deactivate Shift 2, Shift 3 first"):
        archive_shift(session, None, "t", location_id=location.id, acknowledge_affected=True)
    with pytest.raises(ShiftActionError, match="Move the 1 clinician"):
        archive_shift(session, two["shift"]["id"], "t", acknowledge_affected=True)


def test_other_shifts_are_deleted_and_shift_1_can_be_reactivated(centre):
    from services.shift_service import archive_shift, reactivate_shift

    session, location, doctors = centre
    deactivate(session, location_id=location.id)
    fifty = save_shift(session, location_id=location.id, name="Fifty", items=FIFTY, actor="t", activate=True)
    session.commit()
    shift_1 = next(item for item in shifts_overview(session, location.id)["shifts"] if item["name"] == "Shift 1")
    with pytest.raises(ShiftActionError, match="Reactivate it"):
        save_shift(session, location_id=location.id, name="Shift 1", items=SHIFT_2, actor="t", shift_id=shift_1["id"])
    draft = save_shift(session, location_id=location.id, name="Later", actor="t",
                       items=rows(("session", "14:00", "14:50")))
    session.commit()
    # Reactivating warns first: shifts of another length (Fifty, and the 50-minute draft) are deleted.
    with pytest.raises(ShiftActionError) as error:
        reactivate_shift(session, shift_1["id"], "t")
    assert error.value.confirm and error.value.title == "Reactivate Shift 1?"
    assert "will be deleted: Fifty, Later" in str(error.value) and "3 clinician(s) will work Shift 1" in str(error.value)
    result = reactivate_shift(session, shift_1["id"], "t", acknowledge_affected=True)
    session.commit()
    assert result["deleted"] == ["Fifty", "Later"] and draft["shift"]["id"] and fifty["shift"]["id"]
    overview = shifts_overview(session, location.id)
    assert [(item["name"], item["status"]) for item in overview["shifts"]] == [("Shift 1", "active")]
    assert overview["sessionMinutes"] == 40 and overview["lengthLocked"] is True
    problem = load_scheduling_problem(session, MONDAY)
    assert available(problem, doctors["S1"].id) == set(range(10))


def test_reactivating_shift_1_keeps_shifts_of_the_same_length(centre):
    from services.shift_service import reactivate_shift

    session, location, doctors = centre
    deactivate(session, location_id=location.id)
    forty = save_shift(session, location_id=location.id, name="Forty late", actor="t", activate=True,
                       items=rows(("session", "12:35", "13:15"), ("session", "13:15", "13:55")))
    session.commit()
    shift_1 = next(item for item in shifts_overview(session, location.id)["shifts"] if item["name"] == "Shift 1")
    with pytest.raises(ShiftActionError) as error:
        reactivate_shift(session, shift_1["id"], "t")
    assert "Forty late stays in use" in str(error.value) and "deleted" not in str(error.value)
    reactivate_shift(session, shift_1["id"], "t", acknowledge_affected=True)
    session.commit()
    listed = {item["name"]: (item["status"], item["isDefault"]) for item in shifts_overview(session, location.id)["shifts"]}
    assert listed == {"Shift 1": ("active", True), "Forty late": ("active", False)}
    # Clinicians who were on the 40-minute shift stay on it.
    problem = load_scheduling_problem(session, MONDAY)
    assert available(problem, doctors["S1"].id) == {session_at("12:35"), session_at("13:15")}
    assert forty["shift"]["id"]


def test_reactivating_shift_1_is_blocked_by_overlapping_times(centre):
    from services.shift_service import reactivate_shift

    session, location, _ = centre
    deactivate(session, location_id=location.id)
    save_shift(session, location_id=location.id, name="Custom", actor="t", activate=True,
               items=rows(("session", "09:20", "10:00")))
    session.commit()
    shift_1 = next(item for item in shifts_overview(session, location.id)["shifts"] if item["name"] == "Shift 1")
    # 9:20–10:00 partly overlaps Shift 1's 9:00–9:40: no confirmation is offered, even when acknowledged.
    with pytest.raises(ShiftActionError) as error:
        reactivate_shift(session, shift_1["id"], "t", acknowledge_affected=True)
    assert not error.value.confirm and "can't be reactivated" in str(error.value)
    assert any("overlaps" in item and "(Custom)" in item for item in error.value.warnings)
    session.rollback()
    listed = {item["name"]: item["status"] for item in shifts_overview(session, location.id)["shifts"]}
    assert listed == {"Shift 1": "inactive", "Custom": "active"}


def test_moving_a_visit_outside_the_doctors_shift_says_so(centre):
    from services.appointment_service import move_appointment

    session, location, doctors = centre
    setup_iqraa(session, location, doctors)  # S2 works Shift 2 (from 9:40)
    patient = patient_with(session, "MOVE", [(doctors["S2"], 1)])
    visit = book(session, patient, doctors["S2"], MONDAY, session_at("10:35"))
    session.commit()
    problem = load_scheduling_problem(session, MONDAY)
    moved, messages = move_appointment(session, problem, problem.pinned, visit, MONDAY, 0)  # 9:00, Shift 1 only
    assert not moved
    assert messages == ["09:00–09:40 isn't a session time in this doctor's shift. Choose one of their shift times."]
    moved, messages = move_appointment(session, problem, problem.pinned, visit, MONDAY, session_at("11:15"))
    assert moved and messages == []


def test_clinical_team_flags_clinicians_with_no_shift(centre, monkeypatch):
    """Clinical team "No shift (n)" filter: clinicians left without an active shift, to assign."""
    from contextlib import nullcontext

    import web_app

    session, location, _ = centre
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(session))
    no_shift = lambda: [row["noShift"] for row in web_app.therapists_payload(MONDAY)["therapists"]]
    assert no_shift() == [False, False, False]
    deactivate(session, location_id=location.id)
    assert no_shift() == [True, True, True]
    save_shift(session, location_id=location.id, name="Fifty", actor="t", activate=True,
               items=rows(("session", "09:00", "09:50")), acknowledge_affected=True)
    session.commit()
    assert no_shift() == [False, False, False]

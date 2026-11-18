"""Picked preferred days: weekly weighting and the daily planner's hold for a picked day."""

from datetime import date, timedelta

from config import SolverSettings
from database.repository import hold_for_picked_days, limit_new_bookings_to_day
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.types import ScheduledAppointment
from tests.conftest import req

MONDAY = date(2026, 9, 7)


def pick_days(problem, patient, days):
    """Scores the patient page saves for picked days with no preferred time."""
    for day in days:
        for slot in range(10):
            problem.patient_preferences[(patient, day, slot)] = 300


def booked(day):
    return ScheduledAppointment(1, 1, 1, 1, 1, MONDAY + timedelta(days=day), 0, f"B-{day}")


def test_picked_days_beat_even_spacing_in_weekly_planning(base_problem):
    problem = base_problem([req(1, 1, 1, 1, frequency=3)])
    pick_days(problem, 1, [0, 1, 2])

    result = generate_weekly_schedule(problem)

    assert not any(result.validation.values())
    assert sorted(item.date.weekday() for item in result.appointments) == [0, 1, 2]


def test_child_is_held_for_picked_days_that_can_still_hold_the_week(base_problem):
    problem = base_problem([req(1, 1, 1, 1, frequency=3)])
    pick_days(problem, 1, [0, 2, 4])
    problem.pinned = [booked(0)]
    tuesday = MONDAY + timedelta(days=1)

    assert hold_for_picked_days(problem, tuesday) == {1}
    assert problem.requirements[0].sessions_per_week == 1  # only the saved Monday visit counts

    problem = base_problem([req(1, 1, 1, 1, frequency=3)])
    pick_days(problem, 1, [0, 2, 4])
    problem.pinned = [booked(0)]
    limit_new_bookings_to_day(problem, tuesday)
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=2))
    assert [item for item in result.appointments if item.date == tuesday] == []
    assert result.unmet == []


def test_unpicked_visits_and_a_full_picked_day_are_not_held(base_problem):
    # "Up to": 3 visits, 2 picked (Wed, Fri) -> the third visit may go on Monday.
    problem = base_problem([req(1, 1, 1, 1, frequency=3)])
    pick_days(problem, 1, [2, 4])
    assert hold_for_picked_days(problem, MONDAY) == set()

    # Wednesday was full: on Thursday only Friday is left for 2 visits, so book now.
    problem = base_problem([req(1, 1, 1, 1, frequency=3)])
    pick_days(problem, 1, [0, 2, 4])
    problem.pinned = [booked(0)]
    assert hold_for_picked_days(problem, MONDAY + timedelta(days=3)) == set()

    # A picked day itself is never held, and a plain time preference (no day bonus) never holds.
    problem = base_problem([req(1, 1, 1, 1, frequency=1)])
    pick_days(problem, 1, [2])
    assert hold_for_picked_days(problem, MONDAY + timedelta(days=2)) == set()
    problem = base_problem([req(1, 1, 1, 1, frequency=1)])
    problem.patient_preferences[(1, 4, 0)] = 100
    assert hold_for_picked_days(problem, MONDAY) == set()


def test_daily_planner_books_the_child_on_the_picked_day(base_problem):
    problem = base_problem([req(1, 1, 1, 1, frequency=1)])
    pick_days(problem, 1, [3])
    thursday = MONDAY + timedelta(days=3)
    limit_new_bookings_to_day(problem, thursday)

    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=2))

    assert [item.date for item in result.appointments] == [thursday]

"""Bounded daily search must not hide free times through candidate thinning."""

from collections import Counter
from datetime import date

import pytest

from config import SolverSettings
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.cp_sat_scheduler import _candidate_placements
from scheduling.bundle_builder import build_bundles
from scheduling.slot_config import SLOTS
from scheduling.types import RequirementInput, SchedulingProblem


def test_unbounded_weekly_search_keeps_every_valid_start(base_problem):
    problem = base_problem([RequirementInput(1, 1, 1, 1, 1, 1, 0)])
    bundles = build_bundles(problem.requirements)
    candidates, _ = _candidate_placements(problem, bundles)
    assert {(day, slot) for day, slot, _ in candidates[bundles[0].id]} == {
        (day, slot.slot_index) for day in range(6) for slot in SLOTS
    }


def test_weekly_search_can_place_three_patients_when_old_two_start_shortcut_collided():
    ids = (1, 11, 21)
    problem = SchedulingProblem(week_start=date(2026, 10, 19),
        requirements=[RequirementInput(i, i, 1, 1, 1, 1, 0) for i in ids],
        therapist_availability={(1, 0, slot.slot_index) for slot in SLOTS},
        patient_availability={(patient, 0, slot.slot_index) for patient in ids for slot in SLOTS},
        active_therapists={1}, location_capacities={1: 1})
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=20))
    assert result.status in {"FEASIBLE", "OPTIMAL"}
    assert len(result.appointments) == 3 and not result.unmet
    assert len({item.slot_index for item in result.appointments}) == 3
    assert not any(result.validation.values())


@pytest.mark.parametrize("seconds,blocked,expected", [(5, False, 100), (5, True, 99), (21, False, 100)])
def test_large_daily_schedule_uses_all_valid_slots_and_respects_absences(seconds, blocked, expected):
    target = date(2026, 10, 23)
    requirements = [RequirementInput(
        id=i+1, patient_id=i+1, department_id=1, therapist_id=i % 10 + 1,
        location_id=1 if i % 10 < 5 else 2, sessions_per_week=2, sequence_order=0,
    ) for i in range(100)]
    problem = SchedulingProblem(
        week_start=date(2026, 10, 19), requirements=requirements,
        therapist_availability={(doctor, day, slot.slot_index)
                                for doctor in range(1, 11) for day in range(6) for slot in SLOTS},
        patient_availability={(patient, day, slot.slot_index)
                              for patient in range(1, 101) for day in range(6) for slot in SLOTS},
        active_therapists=set(range(1, 11)), location_capacities={1: 5, 2: 5},
        planning_start_date=target,
        absences={(1, target, 0)} if blocked else set(),
        new_booking_slots={(r.therapist_id, r.department_id, r.location_id, target, slot.slot_index)
                           for r in requirements for slot in SLOTS},
    )
    result = generate_weekly_schedule(problem, settings=SolverSettings(max_time_seconds=seconds))
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert not any(result.validation.values())
    assert len(result.appointments) == expected
    assert len({a.patient_id for a in result.appointments}) == expected
    assert {a.date for a in result.appointments} == {target}
    assert max(Counter((a.therapist_id, a.slot_index) for a in result.appointments).values()) == 1
    assert max(Counter((a.location_id, a.slot_index) for a in result.appointments).values()) <= 5
    assert not blocked or not any(a.therapist_id == 1 and a.slot_index == 0 for a in result.appointments)

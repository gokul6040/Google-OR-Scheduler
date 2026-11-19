from datetime import timedelta

from scheduling.slot_config import WORKING_DAYS, timetable_for
from scheduling.types import SchedulingProblem, TherapyBundle


def bundle_sessions(problem: SchedulingProblem, bundle: TherapyBundle, start_slot: int) -> tuple[int, ...] | None:
    """The back-to-back sessions a visit starting at ``start_slot`` would use, or None."""
    return timetable_for(problem, bundle.items[0].location_id).block(start_slot, len(bundle.items))


def placement_rejection_reason(
    problem: SchedulingProblem,
    bundle: TherapyBundle,
    day: int,
    start_slot: int,
    therapist_ids: tuple[int, ...] | None = None,
    check_therapists: bool = True,
) -> str | None:
    target_date = problem.week_start + timedelta(days=day)
    weekday = target_date.weekday()
    if day < 0 or day >= problem.horizon_days or weekday not in WORKING_DAYS:
        return "NON_WORKING_DAY"
    if problem.planning_start_date and target_date < problem.planning_start_date:
        return "PAST_PLANNING_DATE"
    locations = {item.location_id for item in bundle.items}
    if len(locations) > 1:
        return "LOCATION_TRANSITION_IMPOSSIBLE"
    sessions = bundle_sessions(problem, bundle, start_slot)
    if sessions is None:
        return "CONSECUTIVE_BLOCK_UNAVAILABLE"
    if therapist_ids is None:
        therapist_ids = tuple(item.therapist_id for item in bundle.items)
    for offset, item in enumerate(bundle.items):
        slot = sessions[offset]
        if item.plan_start_date and target_date < item.plan_start_date:
            return "PLAN_NOT_STARTED"
        if item.plan_end_date and target_date > item.plan_end_date:
            return "PLAN_ENDED"
        if (bundle.patient_id, weekday, slot) not in problem.patient_availability:
            return "PATIENT_AVAILABILITY"
        if (bundle.patient_id, target_date, slot) in problem.patient_absences:
            return "PATIENT_ABSENCE"
        if problem.new_booking_slots is not None and (
            therapist_ids[offset], item.department_id, item.location_id, target_date, slot
        ) not in problem.new_booking_slots:
            return "OUTSIDE_REPAIR_OPENING"
        if check_therapists:
            therapist_id = therapist_ids[offset]
            if therapist_id not in problem.active_therapists:
                return "NO_VALID_ASSIGNED_THERAPIST"
            if (therapist_id, weekday, slot) not in problem.therapist_availability:
                return "THERAPIST_CAPACITY"
            if (therapist_id, target_date, slot) in problem.absences:
                return "THERAPIST_ABSENCE"
        if problem.location_capacities.get(item.location_id, 0) < 1:
            return "LOCATION_CAPACITY"
    return None

from collections import Counter, defaultdict

from scheduling.slot_config import known_slot, overbooked_moments, timetable_for
from scheduling.types import ScheduledAppointment, SchedulingProblem, ValidationIssue


REPORT_KEYS = (
    "wrong_therapist",
    "therapist_overlaps",
    "patient_overlaps",
    "absence_conflicts",
    "patient_absence_conflicts",
    "location_capacity_violations",
    "patient_location_transitions",
    "invalid_slot_durations",
    "broken_consecutive_bundles",
    "therapist_availability_conflicts",
    "patient_availability_conflicts",
)


def validate_schedule(problem: SchedulingProblem, appointments: list[ScheduledAppointment]):
    issues: list[ValidationIssue] = []
    requirement_map = {item.id: item for item in problem.requirements}
    # Bookings per clinician/child/room and day, compared by clock time (9:00-9:40 clashes with 9:20-10:00).
    therapist_slots: dict[tuple, Counter] = defaultdict(Counter)
    patient_slots: dict[tuple, Counter] = defaultdict(Counter)
    location_slots: dict[tuple, Counter] = defaultdict(Counter)
    for a in appointments:
        therapist_slots[(a.therapist_id, a.date)][a.slot_index] += 1
        patient_slots[(a.patient_id, a.date)][a.slot_index] += 1
        location_slots[(a.location_id, a.date)][a.slot_index] += 1
    patient_day_locations: dict[tuple[int, object], set[int]] = defaultdict(set)
    pinned = set(problem.pinned)

    for appointment in appointments:
        accepted = set(appointment.overridden_warnings) if appointment in pinned else set()
        requirement = requirement_map.get(appointment.requirement_id)
        mismatched_assignment = requirement is None or (
            appointment.therapist_id != requirement.therapist_id
            or appointment.department_id != requirement.department_id
        )
        saved_off_plan = appointment in pinned and (
            not appointment.counts_toward_requirement or appointment.assignment_override
        )
        if mismatched_assignment and not saved_off_plan:
            issues.append(ValidationIssue("wrong_therapist", "Assigned therapist/department mismatch."))
        if not known_slot(appointment.slot_index):
            issues.append(ValidationIssue("invalid_slot_durations", "Unknown slot index."))
        completed_history = appointment in pinned and appointment.status == "COMPLETED"
        if not completed_history and "THERAPIST_ABSENCE" not in accepted and (appointment.therapist_id, appointment.date, appointment.slot_index) in problem.absences:
            issues.append(ValidationIssue("absence_conflicts", "Therapist is absent."))
        if "PATIENT_ABSENCE" not in accepted and (appointment.patient_id, appointment.date, appointment.slot_index) in problem.patient_absences:
            issues.append(ValidationIssue("patient_absence_conflicts", "Patient is absent."))
        weekday = appointment.date.weekday()
        if "THERAPIST_UNAVAILABLE" not in accepted and (appointment.therapist_id, weekday, appointment.slot_index) not in problem.therapist_availability:
            issues.append(ValidationIssue(
                "therapist_availability_conflicts", "Therapist is unavailable in the requested slot."
            ))
        if "PATIENT_UNAVAILABLE" not in accepted and (appointment.patient_id, weekday, appointment.slot_index) not in problem.patient_availability:
            issues.append(ValidationIssue(
                "patient_availability_conflicts", "Patient is unavailable in the requested slot."
            ))
        patient_day_locations[(appointment.patient_id, appointment.date)].add(appointment.location_id)

    for booked in therapist_slots.values():
        for _ in range(overbooked_moments(booked, 1)):
            issues.append(ValidationIssue("therapist_overlaps", "Therapist is double-booked."))
    for booked in patient_slots.values():
        for _ in range(overbooked_moments(booked, 1)):
            issues.append(ValidationIssue("patient_overlaps", "Patient is double-booked."))
    for (location_id, _), booked in location_slots.items():
        for _ in range(overbooked_moments(booked, problem.location_capacities.get(location_id, 0))):
            issues.append(ValidationIssue("location_capacity_violations", "Location capacity exceeded."))
    for locations in patient_day_locations.values():
        if len(locations) > 1:
            issues.append(ValidationIssue(
                "patient_location_transitions", "Patient is scheduled at multiple locations in one day."
            ))

    by_bundle: dict[str, list[ScheduledAppointment]] = defaultdict(list)
    for appointment in appointments:
        if appointment.bundle_id:
            by_bundle[appointment.bundle_id].append(appointment)
    for bundle_appointments in by_bundle.values():
        dates = {item.date for item in bundle_appointments}
        locations = {item.location_id for item in bundle_appointments}
        patients = {item.patient_id for item in bundle_appointments}
        if len(dates) != 1 or len(locations) != 1:
            issues.append(ValidationIssue("broken_consecutive_bundles", "Bundle is not consecutive."))
            continue
        # Back-to-back is judged on the centre's session order (clock time), not on numbers.
        timetable = timetable_for(problem, next(iter(locations)))
        slots = [item.slot_index for item in bundle_appointments]
        if len(set(slots)) != len(slots) or any(slot not in timetable.order for slot in slots):
            issues.append(ValidationIssue("broken_consecutive_bundles", "Bundle is not consecutive."))
            continue
        slots.sort(key=timetable.order.index)
        if len(patients) == 1 and pinned.issuperset(bundle_appointments):
            # An explicitly released session can leave a gap in a saved visit.
            # Use it only to validate the original sequence, never as capacity or demand.
            first = bundle_appointments[0]
            visit = (first.patient_id, first.date, first.location_id, first.bundle_id)
            slots = sorted(set(slots) | {slot for slot in timetable.gap_sessions(slots[0], slots[-1])
                                         if (*visit, slot) in problem.released_bundle_slots},
                           key=timetable.order.index)
        if timetable.block(slots[0], len(slots)) != tuple(slots):
            issues.append(ValidationIssue("broken_consecutive_bundles", "Bundle is not consecutive."))

    report = {key: sum(issue.code == key for issue in issues) for key in REPORT_KEYS}
    return issues, report


def validate_move(
    problem: SchedulingProblem,
    existing: list[ScheduledAppointment],
    appointment: ScheduledAppointment,
    replacement: ScheduledAppointment,
) -> list[ValidationIssue]:
    if appointment.bundle_id and appointment.bundle_id != replacement.bundle_id:
        return [ValidationIssue("broken_consecutive_bundles", "Move the entire bundle together.")]
    candidate = [item for item in existing if item != appointment] + [replacement]
    issues, _ = validate_schedule(problem, candidate)
    return issues

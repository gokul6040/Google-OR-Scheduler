from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timedelta
from math import ceil
from fractions import Fraction
from time import monotonic

from ortools.sat.python import cp_model

from config import SOLVER_SETTINGS, SolverSettings
from scheduling.bundle_builder import build_bundles
from scheduling.rules import bundle_sessions, placement_rejection_reason
from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import WORKING_DAYS, peak_booked, slot_window, timetable_for
from scheduling.types import (
    ScheduleResult,
    ScheduledAppointment,
    SchedulingProblem,
    SolverMode,
    TherapyBundle,
    UnmetRequirement,
)


REASON_PRIORITY = (
    "PAST_PLANNING_DATE",
    "NO_VALID_ASSIGNED_THERAPIST",
    "PLAN_NOT_STARTED",
    "PLAN_ENDED",
    "THERAPIST_ABSENCE",
    "PATIENT_ABSENCE",
    "PATIENT_AVAILABILITY",
    "LOCATION_TRANSITION_IMPOSSIBLE",
    "CONSECUTIVE_BLOCK_UNAVAILABLE",
    "LOCATION_CAPACITY",
    "THERAPIST_CAPACITY",
    "PATIENT_DAILY_VISIT_LIMIT",
    "SOLVER_SEARCH_LIMIT",
)

def _candidate_placements(problem, bundles):
    candidates = defaultdict(list)
    rejected = defaultdict(Counter)
    for bundle in bundles:
        period_start = bundle.period_index * 7
        period_end = min(problem.horizon_days, period_start + 7)
        for day in range(period_start, period_end):
            for start_slot in timetable_for(problem, bundle.items[0].location_id).order:
                reason = placement_rejection_reason(
                    problem, bundle, day, start_slot, check_therapists=False,
                )
                if reason:
                    rejected[bundle.id][reason] += 1
                    continue
                sessions = bundle_sessions(problem, bundle, start_slot)

                target_date = problem.week_start + timedelta(days=day)
                weekday = target_date.weekday()
                therapist_options = []
                option_failure = None
                for offset, item in enumerate(bundle.items):
                    slot = sessions[offset]
                    therapist_id = item.therapist_id
                    if therapist_id not in problem.active_therapists:
                        option_failure = "NO_VALID_ASSIGNED_THERAPIST"
                        break
                    if (therapist_id, target_date, slot) in problem.absences:
                        option_failure = "THERAPIST_ABSENCE"
                        break
                    if (therapist_id, weekday, slot) not in problem.therapist_availability:
                        option_failure = "THERAPIST_CAPACITY"
                        break
                    therapist_options.append((therapist_id,))

                if option_failure:
                    rejected[bundle.id][option_failure] += 1
                    continue
                preferred = tuple(options[0] for options in therapist_options)
                candidates[bundle.id].append((day, start_slot, preferred))
    return candidates, rejected


def _ranked_reasons(counter: Counter) -> tuple[str, ...]:
    """Return the main unmet reason followed by useful contributing reasons."""
    ranked = [reason for reason in REASON_PRIORITY if counter.get(reason)]
    ranked.extend(
        sorted(
            reason
            for reason, count in counter.items()
            if count and reason not in REASON_PRIORITY and reason != "NON_WORKING_DAY"
        )
    )
    return tuple(ranked) or ("THERAPIST_CAPACITY",)


class _TimeUsage:
    """Bookings per (clinician or room, day, session), read back by clock time.

    ``usage[(who, day, slot)]`` is the most bookings at any moment of that session, so a
    kept 9:00-9:40 booking makes a 9:20-10:00 session busy too.
    """

    def __init__(self) -> None:
        self._booked: dict[tuple, Counter] = defaultdict(Counter)

    def add(self, key: tuple) -> None:
        who, day, slot = key
        self._booked[(who, day)][slot] += 1

    def __getitem__(self, key: tuple) -> int:
        who, day, slot = key
        booked = self._booked.get((who, day))
        return peak_booked(booked, slot) if booked else 0


def _add_time_limits(model, usage: dict, pinned: dict, capacity) -> None:
    """Cap bookings at every moment of the day (clock time, not session numbers).

    ``usage`` maps (who, day) -> {session: [variables]}; ``pinned`` maps (who, day) ->
    Counter of saved sessions. Without overlapping sessions this is exactly one
    ``new + saved <= capacity`` limit per used session, as before.
    """
    for (who, day), by_slot in usage.items():
        saved = pinned.get((who, day), Counter())
        windows = {slot: slot_window(slot) for slot in set(by_slot) | set(saved)}
        moments = {window[0] for window in windows.values() if window}
        groups = [[slot for slot, window in windows.items() if window and window[0] <= moment < window[1]]
                  for moment in sorted(moments)]
        groups += [[slot] for slot, window in windows.items() if window is None]
        for slots in groups:
            variables = [variable for slot in slots for variable in by_slot.get(slot, ())]
            if variables:
                model.Add(sum(variables) + sum(saved[slot] for slot in slots) <= capacity(who))


def _selected_bundle_blockers(
    problem: SchedulingProblem,
    bundle: TherapyBundle,
    candidates: list[tuple[int, int, tuple[int, ...]]],
    therapist_usage: _TimeUsage,
    location_usage: _TimeUsage,
    patient_days: set[tuple[int, int]],
) -> Counter:
    """Explain which final-schedule constraints block a bundle's valid options."""
    blockers = Counter()
    for day, start, therapist_ids in candidates:
        candidate_blockers = set()
        sessions = bundle_sessions(problem, bundle, start)
        if (bundle.patient_id, day) in patient_days:
            candidate_blockers.add("PATIENT_DAILY_VISIT_LIMIT")
        for offset, item in enumerate(bundle.items):
            slot = sessions[offset]
            if therapist_usage[(therapist_ids[offset], day, slot)]:
                candidate_blockers.add("THERAPIST_CAPACITY")
            if location_usage[(item.location_id, day, slot)] >= problem.location_capacities.get(
                item.location_id, 0
            ):
                candidate_blockers.add("LOCATION_CAPACITY")
        if not candidate_blockers:
            candidate_blockers.add("SOLVER_SEARCH_LIMIT")
        blockers.update(candidate_blockers)
    return blockers


def _add_cadence_preferences(
    problem: SchedulingProblem,
    bundles: list[TherapyBundle],
    candidates: dict[str, list[tuple[int, int, tuple[int, ...]]]],
    variables: dict[tuple[str, int, int, tuple[int, ...]], cp_model.IntVar],
    model: cp_model.CpModel,
    objective: list,
    settings: SolverSettings,
) -> None:
    """Prefer evenly paced visits without sacrificing otherwise valid therapy."""
    frequencies = defaultdict(int)
    for requirement in problem.requirements:
        key = (requirement.patient_id, requirement.location_id)
        frequencies[key] = max(frequencies[key], requirement.sessions_per_week)

    grouped_bundles = defaultdict(list)
    selected = {}
    scheduled_day = {}
    for bundle in bundles:
        location_id = bundle.items[0].location_id
        grouped_bundles[(bundle.patient_id, location_id)].append(bundle)

        placement_vars = [
            variables[(bundle.id, day, start, therapist_ids)]
            for day, start, therapist_ids in candidates[bundle.id]
        ]
        is_selected = model.NewBoolVar(f"selected_{bundle.id}")
        model.Add(is_selected == sum(placement_vars))
        selected[bundle.id] = is_selected

        period_start = bundle.period_index * 7
        period_end = min(problem.horizon_days, period_start + 7)
        day_var = model.NewIntVar(
            period_start,
            max(period_start, period_end - 1),
            f"day_{bundle.id}",
        )
        # An unscheduled bundle uses the start of its period as a harmless sentinel.
        model.Add(
            day_var
            == period_start
            + sum(
                (day - period_start)
                * variables[(bundle.id, day, start, therapist_ids)]
                for day, start, therapist_ids in candidates[bundle.id]
            )
        )
        scheduled_day[bundle.id] = day_var

    for key, group in grouped_bundles.items():
        frequency = frequencies[key]
        if frequency <= 1:
            continue
        preferred_gap = max(1, 7 // frequency)
        ordered = sorted(
            group,
            key=lambda bundle: (bundle.period_index, bundle.occurrence_index),
        )
        for first, second in zip(ordered, ordered[1:]):
            # Do not connect non-adjacent seven-day periods when the horizon ends
            # with a partial period.
            if second.period_index - first.period_index > 1:
                continue
            target_gap = (
                (second.period_index - first.period_index) * 7
                + (second.occurrence_index - first.occurrence_index) * preferred_gap
            )
            if target_gap <= 0:
                continue

            both_selected = model.NewBoolVar(
                f"cadence_pair_{first.id}_{second.id}"
            )
            model.Add(both_selected <= selected[first.id])
            model.Add(both_selected <= selected[second.id])
            model.Add(
                both_selected >= selected[first.id] + selected[second.id] - 1
            )

            deviation = model.NewIntVar(
                0,
                problem.horizon_days + 7,
                f"cadence_deviation_{first.id}_{second.id}",
            )
            actual_gap = scheduled_day[second.id] - scheduled_day[first.id]
            model.Add(deviation >= actual_gap - target_gap).OnlyEnforceIf(
                both_selected
            )
            model.Add(deviation >= target_gap - actual_gap).OnlyEnforceIf(
                both_selected
            )
            model.Add(deviation == 0).OnlyEnforceIf(both_selected.Not())
            objective.append(-settings.cadence_deviation_penalty * deviation)


def location_partitions(problem: SchedulingProblem) -> list[SchedulingProblem] | None:
    """Split independent centres; shared patients or therapists join components."""
    patient_locations, therapist_locations = defaultdict(set), defaultdict(set)
    locations = set()
    for item in [*problem.requirements, *problem.pinned]:
        patient_locations[item.patient_id].add(item.location_id)
        therapist_locations[item.therapist_id].add(item.location_id)
        locations.add(item.location_id)
    parents = {location: location for location in locations}

    def root(location):
        while parents[location] != location:
            parents[location] = parents[parents[location]]
            location = parents[location]
        return location

    for shared in [*patient_locations.values(), *therapist_locations.values()]:
        ordered = sorted(shared)
        for location in ordered[1:]:
            parents[root(location)] = root(ordered[0])
    components = defaultdict(set)
    for location in sorted(locations):
        components[root(location)].add(location)
    if len(components) < 2:
        return None
    partitions = []
    for group in components.values():
        requirements = [r for r in problem.requirements if r.location_id in group]
        requirement_ids = {r.id for r in requirements}
        partitions.append(replace(
            problem, requirements=requirements,
            location_capacities={location: problem.location_capacities.get(location, 0) for location in group},
            eligible_therapists={r: problem.eligible_therapists.get(r, ()) for r in requirement_ids},
            pinned=[a for a in problem.pinned if a.location_id in group],
        ))
    return partitions


def _combined_partition_result(
    problem: SchedulingProblem,
    results: list[ScheduleResult],
    started: float,
) -> ScheduleResult:
    """Merge independent branch results and validate them against the full week."""
    appointments = [item for result in results for item in result.appointments]
    unmet = [item for result in results for item in result.unmet]
    blocked = [location for result in results for location in result.statistics.get("blocked_locations", [])]
    healthy = [item for item in appointments if item.location_id not in blocked]
    issues, validation = validate_schedule(problem, healthy)
    statuses = {result.status for result in results}
    if issues:
        status = "INVALID_SOLUTION"
    elif statuses == {"OPTIMAL"}:
        status = "OPTIMAL"
    elif statuses.issubset({"OPTIMAL", "FEASIBLE"}):
        status = "FEASIBLE"
    else:
        status = next(result.status for result in results if result.status not in {"OPTIMAL", "FEASIBLE"})
    return ScheduleResult(
        status=status,
        appointments=appointments,
        unmet=unmet,
        requested_sessions=sum(result.requested_sessions for result in results),
        scheduled_sessions=len(appointments),
        validation=validation,
        statistics={
            "variables": sum(int(result.statistics.get("variables", 0)) for result in results),
            "branches": sum(int(result.statistics.get("branches", 0)) for result in results),
            "conflicts": sum(int(result.statistics.get("conflicts", 0)) for result in results),
            "partitions": len(results),
            "strategy": "cp_sat",
            "centres": [{"locations": sorted(part.location_capacities), "status": result.status,
                         "search_limit_seconds": result.statistics.get("search_limit_seconds"),
                         "wall_time_seconds": result.statistics.get("wall_time_seconds")}
                        for part, result in zip(location_partitions(problem), results)],
            "blocked_locations": blocked,
            "wall_time_seconds": monotonic() - started,
        },
    )


def _week_bundles(problem: SchedulingProblem) -> tuple[list[TherapyBundle], int, int]:
    """Return outstanding weekly bundles, requested sessions, and period count."""
    period_count = max(1, ceil(problem.horizon_days / 7))
    pinned_counts = Counter()
    for item in problem.pinned:
        day_offset = (item.date - problem.week_start).days
        if item.counts_toward_requirement and 0 <= day_offset < problem.horizon_days:
            pinned_counts[(item.requirement_id, day_offset // 7)] += 1
    bundles = []
    used_bundle_ids = {item.bundle_id for item in problem.pinned}
    for period_index in range(period_count):
        remaining_requirements = [
            replace(
                item,
                sessions_per_week=max(
                    0,
                    item.sessions_per_week - pinned_counts[(item.id, period_index)],
                ),
            )
            for item in problem.requirements
        ]
        for bundle in build_bundles(remaining_requirements, period_index):
            identifier = bundle.id
            suffix = 1
            while identifier in used_bundle_ids:
                identifier = f"{bundle.id}-N{suffix}"
                suffix += 1
            used_bundle_ids.add(identifier)
            bundles.append(replace(bundle, id=identifier))
    requested = sum(item.sessions_per_week for item in problem.requirements) * period_count
    return bundles, requested, period_count


def _greedy_appointments(
    problem: SchedulingProblem,
    bundles: list[TherapyBundle],
    candidates: dict[str, list[tuple[int, int, tuple[int, ...]]]],
) -> tuple[list[ScheduledAppointment], set[str]]:
    """Build a fast, rule-safe best-effort schedule for the quick preview."""
    appointments = list(problem.pinned)
    selected_bundles: set[str] = set()
    therapist_usage = _TimeUsage()
    location_usage = _TimeUsage()
    patient_days = set()
    for item in appointments:
        day = (item.date - problem.week_start).days
        if 0 <= day < problem.horizon_days:
            therapist_usage.add((item.therapist_id, day, item.slot_index))
            location_usage.add((item.location_id, day, item.slot_index))
            patient_days.add((item.patient_id, day))

    frequency_by_patient_location = defaultdict(int)
    for bundle in bundles:
        frequency_by_patient_location[(bundle.patient_id, bundle.items[0].location_id)] = max(
            frequency_by_patient_location[(bundle.patient_id, bundle.items[0].location_id)],
            bundle.occurrence_index + 1,
        )
    booked_days = defaultdict(set)
    targets = defaultdict(int)
    for item in problem.pinned:
        if item.counts_toward_requirement:
            booked_days[item.patient_id].add(item.date)
    for item in problem.requirements:
        targets[item.patient_id] = max(targets[item.patient_id], item.sessions_per_week)
    for bundle in sorted(
        bundles,
        key=lambda item: (
            not any(part.requirement_id in problem.priority_requirement_ids for part in bundle.items),
            Fraction(len(booked_days[item.patient_id]) + item.occurrence_index, max(1, targets[item.patient_id])),
            len(candidates[item.id]),
            item.occurrence_index,
            item.patient_id,
        ),
    ):
        frequency = frequency_by_patient_location[(bundle.patient_id, bundle.items[0].location_id)]
        target_day = min(problem.horizon_days - 1, bundle.occurrence_index * max(1, 6 // frequency))
        session_order = timetable_for(problem, bundle.items[0].location_id).order
        ordered_candidates = sorted(
            candidates[bundle.id],
            # Earliest session first, by clock time (session numbers are identities, not order).
            key=lambda item: (abs(item[0] - target_day), item[0], session_order.index(item[1]), item[2]),
        )
        for day, start, therapist_ids in ordered_candidates:
            if (bundle.patient_id, day) in patient_days:
                continue
            sessions = bundle_sessions(problem, bundle, start)
            if any(
                therapist_usage[(therapist_ids[offset], day, sessions[offset])]
                or location_usage[(item.location_id, day, sessions[offset])] >= problem.location_capacities.get(item.location_id, 0)
                for offset, item in enumerate(bundle.items)
            ):
                continue
            target_date = problem.week_start + timedelta(days=day)
            for offset, item in enumerate(bundle.items):
                slot = sessions[offset]
                appointments.append(ScheduledAppointment(
                    patient_id=bundle.patient_id,
                    requirement_id=item.requirement_id,
                    therapist_id=therapist_ids[offset],
                    department_id=item.department_id,
                    location_id=item.location_id,
                    date=target_date,
                    slot_index=slot,
                    bundle_id=bundle.id,
                ))
                therapist_usage.add((therapist_ids[offset], day, slot))
                location_usage.add((item.location_id, day, slot))
            patient_days.add((bundle.patient_id, day))
            selected_bundles.add(bundle.id)
            break
    return appointments, selected_bundles


def _greedy_result(problem: SchedulingProblem, started: float) -> ScheduleResult:
    """Return a saveable, validated quick-preview schedule without CP-SAT."""
    bundles, requested, period_count = _week_bundles(problem)
    candidates, rejected = _candidate_placements(problem, bundles)
    appointments, selected_bundles = _greedy_appointments(problem, bundles, candidates)
    scheduled_by_requirement = Counter(item.requirement_id for item in appointments if item.counts_toward_requirement)
    therapist_usage = _TimeUsage()
    location_usage = _TimeUsage()
    patient_days = set()
    for appointment in appointments:
        day = (appointment.date - problem.week_start).days
        if 0 <= day < problem.horizon_days:
            therapist_usage.add((appointment.therapist_id, day, appointment.slot_index))
            location_usage.add((appointment.location_id, day, appointment.slot_index))
            patient_days.add((appointment.patient_id, day))
    missing_reasons = defaultdict(Counter)
    for bundle in bundles:
        if bundle.id in selected_bundles:
            continue
        blockers = _selected_bundle_blockers(
            problem, bundle, candidates[bundle.id], therapist_usage, location_usage, patient_days,
        ) if candidates[bundle.id] else rejected[bundle.id]
        for item in bundle.items:
            missing_reasons[(bundle.patient_id, item.requirement_id)].update(blockers)
    unmet = []
    for requirement in problem.requirements:
        missing = max(0, requirement.sessions_per_week * period_count - scheduled_by_requirement[requirement.id])
        if missing:
            ranked = _ranked_reasons(missing_reasons[(requirement.patient_id, requirement.id)])
            unmet.append(UnmetRequirement(
                requirement.patient_id, requirement.id, requirement.department_id,
                missing, ranked[0], ranked[1:],
            ))
    issues, validation = validate_schedule(problem, appointments)
    return ScheduleResult(
        status="INVALID_SOLUTION" if issues else "FEASIBLE",
        appointments=appointments,
        unmet=unmet,
        requested_sessions=requested,
        scheduled_sessions=len(appointments),
        validation=validation,
        statistics={
            "variables": sum(len(values) for values in candidates.values()),
            "strategy": "quick_greedy",
            "wall_time_seconds": monotonic() - started,
        },
    )


def generate_weekly_schedule(
    problem: SchedulingProblem,
    mode: SolverMode = SolverMode.BEST_EFFORT,
    settings: SolverSettings = SOLVER_SETTINGS,
    _allow_partitioning: bool = True,
) -> ScheduleResult:
    started = monotonic()
    partitions = location_partitions(problem) if _allow_partitioning else None
    pinned_issues, pinned_report = validate_schedule(problem, problem.pinned)
    if pinned_issues and not partitions and problem.isolate_saved_failures:
        # Preserve the saved records, but generate nothing in this unsafe component.
        # Other independent branches can continue and staff get an explicit warning.
        return ScheduleResult(
            status="FEASIBLE" if mode == SolverMode.BEST_EFFORT else "INVALID_SOLUTION",
            appointments=list(problem.pinned),
            unmet=[UnmetRequirement(r.patient_id, r.id, r.department_id, r.sessions_per_week,
                                    "SAVED_BOOKING_NEEDS_REVIEW") for r in problem.requirements if r.sessions_per_week],
            requested_sessions=sum(r.sessions_per_week for r in problem.requirements),
            scheduled_sessions=len(problem.pinned),
            validation={key: 0 for key in pinned_report},
            statistics={"blocked_locations": sorted(problem.location_capacities),
                        "saved_validation": pinned_report, "wall_time_seconds": monotonic() - started},
        )
    if partitions:
        results = []
        for partition in partitions:
            # Each independent resource group gets its FULL budget. CP-SAT
            # returns early when optimal/infeasible; no compulsory five-minute wait.
            results.append(generate_weekly_schedule(
                partition,
                mode=mode,
                settings=settings,
                _allow_partitioning=False,
            ))
        return _combined_partition_result(problem, results, started)

    bundles, requested, period_count = _week_bundles(problem)
    candidates, rejected = _candidate_placements(problem, bundles)
    model = cp_model.CpModel()
    variables = {}

    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            key = (bundle.id, day, start, therapist_ids)
            therapist_label = "_".join(str(item_id) for item_id in therapist_ids)
            variables[key] = model.NewBoolVar(
                f"x_{bundle.id}_{day}_{start}_{therapist_label}"
            )

    for bundle in bundles:
        bundle_vars = [
            variables[(bundle.id, day, start, therapist_ids)]
            for day, start, therapist_ids in candidates[bundle.id]
        ]
        if mode == SolverMode.STRICT:
            model.Add(sum(bundle_vars) == 1)
        else:
            model.Add(sum(bundle_vars) <= 1)

    # Frequency occurrences are daily bundles: a patient can receive at most one bundle per day.
    patient_day_bundles = defaultdict(list)
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            patient_day_bundles[(bundle.patient_id, day)].append(
                variables[(bundle.id, day, start, therapist_ids)]
            )
    pinned_patient_days = {
        (item.patient_id, (item.date - problem.week_start).days)
        for item in problem.pinned
        if 0 <= (item.date - problem.week_start).days < problem.horizon_days
    }
    for key, usage in patient_day_bundles.items():
        model.Add(sum(usage) <= (0 if key in pinned_patient_days else 1))

    # (who, day) -> {session: [variables]}; limits are applied by clock time below.
    therapist_usage = defaultdict(lambda: defaultdict(list))
    patient_usage = defaultdict(lambda: defaultdict(list))
    location_usage = defaultdict(lambda: defaultdict(list))
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            variable = variables[(bundle.id, day, start, therapist_ids)]
            sessions = bundle_sessions(problem, bundle, start)
            for offset, item in enumerate(bundle.items):
                slot = sessions[offset]
                therapist_usage[(therapist_ids[offset], day)][slot].append(variable)
                patient_usage[(bundle.patient_id, day)][slot].append(variable)
                location_usage[(item.location_id, day)][slot].append(variable)

    pinned_therapist = defaultdict(Counter)
    pinned_patient = defaultdict(Counter)
    pinned_location = defaultdict(Counter)
    for item in problem.pinned:
        day = (item.date - problem.week_start).days
        if 0 <= day < problem.horizon_days and item.date.weekday() in WORKING_DAYS:
            pinned_therapist[(item.therapist_id, day)][item.slot_index] += 1
            pinned_patient[(item.patient_id, day)][item.slot_index] += 1
            pinned_location[(item.location_id, day)][item.slot_index] += 1

    _add_time_limits(model, therapist_usage, pinned_therapist, lambda _who: 1)
    _add_time_limits(model, patient_usage, pinned_patient, lambda _who: 1)
    _add_time_limits(model, location_usage, pinned_location,
                     lambda location_id: problem.location_capacities.get(location_id, 0))

    objective = []
    booked_days = defaultdict(set)
    targets = defaultdict(int)
    for item in problem.pinned:
        if item.counts_toward_requirement:
            booked_days[item.patient_id].add(item.date)
    for item in problem.requirements:
        targets[item.patient_id] = max(targets[item.patient_id], item.sessions_per_week)
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            variable = variables[(bundle.id, day, start, therapist_ids)]
            weekday = (problem.week_start + timedelta(days=day)).weekday()
            # A preferred time is when the visit STARTS; the visit's other therapies follow on.
            preference = problem.patient_preferences.get((bundle.patient_id, weekday, start), 0)
            # Avoiding unmet clinical sessions dominates every lower-priority preference.
            reward = (settings.unmet_bundle_penalty + settings.scheduled_session_reward) * len(bundle.items)
            reward += settings.preference_reward * preference
            # Within equal capacity, favour the least-served children over IDs.
            served = len(booked_days[bundle.patient_id]) + bundle.occurrence_index
            target = max(1, targets[bundle.patient_id])
            reward += settings.absence_priority_reward * max(0, target - served) // target
            if any(item.requirement_id in problem.priority_requirement_ids for item in bundle.items):
                reward += settings.absence_priority_reward * len(bundle.items)
            objective.append(reward * variable)
    _add_cadence_preferences(
        problem,
        bundles,
        candidates,
        variables,
        model,
        objective,
        settings,
    )
    model.Maximize(sum(objective))

    # Seed the search with a quick placement, but ALWAYS run CP-SAT so its
    # preference/cadence objectives also apply to large centres.
    if mode == SolverMode.BEST_EFFORT:
        hint_appointments, _ = _greedy_appointments(problem, bundles, candidates)
        by_bundle = defaultdict(list)
        pinned = set(problem.pinned)
        for item in hint_appointments:
            if item not in pinned:
                by_bundle[item.bundle_id].append(item)
        selected_keys = set()
        for bundle_id, items in by_bundle.items():
            order = timetable_for(problem, items[0].location_id).order
            ordered = sorted(items, key=lambda item: order.index(item.slot_index) if item.slot_index in order else item.slot_index)
            selected_keys.add((bundle_id, (ordered[0].date - problem.week_start).days,
                               ordered[0].slot_index, tuple(a.therapist_id for a in ordered)))
        for key, variable in variables.items():
            model.AddHint(variable, int(key in selected_keys))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = settings.max_time_seconds
    solver.parameters.random_seed = settings.random_seed
    solver.parameters.num_search_workers = settings.num_search_workers
    # The demo model is a large weighted set-packing problem. Disabling expensive
    # presolve/linearization produces a valid incumbent quickly and lets the time
    # limit improve it, instead of timing out before the first feasible solution.
    solver.parameters.cp_model_presolve = False
    solver.parameters.linearization_level = 0
    status_code = solver.Solve(model)
    status = solver.StatusName(status_code)

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        unmet = [
            UnmetRequirement(
                r.patient_id,
                r.id,
                r.department_id,
                r.sessions_per_week * period_count,
                "STRICT_INFEASIBLE",
            )
            for r in problem.requirements
        ]
        return ScheduleResult(
            status=status,
            appointments=list(problem.pinned),
            unmet=unmet,
            requested_sessions=requested,
            scheduled_sessions=len(problem.pinned),
            validation={key: 0 for key in (
                "wrong_therapist", "therapist_overlaps", "patient_overlaps",
                "absence_conflicts", "patient_absence_conflicts",
                "location_capacity_violations", "patient_location_transitions",
                "invalid_slot_durations", "broken_consecutive_bundles",
                "therapist_availability_conflicts", "patient_availability_conflicts",
            )},
            statistics={"variables": len(variables), "strategy": "cp_sat",
                        "search_limit_seconds": settings.max_time_seconds,
                        "wall_time_seconds": monotonic() - started},
        )

    appointments = list(problem.pinned)
    selected_bundles = set()
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            if solver.BooleanValue(variables[(bundle.id, day, start, therapist_ids)]):
                selected_bundles.add(bundle.id)
                target_date = problem.week_start + timedelta(days=day)
                sessions = bundle_sessions(problem, bundle, start)
                for offset, item in enumerate(bundle.items):
                    appointments.append(
                        ScheduledAppointment(
                            patient_id=bundle.patient_id,
                            requirement_id=item.requirement_id,
                            therapist_id=therapist_ids[offset],
                            department_id=item.department_id,
                            location_id=item.location_id,
                            date=target_date,
                            slot_index=sessions[offset],
                            bundle_id=bundle.id,
                        )
                    )

    generated = [item for item in appointments if item not in problem.pinned]
    scheduled_by_requirement = Counter(item.requirement_id for item in appointments if item.counts_toward_requirement)
    selected_therapist_usage = _TimeUsage()
    selected_location_usage = _TimeUsage()
    selected_patient_days = set()
    for appointment in appointments:
        day = (appointment.date - problem.week_start).days
        if not 0 <= day < problem.horizon_days:
            continue
        selected_therapist_usage.add((appointment.therapist_id, day, appointment.slot_index))
        selected_location_usage.add((appointment.location_id, day, appointment.slot_index))
        selected_patient_days.add((appointment.patient_id, day))

    missing_bundle_reasons = defaultdict(Counter)
    for bundle in bundles:
        if bundle.id not in selected_bundles:
            if candidates[bundle.id]:
                bundle_reasons = _selected_bundle_blockers(
                    problem,
                    bundle,
                    candidates[bundle.id],
                    selected_therapist_usage,
                    selected_location_usage,
                    selected_patient_days,
                )
            else:
                bundle_reasons = rejected[bundle.id]
            for item in bundle.items:
                key = (bundle.patient_id, item.requirement_id)
                missing_bundle_reasons[key].update(bundle_reasons)
    unmet = []
    for requirement in problem.requirements:
        required = requirement.sessions_per_week * period_count
        missing = max(0, required - scheduled_by_requirement[requirement.id])
        if missing:
            ranked_reasons = _ranked_reasons(
                missing_bundle_reasons[(requirement.patient_id, requirement.id)]
            )
            unmet.append(
                UnmetRequirement(
                    requirement.patient_id,
                    requirement.id,
                    requirement.department_id,
                    missing,
                    ranked_reasons[0],
                    ranked_reasons[1:],
                )
            )

    issues, report = validate_schedule(problem, appointments)
    if issues:
        status = "INVALID_SOLUTION"
    return ScheduleResult(
        status=status,
        appointments=appointments,
        unmet=unmet,
        requested_sessions=requested,
        scheduled_sessions=len(appointments),
        validation=report,
        statistics={
            "variables": len(variables),
            "strategy": "cp_sat",
            "search_limit_seconds": settings.max_time_seconds,
            "branches": solver.NumBranches(),
            "conflicts": solver.NumConflicts(),
            "objective": solver.ObjectiveValue(),
            "wall_time_seconds": monotonic() - started,
        },
    )

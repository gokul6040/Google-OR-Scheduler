from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import inspect, select

from database.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    Department,
    PatientAbsence,
    PatientAvailability,
    TherapistAbsence,
    TherapistAvailability,
    TreatmentRequirement,
    WaitlistEntry,
)
from database.repository import load_scheduling_problem
from scheduling.rules import placement_rejection_reason
from scheduling.slot_config import valid_consecutive_block
from scheduling.types import BundleItem, TherapyBundle
from services.admin_reservation_service import (
    RELEASED_STATUSES, is_reserved_source, reserved_source_ids,
)


ACTIVE_REPLACEMENT_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
)


def _released_visit(session, vacant: Appointment) -> list[Appointment]:
    """The still-unreserved released sessions of the opening's visit, in slot order."""
    if not vacant.bundle_id:
        return [vacant]
    visit = list(session.scalars(select(Appointment).where(
        Appointment.patient_id == vacant.patient_id,
        Appointment.date == vacant.date,
        Appointment.bundle_id == vacant.bundle_id,
        Appointment.status.in_(RELEASED_STATUSES),
        Appointment.id.notin_(reserved_source_ids()),
    ).order_by(Appointment.slot_index)))
    return visit if any(item.id == vacant.id for item in visit) else [vacant]


def find_replacement_candidates(session, vacant: Appointment, week_start, *, whole_visit: bool = True):
    """Rank children who can take this released opening, best fit first.

    ``whole_visit`` lists only children who can take every released session of the
    visit. Otherwise every child who can take this session is listed, ranked by how
    much of the released visit they could fill. A substitute may need other
    therapies too (it is only a substitute visit), and a child already booked that
    day qualifies only when the opening joins their visit as one continuous block.
    Suggestions neither move later visits nor reserve or message a patient.
    """
    if vacant.status not in RELEASED_STATUSES or is_reserved_source(session, vacant.id):
        return []
    visit = _released_visit(session, vacant)
    sources = visit if whole_visit else [vacant]
    # Always account against the opening's Monday-Saturday week.
    week_start = vacant.date - timedelta(days=vacant.date.weekday())
    problem = load_scheduling_problem(session, week_start, planning_start_date=vacant.date)
    progress = Counter(item.requirement_id for item in problem.pinned if item.counts_toward_requirement)
    remaining = [replace(item, sessions_per_week=max(
        0, item.sessions_per_week - progress[item.id],
    )) for item in problem.requirements]
    requirements = {item.id: item for item in problem.requirements}
    patients = {item.id: item for item in session.scalars(select(Patient).where(Patient.active.is_(True)))}
    departments = set(session.scalars(select(Department.id).where(Department.active.is_(True))))
    # The retained test schema intentionally has no optional main waitlist table.
    # Absence means ordinary priority, not a new migration or a failed search.
    connection = session.connection()
    schema = connection.get_execution_options().get("schema_translate_map", {}).get(WaitlistEntry.__table__.schema)
    waitlists = {}
    if inspect(connection).has_table(WaitlistEntry.__tablename__, schema=schema):
        for item in session.scalars(select(WaitlistEntry).where(
            WaitlistEntry.status == "ACTIVE",
        ).order_by(WaitlistEntry.id)):
            waitlists.setdefault(item.treatment_requirement_id, item)
    day = (vacant.date - week_start).days
    same_day: dict[int, list] = {}
    for item in problem.pinned:
        if item.date == vacant.date:
            same_day.setdefault(item.patient_id, []).append(item)
    therapist_usage = Counter((item.therapist_id, item.date, item.slot_index) for item in problem.pinned)
    location_usage = Counter((item.location_id, item.date, item.slot_index) for item in problem.pinned)
    open_slot = {source.id: not (
        therapist_usage[(source.therapist_id, source.date, source.slot_index)]
        or location_usage[(source.location_id, source.date, source.slot_index)] >=
        problem.location_capacities.get(source.location_id, 0)
    ) for source in visit}
    needs: dict[int, list] = {}
    for item in remaining:
        if item.sessions_per_week > 0:
            needs.setdefault(item.patient_id, []).append(item)

    def placeable(patient_id: int, requirement, source: Appointment) -> bool:
        """The child's own plan, dates, availability and absences permit this session."""
        if ((requirement.therapist_id, requirement.department_id, requirement.location_id)
                != (source.therapist_id, source.department_id, source.location_id)
                or source.department_id not in departments or not open_slot[source.id]):
            return False
        single = TherapyBundle(id=f"R{patient_id}", patient_id=patient_id, occurrence_index=0, items=(BundleItem(
            requirement_id=requirement.id, department_id=requirement.department_id,
            therapist_id=requirement.therapist_id, location_id=requirement.location_id,
            plan_start_date=requirement.plan_start_date, plan_end_date=requirement.plan_end_date,
        ),))
        return placement_rejection_reason(problem, single, day, source.slot_index) is None

    def assign(patient_id: int, slots: list[Appointment]) -> list | None:
        """Match each slot to an unmet therapy, in the plan's therapy order, as one visit."""
        existing = same_day.get(patient_id, [])
        if any(item.location_id != slots[0].location_id for item in existing):
            return None
        taken = sorted([item.slot_index for item in existing] + [source.slot_index for source in slots])
        # One continuous visit: the opening is the visit, or joins the child's existing visit.
        if (len(set(taken)) != len(taken) or taken != list(range(taken[0], taken[0] + len(taken)))
                or not valid_consecutive_block(taken[0], len(taken))):
            return None
        used, chosen = Counter(), []
        for source in slots:
            options = sorted((item for item in needs.get(patient_id, [])
                              if used[item.id] < item.sessions_per_week and placeable(patient_id, item, source)),
                             key=lambda item: (item.sequence_order, item.id))
            if not options:
                return None
            used[options[0].id] += 1
            chosen.append(options[0])
        orders = [item.sequence_order for item in chosen]
        return chosen if orders == sorted(orders) else None

    position = next(index for index, source in enumerate(visit) if source.id == vacant.id)
    candidates = []
    for patient_id in sorted(needs):
        patient = patients.get(patient_id)
        if patient is None or patient_id == vacant.patient_id:
            continue
        chosen = assign(patient_id, sources)
        if chosen is None:
            continue
        requirement = requirements[chosen[next(i for i, s in enumerate(sources) if s.id == vacant.id)].id]
        # Largest continuous part of the released visit (including this opening) the child could fill.
        covers = max(end - start for start in range(position + 1) for end in range(position + 1, len(visit) + 1)
                     if assign(patient_id, visit[start:end]) is not None)
        joins = bool(same_day.get(patient_id))
        waitlist = waitlists.get(requirement.id)
        priority = waitlist.priority if waitlist else 0
        waiting_days = (vacant.date - waitlist.waiting_since).days if waitlist else 0
        score = 100_000 + (10_000 if waitlist else 0) + priority * 100 + max(0, waiting_days)
        if covers == len(visit):
            fit = "complete_visit"
            reason = (f"Can take all {len(visit)} released session(s) with the same therapist(s) at the same branch."
                      if len(visit) > 1 else "Needs this therapy with the same therapist at the same branch.")
        elif covers > 1:
            fit = "part_of_visit"
            reason = f"Can take {covers} of the {len(visit)} released sessions with the same therapists at the same branch."
        else:
            fit = "this_session"
            reason = "Can take this session only, with the same therapist at the same branch."
        if joins:
            reason += " Joins the child's existing visit that day."
        owed = requirement.sessions_per_week - progress[requirement.id]
        candidates.append({
            "patient_id": patient.id,
            "patient_code": patient.patient_code,
            "patient_name": patient.name or patient.patient_code,
            "requirement_id": requirement.id,
            "weekly_progress": f"{progress[requirement.id]}/{requirement.sessions_per_week}",
            "reason": reason,
            "fit": fit,
            "sessions_covered": covers,
            "visit_sessions": len(visit),
            "joins_existing_visit": joins,
            "priority": priority,
            "score": score,
            "_owed": owed,
        })
    candidates.sort(key=lambda item: (-(item["sessions_covered"] == item["visit_sessions"]),
                                      -item["sessions_covered"], -item["score"], -item["_owed"],
                                      item["patient_code"]))
    for item in candidates:
        del item["_owed"]
    return candidates


def find_pull_forward_candidates(session, vacant: Appointment, horizon_end):
    """Find later continuous visits that could start in a cancelled opening."""
    future = list(session.scalars(select(Appointment).where(
        Appointment.date > vacant.date,
        Appointment.date <= horizon_end,
        Appointment.therapist_id == vacant.therapist_id,
        Appointment.department_id == vacant.department_id,
        Appointment.status.in_(ACTIVE_REPLACEMENT_STATUSES),
    ).order_by(Appointment.date, Appointment.slot_index)))
    candidates = []
    seen_bundles = set()
    for appointment in future:
        bundle_key = (
            appointment.patient_id,
            appointment.date,
            appointment.bundle_id or f"A-{appointment.id}",
        )
        if bundle_key in seen_bundles:
            continue
        seen_bundles.add(bundle_key)
        group = [appointment]
        if appointment.bundle_id:
            group = list(session.scalars(select(Appointment).where(
                Appointment.patient_id == appointment.patient_id,
                Appointment.date == appointment.date,
                Appointment.bundle_id == appointment.bundle_id,
                Appointment.status.in_(ACTIVE_REPLACEMENT_STATUSES),
            ).order_by(Appointment.slot_index)))
        if appointment.id != group[0].id:
            continue
        if vacant.slot_index + len(group) > 10:
            continue

        conflicting_visit = session.scalar(select(Appointment.id).where(
            Appointment.patient_id == appointment.patient_id,
            Appointment.date == vacant.date,
            Appointment.status.in_(ACTIVE_REPLACEMENT_STATUSES),
        ))
        patient_absence = session.scalar(select(PatientAbsence.id).where(
            PatientAbsence.patient_id == appointment.patient_id,
            PatientAbsence.date == vacant.date,
        ))
        if conflicting_visit or patient_absence:
            continue

        target_weekday = vacant.date.weekday()
        viable = True
        for offset, item in enumerate(group):
            target_slot = vacant.slot_index + offset
            patient_available = session.scalar(select(PatientAvailability.id).where(
                PatientAvailability.patient_id == item.patient_id,
                PatientAvailability.day_of_week == target_weekday,
                PatientAvailability.slot_index == target_slot,
                PatientAvailability.available.is_(True),
            ))
            therapist_available = session.scalar(select(TherapistAvailability.id).where(
                TherapistAvailability.therapist_id == item.therapist_id,
                TherapistAvailability.day_of_week == target_weekday,
                TherapistAvailability.slot_index == target_slot,
                TherapistAvailability.available.is_(True),
            ))
            therapist_absent = session.scalar(select(TherapistAbsence.id).where(
                TherapistAbsence.therapist_id == item.therapist_id,
                TherapistAbsence.date == vacant.date,
            ))
            if not patient_available or not therapist_available or therapist_absent:
                viable = False
                break
        if not viable:
            continue

        patient = session.get(Patient, appointment.patient_id)
        days_later = (appointment.date - vacant.date).days
        candidates.append({
            "appointment_id": appointment.id,
            "patient_code": patient.patient_code,
            "scheduled_date": appointment.date,
            "days_later": days_later,
            "current_status": appointment.status.value,
            "continuous_appointments": len(group),
            "reason": (
                f"Scheduled {days_later} day(s) later and available for the cancelled opening."
            ),
            "score": 1_000_000 - days_later * 10_000,
        })
    return sorted(candidates, key=lambda item: (-item["score"], item["patient_code"]))

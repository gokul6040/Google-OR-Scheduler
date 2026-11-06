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
from scheduling.bundle_builder import build_bundles
from scheduling.rules import placement_rejection_reason
from services.admin_reservation_service import (
    RELEASED_STATUSES, is_reserved_source, reserved_source_ids,
)


ACTIVE_REPLACEMENT_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
)


def find_replacement_candidates(session, vacant: Appointment, week_start, *, whole_visit: bool = True):
    """Suggest complete visits for this opening using the scheduler's rules.

    Search all patients' remaining weekly bundles, retaining saved bookings.
    Suggestions neither move later visits nor reserve or message a patient.
    """
    if vacant.status not in RELEASED_STATUSES or is_reserved_source(session, vacant.id):
        return []
    sources = [vacant]
    if whole_visit and vacant.bundle_id:
        sources = list(session.scalars(select(Appointment).where(
            Appointment.patient_id == vacant.patient_id,
            Appointment.date == vacant.date,
            Appointment.bundle_id == vacant.bundle_id,
            Appointment.status.in_(RELEASED_STATUSES),
            Appointment.id.notin_(reserved_source_ids()),
        ).order_by(Appointment.slot_index)))
    # Always account against the opening's Monday-Saturday week.
    week_start = vacant.date - timedelta(days=vacant.date.weekday())
    problem = load_scheduling_problem(session, week_start, planning_start_date=vacant.date)
    progress = Counter(item.requirement_id for item in problem.pinned)
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
    booked_patients = {item.patient_id for item in problem.pinned if item.date == vacant.date}
    therapist_usage = Counter((item.therapist_id, item.date, item.slot_index) for item in problem.pinned)
    location_usage = Counter((item.location_id, item.date, item.slot_index) for item in problem.pinned)
    candidates = []
    seen = set()
    for bundle in build_bundles(remaining):
        patient = patients.get(bundle.patient_id)
        if patient is None or patient.id == vacant.patient_id or patient.id in booked_patients or patient.id in seen:
            continue
        if len(bundle.items) != len(sources) or any(
            (item.therapist_id, item.department_id, item.location_id) !=
            (source.therapist_id, source.department_id, source.location_id)
            or source.slot_index != sources[0].slot_index + offset
            or item.department_id not in departments
            for offset, (item, source) in enumerate(zip(bundle.items, sources))
        ):
            continue
        if placement_rejection_reason(problem, bundle, (vacant.date - week_start).days, sources[0].slot_index):
            continue
        if any(
            therapist_usage[(source.therapist_id, source.date, source.slot_index)]
            or location_usage[(source.location_id, source.date, source.slot_index)] >=
            problem.location_capacities.get(source.location_id, 0)
            for source in sources
        ):
            continue
        selected = bundle.items[next(index for index, source in enumerate(sources) if source.id == vacant.id)]
        requirement = requirements[selected.requirement_id]
        waitlist = waitlists.get(requirement.id)
        priority = waitlist.priority if waitlist else 0
        waiting_days = (vacant.date - waitlist.waiting_since).days if waitlist else 0
        score = 100_000 + (10_000 if waitlist else 0) + priority * 100 + max(0, waiting_days)
        candidates.append({
            "patient_id": patient.id,
            "patient_code": patient.patient_code,
            "patient_name": patient.name or patient.patient_code,
            "requirement_id": requirement.id,
            "weekly_progress": f"{progress[requirement.id]}/{requirement.sessions_per_week}",
            "reason": (
                "Needs this therapy with the same therapist at the same branch "
                f"and can complete the released visit at slot {sources[0].slot_index}."
            ),
            "priority": priority,
            "score": score,
        })
        seen.add(patient.id)
    return sorted(candidates, key=lambda item: (-item["score"], item["patient_code"]))


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

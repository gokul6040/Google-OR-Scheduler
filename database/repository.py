from __future__ import annotations

from collections import Counter
from calendar import monthrange
from dataclasses import replace
from datetime import date, timedelta
import json

from sqlalchemy import Select, and_, or_, select

from database.models import (
    AdminSlotReservation,
    AdminBookingDecision,
    Appointment,
    AppointmentStatus,
    AuditLog,
    Location,
    Patient,
    PatientAbsence,
    PatientAvailability,
    Therapist,
    TherapistAbsence,
    TherapistAvailability,
    TreatmentRequirement,
)
from scheduling.types import RequirementInput, ScheduledAppointment, SchedulingProblem
from scheduling.slot_config import SLOTS


ACTIVE_SCHEDULE_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.COMPLETED,
)


def off_plan_appointment_ids() -> Select[tuple[int]]:
    """Select manual reservations that occupy time without satisfying a plan."""
    return select(AdminSlotReservation.appointment_id).where(
        AdminSlotReservation.counts_toward_requirement.is_(False),
    )


def _plan_end_date(start_date: date | None, duration_months: int | None) -> date | None:
    """Return the final active date for a calendar-month therapy plan."""
    if start_date is None or duration_months is None:
        return None
    month_number = start_date.month - 1 + duration_months
    year = start_date.year + month_number // 12
    month = month_number % 12 + 1
    end_exclusive = date(year, month, min(start_date.day, monthrange(year, month)[1]))
    return end_exclusive - timedelta(days=1)


def load_scheduling_problem(
    session,
    week_start: date,
    preserve_confirmed: bool = True,
    horizon_days: int = 6,
    planning_start_date: date | None = None,
    carryover_by_requirement: dict[int, int] | None = None,
    priority_requirement_ids: set[int] | None = None,
):
    week_end = week_start + timedelta(days=horizon_days - 1)
    retained_ids = select(Appointment.treatment_requirement_id).where(
        Appointment.date >= week_start, Appointment.date <= week_end,
        Appointment.status.in_(ACTIVE_SCHEDULE_STATUSES),
    )
    # Retired courses supply no new demand, but their saved bookings still
    # occupy capacity and need their original assignment for validation.
    active_demand = and_(TreatmentRequirement.active.is_(True), Patient.active.is_(True))
    requirements = list(session.execute(
        select(TreatmentRequirement, active_demand.label("active_demand")).join(
            Patient, Patient.id == TreatmentRequirement.patient_id,
        ).where(or_(
            active_demand, TreatmentRequirement.id.in_(retained_ids) if preserve_confirmed else False,
        ))
    ))
    carryover_by_requirement = carryover_by_requirement or {}
    therapists = {item.id: item for item in session.scalars(select(Therapist))}
    inputs = [
        RequirementInput(
            id=item.id,
            patient_id=item.patient_id,
            department_id=item.department_id,
            therapist_id=item.assigned_therapist_id,
            location_id=item.location_id or therapists[item.assigned_therapist_id].location_id,
            sessions_per_week=(
                item.sessions_per_week + carryover_by_requirement.get(item.id, 0) if eligible else 0
            ),
            sequence_order=item.sequence_order,
            plan_start_date=item.plan_start_date,
            plan_end_date=_plan_end_date(item.plan_start_date, item.duration_months),
        )
        for item, eligible in requirements
    ]
    # R-13 defines all active staff and patients as available during configured
    # operating hours by default.  Persisted availability rows therefore act as
    # exceptions/preferences.  Loading every generated default row from a
    # hosted database (over 160k patient rows in the live source) delays every
    # weekly preview without adding planning information.
    therapist_availability = {
        (therapist_id, weekday, slot.slot_index)
        for therapist_id in therapists
        for weekday in range(6)
        for slot in SLOTS
    }
    therapist_unavailable = session.scalars(select(TherapistAvailability).where(
        TherapistAvailability.available.is_(False)
    ))
    for item in therapist_unavailable:
        therapist_availability.discard((item.therapist_id, item.day_of_week, item.slot_index))

    patient_ids = {item.patient_id for item in inputs}
    patient_availability = {
        (patient_id, weekday, slot.slot_index)
        for patient_id in patient_ids
        for weekday in range(6)
        for slot in SLOTS
    }
    patient_preferences = {}
    patient_overrides = session.scalars(select(PatientAvailability).where(or_(
        PatientAvailability.available.is_(False),
        PatientAvailability.preference_score != 0,
    )))
    for item in patient_overrides:
        key = (item.patient_id, item.day_of_week, item.slot_index)
        if not item.available:
            patient_availability.discard(key)
        if item.preference_score:
            patient_preferences[key] = item.preference_score
    week_end = week_start + timedelta(days=horizon_days - 1)
    absences = set()
    for item in session.scalars(select(TherapistAbsence).where(
        TherapistAbsence.date >= week_start, TherapistAbsence.date <= week_end
    )):
        slots = range(len(SLOTS)) if item.full_day else range(
            item.start_slot or 0, (item.end_slot or item.start_slot or 0) + 1
        )
        absences.update((item.therapist_id, item.date, slot) for slot in slots)
    patient_absences = set()
    for item in session.scalars(select(PatientAbsence).where(
        PatientAbsence.date >= week_start, PatientAbsence.date <= week_end
    )):
        slots = range(len(SLOTS)) if item.full_day else range(
            item.start_slot or 0, (item.end_slot or item.start_slot or 0) + 1
        )
        patient_absences.update((item.patient_id, item.date, slot) for slot in slots)
    locations = {item.id: item.capacity for item in session.scalars(select(Location).where(Location.active.is_(True)))}
    active_therapists = {item.id for item in therapists.values() if item.active}
    # Therapist continuity is a hard clinical rule. Qualification and branch
    # mappings are still imported for reporting, but they never permit a
    # different clinician to take over a patient's treatment requirement.
    eligible_therapists = {
        item.id: (item.assigned_therapist_id,)
        for item, _ in requirements
    }
    pinned = []
    overrides = {identifier: tuple(warning["code"] for warning in json.loads(warnings))
                 for identifier, warnings in session.execute(select(
                     AdminSlotReservation.appointment_id, AdminBookingDecision.warnings).join(
                     AdminBookingDecision, AdminBookingDecision.request_id == AdminSlotReservation.decision_id).join(
                     Appointment, Appointment.id == AdminSlotReservation.appointment_id).where(
                     Appointment.date.between(week_start, week_end)))}
    released_bundle_slots = set()
    if preserve_confirmed:
        for item, counts_toward_requirement in session.execute(select(
            Appointment, Appointment.id.notin_(off_plan_appointment_ids()),
        ).where(
            Appointment.date >= week_start,
            Appointment.date <= week_end,
            or_(Appointment.status.in_(ACTIVE_SCHEDULE_STATUSES), Appointment.bundle_id.is_not(None)),
        )):
            if item.status not in ACTIVE_SCHEDULE_STATUSES:
                released_bundle_slots.add((item.patient_id, item.date, item.location_id, item.bundle_id, item.slot_index))
                continue
            pinned.append(ScheduledAppointment(
                patient_id=item.patient_id,
                requirement_id=item.treatment_requirement_id,
                therapist_id=item.therapist_id,
                department_id=item.department_id,
                location_id=item.location_id,
                date=item.date,
                slot_index=item.slot_index,
                bundle_id=item.bundle_id or f"PIN-{item.id}",
                status=item.status.value,
                counts_toward_requirement=counts_toward_requirement,
                assignment_override="THERAPIST_MISMATCH" in overrides.get(item.id, ()),
                overridden_warnings=overrides.get(item.id, ()),
            ))
    return SchedulingProblem(
        week_start=week_start,
        requirements=inputs,
        therapist_availability=therapist_availability,
        patient_availability=patient_availability,
        patient_preferences=patient_preferences,
        absences=absences,
        location_capacities=locations,
        active_therapists=active_therapists,
        pinned=pinned,
        horizon_days=horizon_days,
        eligible_therapists=eligible_therapists,
        patient_absences=patient_absences,
        planning_start_date=planning_start_date,
        priority_requirement_ids=priority_requirement_ids or set(),
        released_bundle_slots=released_bundle_slots,
    )


def load_daily_scheduling_problem(session, target: date) -> SchedulingProblem:
    """Apply daily priorities using saved visits and the current plan targets."""
    week_start = target - timedelta(days=target.weekday())
    problem = load_scheduling_problem(session, week_start, planning_start_date=target)
    problem.isolate_saved_failures = True
    previous_start = week_start - timedelta(days=7)
    previous_end = week_start - timedelta(days=2)
    # Never invent missed therapy before this installation began saving visits.
    # A partially observed first week cannot establish a full-week shortfall.
    baseline = session.scalar(select(AuditLog).where(
        AuditLog.action == "daily_planning_started").order_by(AuditLog.id).limit(1))
    first_run = date.fromisoformat(baseline.entity) if baseline else target
    observed_previous_week = first_run <= previous_start
    previous_bookings = Counter(session.scalars(select(Appointment.treatment_requirement_id).where(
        Appointment.date.between(previous_start, previous_end),
        Appointment.status.in_(ACTIVE_SCHEDULE_STATUSES),
        Appointment.id.notin_(off_plan_appointment_ids()),
    )))
    released_requirements = set(session.scalars(select(Appointment.treatment_requirement_id).where(
        Appointment.date.between(week_start, target),
        Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING,
        Appointment.id.notin_(off_plan_appointment_ids()),
    )))
    for index, requirement in enumerate(problem.requirements):
        if requirement.sessions_per_week <= 0:
            continue
        if requirement.id in released_requirements:
            problem.priority_requirement_ids.add(requirement.id)
        if (
            not observed_previous_week
            or
            (requirement.plan_start_date and requirement.plan_start_date > previous_end)
            or (requirement.plan_end_date and requirement.plan_end_date < previous_start)
        ):
            continue
        shortfall = requirement.sessions_per_week - previous_bookings[requirement.id]
        if shortfall > 0:
            problem.requirements[index] = replace(
                requirement, sessions_per_week=requirement.sessions_per_week + shortfall,
            )
            problem.priority_requirement_ids.add(requirement.id)
    return problem


def record_daily_planning_start(session, target: date) -> None:
    """Persist the baseline only with a successful calendar save."""
    if session.scalar(select(AuditLog.id).where(AuditLog.action == "daily_planning_started").limit(1)) is None:
        session.add(AuditLog(action="daily_planning_started", entity=target.isoformat()))


def limit_new_bookings_to_day(problem: SchedulingProblem, target: date) -> None:
    """Keep weekly accounting and allow new placements only on the target day."""
    problem.new_booking_slots = {
        (requirement.therapist_id, requirement.department_id, requirement.location_id, target, slot.slot_index)
        for requirement in problem.requirements
        for slot in SLOTS
    }


def save_proposed_schedule(session, result, week_start: date, horizon_days: int = 6):
    """Add new suggestions without rewriting saved active appointments."""
    week_end = week_start + timedelta(days=horizon_days - 1)
    existing = {
        (
            item.patient_id,
            item.treatment_requirement_id,
            item.therapist_id,
            item.date,
            item.slot_index,
        )
        for item in session.scalars(select(Appointment).where(
            Appointment.date >= week_start,
            Appointment.date <= week_end,
            Appointment.status.in_(ACTIVE_SCHEDULE_STATUSES),
        ))
    }
    new_patient_ids = {
        item.patient_id for item in result.appointments
        if (item.patient_id, item.requirement_id, item.therapist_id, item.date, item.slot_index) not in existing
    }
    if new_patient_ids:
        active = dict(session.execute(select(Patient.id, Patient.active).where(
            Patient.id.in_(new_patient_ids),
        ).order_by(Patient.id).with_for_update()).all())
        if any(not active.get(patient_id) for patient_id in new_patient_ids):
            raise ValueError("Patient scheduling status changed. Generate a fresh preview.")
    added = 0
    added_appointments: list[ScheduledAppointment] = []
    for item in result.appointments:
        key = (
            item.patient_id,
            item.requirement_id,
            item.therapist_id,
            item.date,
            item.slot_index,
        )
        if key in existing:
            continue
        session.add(Appointment(
            patient_id=item.patient_id,
            treatment_requirement_id=item.requirement_id,
            therapist_id=item.therapist_id,
            department_id=item.department_id,
            location_id=item.location_id,
            date=item.date,
            slot_index=item.slot_index,
            status=AppointmentStatus.PROPOSED,
            bundle_id=item.bundle_id,
            source="cp_sat",
        ))
        existing.add(key)
        added_appointments.append(item)
        added += 1
    resolved = 0
    for released in session.scalars(select(Appointment).where(
        Appointment.date >= week_start,
        Appointment.date <= week_end,
        Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING,
        Appointment.id.notin_(off_plan_appointment_ids()),
    )):
        if any(
            item.requirement_id == released.treatment_requirement_id
            and item.date >= released.date
            for item in added_appointments
        ):
            released.status = AppointmentStatus.CANCELLED_OTHER
            resolved += 1
    session.add(AuditLog(
        action="schedule_generated",
        entity="appointment",
        new_value=(
            f"start={week_start}; horizon_days={horizon_days}; added={added}; resolved={resolved}; "
            f"scheduled={result.scheduled_sessions}; status={result.status}"
        ),
    ))
    if horizon_days == 1:
        record_daily_planning_start(session, week_start)
    session.commit()
    return added

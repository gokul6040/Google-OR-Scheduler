"""Centre shifts: timetables of sessions and breaks, and which shift each clinician works.

Rules (user decisions, 18 Nov 2026):
- The working day runs 07:00-21:00. Every centre starts on the original 40-minute
  timetable ("Shift 1", sessions 0-9) and every clinician starts on their centre's
  default shift.
- One session length per centre. Inside a centre a session must either match another
  shift's session exactly or not touch it; a partial overlap is a warning, and a shift
  with a warning can be saved as a draft but not activated.
- Breaks are "short" (a child may wait through it between therapies) or "lunch" (not).
- Session numbers are identities: 0-9 keep their original meaning and a new time's
  number encodes the time itself (scheduling.slot_config.encode_slot), so saved
  bookings never need renumbering and every database agrees on what a number means.

These tables are additive (own metadata, created on start-up like the portal tables);
existing tables are never altered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import date, datetime, time, timedelta

from sqlalchemy import Boolean, DateTime, Integer, String, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from scheduling.slot_config import (
    DAY_END, DAY_START, SESSION_DURATION_MINUTES, SLOTS, Timetable, encode_slot, known_slot, minutes,
    peak_booked, register_slots, slot_def,
)

STATUSES = ("draft", "active", "inactive", "archived")  # inactive = Shift 1 kept but not used
BREAK_KINDS = ("short", "lunch")
STANDARD_NAME = "Shift 1"
NO_SHIFT_NAME = "No shift"
# The original timetable, with its breaks, as a shift.
STANDARD_ITEMS = (
    ("session", None, "09:00", "09:40"), ("session", None, "09:40", "10:20"),
    ("break", "short", "10:20", "10:35"),
    ("session", None, "10:35", "11:15"), ("session", None, "11:15", "11:55"), ("session", None, "11:55", "12:35"),
    ("break", "lunch", "12:35", "13:15"),
    ("session", None, "13:15", "13:55"), ("session", None, "13:55", "14:35"), ("session", None, "14:35", "15:15"),
    ("break", "short", "15:15", "15:30"),
    ("session", None, "15:30", "16:10"), ("session", None, "16:10", "16:50"),
)


class ShiftBase(DeclarativeBase):
    pass


class CentreSessionSetting(ShiftBase):
    __tablename__ = "centre_session_settings"
    location_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    session_minutes: Mapped[int] = mapped_column(Integer, default=SESSION_DURATION_MINUTES)


class Shift(ShiftBase):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(80))
    session_minutes: Mapped[int] = mapped_column(Integer, default=SESSION_DURATION_MINUTES)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="draft")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(120))


class ShiftItem(ShiftBase):
    __tablename__ = "shift_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))  # session | break
    break_kind: Mapped[str | None] = mapped_column(String(10))  # short | lunch
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    slot_id: Mapped[int | None] = mapped_column(Integer)


class TherapistShift(ShiftBase):
    __tablename__ = "therapist_shifts"
    therapist_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    shift_id: Mapped[int] = mapped_column(Integer, index=True)
    assigned_by: Mapped[str | None] = mapped_column(String(120))
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


CREATE_LOCK_KEY = 0x5348494654  # PostgreSQL advisory lock id ("SHIFT") for creating the tables


def create_shift_tables(connection) -> None:
    """Create the missing shift tables on this connection's transaction, safely under concurrency.

    On PostgreSQL two processes creating the same tables at once can fail (seen on a
    production copy: IntegrityError). The first creator takes a transaction lock; a second
    waits, then finds the tables already there. Existing tables and rows are untouched.
    """
    if connection.dialect.name == "postgresql":
        schema = (connection.get_execution_options().get("schema_translate_map") or {}).get(None)
        if inspect(connection).has_table(Shift.__tablename__, schema=schema):
            return
        from sqlalchemy import text

        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CREATE_LOCK_KEY})
    ShiftBase.metadata.create_all(connection)


def ensure_shift_tables(engine) -> None:
    """Create only the missing shift tables; existing tables and rows are untouched."""
    with engine.begin() as connection:
        create_shift_tables(connection)


def shift_tables_present(session) -> bool:
    # Use the session's own connection: a second connection would end an open
    # savepoint/transaction (e.g. parent replies processed in a nested transaction).
    try:
        connection = session.connection()
        schema = (connection.get_execution_options().get("schema_translate_map") or {}).get(None)
        return inspect(connection).has_table(Shift.__tablename__, schema=schema)
    except Exception:  # noqa: BLE001 - a store without the tables simply uses the original timetable
        return False


# --- time helpers -------------------------------------------------------------

def parse_hhmm(value: object) -> time:
    text = str(value or "").strip()
    try:
        hours, mins = text.split(":")
        parsed = time(int(hours), int(mins))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"'{text or 'blank'}' is not a time (use HH:MM)") from exc
    return parsed


def hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def label(value: time) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


# --- catalogue ----------------------------------------------------------------

def load_catalog(session) -> None:
    """List every shift session among the known sessions (numbers already encode their times)."""
    if not shift_tables_present(session):
        return
    register_slots(slot_def(slot) for slot in session.scalars(
        select(ShiftItem.slot_id).where(ShiftItem.slot_id.is_not(None)).distinct()))


def session_slot_id(session, start: time, end: time) -> int:
    """The stable number for this session time (0-9 for the original times)."""
    slot = encode_slot(start, end)
    register_slots([slot_def(slot)])
    return slot


# --- shift context used by the scheduler and the screens -----------------------

@dataclass
class ShiftContext:
    """Everything the planner and screens need about shifts, read once per request."""

    shifts: dict[int, Shift] = field(default_factory=dict)
    items: dict[int, list[ShiftItem]] = field(default_factory=dict)
    assignments: dict[int, int] = field(default_factory=dict)  # therapist -> shift
    centre_minutes: dict[int, int] = field(default_factory=dict)

    def active_shifts(self, location_id: int) -> list[Shift]:
        return [shift for shift in self.shifts.values() if shift.location_id == location_id and shift.status == "active"]

    def standard_shift(self, location_id: int) -> Shift | None:
        """The centre's original Shift 1 (its first stored shift); kept, not deleted, when deactivated."""
        stored = [shift for shift in self.shifts.values() if shift.location_id == location_id]
        return min(stored, key=lambda shift: shift.id) if stored else None

    def is_standard(self, shift) -> bool:
        standard = self.standard_shift(shift.location_id)
        return standard is not None and standard.id == shift.id

    def default_shift(self, location_id: int) -> Shift | None:
        return next((shift for shift in self.active_shifts(location_id) if shift.is_default), None)

    def sessions(self, shift_id: int) -> list[int]:
        return [item.slot_id for item in self.items.get(shift_id, []) if item.kind == "session" and item.slot_id is not None]

    def therapist_shift(self, therapist_id: int, location_id: int | None) -> Shift | None:
        """The clinician's shift: their assignment if active, else their centre's default."""
        shift = self.shifts.get(self.assignments.get(therapist_id, -1))
        if shift is not None and shift.status == "active" and shift.location_id == location_id:
            return shift
        return self.default_shift(location_id) if location_id is not None else None

    def therapist_sessions(self, therapist_id: int, location_id: int | None) -> list[int]:
        """Sessions the clinician works. A centre whose shifts are all deactivated has none
        (nobody is scheduled there until a new shift is created)."""
        shift = self.therapist_shift(therapist_id, location_id)
        if shift is not None:
            return self.sessions(shift.id)
        return [] if location_id in self.configured_locations() else [slot.slot_index for slot in SLOTS]

    def session_minutes(self, location_id: int) -> int:
        return self.centre_minutes.get(location_id, SESSION_DURATION_MINUTES)

    def length_locked(self, location_id: int) -> bool:
        """The session length can change only while the centre has no active shift."""
        return bool(self.active_shifts(location_id))

    def configured_locations(self) -> set[int]:
        """Centres that have stored shifts (active or deactivated); others use the original day."""
        return {shift.location_id for shift in self.shifts.values()}

    def timetable(self, location_id: int, extra_sessions=()) -> Timetable:
        """Sessions of the centre's active shifts (plus saved bookings' sessions) in clock order.

        Two neighbouring sessions chain when there is no gap, or the gap is a short
        break in some shift and no shift has lunch in it.
        """
        shifts = self.active_shifts(location_id)
        sessions = {slot for shift in shifts for slot in self.sessions(shift.id)} | set(extra_sessions)
        order = tuple(sorted(sessions, key=lambda slot: (slot_def(slot).start_time, slot_def(slot).end_time, slot)))
        breaks = [(item.break_kind, minutes(parse_hhmm(item.start_time)), minutes(parse_hhmm(item.end_time)))
                  for shift in shifts for item in self.items.get(shift.id, []) if item.kind == "break"]
        links = set()
        for a, b in zip(order, order[1:]):
            end, start = minutes(slot_def(a).end_time), minutes(slot_def(b).start_time)
            if start == end:
                links.add((a, b))
            elif start > end:
                lunch = any(kind == "lunch" and lo < start and hi > end for kind, lo, hi in breaks)
                short = any(kind == "short" and lo <= end and hi >= start for kind, lo, hi in breaks)
                if short and not lunch:
                    links.add((a, b))
        return Timetable(order=order, links=frozenset(links))


def load_shift_context(session) -> ShiftContext:
    """Read shifts once; a store without shift tables behaves exactly like before."""
    context = ShiftContext()
    if not shift_tables_present(session):
        return context
    load_catalog(session)
    context.shifts = {shift.id: shift for shift in session.scalars(select(Shift))}
    for item in session.scalars(select(ShiftItem).order_by(ShiftItem.shift_id, ShiftItem.position)):
        context.items.setdefault(item.shift_id, []).append(item)
    context.assignments = dict(session.execute(select(TherapistShift.therapist_id, TherapistShift.shift_id)).all())
    context.centre_minutes = dict(session.execute(
        select(CentreSessionSetting.location_id, CentreSessionSetting.session_minutes)).all())
    return context


def centre_timetable(session, location_id: int | None) -> Timetable:
    """A centre's timetable for services outside the planner (moves, replacements)."""
    from scheduling.slot_config import DEFAULT_TIMETABLE

    context = load_shift_context(session)
    if location_id is None or location_id not in context.configured_locations():
        return DEFAULT_TIMETABLE
    return context.timetable(location_id)


def clock_key(slot_index: int) -> tuple:
    """Sort key putting sessions in clock order (numbers are identities, not order)."""
    try:
        slot = slot_def(slot_index)
        return (slot.start_time, slot.end_time, slot_index)
    except ValueError:
        return (time.max, time.max, slot_index)


# --- editing ---------------------------------------------------------------------

@dataclass
class ShiftCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows: list[tuple[str, str | None, time, time]] = field(default_factory=list)
    session_minutes: int = SESSION_DURATION_MINUTES


MIN_SESSION, MAX_SESSION = 15, 180


def check_shift(context: ShiftContext, location_id: int, items: list[dict], *, shift_id: int | None = None) -> ShiftCheck:
    """Validate a shift's rows; partial overlaps with other active shifts are warnings."""
    check = ShiftCheck()
    # While the centre has an active shift every session keeps its length; with none active,
    # the first session (by start time) of this shift sets the length for all the others.
    locked = context.length_locked(location_id)
    length = context.session_minutes(location_id) if locked else None
    numbered = []
    if not isinstance(items, list) or not items:
        check.errors.append("Add at least one session.")
        return check
    for number, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            check.errors.append(f"Row {number}: invalid row.")
            continue
        kind = raw.get("kind")
        if kind not in ("session", "break"):
            check.errors.append(f"Row {number}: choose Session or Break.")
            continue
        break_kind = raw.get("breakKind") if kind == "break" else None
        if kind == "break" and break_kind not in BREAK_KINDS:
            check.errors.append(f"Row {number}: choose Short break or Lunch.")
            continue
        try:
            start, end = parse_hhmm(raw.get("start")), parse_hhmm(raw.get("end"))
        except ValueError as exc:
            check.errors.append(f"Row {number}: {exc}.")
            continue
        if end <= start:
            check.errors.append(f"Row {number}: the end time must be after the start time.")
            continue
        if start < DAY_START or end > DAY_END:
            check.errors.append(f"Row {number}: times must be between {label(DAY_START)} and {label(DAY_END)}.")
            continue
        check.rows.append((kind, break_kind, start, end))
        numbered.append(number)
    if check.errors:
        return check
    sessions = sorted((row[2], row[3], number) for row, number in zip(check.rows, numbered) if row[0] == "session")
    if length is None and sessions:
        length = minutes(sessions[0][1]) - minutes(sessions[0][0])
        if not MIN_SESSION <= length <= MAX_SESSION:
            check.errors.append(f"A session must be {MIN_SESSION} minutes to {MAX_SESSION // 60} hours long.")
            return check
    for start, end, number in sessions:
        if minutes(end) - minutes(start) != length:
            expected = (datetime.combine(date.min, start) + timedelta(minutes=length)).time()
            reason = "at this centre" if locked else "in this shift (set by its first session)"
            check.errors.append(f"Row {number}: sessions {reason} are {length} minutes "
                                f"({label(start)} should end at {label(expected)}).")
    if check.errors:
        return check
    check.session_minutes = length or context.session_minutes(location_id)
    if not any(kind == "session" for kind, *_ in check.rows):
        check.errors.append("Add at least one session.")
    ordered = sorted(check.rows, key=lambda row: row[2])
    for first, second in zip(ordered, ordered[1:]):
        if second[2] < first[3]:
            check.errors.append(f"{label(first[2])}–{label(first[3])} and {label(second[2])}–{label(second[3])} "
                                "overlap. A break cannot cut through a session.")
    others = [shift for shift in context.active_shifts(location_id) if shift.id != shift_id]
    check.warnings = session_overlaps(context, [(start, end) for kind, _, start, end in check.rows
                                                if kind == "session"], others)
    return check


def session_overlaps(context: ShiftContext, sessions, others) -> list[str]:
    """Sessions that partly overlap another shift's sessions. Exact matches and back-to-back are fine."""
    found = []
    timed = [(slot_def(slot).start_time, slot_def(slot).end_time, shift.name)
             for shift in others for slot in context.sessions(shift.id)]
    for start, end in sorted(sessions):
        for other_start, other_end, name in timed:
            identical = (other_start, other_end) == (start, end)
            if not identical and start < other_end and other_start < end:
                found.append(f"{label(start)}–{label(end)} overlaps {label(other_start)}–{label(other_end)} "
                             f"({name}). Use the same times or times that do not touch.")
    return list(dict.fromkeys(found))


def ensure_default_shift(session, context: ShiftContext, location_id: int, actor: str) -> Shift | None:
    """Store the centre's original timetable as its default "Shift 1" the first time shifts are
    edited. A centre whose shifts were all deactivated gets no new default (None)."""
    existing = context.default_shift(location_id)
    if existing is not None:
        return existing
    if location_id in context.configured_locations():
        return None
    shift = Shift(location_id=location_id, name=STANDARD_NAME, session_minutes=context.session_minutes(location_id),
                  is_default=True, status="active", updated_by=actor)
    session.add(shift)
    session.flush()
    rows = []
    for position, (kind, break_kind, start, end) in enumerate(STANDARD_ITEMS):
        slot_id = session_slot_id(session, parse_hhmm(start), parse_hhmm(end)) if kind == "session" else None
        rows.append(ShiftItem(shift_id=shift.id, position=position, kind=kind, break_kind=break_kind,
                              start_time=start, end_time=end, slot_id=slot_id))
    session.add_all(rows)
    session.flush()
    context.shifts[shift.id] = shift
    context.items[shift.id] = rows
    return shift


def standard_shift_payload() -> dict:
    """The original timetable shown for a centre that has never edited its shifts."""
    return {"id": None, "name": STANDARD_NAME, "status": "active", "isDefault": True, "implicit": True, "standard": True,
            "sessionMinutes": SESSION_DURATION_MINUTES,
            "items": [{"kind": kind, "breakKind": break_kind, "start": start, "end": end}
                      for kind, break_kind, start, end in STANDARD_ITEMS]}


def shift_payload(context: ShiftContext, shift: Shift, doctors: int = 0) -> dict:
    return {"id": shift.id, "name": shift.name, "status": shift.status, "isDefault": shift.is_default,
            "implicit": False, "standard": context.is_standard(shift), "sessionMinutes": shift.session_minutes, "doctors": doctors,
            "items": [{"kind": item.kind, "breakKind": item.break_kind, "start": item.start_time,
                       "end": item.end_time} for item in context.items.get(shift.id, [])]}


# --- staff operations (Shifts screen and Clinical team page) -------------------

class ShiftActionError(ValueError):
    """A shift change needs staff attention; carries overlap warnings or affected bookings."""

    def __init__(self, message: str, *, warnings=None, affected=None, confirm: bool = False, title: str = "",
                 confirm_label: str = "Deactivate"):
        super().__init__(message)
        self.confirm_label = confirm_label
        self.warnings = warnings or []
        self.affected = affected or []
        self.confirm = confirm  # Needs "are you sure" even when no booking is affected.
        self.title = title


UPCOMING = ("PROPOSED", "PENDING_CONFIRMATION", "CONFIRMED")


def _today():
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _prepare(session) -> None:
    """Create the shift tables inside this transaction the first time shifts are edited."""
    create_shift_tables(session.connection())


def affected_bookings(session, allowed: dict[int, set[int]]) -> list[dict]:
    """Upcoming bookings that fall outside the sessions each clinician would now work."""
    from database.models import Appointment, AppointmentStatus, Patient, Therapist
    from scheduling.slot_config import slot_label

    if not allowed:
        return []
    rows = session.execute(
        select(Appointment, Patient.patient_code, Therapist.name)
        .join(Patient, Patient.id == Appointment.patient_id)
        .join(Therapist, Therapist.id == Appointment.therapist_id)
        .where(Appointment.therapist_id.in_(list(allowed)), Appointment.date >= _today(),
               Appointment.status.in_([AppointmentStatus(status) for status in UPCOMING]))
        .order_by(Appointment.date)).all()
    found = []
    for item, code, name in rows:
        sessions = allowed[item.therapist_id]
        if item.slot_index in sessions:
            continue
        # A kept booking that partly overlaps a new session: the planner skips that new session on that day.
        clashes = [slot for slot in sorted(sessions, key=clock_key) if peak_booked({item.slot_index: 1}, slot)]
        found.append({"appointmentId": item.id, "date": item.date.isoformat(), "time": slot_label(item.slot_index),
                      "clinician": name, "patientCode": code,
                      "clashesWith": [slot_label(slot) for slot in clashes]})
    return found


def split_visits(session, before: ShiftContext, after: ShiftContext, location_id: int) -> list[dict]:
    """Upcoming back-to-back visits at this centre that a shift change would split, e.g. a
    short break turned into lunch, or a shift removed whose short break joined two sessions."""
    from database.models import Appointment, AppointmentStatus, Patient, Therapist
    from scheduling.slot_config import slot_label

    rows = session.execute(
        select(Appointment, Patient.patient_code, Therapist.name)
        .join(Patient, Patient.id == Appointment.patient_id)
        .join(Therapist, Therapist.id == Appointment.therapist_id)
        .where(Appointment.location_id == location_id, Appointment.date >= _today(),
               Appointment.status.in_([AppointmentStatus(status) for status in UPCOMING]))
        .order_by(Appointment.date, Appointment.id)).all()
    visits: dict[tuple, list] = {}
    for item, code, name in rows:
        visits.setdefault((item.patient_id, item.date), []).append((item, code, name))
    booked = {item.slot_index for item, _, _ in rows}
    old, new = before.timetable(location_id, booked), after.timetable(location_id, booked)
    found = []
    for parts in visits.values():
        parts.sort(key=lambda part: clock_key(part[0].slot_index))
        slots = [part[0].slot_index for part in parts]
        if len(slots) > 1 and old.chained(slots) and not new.chained(slots):
            first = parts[0][0]
            found.append({"appointmentId": first.id, "date": first.date.isoformat(),
                          "time": f"{slot_label(slots[0])[:5]}–{slot_label(slots[-1])[-5:]}",
                          "clinician": ", ".join(dict.fromkeys(name for _, _, name in parts)),
                          "patientCode": parts[0][1], "clashesWith": [], "splitVisit": True})
    return found


def _context_with(context: ShiftContext, shift, rows=None, status: str | None = None) -> ShiftContext:
    """A copy of ``context`` as if this shift had these rows/status (nothing is saved)."""
    key = shift.id if shift is not None and shift.id is not None else -1
    current = context.shifts.get(key)
    view = SimpleNamespace(id=key, location_id=shift.location_id, is_default=getattr(shift, "is_default", False),
                           name=getattr(shift, "name", ""),
                           status=status or (current.status if current is not None else shift.status))
    items = dict(context.items)
    if rows is not None:
        items[key] = rows
    return ShiftContext(shifts={**context.shifts, key: view}, items=items, assignments=context.assignments,
                        centre_minutes=context.centre_minutes)


def affected_message(affected: list[dict], where: str) -> str:
    """The confirmation text for bookings left outside a clinician's new hours, or visits split."""
    outside = [item for item in affected if not item.get("splitVisit")]
    split = len(affected) - len(outside)
    parts = []
    if outside:
        parts.append(f"{len(outside)} upcoming booking(s) fall outside {where}.")
        clashing = sum(1 for item in outside if item.get("clashesWith"))
        if clashing:
            parts.append(f"{clashing} of them overlap the new times, so the clinician won't be booked "
                         "at the overlapping new time on those days.")
    if split:
        parts.append(f"{split} upcoming back-to-back visit(s) would be split by a lunch break or gap.")
    parts.append("They will stay as booked; review them after saving.")
    return " ".join(parts)


def _doctors_on(session, context: ShiftContext, shift: Shift) -> list[int]:
    """Clinicians who work this shift: assigned to it, or on the centre default without an assignment."""
    from database.models import Therapist

    ids = list(session.scalars(select(Therapist.id).where(Therapist.location_id == shift.location_id)))
    return [therapist_id for therapist_id in ids
            if (context.therapist_shift(therapist_id, shift.location_id) or shift).id == shift.id]


def save_shift(session, *, location_id: int, name: str, items: list[dict], actor: str,
               shift_id: int | None = None, activate: bool = False, acknowledge_affected: bool = False,
               edit_default: bool = False) -> dict:
    """Create or edit a shift (``edit_default`` edits the centre's default "Shift 1"). Overlap warnings allow a draft only; affected bookings need acknowledgement."""
    import json

    from database.models import AuditLog, Location

    location = session.get(Location, location_id)
    if location is None:
        raise ShiftActionError("Choose a centre")
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise ShiftActionError("Enter a shift name of 1 to 80 characters")
    _prepare(session)
    context = load_shift_context(session)
    default = ensure_default_shift(session, context, location_id, actor)
    if shift_id is None and edit_default and default is None:
        raise ShiftActionError("This centre has no active shift. Create a new shift instead.")
    shift = default if (shift_id is None and edit_default) else None
    if shift_id is not None:
        shift = context.shifts.get(shift_id)
        if shift is None or shift.location_id != location_id or shift.status == "archived":
            raise ShiftActionError("This shift no longer exists. Refresh the page.")
        if shift.status == "inactive":
            raise ShiftActionError(f"{shift.name} is deactivated. Reactivate it to use or change it.")
    clash = next((other for other in context.shifts.values() if other.location_id == location_id
                  and other.status != "archived" and other.name.strip().lower() == name.lower()
                  and (shift is None or other.id != shift.id)), None)
    if clash is not None:
        raise ShiftActionError(f"Another shift at this centre is already called “{clash.name}”")
    check = check_shift(context, location_id, items, shift_id=shift.id if shift else None)
    if check.errors:
        raise ShiftActionError(" ".join(check.errors))
    becomes_active = activate or (shift is not None and shift.status == "active")
    if shift is not None and shift.is_default:
        becomes_active = True  # The centre's default shift always stays in use.
    if becomes_active and check.warnings:
        raise ShiftActionError("Fix the overlapping times before this shift can be used for scheduling. "
                               "You can save it as a draft meanwhile.", warnings=check.warnings)
    rows = []
    for position, (kind, break_kind, start, end) in enumerate(sorted(check.rows, key=lambda row: row[2])):
        slot_id = session_slot_id(session, start, end) if kind == "session" else None
        rows.append(ShiftItem(position=position, kind=kind, break_kind=break_kind,
                              start_time=hhmm(start), end_time=hhmm(end), slot_id=slot_id))
    # With no active shift at the centre, the shift put in use becomes its default (and sets its length).
    becomes_default = becomes_active and context.default_shift(location_id) is None
    affected = []
    kept = {row.slot_id for row in rows if row.slot_id is not None}
    if shift is not None and shift.status == "active" and becomes_active:
        doctors = _doctors_on(session, context, shift)
        affected = affected_bookings(session, {doctor: kept for doctor in doctors})
    elif becomes_default:
        from database.models import Therapist
        idle = [therapist_id for therapist_id in session.scalars(select(Therapist.id).where(
            Therapist.location_id == location_id)) if context.therapist_shift(therapist_id, location_id) is None]
        affected = affected_bookings(session, {doctor: kept for doctor in idle})
    if becomes_active:
        # A changed break (short -> lunch) or new lunch can split visits already booked back to back.
        planned = shift or SimpleNamespace(id=None, location_id=location_id, is_default=False, status="active")
        affected += split_visits(session, context, _context_with(context, planned, rows, "active"), location_id)
    if affected and not acknowledge_affected:
        raise ShiftActionError(affected_message(affected, "the edited shift"), affected=affected)
    if shift is None:
        shift = Shift(location_id=location_id, name=name, session_minutes=check.session_minutes,
                      is_default=becomes_default, status="active" if becomes_active else "draft", updated_by=actor)
        session.add(shift)
        session.flush()
        # Clinicians already at the centre are recorded on the default shift, so only
        # clinicians who arrive later (e.g. from the Zoho sync) show "Check shift".
        if default is not None and not becomes_default:
            from database.models import Therapist
            for therapist_id in session.scalars(select(Therapist.id).where(Therapist.location_id == location_id)):
                if therapist_id not in context.assignments:
                    session.add(TherapistShift(therapist_id=therapist_id, shift_id=default.id, assigned_by=actor))
                    context.assignments[therapist_id] = default.id
    else:
        for old in context.items.get(shift.id, []):
            session.delete(old)
        session.flush()
        shift.name = name
        shift.status = "active" if becomes_active else "draft"
        shift.session_minutes = check.session_minutes
        if becomes_default:
            shift.is_default = True
    if becomes_active and not context.length_locked(location_id):
        # The centre's first active shift sets its session length.
        setting = session.get(CentreSessionSetting, location_id)
        if setting is None:
            session.add(CentreSessionSetting(location_id=location_id, session_minutes=check.session_minutes))
        else:
            setting.session_minutes = check.session_minutes
        context.centre_minutes[location_id] = check.session_minutes
    shift.updated_at, shift.updated_by = datetime.utcnow(), actor
    for row in rows:
        row.shift_id = shift.id
    session.add_all(rows)
    session.add(AuditLog(action="shift_saved", entity=f"shift:{shift.id}", user=actor,
                         new_value=json.dumps({"location": location_id, "name": name, "status": shift.status,
                                               "items": [{"kind": r.kind, "break": r.break_kind,
                                                          "start": r.start_time, "end": r.end_time} for r in rows]})))
    session.flush()
    context.shifts[shift.id] = shift
    context.items[shift.id] = rows
    default_id = shift.id if shift.is_default else (default.id if default is not None else None)
    return {"shift": shift_payload(context, shift), "warnings": check.warnings, "defaultShiftId": default_id}


def archive_shift(session, shift_id: int | None, actor: str, *, location_id: int | None = None,
                  acknowledge_affected: bool = False) -> dict:
    """Deactivate a shift after confirmation (``shift_id`` None = the centre's default Shift 1).

    The default shift goes last: once a centre has no active shift its clinicians are not
    scheduled, and the first session of the next shift sets the centre's session length.
    Bookings are never moved or cancelled.
    """
    from database.models import AuditLog

    _prepare(session)
    context = load_shift_context(session)
    if shift_id is None:
        if location_id is None:
            raise ShiftActionError("Choose a shift")
        shift = ensure_default_shift(session, context, location_id, actor)
    else:
        shift = context.shifts.get(shift_id)
    if shift is None or shift.status == "archived":
        raise ShiftActionError("This shift no longer exists. Refresh the page.")
    if shift.status == "inactive":
        raise ShiftActionError(f"{shift.name} is already deactivated.")
    keep = context.is_standard(shift)  # Shift 1 stays in the list (Inactive); other shifts are deleted.
    others = sorted(item.name for item in context.active_shifts(shift.location_id) if item.id != shift.id)
    if shift.is_default and shift.status == "active" and others:
        raise ShiftActionError(f"{shift.name} is this centre's default shift, so it is deactivated last. "
                               f"Deactivate {', '.join(others)} first.")
    if not shift.is_default:
        assigned = [therapist for therapist, assigned_shift in context.assignments.items() if assigned_shift == shift.id]
        if assigned:
            raise ShiftActionError(f"Move the {len(assigned)} clinician(s) on this shift to another shift first")
    affected, parts = [], []
    if shift.status == "active" and shift.is_default:
        doctors = _doctors_on(session, context, shift)
        affected = affected_bookings(session, {doctor: set() for doctor in doctors})
        count = len(doctors)
        parts.append(f"{count} clinician{'' if count == 1 else 's'} won't be scheduled until you add a new shift. "
                     "Its first session sets the session length.")
        if affected:
            parts.append(f"{len(affected)} upcoming booking{'' if len(affected) == 1 else 's'} stay as booked.")
    elif shift.status == "active":
        # Removing a shift can remove the short break that joined a booked visit.
        affected = split_visits(session, context, _context_with(context, shift, status="archived"), shift.location_id)
        parts.append(f"{shift.name} will be deleted and no longer used for scheduling.")
        if affected:
            parts.append(f"{len(affected)} back-to-back visit{'' if len(affected) == 1 else 's'} "
                         "would be split; they stay as booked.")
    else:
        parts.append(f"The draft {shift.name} will be deleted.")
    if keep:
        parts.append(f"{shift.name} stays here as Inactive.")
    if not acknowledge_affected:
        # One line per point (the dialog shows each on its own line).
        raise ShiftActionError("\n".join(parts), affected=affected, confirm=True,
                               title=f"{'Deactivate' if keep else 'Delete'} {shift.name}?",
                               confirm_label="Deactivate" if keep else "Delete")
    shift.status = "inactive" if keep else "archived"
    shift.updated_at, shift.updated_by = datetime.utcnow(), actor
    session.add(AuditLog(action="shift_deactivated" if keep else "shift_archived", entity=f"shift:{shift.id}",
                         user=actor))
    return {"affected": len(affected), "kept": keep}


def reactivate_shift(session, shift_id: int, actor: str, *, acknowledge_affected: bool = False) -> dict:
    """Put a deactivated Shift 1 back in use, after a warning.

    A centre uses one session length, so shifts of another length (active or draft) are
    deleted; shifts that already use Shift 1's length stay, and their clinicians stay on
    them. Clinicians left without a shift work Shift 1. Bookings are never moved.
    """
    from database.models import AuditLog, Therapist

    _prepare(session)
    context = load_shift_context(session)
    shift = context.shifts.get(shift_id)
    if shift is None or shift.status != "inactive":
        raise ShiftActionError("Only a deactivated Shift 1 can be reactivated. Refresh the page.")
    location_id, length = shift.location_id, shift.session_minutes
    others = [item for item in context.shifts.values() if item.location_id == location_id
              and item.id != shift.id and item.status in ("active", "draft")]
    removed = sorted((item for item in others if item.session_minutes != length), key=lambda item: item.name.lower())
    kept = sorted((item for item in others if item.session_minutes == length and item.status == "active"),
                  key=lambda item: item.name.lower())
    removed_ids = {item.id for item in removed}
    # Same overlap check as "Save and use": Shift 1 stays inactive until the clashing times are fixed.
    clashes = session_overlaps(context, [(slot_def(slot).start_time, slot_def(slot).end_time)
                                         for slot in context.sessions(shift.id)], kept)
    if clashes:
        raise ShiftActionError(f"{shift.name} can't be reactivated yet. {' '.join(clashes)} "
                               f"Fix those times, then reactivate {shift.name}.", warnings=clashes)
    current_default = context.default_shift(location_id)
    doctors = list(session.scalars(select(Therapist.id).where(Therapist.location_id == location_id)))
    movers = []
    for doctor in doctors:
        current = context.therapist_shift(doctor, location_id)
        if current is None or current.id in removed_ids:
            movers.append(doctor)
    affected = affected_bookings(session, {doctor: set(context.sessions(shift.id)) for doctor in movers})
    parts = [f"{shift.name} ({length}-minute sessions) will be used for scheduling again."]
    if removed:
        parts.append(f"A centre uses one session length, so these shifts will be deleted: "
                     f"{', '.join(item.name for item in removed)}.")
    if kept:
        parts.append(f"{', '.join(item.name for item in kept)} stay{'s' if len(kept) == 1 else ''} in use.")
    if movers:
        parts.append(f"{len(movers)} clinician(s) will work {shift.name}.")
    if affected:
        parts.append(f"{len(affected)} upcoming booking(s) fall outside {shift.name}; they stay as booked.")
    if not acknowledge_affected:
        raise ShiftActionError(" ".join(parts), affected=affected, confirm=True, title=f"Reactivate {shift.name}?",
                               confirm_label="Reactivate")
    if current_default is not None and current_default.id not in removed_ids:
        # The kept default hands "default" back to Shift 1; its clinicians stay on it.
        for doctor in doctors:
            on_default = context.therapist_shift(doctor, location_id)
            if on_default is None or on_default.id != current_default.id:
                continue
            row = session.get(TherapistShift, doctor)
            if row is None:
                session.add(TherapistShift(therapist_id=doctor, shift_id=current_default.id, assigned_by=actor))
            elif row.shift_id != current_default.id:
                row.shift_id, row.assigned_by, row.assigned_at = current_default.id, actor, datetime.utcnow()
        current_default.is_default = False
    for item in removed:
        item.status = "archived"
        item.updated_at, item.updated_by = datetime.utcnow(), actor
        session.add(AuditLog(action="shift_archived", entity=f"shift:{item.id}", user=actor))
    shift.status, shift.is_default = "active", True
    shift.updated_at, shift.updated_by = datetime.utcnow(), actor
    setting = session.get(CentreSessionSetting, location_id)
    if setting is None:
        session.add(CentreSessionSetting(location_id=location_id, session_minutes=length))
    else:
        setting.session_minutes = length
    session.add(AuditLog(action="shift_reactivated", entity=f"shift:{shift.id}", user=actor))
    return {"shiftId": shift.id, "shiftName": shift.name, "sessionMinutes": length,
            "deleted": [item.name for item in removed], "affected": len(affected)}


def assign_shift(session, therapist_ids: list[int], shift_id: int | None, actor: str, *,
                 acknowledge_affected: bool = False) -> dict:
    """Put clinicians on a shift of their own centre (None = the centre's default shift)."""
    import json

    from database.models import AuditLog, Therapist

    if not isinstance(therapist_ids, list) or not therapist_ids or not all(type(item) is int for item in therapist_ids):
        raise ShiftActionError("Choose at least one clinician")
    _prepare(session)
    context = load_shift_context(session)
    therapists = {item.id: item for item in session.scalars(select(Therapist).where(Therapist.id.in_(therapist_ids)))}
    if len(therapists) != len(set(therapist_ids)):
        raise ShiftActionError("A clinician was not found. Refresh the page.")
    if shift_id is None:
        centres = {item.location_id for item in therapists.values()}
        if len(centres) != 1:
            raise ShiftActionError("Choose clinicians from one centre")
        shift = ensure_default_shift(session, context, next(iter(centres)), actor)
        if shift is None:
            raise ShiftActionError("This centre has no active shift. Create one on the Shifts screen first.")
    else:
        shift = context.shifts.get(shift_id)
        if shift is None or shift.status != "active":
            raise ShiftActionError("Choose an active shift")
    wrong = [item.name for item in therapists.values() if item.location_id != shift.location_id]
    if wrong:
        raise ShiftActionError(f"{shift.name} belongs to a different centre than {', '.join(sorted(wrong)[:3])}")
    allowed = set(context.sessions(shift.id))
    affected = affected_bookings(session, {therapist_id: allowed for therapist_id in therapists})
    if affected and not acknowledge_affected:
        raise ShiftActionError(affected_message(affected, shift.name), affected=affected)
    for therapist_id in therapists:
        row = session.get(TherapistShift, therapist_id)
        if row is None:
            session.add(TherapistShift(therapist_id=therapist_id, shift_id=shift.id, assigned_by=actor))
        else:
            row.shift_id, row.assigned_by, row.assigned_at = shift.id, actor, datetime.utcnow()
    session.add(AuditLog(action="therapist_shift_assigned", entity=f"shift:{shift.id}", user=actor,
                         new_value=json.dumps({"therapists": sorted(therapists)})))
    return {"shiftId": shift.id, "shiftName": shift.name, "assigned": len(therapists), "affected": len(affected)}


def centre_shift_options(session, location_ids) -> dict[str, list[dict]]:
    """Each centre's active shifts for the Clinical team dropdowns (default first)."""
    context = load_shift_context(session)
    options = {}
    for location_id in {item for item in location_ids if item is not None}:
        active = sorted(context.active_shifts(location_id), key=lambda shift: (not shift.is_default, shift.name.lower()))
        fallback = [] if location_id in context.configured_locations() else [
            {"id": None, "name": STANDARD_NAME, "isDefault": True}]
        options[str(location_id)] = ([{"id": shift.id, "name": shift.name, "isDefault": shift.is_default}
                                      for shift in active] or fallback)
    return options


def calendar_shift_data(session, therapists, booked_slots=()) -> dict:
    """What the calendar needs: every session in clock order, each clinician's shift, the filter names.

    ``booked_slots`` are the sessions of the bookings being shown: they always get a calendar
    row, even when no shift uses that time any more, so a saved booking never disappears.
    """
    from scheduling.slot_config import slot_label

    context = load_shift_context(session)
    standard_breaks = [{"start": start, "end": end, "kind": kind}
                       for item, kind, start, end in STANDARD_ITEMS if item == "break"]
    used = {slot.slot_index for slot in SLOTS} | {slot for slot in booked_slots if known_slot(slot)}
    centre_sessions = {}
    for location_id in context.configured_locations():
        order = context.timetable(location_id).order
        centre_sessions[str(location_id)] = list(order)
        used.update(order)
    per_therapist, names = {}, {}
    for therapist in therapists:
        shift = context.therapist_shift(therapist.id, therapist.location_id)
        if shift is None and therapist.location_id in context.configured_locations():
            entry = {"shiftId": None, "shiftName": NO_SHIFT_NAME, "shiftDefault": False, "sessions": [], "breaks": []}
        elif shift is None:
            entry = {"shiftId": None, "shiftName": STANDARD_NAME, "shiftDefault": True,
                     "sessions": [slot.slot_index for slot in SLOTS], "breaks": standard_breaks}
        else:
            entry = {"shiftId": shift.id, "shiftName": shift.name, "shiftDefault": shift.is_default,
                     "sessions": context.sessions(shift.id),
                     "breaks": [{"start": item.start_time, "end": item.end_time, "kind": item.break_kind}
                                for item in context.items.get(shift.id, []) if item.kind == "break"]}
        entry["shiftNeedsCheck"] = (therapist.location_id in context.configured_locations()
                                    and therapist.id not in context.assignments
                                    and len(context.active_shifts(therapist.location_id)) > 1)
        per_therapist[therapist.id] = entry
        names[entry["shiftName"]] = names.get(entry["shiftName"], False) or entry["shiftDefault"]
    ordered = sorted(used, key=lambda slot: (slot_def(slot).start_time, slot_def(slot).end_time, slot))
    return {
        "sessions": [{"id": slot, "start": hhmm(slot_def(slot).start_time), "end": hhmm(slot_def(slot).end_time),
                      "label": slot_label(slot)} for slot in ordered],
        "slotLabels": {str(slot): slot_label(slot) for slot in ordered},
        "centreSessions": centre_sessions,
        "defaultSessions": [slot.slot_index for slot in SLOTS],
        "therapistShifts": per_therapist,
        "shiftOptions": sorted(names, key=lambda name: (not names[name], name.lower())),
    }


def shifts_overview(session, location_id: int | None) -> dict:
    """Centres, their shifts (the original timetable until edited) and clinician counts."""
    from database.models import Location, Therapist

    context = load_shift_context(session)
    locations = list(session.scalars(select(Location).where(Location.active.is_(True)).order_by(Location.name)))
    if location_id is None and locations:
        location_id = locations[0].id
    shifts = []
    if location_id is not None:
        doctors = list(session.scalars(select(Therapist.id).where(Therapist.location_id == location_id,
                                                                  Therapist.active.is_(True))))
        counts: dict[int | None, int] = {}
        for doctor in doctors:
            shift = context.therapist_shift(doctor, location_id)
            key = shift.id if shift else None
            counts[key] = counts.get(key, 0) + 1
        stored = sorted((shift for shift in context.shifts.values()
                         if shift.location_id == location_id and shift.status != "archived"),
                        key=lambda shift: (not shift.is_default, shift.name.lower()))
        shifts = [shift_payload(context, shift, counts.get(shift.id, 0)) for shift in stored]
        configured = location_id in context.configured_locations()
        if not configured:
            shifts.insert(0, {**standard_shift_payload(), "doctors": counts.get(None, 0)})
    else:
        configured, counts = False, {}
    return {"locations": [{"id": item.id, "name": item.name} for item in locations],
            "locationId": location_id,
            "sessionMinutes": (context.session_minutes(location_id) if location_id is not None
                               else SESSION_DURATION_MINUTES),
            # False only when every shift is deactivated: the next shift's first session sets the length.
            "lengthLocked": not configured or context.length_locked(location_id),
            "withoutShift": counts.get(None, 0) if configured else 0,
            "dayStart": hhmm(DAY_START), "dayEnd": hhmm(DAY_END), "shifts": shifts}

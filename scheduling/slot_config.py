from dataclasses import dataclass
from datetime import time
from functools import lru_cache

SESSION_DURATION_MINUTES = 40
WORKING_DAYS = (0, 1, 2, 3, 4, 5)  # Monday through Saturday


@dataclass(frozen=True)
class Slot:
    slot_index: int
    start_time: time
    end_time: time


SLOTS = (
    Slot(0, time(9, 0), time(9, 40)),
    Slot(1, time(9, 40), time(10, 20)),
    Slot(2, time(10, 35), time(11, 15)),
    Slot(3, time(11, 15), time(11, 55)),
    Slot(4, time(11, 55), time(12, 35)),
    Slot(5, time(13, 15), time(13, 55)),
    Slot(6, time(13, 55), time(14, 35)),
    Slot(7, time(14, 35), time(15, 15)),
    Slot(8, time(15, 30), time(16, 10)),
    Slot(9, time(16, 10), time(16, 50)),
)

# A short operational break can remain consecutive; lunch cannot.
CONSECUTIVE_TO_NEXT = {
    0: True,
    1: True,
    2: True,
    3: True,
    4: False,
    5: True,
    6: True,
    7: True,
    8: True,
    9: False,
}


def valid_consecutive_block(start_slot: int, length: int) -> bool:
    if length < 1 or start_slot < 0 or start_slot + length > len(SLOTS):
        return False
    return all(CONSECUTIVE_TO_NEXT[index] for index in range(start_slot, start_slot + length - 1))


# --- Session catalogue -------------------------------------------------------
# Sessions 0-9 are the original timetable and keep their meaning forever, so saved
# bookings never need renumbering. Shift sessions at other times get new, stable
# numbers (10, 11, ...). A session number is an identity, NOT a position: time
# order always comes from the clock (see Timetable).
DAY_START = time(7, 0)
DAY_END = time(21, 0)
# Numbers for new session times encode the times themselves (the same in every
# database and process): ENCODED_BASE + start minute * 1440 + end minute.
ENCODED_BASE = 100_000
_CATALOG: dict[int, Slot] = {slot.slot_index: slot for slot in SLOTS}


def encode_slot(start: time, end: time) -> int:
    """The session number for these times (a built-in number when they match 0-9)."""
    for slot in SLOTS:
        if slot.start_time == start and slot.end_time == end:
            return slot.slot_index
    return ENCODED_BASE + (start.hour * 60 + start.minute) * 1440 + end.hour * 60 + end.minute


def _decode(slot_index: int) -> Slot | None:
    if slot_index < ENCODED_BASE:
        return None
    start, end = divmod(slot_index - ENCODED_BASE, 1440)
    if not 0 <= start < end < 1440:
        return None
    return Slot(slot_index, time(start // 60, start % 60), time(end // 60, end % 60))


def slot_def(slot_index: int) -> Slot:
    """The session with this number (built-in 0-9, or a shift session decoded from its number)."""
    if type(slot_index) is int:
        found = _CATALOG.get(slot_index) or _decode(slot_index)
        if found is not None:
            return found
    raise ValueError(f"Unknown session {slot_index!r}")


def known_slot(slot_index: object) -> bool:
    return type(slot_index) is int and (slot_index in _CATALOG or _decode(slot_index) is not None)


def register_slots(slots) -> None:
    """Add stored shift sessions to the catalogue; built-in 0-9 cannot change."""
    for slot in slots:
        if slot.slot_index < len(SLOTS) and _CATALOG[slot.slot_index] != slot:
            raise ValueError("Built-in sessions 0-9 cannot be redefined")
        _CATALOG[slot.slot_index] = slot


def catalog_slots() -> list[Slot]:
    """Every known session in clock order."""
    return sorted(_CATALOG.values(), key=lambda slot: (slot.start_time, slot.end_time, slot.slot_index))


def slot_by_times(start: time, end: time) -> Slot | None:
    return slot_def(encode_slot(start, end)) if end > start else None


def slot_label(slot_index: int) -> str:
    slot = slot_def(slot_index)
    return f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}"


def minutes(value: time) -> int:
    return value.hour * 60 + value.minute


@lru_cache(maxsize=None)
def _window(slot_index: int) -> tuple[int, int] | None:
    try:
        slot = slot_def(slot_index)
    except ValueError:
        return None
    return minutes(slot.start_time), minutes(slot.end_time)


def slot_window(slot_index: object) -> tuple[int, int] | None:
    """(start minute, end minute) of a session, or None for an unknown session."""
    return _window(slot_index) if type(slot_index) is int else None


def _moments(booked: dict, start: int, end: int) -> set[int]:
    """Start moments inside [start, end) where the number of bookings can change."""
    return {start} | {window[0] for window in map(slot_window, booked) if window and start < window[0] < end}


def _at(booked: dict, moment: int) -> int:
    total = 0
    for slot, count in booked.items():
        window = slot_window(slot)
        if window and window[0] <= moment < window[1]:
            total += count
    return total


def peak_booked(booked: dict, slot_index: int) -> int:
    """Most bookings at any moment of this session, given {session: count} for one clinician,
    child or room on one day. Sessions are compared by clock time, so 9:00–9:40 and
    9:20–10:00 clash even though they are different sessions."""
    window = slot_window(slot_index)
    if window is None:
        return booked.get(slot_index, 0)
    return max(_at(booked, moment) for moment in _moments(booked, *window))


def overbooked_moments(booked: dict, capacity: int) -> int:
    """How many moments have more than ``capacity`` bookings at once (clock-time overlap)."""
    known = {slot: count for slot, count in booked.items() if slot_window(slot)}
    unknown = sum(1 for slot, count in booked.items() if slot not in known and count > capacity)
    moments = {slot_window(slot)[0] for slot in known}
    return unknown + sum(1 for moment in moments if _at(known, moment) > capacity)


@dataclass(frozen=True)
class Timetable:
    """One centre's sessions in clock order and which neighbours a visit may chain.

    ``links`` holds (a, b) pairs where session b directly follows a with no gap or
    only a short break. Lunch, unexplained gaps and the end of the day break a chain.
    """

    order: tuple[int, ...]
    links: frozenset[tuple[int, int]]

    def block(self, start_slot: int, length: int) -> tuple[int, ...] | None:
        """Sessions for a back-to-back visit starting at ``start_slot``, or None."""
        if length < 1 or start_slot not in self.order:
            return None
        position = self.order.index(start_slot)
        sessions = self.order[position:position + length]
        if len(sessions) < length:
            return None
        if any((a, b) not in self.links for a, b in zip(sessions, sessions[1:])):
            return None
        return sessions

    def chained(self, sessions) -> bool:
        """True when these saved sessions form one back-to-back visit in this centre."""
        ordered = sorted(sessions, key=lambda item: self.order.index(item) if item in self.order else -1)
        if not ordered or any(item not in self.order for item in ordered):
            return False
        return self.block(ordered[0], len(ordered)) == tuple(ordered)

    def gap_sessions(self, first: int, last: int) -> list[int]:
        """Sessions strictly between two sessions of this centre, in clock order."""
        if first not in self.order or last not in self.order:
            return []
        a, b = sorted((self.order.index(first), self.order.index(last)))
        return list(self.order[a + 1:b])


DEFAULT_TIMETABLE = Timetable(
    order=tuple(slot.slot_index for slot in SLOTS),
    links=frozenset((index, index + 1) for index in range(len(SLOTS) - 1) if CONSECUTIVE_TO_NEXT[index]),
)


def timetable_for(problem, location_id: int) -> Timetable:
    """A centre's timetable; centres without configured shifts use the original one."""
    return (getattr(problem, "timetables", None) or {}).get(location_id, DEFAULT_TIMETABLE)

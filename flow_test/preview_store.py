"""Reviewed schedule previews kept in the workflow database (review item #28).

With previews in process memory, a preview made on one app copy could not be
saved through another, and a restart lost every preview. This store keeps them
in ``flow_test_previews`` so any copy can save them. Previews expire after
``TTL``; they are short-lived review drafts, never clinical records.

Values are stored as tagged JSON (never pickle): dates, datetimes, tuples, sets
and the scheduling result dataclasses round-trip exactly.
"""

from collections.abc import MutableMapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta
import json
from typing import Any, Iterator

from sqlalchemy import delete, select

from flow_test.models import FlowPreview
from scheduling.types import ScheduledAppointment, ScheduleResult, UnmetRequirement

TTL = timedelta(hours=24)
_DATACLASSES = {cls.__name__: cls for cls in (ScheduleResult, ScheduledAppointment, UnmetRequirement)}


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if name not in _DATACLASSES:
            raise TypeError(f"Previews cannot store {name}")
        return {"__dc__": name, "fields": {f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        return {"__set__": [_encode(item) for item in sorted(value, key=repr)]}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            return {"__dict__": [[_encode(key), _encode(item)] for key, item in value.items()]}
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Previews cannot store {type(value).__name__}")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__dc__" in value:
        cls = _DATACLASSES[value["__dc__"]]
        return cls(**{key: _decode(item) for key, item in value["fields"].items()})
    if "__datetime__" in value:
        return datetime.fromisoformat(value["__datetime__"])
    if "__date__" in value:
        return date.fromisoformat(value["__date__"])
    if "__tuple__" in value:
        return tuple(_decode(item) for item in value["__tuple__"])
    if "__set__" in value:
        return {_decode(item) for item in value["__set__"]}
    if "__dict__" in value:
        return {_decode(key): _decode(item) for key, item in value["__dict__"]}
    return {key: _decode(item) for key, item in value.items()}


def encode_preview(value: dict) -> str:
    return json.dumps(_encode(value), ensure_ascii=False, separators=(",", ":"))


def decode_preview(text: str) -> dict:
    return _decode(json.loads(text))


class PreviewStore(MutableMapping):
    """Dict-like, database-backed preview map shared by every app copy.

    A preview read with ``get`` is a copy: after changing it, assign it back
    (``store[preview_id] = preview``) to save the change.
    """

    def __init__(self, flow) -> None:
        # Resolved on use: the runtime's flow owns the session factory.
        self._flow = flow

    @property
    def _sessions(self):
        return self._flow.sessions

    def _fresh(self, row: FlowPreview | None) -> bool:
        return row is not None and row.created_at > datetime.utcnow() - TTL

    def __getitem__(self, key: str) -> dict:
        with self._sessions() as session:
            row = session.get(FlowPreview, str(key))
            if not self._fresh(row):
                raise KeyError(key)
            return decode_preview(row.data)

    def __setitem__(self, key: str, value: dict) -> None:
        data = encode_preview(value)
        with self._sessions.begin() as session:
            # Drop expired drafts as new ones are written; this keeps the table small.
            session.execute(delete(FlowPreview).where(FlowPreview.created_at <= datetime.utcnow() - TTL))
            row = session.get(FlowPreview, str(key))
            if row is None:
                session.add(FlowPreview(id=str(key), created_at=datetime.utcnow(), data=data))
            else:
                row.data = data

    def __delitem__(self, key: str) -> None:
        with self._sessions.begin() as session:
            if session.execute(delete(FlowPreview).where(FlowPreview.id == str(key))).rowcount == 0:
                raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self._sessions() as session:
            keys = list(session.scalars(select(FlowPreview.id).where(
                FlowPreview.created_at > datetime.utcnow() - TTL).order_by(FlowPreview.created_at)))
        return iter(keys)

    def __len__(self) -> int:
        return len(list(iter(self)))

    def clear(self) -> None:
        """Invalidate every preview (after an import, reset or export prepare)."""
        with self._sessions.begin() as session:
            session.execute(delete(FlowPreview))

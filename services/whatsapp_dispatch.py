"""Shared, durable rate and outage controls. Contains no message content."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time

from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, inspect, select, text
from sqlalchemy.engine import Engine, make_url

from database.workflow_lock import WorkflowLock
from services.whatsapp_service import WhatsAppDeliveryError


METADATA = MetaData()
CONTROL = Table("whatsapp_dispatch_state", METADATA,
                Column("phone_id", String(64), primary_key=True), Column("state", Text, nullable=False))


def integer_setting(name: str, default: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


class DispatchControl:
    """All senders of a business number must use the same control database.

    PostgreSQL uses a dedicated schema, shared across workflow schemas. SQLite
    uses a sidecar so test namespace ownership checks remain unchanged.
    """

    def __init__(self, queue_engine: Engine):
        configured = os.getenv("WHATSAPP_DISPATCH_DATABASE_URL", "").strip()
        url = make_url(configured) if configured else queue_engine.url
        if url.get_backend_name() == "sqlite" and not configured:
            path = url.database
            url = url.set(database=(str(Path(path).resolve()) + ".dispatch.sqlite")
                          if path and path != ":memory:" else ":memory:")
        options = {"hide_parameters": True, "pool_pre_ping": True}
        self.schema = "whatsapp_dispatch" if url.get_backend_name() == "postgresql" else None
        if self.schema:
            options.update(execution_options={"schema_translate_map": {None: self.schema}},
                           connect_args={"connect_timeout": 15})
        elif url.get_backend_name() == "sqlite":
            options["connect_args"] = {"check_same_thread": False}
        else:
            raise ValueError("Dispatch controls require PostgreSQL or SQLite")
        self.engine = create_engine(url, **options)
        self.rate = integer_setting("WHATSAPP_SENDS_PER_SECOND", 5, 1000)
        self.threshold = integer_setting("WHATSAPP_OUTAGE_RECIPIENTS", 3, 1000)
        if self.threshold < 2:
            raise ValueError("WHATSAPP_OUTAGE_RECIPIENTS must be at least 2 distinct parents")
        self.window = integer_setting("WHATSAPP_OUTAGE_WINDOW_SECONDS", 60, 3600)
        self.cooldown = integer_setting("WHATSAPP_OUTAGE_PAUSE_SECONDS", 30, 3600)
        # Global control lock is independent of workflow schema and queue locks.
        self.lock = WorkflowLock(self.engine, "control", self.schema)
        if self.schema is None:
            with self.lock, self.engine.begin() as connection:
                METADATA.create_all(connection)

    def prepare(self) -> None:
        """Explicit additive provisioning; never touches workflow/clinical rows."""
        with self.lock, self.engine.begin() as connection:
            if self.schema:
                connection.execute(text('CREATE SCHEMA IF NOT EXISTS whatsapp_dispatch'))
            METADATA.create_all(connection)

    def check(self) -> None:
        if not inspect(self.engine).has_table(CONTROL.name, schema=self.schema):
            raise ValueError("Provision dispatch controls with python -m services.whatsapp_dispatch --init before starting senders")

    @contextmanager
    def state(self, phone_id: str):
        with self.lock, self.engine.begin() as connection:
            raw = connection.scalar(select(CONTROL.c.state).where(CONTROL.c.phone_id == phone_id))
            saved = json.loads(raw) if raw else {"generation": 0}
            now = (float(connection.scalar(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")))
                   if self.schema else time.time())
            yield saved, now
            encoded = json.dumps(saved)
            if raw is None:
                connection.execute(CONTROL.insert().values(phone_id=phone_id, state=encoded))
            else:
                connection.execute(CONTROL.update().where(CONTROL.c.phone_id == phone_id).values(state=encoded))

    @staticmethod
    def recipient_key(recipient: str) -> str:
        return hashlib.sha256(recipient.encode()).hexdigest()

    def reserve(self, phone_id: str, recipient: str, owner: str, *, priority: bool = False) -> dict | None:
        """Reserve immediately or return; never sleep holding a queue lock."""
        with self.state(phone_id) as (state, now):
            starts = [stamp for stamp in state.get("starts", []) if stamp > now - 1]
            parents = {key: until for key, until in state.get("parents", {}).items() if until > now}
            inflight = {key: item for key, item in state.get("inflight", {}).items() if item["until"] > now}
            key = self.recipient_key(recipient)
            if key in parents or key in inflight:
                return None
            if priority:
                state["priority_until"] = now + 1
            if (state.get("open_until", 0) > now or state.get("probe_until", 0) > now
                    or len(starts) >= self.rate or (not priority and state.get("priority_until", 0) > now)):
                return None
            probe = bool(state.get("open_until"))
            if probe:
                # Only one probe may be in flight across processes. An abandoned
                # probe can be replaced after two minutes; its job is not resent.
                state.update(probe_until=now + 120, probe_owner=owner)
            if priority:
                state.pop("priority_until", None)
            state["starts"] = starts + [now]
            parents[key] = now + 6
            state["parents"] = parents
            inflight[key] = {"owner": owner, "until": now + 120}
            state["inflight"] = inflight
            return {"generation": state["generation"], "probe": probe, "owner": owner}

    def outcome(self, phone_id: str, recipient: str, ticket: dict, exc: Exception | None) -> None:
        provider_failure = (isinstance(exc, (WhatsAppDeliveryError, OSError)) and (
            not isinstance(exc, WhatsAppDeliveryError) or not exc.rejected or
            exc.code in {2, 130429, 131000, 131016}))
        with self.state(phone_id) as (state, now):
            key = self.recipient_key(recipient)
            if state.get("inflight", {}).get(key, {}).get("owner") == ticket["owner"]:
                state["inflight"].pop(key, None)
                state.setdefault("parents", {})[key] = now + 6
            # A late result from before an outage must not close a newer circuit.
            if ticket["generation"] != state["generation"]:
                return
            if ticket["probe"]:
                if state.get("probe_owner") != ticket["owner"]:
                    return
                state.pop("probe_until", None)
                state.pop("probe_owner", None)
                state["generation"] += 1
                if provider_failure:
                    state["open_until"] = now + self.cooldown
                else:
                    state.pop("open_until", None)
                    state["failures"] = []
                return
            failures = [item for item in state.get("failures", []) if item[0] > now - self.window]
            if provider_failure:
                failures.append([now, self.recipient_key(recipient)])
            state["failures"] = failures
            if len({item[1] for item in failures}) >= self.threshold:
                state.update(open_until=now + self.cooldown, generation=state["generation"] + 1)

    def summary(self, phone_id: str) -> dict:
        with self.state(phone_id) as (state, now):
            return {"open_until": state.get("open_until"), "probing": state.get("probe_until", 0) > now}


def main() -> None:
    import config  # Load local environment without logging credentials.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create only the shared dispatch-control schema/table")
    args = parser.parse_args()
    url = os.getenv("WHATSAPP_DISPATCH_DATABASE_URL")
    if not url:
        parser.error("Set WHATSAPP_DISPATCH_DATABASE_URL to the shared control database")
    queue = create_engine(url, hide_parameters=True)
    control = DispatchControl(queue)
    try:
        if args.init:
            control.prepare()
        control.check()
        print("Dispatch control storage ready. No messages sent.")
    finally:
        control.engine.dispose()
        queue.dispose()


if __name__ == "__main__":
    main()

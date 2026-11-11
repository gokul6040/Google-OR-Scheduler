"""Reentrant workflow mutexes shared by application processes.

PostgreSQL session advisory locks span the queue's short transactions and HTTP
sends. Each lock keeps one dedicated connection, outside the application pool,
and reuses it for every acquisition instead of opening a new (TLS) connection
each time. A dropped connection is replaced before the lock is taken; it is never
replaced while the lock is held, because the server releases a session's advisory
locks when that session ends. SQLite previews use OS file locks; in-memory tests
stay local. Always acquire workflow before inbound when both are needed.
"""

import hashlib
import os
from pathlib import Path
import threading
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

# libpq keepalives detect a silently dropped idle connection (for example an
# Azure idle timeout) so the next acquisition reconnects instead of hanging.
KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}


class WorkflowLock:
    """Context-manager lock scoped to a database, schema and operation lane."""

    def __init__(self, engine: Engine, lane: str, schema: str | None = None):
        self._local = threading.RLock()
        self._depth = 0
        self._owner = None
        self._resource = None
        self._lane = lane
        self._engine = engine
        self._schema = schema
        self._lock_engine = None
        self._connection = None  # Reused PostgreSQL session; guarded by self._local.
        self._key = None
        self.connections_opened = 0  # Diagnostic counter for tests and load checks.
        if engine.dialect.name == "postgresql":
            self._lock_engine = create_engine(
                engine.url, poolclass=NullPool, hide_parameters=True,
                connect_args={"connect_timeout": 15, **KEEPALIVE},
            )

    def __enter__(self):
        self.acquire()
        return self

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Match RLock's public API, including timed/nonblocking acquisition."""
        deadline = time.monotonic() + timeout if timeout >= 0 else None
        if not self._local.acquire(blocking, timeout):
            return False
        try:
            if self._depth == 0 and not self._acquire_shared(blocking, deadline):
                self._local.release()
                return False
            self._depth += 1
            self._owner = threading.get_ident()
            return True
        except BaseException:
            self._local.release()
            raise

    @staticmethod
    def _wait(blocking, deadline):
        if not blocking or (deadline is not None and time.monotonic() >= deadline):
            return False
        time.sleep(0.02)
        return True

    def _discard_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing a broken session is best effort
                pass

    def _session(self):
        """Return the reusable lock session, opening it only when needed."""
        if self._connection is None or self._connection.closed or self._connection.invalidated:
            self._discard_connection()
            connection = self._lock_engine.connect()
            self.connections_opened += 1
            try:
                if self._key is None:
                    schema = self._schema or connection.scalar(text("select current_schema()"))
                    identity = f"therapy-scheduler:{schema}:{self._lane}"
                    self._key = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8],
                                               "big", signed=True)
                connection.commit()
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    def _lock_postgres(self, connection, blocking, deadline) -> bool:
        if blocking and deadline is None:
            connection.execute(text("select pg_advisory_lock(:key)"), {"key": self._key})
        else:
            while not connection.scalar(text("select pg_try_advisory_lock(:key)"), {"key": self._key}):
                connection.commit()
                if not self._wait(blocking, deadline):
                    return False
        connection.commit()
        return True

    def _acquire_shared(self, blocking, deadline):
        if self._lock_engine is not None:
            for attempt in (1, 2):
                try:
                    connection = self._session()
                    if not self._lock_postgres(connection, blocking, deadline):
                        return False
                    self._resource = connection
                    break
                except DBAPIError as exc:
                    # A dropped idle session is replaced once. The lock was not
                    # held on it (it failed while being taken), so nothing leaks.
                    self._discard_connection()
                    if attempt == 2 or not exc.connection_invalidated:
                        raise
                except BaseException:
                    self._discard_connection()
                    raise
        elif self._engine.dialect.name == "sqlite":
            database = self._engine.url.database
            if not database or database == ":memory:":
                return True
            path = Path(database).resolve()
            handle = open(str(path) + f".{self._lane}.lock", "a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    while True:
                        handle.seek(0)
                        try:
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError as exc:
                            if exc.errno not in {11, 13, 36}:
                                raise
                            if not self._wait(blocking, deadline):
                                handle.close()
                                return False
                else:
                    import fcntl

                    while True:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if not self._wait(blocking, deadline):
                                handle.close()
                                return False
                self._resource = handle
            except BaseException:
                handle.close()
                raise
        else:
            raise ValueError("Workflow coordination requires PostgreSQL or SQLite")
        return True

    def __exit__(self, *args):
        self.release()

    def release(self) -> None:
        """Release only the owning thread's outermost shared lock."""
        if self._owner != threading.get_ident() or self._depth == 0:
            raise RuntimeError("Cannot release an unowned workflow lock")
        try:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
            if self._depth == 0 and self._resource is not None:
                resource, self._resource = self._resource, None
                if self._lock_engine is not None:
                    try:
                        released = resource.scalar(text("select pg_advisory_unlock(:key)"), {"key": self._key})
                        resource.commit()
                        if not released:
                            # The session no longer held the lock; never reuse it.
                            self._discard_connection()
                    except BaseException:
                        # Ending the session releases its advisory lock on the server.
                        self._discard_connection()
                        raise
                else:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            resource.seek(0)
                            msvcrt.locking(resource.fileno(), msvcrt.LK_UNLCK, 1)
                    finally:
                        # File locks are released on close.
                        resource.close()
        finally:
            self._local.release()

    def close(self) -> None:
        """Close the reusable lock session (for shutdown). Never call while held."""
        with self._local:
            if self._depth:
                raise RuntimeError("Cannot close a held workflow lock")
            self._discard_connection()
            if self._lock_engine is not None:
                self._lock_engine.dispose()


def workflow_locks(engine: Engine, schema: str | None = None) -> tuple[WorkflowLock, WorkflowLock]:
    """Keep callback persistence independent of a slow outbound HTTP request."""
    return WorkflowLock(engine, "workflow", schema), WorkflowLock(engine, "inbound", schema)

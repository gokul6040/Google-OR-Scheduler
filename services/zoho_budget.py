"""Host-shared Zoho pacing across source reads, writes and reconciliation.

One lock spans the response so clients cannot exceed concurrency. PostgreSQL
coordinates replicas using the configured shared database. SQLite clients on
different hosts must share ZOHO_CALL_BUDGET_FILE or use one integration worker.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from sqlalchemy import Column, Float, MetaData, String, Table, create_engine, select, text

from database.workflow_lock import WorkflowLock


@contextmanager
def call_budget(url: str):
    """Space Creator calls at 48/minute, including calls in other processes."""
    parsed = urlsplit(url)
    if "/creator/" not in parsed.path:
        yield
        return
    from config import DATABASE_URL

    database_url = os.getenv("ZOHO_BUDGET_DATABASE_URL", DATABASE_URL)
    if database_url.startswith("postgresql"):
        # All replicas use the same database advisory lock and server clock.
        engine = create_engine(database_url, hide_parameters=True, pool_pre_ping=True)
        metadata = MetaData()
        budget = Table("zoho_api_call_budget", metadata,
                       Column("account", String(64), primary_key=True), Column("next_call", Float, nullable=False))
        parts = parsed.path.split("/")
        key = hashlib.sha256(f"{parsed.netloc}:{parts[4]}".encode()).hexdigest()
        try:
            with WorkflowLock(engine, "zoho-api-budget"):
                with engine.begin() as connection:
                    metadata.create_all(connection)
                    due = connection.scalar(select(budget.c.next_call).where(budget.c.account == key))
                    now = float(connection.scalar(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")))
                    if due and due > now:
                        time.sleep(due - now)
                    now = float(connection.scalar(text("SELECT EXTRACT(EPOCH FROM clock_timestamp())")))
                    if due is None:
                        connection.execute(budget.insert().values(account=key, next_call=now + 1.25))
                    else:
                        connection.execute(budget.update().where(budget.c.account == key).values(next_call=now + 1.25))
                yield
        finally:
            engine.dispose()
        return
    path = Path(os.getenv("ZOHO_CALL_BUDGET_FILE", str(Path(tempfile.gettempdir()) / "therapy-zoho-budget.json")))
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine("sqlite:///" + str(path) + ".coordination")
    lock = WorkflowLock(engine, "zoho")
    # Same account shares a budget even across app/report endpoints and tokens.
    parts = parsed.path.split("/")
    owner = parts[4] if len(parts) > 4 else "unknown"
    key = hashlib.sha256(f"{parsed.netloc}:{owner}".encode()).hexdigest()
    try:
        with lock:
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
            except (ValueError, OSError):
                # A corrupt counter must fail closed, not permit unpaced calls.
                raise ValueError("Zoho call budget is unavailable; repair its storage before retrying") from None
            delay = max(0, float(state.get(key, 0)) - time.time())
            if delay:
                time.sleep(delay)
            state[key] = time.time() + 1.25
            temporary = path.with_suffix(".new")
            temporary.write_text(json.dumps(state))
            temporary.replace(path)
            yield
    finally:
        engine.dispose()

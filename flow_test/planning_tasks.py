"""Database-backed preview jobs, executed in the app without external services.

The planning advisory/file lock serializes searches across replicas. Polling
restarts a consumer after an app restart; an interrupted preview is safe to
recompute because it never saves appointments or sends messages.
"""

from dataclasses import asdict
from datetime import date, datetime
import json
import threading
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from config import SolverSettings
from database.workflow_lock import WorkflowLock
from flow_test.models import FlowJob

_start_lock = threading.Lock()


class PreviewRunner:
    """Run unsaved daily searches serially across application replicas."""

    def __init__(self, instance: Any) -> None:
        self.instance = instance
        flow = instance.flow
        self.lock = WorkflowLock(flow.engine, "planning", flow.database_schema)
        self.thread = None

    def start(self) -> None:
        with _start_lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, name="schedule-planning", daemon=True)
                self.thread.start()

    def run(self) -> None:
        if not self.lock.acquire(blocking=False):
            return  # Another app copy is already working; polling checks again.
        flow = self.instance.flow
        try:
            while True:
                with flow.lock, flow.sessions.begin() as session:
                    job = session.scalar(select(FlowJob).where(
                        FlowJob.kind == "PLANNING", FlowJob.status.in_(("QUEUED", "PROCESSING")),
                    ).order_by(FlowJob.id).limit(1))
                    if job is None:
                        return
                    ident, payload = job.id, json.loads(job.payload)
                    job.status, job.locked_by = "PROCESSING", flow.worker_id
                    job.attempts += 1
                try:
                    from flow_test.shared_ui import build_daily_preview
                    result = build_daily_preview(self.instance, date.fromisoformat(payload["date"]),
                                                 payload.get("patients"), SolverSettings(**payload["settings"]))
                    payload["previewId"] = result["previewId"]
                    status, error = "DONE", None
                except Exception as exc:
                    # Validation errors are useful; unexpected errors can include
                    # SQL parameters or credentials and must not reach the browser.
                    import web_app
                    error = str(exc) if isinstance(exc, (ValueError, web_app.ApiError)) else "Planning failed. Generate a fresh preview."
                    status = "FAILED"
                with flow.lock, flow.sessions.begin() as session:
                    job = session.get(FlowJob, ident)
                    job.status, job.error = status, error
                    job.payload = json.dumps(payload)
                    job.locked_by, job.locked_until = None, None
        finally:
            self.lock.release()


def runner(instance: Any) -> PreviewRunner:
    """Resolve the app copy's consumer, starting it separately from the request."""
    with _start_lock:
        if not hasattr(instance, "planning_runner"):
            instance.planning_runner = PreviewRunner(instance)
        return instance.planning_runner


def submit_preview(instance: Any, target: date, patients: list[str] | None, settings: SolverSettings) -> dict:
    """Enqueue at most one identical pending search; the response contains no PHI."""
    flow = instance.flow
    payload = {"date": target.isoformat(), "patients": patients, "settings": asdict(settings)}
    with flow.lock, flow.sessions.begin() as session:
        # An import or staff edit during an existing search must enqueue a fresh
        # snapshot, rather than deduplicating onto the now-stale search.
        _, payload["inputVersion"] = flow._daily_inputs(session, target, patients)
        pending = list(session.scalars(select(FlowJob).where(
            FlowJob.kind == "PLANNING", FlowJob.status.in_(("QUEUED", "PROCESSING")))))
        job = next((job for job in pending if json.loads(job.payload) == payload), None)
        if job is None:
            if len(pending) >= 10:
                raise ValueError("Planning queue is busy. Wait for the existing previews to finish.")
            job = FlowJob(event_key=f"planning:{uuid4().hex}", kind="PLANNING", payload=json.dumps(payload),
                          status="QUEUED", created_at=datetime.utcnow())
            session.add(job)
            session.flush()
        response = {"jobId": job.id, "status": job.status}
    runner(instance).start()
    return response


def preview_status(instance: Any, ident: int) -> dict:
    """Read from shared storage, so polling may reach any app replica."""
    import web_app

    with instance.flow.sessions() as session:
        job = session.get(FlowJob, ident)
        if job is None or job.kind != "PLANNING":
            raise web_app.ApiError(404, "Planning job not found")
        result = {"jobId": ident, "status": job.status}
        payload = json.loads(job.payload)
        if job.status == "DONE":
            preview = instance.previews.get(payload["previewId"])
            if preview is None:
                return {**result, "status": "FAILED", "error": "Preview expired or inputs were imported. Generate again."}
            result["preview"] = preview["response"]
        elif job.status == "FAILED":
            result["error"] = job.error
    if result["status"] in {"QUEUED", "PROCESSING"}:
        runner(instance).start()
    return result

"""Durable manual schedule finalisation/export, isolated from the source importer."""

from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Appointment, AppointmentStatus, AuditLog, Patient, Therapist
from flow_test.models import FlowExportItem, FlowInbox, FlowJob, FlowOffer, FlowOfferAppointment, FlowSetting
from flow_test.zoho_import import put_setting, require_reconciled_source
from scheduling.slot_config import SLOTS

if TYPE_CHECKING:
    from flow_test.service import FlowTest


TARGET = "nidhin.kumar_turnb/test-gokul/Scheduler_Report"
PREFIX = "zoho_scheduler_export"
FINAL = {AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ist_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")


def setting(session: Session, key: str, default: Any = None) -> Any:
    item = session.get(FlowSetting, f"{PREFIX}:{key}")
    return json.loads(item.value) if item else default


def store(session: Session, key: str, value: Any) -> None:
    put_setting(session, f"{PREFIX}:{key}", value)


def batch_for_date(session: Session, target: date) -> dict | None:
    batch = setting(session, f"batch:{target.isoformat()}")
    legacy = setting(session, "batch")
    return batch or (legacy if legacy and legacy["date"] == target.isoformat() else None)


def batch_for_id(session: Session, identifier: str) -> dict | None:
    day = setting(session, f"batchid:{identifier}")
    if day:
        return setting(session, f"batch:{day}")
    legacy = setting(session, "batch")
    return legacy if legacy and legacy["id"] == identifier else None


def store_batch(session: Session, batch: dict) -> None:
    store(session, f"batch:{batch['date']}", batch)
    store(session, f"batchid:{batch['id']}", batch["date"])
    legacy = session.get(FlowSetting, f"{PREFIX}:batch")
    if legacy and json.loads(legacy.value)["id"] == batch["id"]:
        session.delete(legacy)


def offers_on(session: Session, target: date, states: tuple[str, ...] | None = None) -> list[FlowOffer]:
    """Offers covering any appointment currently dated ``target`` (indexed link table).

    Equivalent to scanning every offer for an appointment on that date, including
    a visit moved to it after its offer was made, but reads only that date's rows.
    """
    day_ids = select(Appointment.id).where(Appointment.date == target)
    tokens = select(FlowOfferAppointment.token).where(FlowOfferAppointment.appointment_id.in_(day_ids))
    query = select(FlowOffer).where(FlowOffer.token.in_(tokens))
    if states is not None:
        query = query.where(FlowOffer.state.in_(states))
    return list(session.scalars(query.order_by(FlowOffer.created_at, FlowOffer.token)))


def finalise_unanswered(flow: "FlowTest", session: Session, target: date, *, dry_run: bool = False) -> int:
    """Manual cutoff assumes acceptance only for sent visits with no parent reply."""
    assumed = 0
    offers = offers_on(session, target, ("PENDING", "EXPIRED"))
    if not offers:
        return 0
    inbound = list(session.scalars(select(FlowJob).where(FlowJob.kind == "INBOUND",
        FlowJob.created_at >= min(o.created_at for o in offers))))
    inbound_by_parent = {}
    for job in inbound:
        inbound_by_parent.setdefault(json.loads(job.payload).get("from"), []).append(job)
    codes = {o.patient_code for o in offers}
    # Only items that can matter: this date's children, or unmatched (no child) items.
    unresolved = list(session.scalars(select(FlowInbox).where(
        FlowInbox.resolved == 0, (FlowInbox.patient_code.in_(codes)) | FlowInbox.patient_code.is_(None))))
    active_codes = set(session.scalars(select(Patient.patient_code).where(
        Patient.active.is_(True), Patient.patient_code.in_(codes))))
    sent_states = ("SENT", "SIMULATED") if flow.is_test and not flow.live else ("SENT",)
    unsubscribed = flow.unsubscribed_numbers(session) if hasattr(flow, "unsubscribed_numbers") else {}
    for offer in offers:
        if offer.patient_code not in active_codes or offer.recipient in unsubscribed:
            # A parent who unsubscribed is never assumed to have accepted; staff confirm another way.
            continue
        try:
            items = flow._offer_items(session, offer)
        except ValueError:
            continue  # Changed offers require staff review, never implicit acceptance.
        if not items or any(a.date != target for a in items):
            continue
        sent = session.scalar(select(FlowJob).where(
            FlowJob.event_key == hashlib.sha256(f"offer:{offer.token}".encode()).hexdigest(),
            FlowJob.status.in_(sent_states),
            (FlowJob.delivery.is_(None) | (FlowJob.delivery != "failed"))))
        replied = session.scalar(select(AuditLog.id).where(
            AuditLog.entity == f"offer:{offer.token}", AuditLog.action.like("flow_reply_%")))
        # A late/invalid response is still a response. Resolving its inbox item
        # alone is not family acceptance; staff must confirm the booking explicitly.
        referenced_reply = any(
            json.loads(job.payload).get("from") == offer.recipient and (
                offer.token in job.payload or
                (sent is not None and sent.message_id is not None and
                 json.loads(job.payload).get("context", {}).get("id") == sent.message_id))
            for job in inbound_by_parent.get(offer.recipient, []))
        review = any(item.patient_code == offer.patient_code and not item.event_key.startswith("expiry:")
                     for item in unresolved)
        ambiguous = any(item.event_key == job.event_key and item.patient_code is None
                        and job.created_at >= offer.created_at
                        and json.loads(job.payload).get("from") == offer.recipient
                        for item in unresolved for job in inbound_by_parent.get(offer.recipient, []))
        if sent is None or replied is not None or referenced_reply or review or ambiguous or any(
                a.status != AppointmentStatus.PENDING_CONFIRMATION for a in items):
            continue
        if dry_run:
            assumed += 1
            continue
        for item in items:
            item.status = AppointmentStatus.CONFIRMED
        offer.state, offer.expires_at = "CONFIRMED", None
        expiry = session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"expiry:{offer.token}"))
        if expiry:
            expiry.resolved = 1
        flow._audit(session, "flow_assumed_yes", f"offer:{offer.token}")
        assumed += 1
    session.flush()
    return assumed


def export_rows(flow: "FlowTest", session: Session, target: date, namespace: str,
                *, diagnostics: list | None = None) -> tuple[list[dict], list[int]]:
    """Snapshot saved appointments, including withdrawals of previously exported rows."""
    assumed_ids = set()
    offers = offers_on(session, target)
    decisions = {}
    if offers:
        for entity, action in session.execute(select(AuditLog.entity, AuditLog.action).where(
                AuditLog.entity.in_([f"offer:{offer.token}" for offer in offers]),
                AuditLog.action.in_(("flow_assumed_yes", "flow_reply_yes")))):
            decisions.setdefault(entity, set()).add(action)
    for offer in offers:
        actions = decisions.get(f"offer:{offer.token}", set())
        if "flow_assumed_yes" in actions and "flow_reply_yes" not in actions:
            assumed_ids.update(json.loads(offer.appointment_ids))
    rows, blocked = [], []
    from main_workflow.thanal_export import appointment_fields, enabled as thanal_export_enabled, export_context
    thanal = thanal_export_enabled(flow)
    # Rows last pushed under this date (including visits since moved elsewhere).
    tracked_ids = list(session.scalars(select(FlowExportItem.appointment_id).where(
        FlowExportItem.therapy_date == target)))
    selected = list(session.execute(select(Appointment, Patient, Therapist).join(
            Patient, Patient.id == Appointment.patient_id).join(
            Therapist, Therapist.id == Appointment.therapist_id).where(
                (Appointment.date == target) | Appointment.id.in_(tracked_ids)).order_by(Appointment.id)))
    context = export_context(session, [item.treatment_requirement_id for item, _, _ in selected]) if thanal else None
    pushed = ({row.appointment_id: row for row in session.scalars(select(FlowExportItem).where(
        FlowExportItem.appointment_id.in_([item.id for item, _, _ in selected])))} if selected else {})
    for item, patient, doctor in selected:
        previous = pushed.get(item.id)
        if item.status not in FINAL and previous is None:
            if item.status in {AppointmentStatus.PROPOSED, AppointmentStatus.PENDING_CONFIRMATION}:
                blocked.append(item.id)
            continue
        status = item.status.value
        if status == "CONFIRMED" and item.id in assumed_ids:
            status = "ASSUMED_CONFIRMED"
        fields = {"Scheduler_Key": f"{namespace}:{item.id}", "Reg_ID": patient.patient_code,
                  "PatientName": patient.name or patient.patient_code, "Doctor": doctor.name,
                  "Therapy_Date": item.date.strftime("%d-%m-%Y"),
                  "TherapyTime": SLOTS[item.slot_index].start_time.strftime("%I:%M %p"), "Status": status}
        if thanal:
            try:
                fields = appointment_fields(session, item, patient, doctor, namespace, context=context)
            except ValueError as exc:
                blocked.append(item.id)
                if diagnostics is not None:
                    diagnostics.append({"appointmentId": item.id, "patientCode": patient.patient_code,
                                        "patientName": patient.name, "reason": str(exc)})
                continue
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        if previous is not None and previous.fingerprint == digest:
            continue
        row = {"appointmentId": item.id, "fingerprint": digest, "fields": fields}
        if thanal:
            row.update(patientCode=patient.patient_code, patientName=patient.name,
                       display={"Registration": patient.name, "Consultant": doctor.name,
                                "Appointment_Time_Slot": SLOTS[item.slot_index].start_time.strftime("%H:%M")})
        rows.append(row)
    return rows, blocked


def export_preview(flow: "FlowTest", target: date) -> dict:
    with flow.lock, flow.sessions() as session:
        namespace = setting(session, "namespace", "preview")
        diagnostics = []
        rows, blocked = export_rows(flow, session, target, namespace, diagnostics=diagnostics)
        return {"target": getattr(flow, "export_target", TARGET), "date": target.isoformat(), "changes": rows,
                "awaitingFinalisation": blocked, "lastPush": setting(session, "last_push"),
                "wouldAssumeAcceptedVisits": finalise_unanswered(flow, session, target, dry_run=True),
                "openBatch": batch_for_date(session, target), "mode": "PREVIEW ONLY",
                "blockedReasons": diagnostics}


def prepare_export(flow: "FlowTest", target: date) -> dict:
    """Freeze one resumable batch; parent events already queued take precedence."""
    with flow.lock, flow.inbound_lock:
        with flow.sessions() as session:
            require_reconciled_source(session)
            batch = batch_for_date(session, target)
        if batch is not None:
            return {**batch, "resumed": True}
        # Do not assume Yes while an already-received No/Help is waiting to be handled.
        with flow.sessions() as session:
            pending = relevant_reply_ids(session, target, {"QUEUED"})
        for job_id in pending:
            flow.process_one(job_id=job_id)
        with flow.sessions.begin() as session:
            if relevant_reply_ids(session, target, {"QUEUED", "FAILED", "PROCESSING"}):
                raise ValueError("An incoming reply needs staff review before finalising the schedule")
            namespace = setting(session, "namespace") or f"{getattr(flow, 'export_key_prefix', 'test')}-{uuid4().hex}"
            store(session, "namespace", namespace)
            assumed = finalise_unanswered(flow, session, target)
            diagnostics = []
            rows, blocked = export_rows(flow, session, target, namespace, diagnostics=diagnostics)
            if diagnostics:
                raise ValueError("Resolve the export preview's lookup/status issues before pushing: "
                                 + "; ".join(dict.fromkeys(row["reason"] for row in diagnostics)))
            # A failed date must not block unrelated dates. A moved appointment
            # already frozen elsewhere waits for that record's older batch only.
            open_batches = [json.loads(item.value) for item in session.scalars(select(FlowSetting).where(
                FlowSetting.key.like(f"{PREFIX}:batch:%")))]
            legacy = setting(session, "batch")
            if legacy:
                open_batches.append(legacy)
            reserved = {row["appointmentId"] for other in open_batches for row in other["changes"]}
            blocked.extend(row["appointmentId"] for row in rows if row["appointmentId"] in reserved)
            rows = [row for row in rows if row["appointmentId"] not in reserved]
            batch = {"id": uuid4().hex, "date": target.isoformat(), "target": getattr(flow, "export_target", TARGET),
                     "cutoffIST": ist_time(utc_now()), "assumedAcceptedVisits": assumed,
                     "changes": rows, "awaitingStaff": blocked, "acknowledged": {}}
            store_batch(session, batch)
            flow._audit(session, "flow_zoho_export_prepared", f"export:{batch['id']}")
            return batch


def relevant_reply_ids(session: Session, target: date, statuses: set[str]) -> list[int]:
    """An unresolved reply blocks its own visit date, not every export date."""
    offers = offers_on(session, target)
    if not offers:
        return []
    selected = []
    for job in session.scalars(select(FlowJob).where(FlowJob.kind == "INBOUND", FlowJob.status.in_(statuses))):
        payload = json.loads(job.payload)
        # Explicit tokens bind a reply to one date, including siblings and old offers.
        tokens = set(re.findall(r"[a-f0-9]{20}", job.payload))
        if tokens:
            relevant = any(o.token in tokens for o in offers)
        else:
            relevant = any(o.recipient == payload.get("from") and job.created_at >= o.created_at for o in offers)
        if relevant:
            selected.append(job.id)
    return selected


def acknowledge_export(flow: "FlowTest", batch_id: str, appointment_id: int, fingerprint: str, record_id: str,
                       *, records: list[dict] | None = None) -> dict:
    if records is None:
        records = [{"appointmentId": appointment_id, "fingerprint": fingerprint, "recordId": record_id}]
    if not isinstance(records, list) or not records or len(records) > 200 or any(
            not isinstance(record, dict) or set(record) != {"appointmentId", "fingerprint", "recordId"}
            or type(record["appointmentId"]) is not int for record in records):
        raise ValueError("Acknowledge between 1 and 200 records")
    with flow.lock, flow.sessions.begin() as session:
        batch = batch_for_id(session, batch_id)
        if not batch or batch["id"] != batch_id:
            raise ValueError("Export acknowledgement does not match the prepared batch")
        rows = {r["appointmentId"]: r for r in batch["changes"]}
        for record in records:
            appointment_id, fingerprint, record_id = record["appointmentId"], record["fingerprint"], record["recordId"]
            row = rows.get(appointment_id)
            if not isinstance(record_id, str) or not record_id.isdigit():
                raise ValueError("Zoho must return a numeric record ID")
            if not row or row["fingerprint"] != fingerprint:
                raise ValueError("Export acknowledgement does not match the prepared batch")
            batch["acknowledged"][str(appointment_id)] = record_id
            fields = row["fields"]
            therapy_date = (datetime.strptime(fields["Date_field"], "%d/%m/%Y").date()
                            if "Date_field" in fields else
                            datetime.strptime(fields["Therapy_Date"], "%d-%m-%Y").date())
            pushed = session.get(FlowExportItem, appointment_id)
            if pushed is None:
                session.add(FlowExportItem(appointment_id=appointment_id, therapy_date=therapy_date,
                                           fingerprint=fingerprint, record_id=record_id))
            else:
                pushed.therapy_date, pushed.fingerprint, pushed.record_id = therapy_date, fingerprint, record_id
                pushed.updated_at = datetime.utcnow()
            # A record that saves after an earlier rejection closes its staff task.
            for task in session.scalars(select(FlowInbox).where(
                    FlowInbox.event_key == f"zoho-export:{appointment_id}", FlowInbox.resolved == 0)):
                task.resolved = 1
        store_batch(session, batch)
        return {"acknowledged": appointment_id, "count": len(records)}


def record_export_failures(flow: "FlowTest", batch_id: str, failures: list[dict]) -> dict:
    """Turn each record Zoho did not save into a staff task naming the child, date and reason."""
    if not isinstance(failures, list) or len(failures) > 1000:
        raise ValueError("Supply the failed export records")
    with flow.lock, flow.sessions.begin() as session:
        batch = batch_for_id(session, batch_id)
        if not batch or batch["id"] != batch_id:
            raise ValueError("Export batch is no longer current")
        rows = {r["appointmentId"]: r for r in batch["changes"]}
        recorded = 0
        for failure in failures:
            appointment_id = failure.get("appointmentId") if isinstance(failure, dict) else None
            row = rows.get(appointment_id)
            if row is None:
                continue
            fields = row["fields"]
            reason = str(failure.get("reason") or "Zoho did not confirm this record")[:400]
            code = row.get("patientCode", fields.get("Reg_ID"))
            day = fields.get("Date_field", fields.get("Therapy_Date"))
            time = row.get("display", {}).get("Appointment_Time_Slot", fields.get("TherapyTime"))
            text = (f"Zoho did not save the schedule record for {code} on {day} "
                    f"at {time} ({fields.get('Status')}). Reason: {reason}. "
                    "Other records were saved. Fix the cause, then push this date again.")
            flow._job_warning(session, f"zoho-export:{appointment_id}", "Zoho record not saved", text,
                              code=code)
            recorded += 1
        batch["failures"] = {str(f.get("appointmentId")): str(f.get("reason", ""))[:400]
                             for f in failures if isinstance(f, dict)}
        store_batch(session, batch)
        return {"recorded": recorded}


def finish_export(flow: "FlowTest", batch_id: str) -> dict:
    with flow.lock, flow.sessions.begin() as session:
        batch = batch_for_id(session, batch_id)
        if not batch or batch["id"] != batch_id:
            raise ValueError("Export batch is no longer current")
        if any(str(r["appointmentId"]) not in batch["acknowledged"] for r in batch["changes"]):
            raise ValueError("Export is incomplete; last successful push time was not advanced")
        result = {"batchId": batch_id, "date": batch["date"], "capturedThroughIST": batch["cutoffIST"],
                  "completedAtIST": ist_time(utc_now()), "pushed": len(batch["changes"]),
                  "awaitingStaff": batch["awaitingStaff"]}
        store(session, "last_push", result)
        for key in (f"batch:{batch['date']}", f"batchid:{batch_id}", "batch"):
            item = session.get(FlowSetting, f"{PREFIX}:{key}")
            if item and (key != "batch" or json.loads(item.value)["id"] == batch_id):
                session.delete(item)
        flow._audit(session, "flow_zoho_export_completed", f"export:{batch_id}")
        return result


def dispatch_export(request, instance, operation: str) -> None:
    import web_app

    body = request.read_json()
    try:
        if operation in {"preview", "prepare"}:
            target = web_app.parse_date(body.get("date"), "date")
            result = (export_preview if operation == "preview" else prepare_export)(instance.flow, target)
            if operation == "prepare":
                instance.previews.clear()
        elif operation == "ack":
            if type(body.get("appointmentId")) is not int:
                raise ValueError("Supply the exported appointment ID")
            result = acknowledge_export(instance.flow, body.get("batchId"), body["appointmentId"],
                                        body.get("fingerprint"), body.get("recordId"), records=body.get("records"))
        elif operation == "failures":
            result = record_export_failures(instance.flow, body.get("batchId"), body.get("failures"))
        else:
            result = finish_export(instance.flow, body.get("batchId"))
        request.send_json(result)
    except ValueError as exc:
        raise web_app.ApiError(409, str(exc)) from None

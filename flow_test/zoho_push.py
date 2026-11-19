"""Manually finalise one test date and incrementally publish it to Scheduler."""

import argparse
from dataclasses import replace
from datetime import date, datetime
from http.client import HTTPException
import json
import os
from time import monotonic
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from config import load_local_env_file
from flow_test.reset_live import BASE_URL, _admin_token
from flow_test.zoho_daily import test_api
from flow_test.zoho_import import APP, OWNER
from services.zoho_creator_service import (
    ZohoCreatorSettings, get_report_records, open_zoho_request, refresh_access_token, settings_from_environment,
)
from services.zoho_retry import retry_read


FORM = "Scheduler"
REPORT = "Scheduler_Report"
REQUIRED_FIELDS = {"Reg_ID", "PatientName", "Therapy_Date", "Doctor", "TherapyTime", "Scheduler_Key", "Status"}
REQUIRED_SCOPES = {"ZohoCreator.form.CREATE", "ZohoCreator.report.READ", "ZohoCreator.report.UPDATE",
                   "ZohoCreator.meta.form.READ"}


def required_fields(settings: ZohoCreatorSettings) -> set[str]:
    from main_workflow.thanal_export import FIELDS, matches
    return set(FIELDS) if matches(settings) else REQUIRED_FIELDS


def validate_payload(settings: ZohoCreatorSettings, fields: dict, key_prefix: str) -> None:
    if set(fields) != required_fields(settings) or not fields.get("Scheduler_Key", "").startswith(key_prefix):
        raise ValueError("Unexpected Scheduler payload")
    from main_workflow.thanal_export import matches, validate_fields
    if matches(settings):
        validate_fields(fields)


def field_matches(actual: Any, expected: Any) -> bool:
    """Creator returns lookup objects although writes supply the record ID."""
    if isinstance(actual, dict) and "ID" in actual and not isinstance(expected, dict):
        actual = actual["ID"]
    if type(expected) is bool:
        return actual is expected or str(actual).lower() == str(expected).lower()
    return str(actual) == str(expected)


def fields_match(actual: dict, expected: dict) -> bool:
    def matches(name, value):
        returned = actual.get(name, "")
        if name in {"Date_Time", "End_Time"}:
            def parse(text):
                for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
                    try:
                        return datetime.strptime(text, pattern)
                    except (ValueError, TypeError):
                        pass
                return None
            parsed = parse(value)
            return parsed is not None and parse(returned) == parsed
        return field_matches(returned, value)
    return all(matches(name, value) for name, value in expected.items())


def export_settings() -> ZohoCreatorSettings:
    settings = settings_from_environment()
    overrides = {field: os.environ[f"FLOW_TEST_ZOHO_{field.upper()}"]
                 for field in ("client_id", "client_secret", "refresh_token")
                 if os.environ.get(f"FLOW_TEST_ZOHO_{field.upper()}")}
    return replace(settings, account_owner=OWNER, app_link_name=APP,
                   form_link_name=FORM, report_link_name=REPORT, **overrides)


def verify_target(settings: ZohoCreatorSettings) -> None:
    host = urlsplit(settings.api_url)
    if ((settings.account_owner, settings.app_link_name, settings.form_link_name, settings.report_link_name)
            != (OWNER, APP, FORM, REPORT) or host.query or host.fragment or host.username
            or not ((host.scheme == "https" and host.hostname == "www.zohoapis.in" and not host.path)
                    or (host.scheme == "http" and host.hostname in {"localhost", "127.0.0.1"}
                        and host.path in {"", "/zoho"}))):
        raise ValueError("Schedule exports are restricted to the authorized test-gokul Scheduler form")


def workflow_options(settings: ZohoCreatorSettings) -> dict:
    """Skip Zoho's form workflows and schedules only for Thanal's trial form when the switch is on.

    Thanal's form workflow refuses rows that overlap its main schedule and, on success, creates
    session records in other reports. With the switch on, rows land in the trial form only.
    """
    from main_workflow.thanal_export import matches
    enabled = os.getenv("MAIN_WORKFLOW_ZOHO_SKIP_WORKFLOW", "false").strip().lower() == "true"
    return {"skip_workflow": ["form_workflow", "schedules"]} if enabled and matches(settings) else {}


def write_record(settings: ZohoCreatorSettings, access_token: str, fields: dict, record_id: str | None,
                 *, verify: Callable = verify_target, key_prefix: str = "test-") -> str:
    """POST a new unique key or PATCH the same owned row; workflows run unless ``workflow_options`` skips them."""
    verify(settings)
    validate_payload(settings, fields, key_prefix)
    if record_id is not None and not record_id.isdigit():
        raise ValueError("Invalid Scheduler record ID")
    suffix = f"report/{settings.report_link_name}/{record_id}" if record_id else f"form/{settings.form_link_name}"
    request = Request(f"{settings.api_url}/creator/v2.1/data/{settings.account_owner}/{settings.app_link_name}/{suffix}",
                      data=json.dumps({"data": fields, **workflow_options(settings)}).encode(),
                      method="PATCH" if record_id else "POST",
                      headers={"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"})
    try:
        with open_zoho_request(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (ValueError, AttributeError, OSError):
            body = None
        # Keep Zoho's own explanation (for example "Doctor is inactive"), not just its code.
        detail = zoho_reason(body) if isinstance(body, dict) else f"HTTP {exc.code}"
        raise ValueError(f"Scheduler write rejected ({detail}); batch retained for review/retry") from None
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        raise ValueError("Scheduler write outcome is uncertain; batch retained. Rerun to reconcile by Scheduler Key") from None
    if not isinstance(payload, dict):
        raise ValueError("Scheduler returned an invalid response; batch retained for reconciliation")
    result = payload.get("result", payload)
    if isinstance(result, list):
        result = result[0] if len(result) == 1 else {}
    # When Zoho answers but refuses, keep its explanation for staff.
    refusal = next((zoho_reason(item) for item in (result, payload) if isinstance(item, dict)
                    and item.get("code", 3000) != 3000), None)
    suffix = f" ({refusal})" if refusal else ""
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        raise ValueError(f"Scheduler did not confirm the record{suffix}; batch retained for reconciliation")
    identifier = result["data"].get("ID")
    if payload.get("code") != 3000 or result.get("code", 3000) != 3000 or not str(identifier or "").isdigit():
        raise ValueError(f"Scheduler did not confirm the record{suffix}; batch retained for reconciliation")
    return str(identifier)


def validate_schema(settings: ZohoCreatorSettings, access_token: str, *, verify: Callable = verify_target) -> None:
    """Verify the real field names and uniqueness even when the report is empty."""
    verify(settings)
    request = Request(f"{settings.api_url}/creator/v2.1/meta/{settings.account_owner}/{settings.app_link_name}/form/{settings.form_link_name}/fields",
                      headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
    def fetch_fields():
        with open_zoho_request(request, timeout=30) as response:
            return json.load(response)

    try:
        payload = retry_read(fetch_fields)
    except HTTPError:
        raise ValueError("Cannot read Scheduler field definitions. Run python -m flow_test.zoho_authorize once") from None
    except (URLError, OSError, HTTPException):
        raise ValueError("Scheduler field definitions could not be reached; no finalisation or push was attempted") from None
    fields = {field["link_name"]: field for field in payload.get("fields", []) if "link_name" in field}
    required = required_fields(settings)
    if payload.get("code") != 3000 or required - fields.keys():
        raise ValueError("Scheduler is missing field link names: " + ", ".join(sorted(required - fields.keys())))
    from main_workflow.thanal_export import matches, validate_schema as validate_thanal_schema
    if matches(settings):
        validate_thanal_schema(fields)
        return
    key, status = fields["Scheduler_Key"], fields["Status"]
    if key.get("type") != 1 or key.get("unique") is not True or int(key.get("max_char", 255)) < 60:
        raise ValueError("Scheduler_Key must be Single Line, at least 60 characters, with No duplicate values enabled")
    if status.get("type") != 1:
        raise ValueError("Status must be a Single Line field")


def read_owned_keys(settings, token, keys):
    """Bound reconciliation to owned keys; never scan all historical records."""
    found = {}
    keys = sorted(set(keys))
    for start in range(0, len(keys), 25):
        wanted = keys[start:start + 25]
        criteria = " || ".join(f'Scheduler_Key == {json.dumps(key)}' for key in wanted)
        for row in get_report_records(settings, token(), settings.report_link_name, criteria=criteria):
            key = row.get("Scheduler_Key")
            if key not in wanted:
                raise ValueError("Scheduler returned a row outside the requested owned keys")
            if key in found or not str(row.get("ID", "")).isdigit():
                raise ValueError("Duplicate Scheduler Key or invalid record ID in Zoho; review before resuming")
            found[key] = row
    return found


def zoho_reason(value) -> str:
    """Short, staff-readable reason from a Zoho result, HTTP error or exception."""
    if isinstance(value, HTTPError):
        try:
            body = json.loads(value.read() or b"{}")
        except (ValueError, OSError):
            body = {}
        detail = body.get("message") or body.get("error") or body.get("description")
        return f"HTTP {value.code}" + (f": {str(detail)[:200]}" if detail else "")
    if isinstance(value, dict):
        parts = []
        for field in ("message", "error", "description"):
            item = value.get(field)
            if item:
                text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                if text not in parts:
                    parts.append(text)
        detail = "; ".join(parts) or "not accepted"
        return f"Zoho code {value.get('code', 'unknown')}: {detail[:300]}"
    return f"{type(value).__name__}: {str(value)[:200]}"


def write_records(settings, token, fields, *, verify=verify_target, key_prefix="test-", errors=None):
    """Create up to 200 rows. Match acknowledgements by key, never list position.

    When ``errors`` is supplied, rejected rows get a reason for staff. Zoho returns rejected
    rows without their key, so a reason is attached by position only to explain the
    rejection; acknowledgement still requires the returned key and ID.
    """
    verify(settings)
    if not 1 <= len(fields) <= 200:
        raise ValueError("Unexpected Scheduler batch payload")
    for row in fields:
        validate_payload(settings, row, key_prefix)
    request = Request(
        f"{settings.api_url}/creator/v2.1/data/{settings.account_owner}/{settings.app_link_name}/form/{settings.form_link_name}",
        data=json.dumps({"data": fields, "result": {"fields": ["Scheduler_Key"]},
                         **workflow_options(settings)}).encode(), method="POST",
        headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"})
    with open_zoho_request(request, timeout=60) as response:
        payload = json.load(response)
    acknowledged = {}
    allowed = {row["Scheduler_Key"] for row in fields}
    for result in payload.get("result", []) if isinstance(payload, dict) else []:
        data = result.get("data", {})
        key, identifier = data.get("Scheduler_Key"), str(data.get("ID", ""))
        if result.get("code") == 3000 and key in allowed and identifier.isdigit():
            if key in acknowledged:
                raise ValueError("Duplicate key in Scheduler response; reconcile before resuming")
            acknowledged[key] = identifier
    if errors is not None:
        record_create_rejections(fields, payload, acknowledged, errors)
    return acknowledged


def record_create_rejections(fields: list[dict], payload, acknowledged: dict, errors: dict) -> None:
    """Attach Zoho's rejection reasons to rows only when the match is provable.

    Zoho returns rejected rows without their key. A reason is tied to a row by position
    only when the response proves its order: one result per row and every accepted
    result carries the key of the row in that same position. Otherwise, or when the
    response cannot be read, each unconfirmed row keeps every rejection reason from the
    batch, labelled as unmatched, so staff can investigate without a guessed match.
    """
    results = payload.get("result", []) if isinstance(payload, dict) else []
    if not isinstance(results, list):
        results = []
    missing = [row["Scheduler_Key"] for row in fields if row["Scheduler_Key"] not in acknowledged]
    if not missing:
        return
    rejected = [(index, zoho_reason(item)) for index, item in enumerate(results)
                if isinstance(item, dict) and item.get("code") != 3000]
    accepted_count = sum(1 for item in results if isinstance(item, dict) and item.get("code") == 3000)
    ordered = len(results) == len(fields) and accepted_count > 0 and all(
        not isinstance(item, dict) or item.get("code") != 3000
        or (item.get("data") or {}).get("Scheduler_Key") == row["Scheduler_Key"]
        for row, item in zip(fields, results))
    if ordered:
        for index, reason in rejected:
            errors[fields[index]["Scheduler_Key"]] = reason
    elif len(missing) == 1 and len(rejected) == 1:
        errors[missing[0]] = rejected[0][1]
    elif rejected:
        summary = " | ".join(reason for _, reason in rejected[:5])
        more = f" (+{len(rejected) - 5} more)" if len(rejected) > 5 else ""
        for key in missing:
            errors.setdefault(key, f"Zoho rejected {len(rejected)} row(s) in this batch without naming them; "
                                   f"reasons: {summary}{more}")


def write_updates(settings, token, changes, existing, *, verify=verify_target, errors=None):
    """Group identical field changes (not whole patient rows) for bulk PATCH."""
    verify(settings)
    groups = {}
    for change in changes:
        fields = change["fields"]
        old = existing[fields["Scheduler_Key"]]
        delta = {k: v for k, v in fields.items() if not field_matches(old.get(k, ""), v)}
        if delta:
            groups.setdefault(json.dumps(delta, sort_keys=True), []).append(old)
    confirmed = {}
    for encoded, rows in groups.items():
        by_id = {str(row["ID"]): row["Scheduler_Key"] for row in rows}
        if not all(identifier.isdigit() for identifier in by_id):
            raise ValueError("Invalid Scheduler update ID")
        request = Request(
            f"{settings.api_url}/creator/v2.1/data/{settings.account_owner}/{settings.app_link_name}/report/{settings.report_link_name}",
            data=json.dumps({"criteria": " || ".join(f"ID == {identifier}" for identifier in by_id),
                             "data": json.loads(encoded), "result": {"fields": ["Scheduler_Key"]},
                             **workflow_options(settings)}).encode(),
            method="PATCH", headers={"Authorization": f"Zoho-oauthtoken {token()}", "Content-Type": "application/json"})
        try:
            with open_zoho_request(request, timeout=60) as response:
                payload = json.load(response)
            for result in payload.get("result", []):
                identifier = str(result.get("data", {}).get("ID", ""))
                if result.get("code") == 3000 and identifier in by_id:
                    confirmed[by_id[identifier]] = identifier
                elif errors is not None and identifier in by_id:
                    errors[by_id[identifier]] = zoho_reason(result)
        except (ValueError, OSError, HTTPException) as exc:
            # Reconcile even when part of the group committed before a timeout,
            # but keep the reason so staff can see why a record may be missing.
            if errors is not None:
                for key in by_id.values():
                    errors.setdefault(key, zoho_reason(exc))
    return confirmed


def held_summary(rows: list[dict]) -> list[dict]:
    """Rows not pushed this time and why (child code and reason only)."""
    return [{"appointmentId": row["appointmentId"], "patientCode": row.get("patientCode", ""),
             "reason": row["reason"]} for row in rows]


def run_push(settings: ZohoCreatorSettings, access_token: str, base_url: str, admin_token: str, target: date, *, apply: bool,
             exchange: Callable[[str, dict], dict] | None = None,
             verify: Callable = verify_target, key_prefix: str = "test-") -> dict:
    """Share the export protocol between the CLI and the calendar's local adapter."""
    if exchange is None:
        exchange = lambda operation, body: test_api(base_url, admin_token, f"export/{operation}", body)
    verify(settings)
    validate_schema(settings, access_token, verify=verify)
    preview = exchange("preview", {"date": target.isoformat()})
    if not apply:
        return {"mode": "PREVIEW ONLY", "date": target.isoformat(), "target": preview["target"],
                "changesReady": len(preview["changes"]), "wouldAssumeAcceptedVisits": preview["wouldAssumeAcceptedVisits"],
                "awaitingFinalisation": preview["awaitingFinalisation"], "lastPush": preview["lastPush"],
                "held": held_summary(preview.get("blockedReasons", []))}
    # Rows with a lookup/status issue wait on their own; the rest of the day still goes.
    connection = {"token": access_token, "renew_at": monotonic() + 2700}

    def token():
        if settings.refresh_token and monotonic() >= connection["renew_at"]:
            connection["token"] = refresh_access_token(settings)["access_token"]
            connection["renew_at"] = monotonic() + 2700
        return connection["token"]

    batch = exchange("prepare", {"date": target.isoformat()})
    created = updated = recovered = 0
    pending = [r for r in batch["changes"] if str(r["appointmentId"]) not in batch["acknowledged"]]
    failures = []
    errors: dict[str, str] = {}
    for start in range(0, len(pending), 200):
        changes = pending[start:start + 200]
        by_key = read_owned_keys(settings, token, [r["fields"]["Scheduler_Key"] for r in changes])
        acknowledgements = []
        creates = [r for r in changes if r["fields"]["Scheduler_Key"] not in by_key]
        confirmed = {}
        if creates:
            try:
                confirmed = write_records(settings, token(), [r["fields"] for r in creates],
                                          verify=verify, key_prefix=key_prefix, errors=errors)
                created += len(confirmed)
            except (ValueError, OSError, HTTPException) as exc:
                # A write may have committed. Reconcile, never blindly repeat.
                for row in creates:
                    errors.setdefault(row["fields"]["Scheduler_Key"], zoho_reason(exc))
            uncertain = [r["fields"]["Scheduler_Key"] for r in creates
                         if r["fields"]["Scheduler_Key"] not in confirmed]
            if uncertain:
                by_key.update(read_owned_keys(settings, token, uncertain))
        updates = [r for r in changes if r not in creates and not fields_match(
            by_key[r["fields"]["Scheduler_Key"]], r["fields"])]
        bulk_updates = {}
        if len(updates) > 1:
            bulk_updates = write_updates(settings, token, updates, by_key, verify=verify, errors=errors)
            updated += len(bulk_updates)
            uncertain = [r["fields"]["Scheduler_Key"] for r in updates
                         if r["fields"]["Scheduler_Key"] not in bulk_updates]
            if uncertain:
                by_key.update(read_owned_keys(settings, token, uncertain))
        for change in changes:
            fields = change["fields"]
            key = fields["Scheduler_Key"]
            found = by_key.get(key)
            record_id = confirmed.get(key) or bulk_updates.get(key)
            if record_id is None and found and fields_match(found, fields):
                record_id = str(found["ID"])
                recovered += 1
            elif record_id is None and found and change not in creates:
                # One update at a time, also when a bulk group failed: one bad record
                # must not stop the good records in its group from being saved.
                try:
                    record_id = write_record(settings, token(), fields, str(found["ID"]),
                                             verify=verify, key_prefix=key_prefix)
                    updated += 1
                except (ValueError, OSError, HTTPException) as exc:
                    errors[key] = zoho_reason(exc)
                    check = read_owned_keys(settings, token, [key]).get(key)
                    if check and fields_match(check, fields):
                        record_id = str(check["ID"])
                        recovered += 1
            if record_id is None:
                failures.append({"appointmentId": change["appointmentId"], "key": key,
                                 "patient": change.get("patientCode", fields.get("Reg_ID", "")),
                                 "date": fields.get("Date_field", fields.get("Therapy_Date", "")),
                                 "reason": errors.get(key) or "Zoho did not confirm this record"})
                continue
            acknowledgements.append({"appointmentId": change["appointmentId"],
                                     "fingerprint": change["fingerprint"], "recordId": record_id})
        if acknowledgements:
            exchange("ack", {"batchId": batch["id"], **acknowledgements[0], "records": acknowledgements})
    if failures:
        tasks = "Staff tasks were created for each record."
        try:
            # Each failed record becomes a staff task naming the child, date and reason.
            exchange("failures", {"batchId": batch["id"], "failures": failures})
        except Exception as exc:  # noqa: BLE001 - reported below; never hidden
            tasks = (f"WARNING: staff tasks could NOT be created ({type(exc).__name__}: {str(exc)[:200]}); "
                     "copy this list to staff.")
        listed = "; ".join(f"{item['patient']} {item['date']}: {item['reason']}" for item in failures[:10])
        more = f" (+{len(failures) - 10} more)" if len(failures) > 10 else ""
        raise ValueError(f"Zoho did not save {len(failures)} record(s): {listed}{more}. "
                         f"Successful rows were saved; batch retained. {tasks} Fix these records, then push again.")
    result = exchange("finish", {"batchId": batch["id"]})
    summary = {**result, "created": created, "updated": updated, "reconciled": recovered,
               "assumedAcceptedVisits": batch["assumedAcceptedVisits"], "mode": "APPLIED",
               "held": held_summary(batch.get("held", []))}
    if batch.get("resumed"):
        # Finish the frozen attempt, then catch changes received since its cutoff.
        current = run_push(settings, token(), base_url, admin_token, target, apply=True, exchange=exchange,
                           verify=verify, key_prefix=key_prefix)
        for field in ("created", "updated", "reconciled", "assumedAcceptedVisits", "pushed"):
            current[field] += summary[field]
        current["held"] = summary["held"] + [item for item in current["held"]
                                             if item["appointmentId"] not in {h["appointmentId"] for h in summary["held"]}]
        current["resumedBatch"] = batch["id"]
        return current
    return summary


def main() -> None:
    load_local_env_file(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Therapy schedule date, YYYY-MM-DD (India)")
    parser.add_argument("--apply", action="store_true", help="Manual cutoff: assume unanswered sent offers accepted and push changes")
    args = parser.parse_args()
    try:
        target = date.fromisoformat(args.date)
        base_url = os.getenv("FLOW_TEST_APP_URL", BASE_URL)
        admin_token = os.getenv("FLOW_TEST_ADMIN_TOKEN") or _admin_token()
        if args.apply:
            # Run inside the app, under the same database push lock as the
            # Operations button, so pushes never overlap across app copies.
            result = test_api(base_url, admin_token, "export/push", {"date": target.isoformat()}, timeout=3600)
        else:
            settings = export_settings()
            verify_target(settings)
            connection = refresh_access_token(settings)
            result = run_push(settings, connection["access_token"], base_url, admin_token, target, apply=False)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError) as exc:
        print(f"Test schedule push stopped: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

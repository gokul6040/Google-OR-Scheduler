"""Registration-only projection and diagnostics. Never activates patients.

Keep Creator IDs for plan lookup joins, registration codes for patient identity,
and verified phone numbers for later contact binding. Discard unrelated personal
and clinical columns. A source pull is not a clinical eligibility decision.
"""

from collections import Counter
from datetime import datetime

from flow_test.zoho_import import phone_number
from services.thanal_source_reader import _lookup, _text


def prepare_registrations(rows: list[dict]) -> dict:
    """Select required fields; flag invalid identities/contacts without guessing.

    An invalid phone is retained as None, never replaced by a previous phone.
    Duplicate registration codes remain distinct Creator records for review.
    Summary contains no names, phones or other personal data.
    """
    if not isinstance(rows, list):
        raise ValueError("All_Registrations must contain a list of records")
    records, issues, seen = [], [], set()
    for index, row in enumerate(rows, 1):
        ident = ""
        try:
            if not isinstance(row, dict):
                raise ValueError("invalid_record")
            ident = _text(row.get("ID"))
            if not ident.isdigit() or len(ident) > 80 or ident in seen:
                raise ValueError("missing_or_duplicate_source_id")
            seen.add(ident)
            code, name = _text(row.get("Registration_Id")), _text(row.get("Name2"))
            if not code or len(code) > 40:
                issues.append({"row": index, "source_id": ident, "reason": "missing_or_invalid_registration_code"})
            if not name or len(name) > 120:
                issues.append({"row": index, "source_id": ident, "reason": "missing_or_invalid_name"})
            try:
                branch = _lookup(row.get("Branch"), "Branch_Name")
                if not branch["id"].isdigit() or not branch["label"] or len(branch["label"]) > 120:
                    raise ValueError("invalid branch")
            except ValueError:
                branch = {"id": "", "label": ""}
                issues.append({"row": index, "source_id": ident, "reason": "missing_or_invalid_branch"})
            raw_phone = _text(row.get("Phone_Number"))
            try:
                phone = phone_number(raw_phone)
            except ValueError:
                phone = None
                issues.append({"row": index, "source_id": ident,
                               "reason": "invalid_phone" if raw_phone else "missing_phone"})
            added = _text(row.get("Added_Time"))
            try:
                datetime.strptime(added, "%d/%m/%Y %H:%M:%S")
            except ValueError:
                issues.append({"row": index, "source_id": ident, "reason": "missing_or_invalid_added_time"})
            records.append({"id": ident, "code": code, "name": name, "phone": phone,
                            "branch": branch, "status": _text(row.get("Status")), "added_time": added})
        except (ValueError, TypeError) as exc:
            issues.append({"row": index, "source_id": ident, "reason": str(exc)})
    codes = Counter(r["code"] for r in records if r["code"])
    duplicated = {code for code, count in codes.items() if count > 1}
    for row in records:
        if row["code"] in duplicated:
            issues.append({"source_id": row["id"], "reason": "duplicate_registration_code"})
    return {"records": records, "issues": issues, "summary": {
        "downloaded": len(rows), "projected": len(records),
        "verified_phones": sum(r["phone"] is not None for r in records),
        "distinct_duplicate_codes": len(duplicated),
        "records_with_issues": len({i["source_id"] or f"row:{i.get('row')}" for i in issues}),
        "issue_counts": dict(Counter(i["reason"] for i in issues)),
        "statuses": dict(Counter(r["status"] for r in records)),
        "database_records_written": 0, "messages_sent": 0,
    }}

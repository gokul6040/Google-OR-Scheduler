"""Registration reads use fictional input; no HTTP or database access."""

from copy import deepcopy

from services.thanal_registration_reader import prepare_registrations


def row():
    return {"ID": "100", "Registration_Id": "FAKE/1", "Name2": "Fictional child",
            "Phone_Number": "+91 99999 99999", "Added_Time": "07/11/2026 20:14:22",
            "Status": "Discharged", "Branch": {"ID": "10", "Branch_Name": "Fictional centre"},
            "Aadhaar_No": "must be discarded", "Current_Address": {"private": "discard"}}


def test_projection_extracts_lookups_and_phone_but_does_not_infer_active_status():
    data = [row()]
    original = deepcopy(data)
    result = prepare_registrations(data)
    record = result["records"][0]
    assert record["phone"] == "919999999999"
    assert record["branch"] == {"id": "10", "label": "Fictional centre"}
    assert record["status"] == "Discharged" and "active" not in record
    assert "Aadhaar_No" not in record and "Current_Address" not in record
    assert not result["issues"] and data == original
    assert "Fictional child" not in str(result["summary"])


def test_missing_fields_are_flagged_and_phone_is_not_guessed():
    item = row()
    item.update(Branch={}, Phone_Number="+65 8123 4567", Registration_Id="", Name2="")
    result = prepare_registrations([item])
    assert result["records"][0]["phone"] is None
    assert {i["reason"] for i in result["issues"]} == {
        "missing_or_invalid_branch", "invalid_phone", "missing_or_invalid_name",
        "missing_or_invalid_registration_code"}


def test_duplicate_codes_do_not_merge_distinct_lookup_ids():
    other = row()
    other["ID"] = "101"
    result = prepare_registrations([row(), other])
    assert len(result["records"]) == 2
    assert result["summary"]["distinct_duplicate_codes"] == 1
    assert sum(i["reason"] == "duplicate_registration_code" for i in result["issues"]) == 2


def test_duplicate_ids_and_invalid_timestamps_are_reported():
    item = row()
    item["Added_Time"] = "not a date"
    result = prepare_registrations([item, deepcopy(item)])
    assert len(result["records"]) == 1
    assert {i["reason"] for i in result["issues"]} == {
        "missing_or_invalid_added_time", "missing_or_duplicate_source_id"}

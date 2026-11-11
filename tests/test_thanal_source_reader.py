"""Fictional lookup/subform records; never call Zoho or open a database."""

from copy import deepcopy

import pytest

from services.thanal_source_reader import prescription, project_reports, readiness_summary


def reports():
    return {
        "All_Registrations": [{"ID": "10", "Registration_Id": "FAKE-1", "Name2": "Fictional child",
                               "Phone_Number": "919999999999", "Sensitive_extra": "ignored"}],
        "All_Consultants": [{"ID": "20", "Name": "Fictional therapist", "Status": "Active",
                             "Consultation_Type": "Full-Time", "Consultations": [{"ID": "30", "Value": "Speech"}]}],
        "All_Therapy_Schedule_Plans": [{"ID": "40", "Name": {"ID": "10", "Name2": "stale name"},
            "Name.Registration_Id": "FAKE-1", "Status": "Draft", "Added_Time": "07/11/2026 20:14:22",
            "Branch": {"ID": "50", "Branch_Name": "Fictional centre"},
            "Schedule": [{"ID": "60", "Therapy": {"ID": "30", "Value": "Speech"},
                          "Consultant": {"ID": "20", "Name": "stale therapist name"},
                          "Per_Week": "2", "No_Of_Week": "3"}]}],
    }


@pytest.mark.parametrize("weekly,months", [(1, 1), (2, 3)])
def test_confirmed_frequency_and_months(weekly, months):
    assert prescription({"Per_Week": str(weekly), "No_Of_Week": str(months)}) == {
        "sessions_per_week": weekly, "duration_months": months}


def test_lookup_ids_are_authoritative_and_employment_type_is_not_therapy():
    raw = reports()
    original = deepcopy(raw)
    source = project_reports(raw)
    assert raw == original
    assert source["registrations"]["10"]["name"] == "Fictional child"
    assert "Sensitive_extra" not in source["registrations"]["10"]
    assert source["consultants"]["20"]["therapies"][0]["label"] == "Speech"
    assert source["consultants"]["20"]["employment_type"] == "Full-Time"
    assert source["plans"][0]["therapies"][0]["consultant_id"] == "20"
    assert not source["issues"]


def test_latest_plan_includes_draft_and_uses_numeric_indian_dates():
    raw = reports()
    older = deepcopy(raw["All_Therapy_Schedule_Plans"][0])
    older.update(ID="41", Added_Time="30/10/2026 16:10:54", Status="Scheduled")
    raw["All_Therapy_Schedule_Plans"].append(older)
    source = project_reports(raw)
    assert len(source["plans"]) == 2
    summary = readiness_summary(source)
    assert summary["unambiguous_current_plans"] == 1
    assert summary["plan_status_filter"] == "none"
    assert summary["ready_for_import"] is False
    assert "Fictional child" not in str(summary)
    assert "919999999999" not in str(summary)


def test_missing_assignments_and_invalid_duration_are_flagged_without_guessing():
    raw = reports()
    row = raw["All_Therapy_Schedule_Plans"][0]["Schedule"][0]
    row.update(Consultant={}, No_Of_Week="")
    source = project_reports(raw)
    assert source["plans"][0]["therapies"][0]["prescription"] is None
    assert {i["reason"] for i in source["issues"]} == {
        "missing_consultant_assignment", "invalid_frequency_or_duration"}


def test_duplicate_source_ids_and_nested_scalar_values_are_rejected():
    raw = reports()
    raw["All_Registrations"].append(deepcopy(raw["All_Registrations"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        project_reports(raw)
    raw = reports()
    raw["All_Consultants"][0]["Name"] = {"unexpected": "shape"}
    with pytest.raises(ValueError, match="scalar"):
        project_reports(raw)


def test_tied_latest_plans_require_review():
    raw = reports()
    duplicate = deepcopy(raw["All_Therapy_Schedule_Plans"][0])
    duplicate["ID"] = "41"
    raw["All_Therapy_Schedule_Plans"].append(duplicate)
    summary = readiness_summary(project_reports(raw))
    assert summary["issues_across_all_plan_versions"]["ambiguous_latest_plan"] == 1
    assert summary["unambiguous_current_plans"] == 0

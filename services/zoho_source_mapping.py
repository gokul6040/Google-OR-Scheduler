"""Project stable Zoho fields out of reports and join registration contacts.

Unknown columns are deliberately ignored. This is an explicit field mapping,
not a guess based on column order, names of people, or similar-looking values.
"""

from typing import Any


PLAN_REPORT = "Plan_Report"
CONSULTANT_REPORT = "All_Consultants"
REGISTRATION_REPORT = "All_Registrations"
BASE_REPORTS = (PLAN_REPORT, CONSULTANT_REPORT)

COMMON_FIELDS = {
    "ID": ("ID",),
    "Branch": ("Branch",),
    "Added_Time": ("Added_Time", "Added Time"),
    "Modified_Time": ("Modified_Time", "Modified Time"),
}
REGISTRATION_ID = ("RegistrationID", "Registration_Id", "Registration Id")
REPORT_FIELDS = {
    PLAN_REPORT: {
        **COMMON_FIELDS,
        "RegistrationID": REGISTRATION_ID,
        "PatientName": ("PatientName", "Patient_Name", "Name"),
        "Parent_No": ("Parent_No",),
        "Schedule": ("Schedule",),
    },
    CONSULTANT_REPORT: {
        **COMMON_FIELDS,
        "Name": ("Name",),
        "ConsultationType": ("ConsultationType", "Consultation_Type", "Consultation Type"),
        "Status": ("Status",),
    },
    REGISTRATION_REPORT: {
        **COMMON_FIELDS,
        "RegistrationID": REGISTRATION_ID,
        "Phone": ("Phone",),
    },
}


def report_names(reports: dict) -> tuple[str, ...]:
    """Keep the legacy two-report contract, optionally adding registrations."""
    if not isinstance(reports, dict) or set(reports) not in (
        set(BASE_REPORTS), set(BASE_REPORTS) | {REGISTRATION_REPORT}
    ):
        raise ValueError("Supply plans and consultants, optionally with All_Registrations")
    return (*BASE_REPORTS, REGISTRATION_REPORT) if REGISTRATION_REPORT in reports else BASE_REPORTS


def normalize_reports(reports: dict) -> dict[str, list[dict[str, Any]]]:
    """Select mapped scalar fields; reject conflicting aliases without values."""
    result = {}
    for report in report_names(reports):
        rows = reports[report]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{report}: expected a list of source records")
        result[report] = []
        for index, row in enumerate(rows, 1):
            mapped = {}
            for field, aliases in REPORT_FIELDS[report].items():
                present = [row[alias] for alias in aliases if alias in row]
                if not present:
                    continue
                if any(value is not None and not isinstance(value, (str, int))
                       or isinstance(value, bool) for value in present):
                    raise ValueError(f"{report}, row {index}: {field} must be a scalar field")
                values = ["" if value is None else str(value).strip() for value in present]
                if len(set(values)) != 1:
                    raise ValueError(f"{report}, row {index}: conflicting aliases for {field}")
                mapped[field] = values[0]
            result[report].append(mapped)
    return result

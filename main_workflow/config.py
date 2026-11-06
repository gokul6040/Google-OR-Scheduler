"""Separate, fail-closed configuration for the normal application's daily workflow."""

from dataclasses import dataclass
import os
import re


def enabled() -> bool:
    return os.getenv("MAIN_WORKFLOW_ENABLED", "false").strip().lower() == "true"


def data_source() -> str:
    """Choose existing test storage or the prepared normal application storage."""
    value = os.getenv("MAIN_WORKFLOW_DATA_SOURCE", "main").strip().lower()
    if value not in {"main", "test"}:
        raise ValueError("MAIN_WORKFLOW_DATA_SOURCE must be main or test")
    return value


def local_no_auth() -> bool:
    """Permit password-free access only for the explicitly selected local test app."""
    return (enabled() and data_source() == "test"
            and os.getenv("MAIN_WORKFLOW_LOCAL_NO_AUTH", "false").lower() == "true")


def requires_access_key() -> bool:
    """Select the main-page access policy independently of its database source."""
    return os.getenv("MAIN_WORKFLOW_REQUIRE_ACCESS_KEY", "true").strip().lower() != "false"


@dataclass(frozen=True)
class WorkflowConfig:
    admin_token: str
    admin_id: str
    owner: str
    app: str
    plan_report: str
    consultant_report: str
    schedule_form: str
    schedule_report: str
    live: bool = False
    registration_report: str = ""

    @property
    def source_name(self) -> str:
        return f"{self.owner}/{self.app}"

    @property
    def export_target(self) -> str:
        return f"{self.source_name}/{self.schedule_report}"

    @classmethod
    def from_environment(cls) -> "WorkflowConfig":
        fields = {field: os.getenv(f"MAIN_WORKFLOW_{name}", "").strip() for field, name in {
            "admin_token": "ADMIN_TOKEN", "admin_id": "ADMIN_ID", "owner": "ZOHO_OWNER",
            "app": "ZOHO_APP", "plan_report": "ZOHO_PLAN_REPORT",
            "consultant_report": "ZOHO_CONSULTANT_REPORT", "schedule_form": "ZOHO_SCHEDULE_FORM",
            "schedule_report": "ZOHO_SCHEDULE_REPORT",
        }.items()}
        if not all(fields.values()) or len(fields["admin_token"]) < 32:
            raise ValueError("Configure the separate main-workflow admin key, operator ID and all Zoho report/form names first")
        if (len(fields["admin_id"]) > 80 or any(ord(c) < 32 for c in fields["admin_id"])
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", fields[name]) for name in (
                    "owner", "app", "plan_report", "consultant_report", "schedule_form", "schedule_report"))):
            raise ValueError("Invalid main-workflow operator or Zoho link name")
        if fields["app"] == "test-gokul":
            raise ValueError("The main workflow must not use the isolated test-gokul app")
        registration_report = os.getenv("MAIN_WORKFLOW_ZOHO_REGISTRATION_REPORT", "").strip()
        if registration_report and (
            not re.fullmatch(r"[A-Za-z0-9_.-]+", registration_report)
            or registration_report in {fields["plan_report"], fields["consultant_report"]}
        ):
            raise ValueError("Configure a distinct, valid registration report link name")
        if os.getenv("MAIN_WORKFLOW_SOURCE_TIMEZONE") != "Asia/Kolkata":
            raise ValueError("Verify both main source timestamps use IST, then set MAIN_WORKFLOW_SOURCE_TIMEZONE=Asia/Kolkata")
        return cls(**fields, live=os.getenv("MAIN_WORKFLOW_LIVE", "false").lower() == "true",
                   registration_report=registration_report)

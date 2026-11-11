"""Explicit main targets; reuse the tested Zoho HTTP and export protocol."""

from dataclasses import replace
import os
from urllib.parse import urlsplit

from flow_test.zoho_daily import snapshot_criteria, window_criteria
from flow_test.zoho_import import REPORTS
from main_workflow.config import WorkflowConfig
from services.zoho_creator_service import (
    ZohoCreatorSettings, get_report_records, refresh_access_token, settings_from_environment,
)
from services.zoho_source_mapping import REGISTRATION_REPORT


class ZohoProvider:
    key_prefix = "main-"
    fictional = False
    def __init__(self, config: WorkflowConfig) -> None:
        self.config = config

    def settings(self) -> ZohoCreatorSettings:
        shared = settings_from_environment()
        credentials = {field: os.getenv(f"MAIN_WORKFLOW_ZOHO_{field.upper()}") or getattr(shared, field)
                       for field in ("client_id", "client_secret", "refresh_token")}
        return replace(shared, **credentials, account_owner=self.config.owner, app_link_name=self.config.app,
                       form_link_name=self.config.schedule_form, report_link_name=self.config.schedule_report,
                       api_url=os.getenv("MAIN_WORKFLOW_ZOHO_API_URL", shared.api_url).rstrip("/"),
                       accounts_url=os.getenv("MAIN_WORKFLOW_ZOHO_ACCOUNTS_URL", shared.accounts_url).rstrip("/"))

    def verify(self, settings: ZohoCreatorSettings) -> None:
        url = urlsplit(settings.api_url)
        if ((settings.account_owner, settings.app_link_name, settings.form_link_name, settings.report_link_name) != (
                self.config.owner, self.config.app, self.config.schedule_form, self.config.schedule_report)
                or settings.app_link_name == "test-gokul" or url.username or url.query or url.fragment
                or not ((url.scheme == "https" and url.hostname == "www.zohoapis.in" and not url.path)
                        or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
                            and url.path in {"", "/zoho"} ))):
            raise ValueError("Zoho target does not match the explicitly configured main workflow")

    def fetch(self, since: str | None, until: str) -> dict:
        settings = self.settings()
        self.verify(settings)
        try:
            token = refresh_access_token(settings)["access_token"]
            from main_workflow.thanal_sync import BRANCH, PLANS, enabled as thanal_enabled
            if thanal_enabled(self.config):
                consultants = get_report_records(settings, token, self.config.consultant_report)
                branches = {str(b["ID"]) for row in consultants for b in [row.get("Branch", {})]
                            if isinstance(b, dict) and b.get("Branch_Name") == BRANCH
                            and str(b.get("ID", "")).isdigit()}
                if len(branches) != 1:
                    raise ValueError("Approved Calicut branch must resolve to one lookup ID")
                branch_id = next(iter(branches))
                registrations = get_report_records(settings, token, self.config.registration_report)
                plans = get_report_records(settings, token, self.config.plan_report,
                                           criteria="Branch == " + branch_id)
                if any(not isinstance(row.get("Branch"), dict)
                       or str(row["Branch"].get("ID")) != branch_id for row in plans):
                    raise ValueError("Plan report returned rows outside the approved branch")
                from main_workflow.thanal_sync import select_fields
                return select_fields({PLANS: plans, "All_Consultants": consultants, "All_Registrations": registrations})
            criteria = (snapshot_criteria(until) if self.config.registration_report
                        else window_criteria(since, until))
            targets = dict(zip(REPORTS, (self.config.plan_report, self.config.consultant_report)))
            if self.config.registration_report:
                targets[REGISTRATION_REPORT] = self.config.registration_report
            return {logical: get_report_records(settings, token, actual, criteria=criteria)
                    for logical, actual in targets.items()}
        except (ValueError, OSError):
            raise ValueError("Zoho source fetch failed. Check its saved connection and configured reports; no import was attempted") from None


class TestZohoProvider:
    """Retain the authorized test-gokul contract when main uses test records."""

    key_prefix = "test-"
    fictional = True

    @staticmethod
    def settings() -> ZohoCreatorSettings:
        from flow_test.zoho_push import export_settings

        return export_settings()

    @staticmethod
    def verify(settings: ZohoCreatorSettings) -> None:
        from flow_test.zoho_push import verify_target

        verify_target(settings)

    @staticmethod
    def fetch(since: str | None, until: str) -> dict:
        from flow_test.zoho_daily import fetch_reports

        return fetch_reports(since=since, until=until)


def workflow_connection() -> tuple[WorkflowConfig, ZohoProvider | TestZohoProvider]:
    """Resolve the same data source used by the main application's root UI."""
    from main_workflow.config import data_source

    if data_source() == "test":
        from main_workflow.retained_data import access_key

        config = WorkflowConfig(access_key(required=False), os.getenv("MAIN_WORKFLOW_ADMIN_ID", "main_app_operator"),
                                "nidhin.kumar_turnb", "test-gokul", "Plan_Report", "All_Consultants",
                                "Scheduler", "Scheduler_Report")
        return config, TestZohoProvider()
    config = WorkflowConfig.from_environment()
    return config, ZohoProvider(config)

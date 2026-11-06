"""User-run one-time OAuth consent, isolated from legacy and test refresh tokens."""

from pathlib import Path

from flow_test.zoho_authorize import authorize
from main_workflow.config import data_source
from main_workflow.provider import workflow_connection


def main() -> None:
    _config, provider = workflow_connection()
    is_test_data = data_source() == "test"
    root = Path(__file__).resolve().parents[1]
    authorize(provider.settings(), root / ("flow_test/.env" if is_test_data else ".env"),
              verify=provider.verify,
              token_key="FLOW_TEST_ZOHO_REFRESH_TOKEN" if is_test_data else "MAIN_WORKFLOW_ZOHO_REFRESH_TOKEN",
              label="main application with test data" if is_test_data else "main workflow")


if __name__ == "__main__":
    main()

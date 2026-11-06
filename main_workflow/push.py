"""Manual main-schedule finalisation/export; preview is the default."""

import argparse
from datetime import date
import json

from flow_test.zoho_push import REQUIRED_SCOPES, run_push
from main_workflow.client import api
from main_workflow.provider import workflow_connection
from services.zoho_creator_service import refresh_access_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        config, provider = workflow_connection()
        settings = provider.settings()
        provider.verify(settings)
        connection = refresh_access_token(settings)
        if args.apply and not REQUIRED_SCOPES <= set(connection.get("scope", "").split()):
            raise ValueError("Missing main export permissions")
        result = run_push(settings, connection["access_token"], "", "", date.fromisoformat(args.date),
                          apply=args.apply, exchange=lambda operation, body: api(config, "export/" + operation, body),
                          verify=provider.verify, key_prefix=provider.key_prefix)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError):
        print("Main schedule push stopped. Check main form fields, unique Scheduler_Key and connection permissions. Any unfinished batch remains available for retry.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

"""Manually fetch the configured main source and prepare a reviewed daily draft."""

import argparse
from datetime import datetime, timezone
import json

from flow_test.zoho_import import SOURCE_TIMEZONES, checkpoint_time, convert_reports
from main_workflow.client import api
from main_workflow.provider import workflow_connection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Update records and prepare a draft; held offers may resume when messaging is enabled")
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    try:
        config, provider = workflow_connection()
        if args.reconcile and not args.apply:
            raise ValueError("Use --reconcile with --apply")
        status = api(config, "status") if args.apply else None
        since = status["checkpoint"] if status else None
        if status and (status.get("source") != config.source_name or status.get("sourceTimezones") != SOURCE_TIMEZONES):
            raise ValueError("The server source/timestamp contract does not match this main client")
        if status and status.get("reconciliationRequired") and not args.reconcile:
            raise ValueError("Run --apply --reconcile once before normal incremental imports")
        until = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if since and checkpoint_time(since) >= checkpoint_time(until):
            print(json.dumps({"mode": "NO NEW WINDOW", "checkpoint": since}))
            return
        reports = provider.fetch(None if args.reconcile else since, until)
        if args.apply:
            body = {"source": config.source_name, "reports": reports,
                    "sourceTimezones": SOURCE_TIMEZONES, "syncWindow": {"from": since, "to": until},
                    "fullSnapshot": args.reconcile}
            from main_workflow.thanal_sync import enabled as thanal_enabled
            result = (api(config, "import", body, timeout=900) if thanal_enabled(config)
                      else api(config, "import", body))
        else:
            from main_workflow.thanal_sync import enabled as thanal_enabled, prepare_snapshot
            if thanal_enabled(config):
                result = {"mode": "VALIDATION ONLY", **prepare_snapshot(reports)["summary"]}
            else:
                scenario = convert_reports(reports, fictional=provider.fictional, source=config.source_name)
                result = {"mode": "VALIDATION ONLY", "patients": len(scenario["patients"]),
                          "consultants": len(scenario["therapists"]), "messages_sent": 0}
        result["downloaded"] = {name: len(rows) for name, rows in reports.items()}
        print(json.dumps(result, indent=2))
        if result.get("draftError") or result.get("partial"):
            raise SystemExit(1)
    except ValueError as exc:
        print(f"Main source sync stopped: {exc}")
        raise SystemExit(1) from None
    except OSError:
        print("Main source sync stopped: the connection or local setup is unavailable after bounded safe retries; the previous checkpoint was retained.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

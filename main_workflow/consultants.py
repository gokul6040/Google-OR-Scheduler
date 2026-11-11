"""Validate Thanal's complete consultant master; --apply explicitly imports it."""

import argparse
import json
import os
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the validated consultant master only")
    args = parser.parse_args()
    try:
        from main_workflow.config import WorkflowConfig, data_source
        from main_workflow.provider import ZohoProvider
        from services.zoho_creator_service import get_report_records, refresh_access_token
        from services.thanal_consultant_import import sync_consultants
        from database.db import engine
        from database.models import AuditLog
        from database.workflow_lock import workflow_locks
        from sqlalchemy.orm import sessionmaker

        config = WorkflowConfig.from_environment()
        if (data_source() != "main" or config.source_name != "thanal/eic"
                or config.consultant_report != "All_Consultants"
                or not os.getenv("THERAPY_SCHEDULER_DATABASE_URL", "").strip()):
            raise ValueError("Select the explicit Thanal main database and All_Consultants report")
        provider = ZohoProvider(config)
        settings = provider.settings()
        provider.verify(settings)
        token = refresh_access_token(settings)["access_token"]
        rows = get_report_records(settings, token, config.consultant_report)
        lock, inbound_lock = workflow_locks(engine)
        def audit(session, action, entity):
            session.add(AuditLog(action=action, entity=entity, user=config.admin_id))
        flow = SimpleNamespace(source_name=config.source_name, is_test=False, lock=lock,
                               inbound_lock=inbound_lock, sessions=sessionmaker(bind=engine), _audit=audit)
        try:
            result = sync_consultants(flow, rows, apply=args.apply)
        finally:
            lock.close()
            inbound_lock.close()
        print(json.dumps(result, indent=2))
        if not result["ready"]:
            raise SystemExit(1)
    except Exception as exc:
        # Provider and database exception strings can contain tokens or connection details.
        print(json.dumps({"error": "Consultant sync stopped; verify configuration and validation findings.",
                          "error_type": type(exc).__name__, "messages_sent": 0}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

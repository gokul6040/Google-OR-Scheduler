"""Default launcher for the EIC Therapy Scheduler web application."""

import argparse
import os
from pathlib import Path

from web_app import run, run_response_demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response-demo", action="store_true")
    parser.add_argument("--local-ui", action="store_true", help="Use configured records without imports or background messaging")
    parser.add_argument("--port", type=int)
    parser.add_argument("--test-data", action="store_true", help="Run the main app on the test database using its existing WhatsApp worker")
    parser.add_argument("--test-database", type=Path, help="Use this existing SQLite test file instead of the configured test database")
    parser.add_argument("--pause-messaging", action="store_true", help="Hold new live test messages instead of queueing them for the remote sender")
    args = parser.parse_args()
    if args.test_database and not args.test_database.is_file():
        parser.error("--test-database must name an existing test database file")
    if args.response_demo and (args.test_data or args.test_database):
        parser.error("Choose the main app with test data or the separate response demo")
    if args.test_data or args.test_database:
        os.environ.update(MAIN_WORKFLOW_ENABLED="true", MAIN_WORKFLOW_DATA_SOURCE="test",
                          MAIN_WORKFLOW_LIVE="false", MAIN_WORKFLOW_LOCAL_NO_AUTH="true",
                          MAIN_WORKFLOW_REMOTE_SENDER="false" if args.pause_messaging else "true")
        if args.test_database:
            os.environ["MAIN_WORKFLOW_TEST_DATABASE_URL"] = "sqlite:///" + args.test_database.resolve().as_posix()
            os.environ.pop("MAIN_WORKFLOW_TEST_DATABASE_SCHEMA", None)
    if args.response_demo:
        run_response_demo(port=args.port or 8766)
    else:
        run(port=args.port or 8765, local_ui_only=args.local_ui)

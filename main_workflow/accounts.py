"""Explicit portal storage/bootstrap command; never imports the default .env DB."""
import argparse
import getpass
import os

from sqlalchemy import create_engine
from services.portal_auth import Accounts, check_password_rule, prepare


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-env", required=True, help="Name of the environment variable containing the verified target database URL")
    parser.add_argument("--prepare", action="store_true", help="Create only portal account tables")
    parser.add_argument("--first-admin", action="store_true")
    args = parser.parse_args()
    url = os.environ.get(args.database_env)
    if not url:
        parser.error("The named database environment variable is missing")
    engine = create_engine(url, hide_parameters=True)
    if args.prepare:
        prepare(engine)
        print("Portal account tables prepared. Clinical records were not changed.")
    if args.first_admin:
        name = input("Admin name: ").strip()
        email = input("Admin email: ").strip()
        store = Accounts(engine)
        print("Set the first admin's permanent password (input is hidden).")
        while True:
            password = getpass.getpass("New password (8+ characters, upper, lower and special): ")
            if password != getpass.getpass("Confirm password: "):
                print("Passwords did not match. Try again.")
                continue
            try:
                check_password_rule(password)  # Validate before writing any account.
                break
            except ValueError as exc:
                print(str(exc))
        store.create(name, email, "admin", actor="bootstrap", permanent_password=password)
        print("Admin ready. Enable PORTAL_AUTH_ENABLED when ready to require sign-in.")


if __name__ == "__main__":
    main()

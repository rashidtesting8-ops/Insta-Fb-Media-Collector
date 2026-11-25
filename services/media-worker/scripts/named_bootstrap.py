"""One-time interactive Named Admin bootstrap CLI.

Supports:
- `--disposable-test`: exercises bootstrap on an ephemeral temp DB (CI/QA).
- `--db <path> --verified-operator <operator_id>`: interactively provisions
  the first Named Admin on an existing worker database. Passwords are read
  exclusively via getpass (never CLI args or env) and never printed.
"""
import argparse
import getpass
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

from app.named_auth import bootstrap

MIN_PRODUCTION_PASSWORD_LEN = 12


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-test", action="store_true", help="Run against a disposable temp DB only")
    parser.add_argument("--db", type=Path, default=None, help="Path to worker jobs.sqlite for one-time Admin #1 bootstrap")
    parser.add_argument("--verified-operator", type=str, default="", help="Verified local operator identity for audit log")
    args = parser.parse_args()

    if args.disposable_test:
        if args.db is not None:
            parser.error("--disposable-test cannot be combined with --db")
        with tempfile.TemporaryDirectory(prefix="pgm-bootstrap-qa-") as directory:
            bootstrap(
                Path(directory) / "qa.sqlite",
                input("Synthetic account name: "),
                getpass.getpass("Disposable test password: "),
                verified_operator="disposable-cli-test",
            )
            print("Disposable bootstrap passed.")
        return

    operator = (args.verified_operator or "").strip()
    if args.db is None or not operator:
        parser.error("Real provisioning requires a designated, verified local bootstrap authority (--db and --verified-operator)")
    if not args.db.is_file():
        parser.error(f"Database file does not exist: {args.db}")

    name = input("Named Admin account name: ").strip()
    password = getpass.getpass("Named Admin password (min 12 chars): ")
    confirm = getpass.getpass("Confirm password: ")
    if len(password) < MIN_PRODUCTION_PASSWORD_LEN:
        print(f"Error: Password must be at least {MIN_PRODUCTION_PASSWORD_LEN} characters.", file=sys.stderr)
        raise SystemExit(1)
    if password != confirm:
        print("Error: Passwords do not match.", file=sys.stderr)
        raise SystemExit(1)

    try:
        principal_id = bootstrap(args.db, name, password, verified_operator=operator)
    except HTTPException as exc:
        print(f"Error ({exc.status_code}): {exc.detail}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"NAMED_BOOTSTRAP_OK principal_id={principal_id}")


if __name__ == "__main__":
    main()

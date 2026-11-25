"""One-time Named Admin bootstrap CLI (interactive terminal or single-use browser token).

Supports:
- `--disposable-test`: exercises bootstrap on an ephemeral temp DB (CI/QA).
- `--db <path> --verified-operator <operator_id>`: interactively provisions
  the first Named Admin on an existing worker database. Passwords are read
  exclusively via getpass (never CLI args or env) and never printed.
- `--issue-browser-token --db <path> --verified-operator <operator_id>`:
  mints a cryptographically random single-use setup capability for the
  loopback-only browser setup page (`/setup-admin`) with zero password input
  in the terminal.
"""
import argparse
from datetime import UTC, datetime
import getpass
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException

from app.named_auth import (
    MIN_BOOTSTRAP_PASSWORD_LEN,
    SETUP_TOKEN_DEFAULT_TTL_S,
    bootstrap,
    issue_setup_token,
)

MIN_PRODUCTION_PASSWORD_LEN = MIN_BOOTSTRAP_PASSWORD_LEN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-test", action="store_true", help="Run against a disposable temp DB only")
    parser.add_argument("--issue-browser-token", action="store_true", help="Issue a single-use browser setup token for /setup-admin")
    parser.add_argument("--ttl-seconds", type=int, default=SETUP_TOKEN_DEFAULT_TTL_S, help="Browser setup token lifetime in seconds (60-3600)")
    parser.add_argument("--db", type=Path, default=None, help="Path to worker jobs.sqlite for one-time Admin #1 bootstrap")
    parser.add_argument("--verified-operator", type=str, default="", help="Verified local operator identity for audit log")
    args = parser.parse_args()

    if args.disposable_test:
        if args.db is not None or args.issue_browser_token:
            parser.error("--disposable-test cannot be combined with --db or --issue-browser-token")
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

    if args.issue_browser_token:
        try:
            issued = issue_setup_token(args.db, verified_operator=operator, ttl_seconds=args.ttl_seconds)
        except HTTPException as exc:
            print(f"Error ({exc.status_code}): {exc.detail}", file=sys.stderr)
            raise SystemExit(1) from None
        expires_iso = datetime.fromtimestamp(issued["expires_at"], tz=UTC).isoformat()
        print("BROWSER_SETUP_URL=http://localhost:3000/setup-admin")
        print(f"BROWSER_SETUP_TOKEN={issued['token']}")
        print(f"BROWSER_SETUP_EXPIRES_UTC={expires_iso}")
        return

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

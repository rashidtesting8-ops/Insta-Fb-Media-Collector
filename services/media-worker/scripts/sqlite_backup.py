"""Online WAL-consistent SQLite backup, SHA-256 manifest, and restore verification.

Uses Python's built-in ``sqlite3.Connection.backup()`` API so committed WAL
frames are captured atomically without stopping the media worker. Never rely on
raw file copies (``cp`` / ``Copy-Item``) against a live WAL-mode SQLite database.

Examples:
    # Create a verified timestamped backup and retain the 14 newest copies:
    python scripts/sqlite_backup.py --db /data/projects/_system/jobs.sqlite --out-dir /backups --keep 14

    # Verify an existing backup file + SHA-256 sidecar and SQLite integrity:
    python scripts/sqlite_backup.py --verify /backups/jobs.sqlite.20261125T160000Z.bak
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_sqlite(path: Path) -> tuple[str, dict[str, int]]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        return integrity, counts
    finally:
        conn.close()


def create_backup(
    db_path: Path,
    out_dir: Path,
    *,
    prefix: str = "jobs.sqlite",
    keep: int = 14,
) -> dict[str, Any]:
    src_path = db_path.resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {src_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"{prefix}.{stamp}.bak"
    # Ensure distinct filename if invoked twice within the same second.
    suffix_idx = 1
    while target.exists():
        target = out_dir / f"{prefix}.{stamp}.{suffix_idx}.bak"
        suffix_idx += 1
    tmp_target = out_dir / f".{target.name}.tmp"

    src_uri = f"file:{src_path.as_posix()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True, timeout=30.0)
    dst_conn = sqlite3.connect(str(tmp_target), timeout=30.0)
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
    except Exception:
        dst_conn.close()
        src_conn.close()
        tmp_target.unlink(missing_ok=True)
        raise
    else:
        dst_conn.close()
        src_conn.close()

    integrity, table_counts = _inspect_sqlite(tmp_target)
    if integrity.lower() != "ok":
        tmp_target.unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed PRAGMA integrity_check: {integrity}")

    sha256 = _sha256_file(tmp_target)
    os.replace(tmp_target, target)

    sha_path = Path(f"{target}.sha256")
    sha_tmp = out_dir / f".{sha_path.name}.tmp"
    sha_tmp.write_text(f"{sha256}  {target.name}\n", encoding="utf-8")
    os.replace(sha_tmp, sha_path)

    pruned: list[str] = []
    if keep > 0:
        existing = sorted(
            out_dir.glob(f"{prefix}.*.bak"),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )
        for old in existing[keep:]:
            old.unlink(missing_ok=True)
            Path(f"{old}.sha256").unlink(missing_ok=True)
            pruned.append(old.name)

    return {
        "ok": True,
        "action": "backup",
        "source_db": str(src_path),
        "backup_file": str(target.resolve()),
        "sha256_file": str(sha_path.resolve()),
        "bytes": target.stat().st_size,
        "sha256": sha256,
        "integrity_check": integrity,
        "table_counts": table_counts,
        "pruned": pruned,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


def verify_backup(backup_path: Path, *, require_sha256: bool = True) -> dict[str, Any]:
    target = backup_path.resolve()
    if not target.is_file():
        return {"ok": False, "action": "verify", "backup_file": str(target), "error": "backup file not found"}

    sha_path = Path(f"{target}.sha256")
    actual_sha = _sha256_file(target)
    sha_verified: bool | None = None
    if sha_path.is_file():
        recorded = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
        sha_verified = recorded == actual_sha.lower()
        if not sha_verified:
            return {
                "ok": False,
                "action": "verify",
                "backup_file": str(target),
                "error": "sha256 mismatch",
                "expected_sha256": recorded,
                "actual_sha256": actual_sha,
            }
    elif require_sha256:
        return {
            "ok": False,
            "action": "verify",
            "backup_file": str(target),
            "error": f"missing sha256 sidecar: {sha_path.name}",
        }

    try:
        integrity, table_counts = _inspect_sqlite(target)
    except sqlite3.DatabaseError as exc:
        return {
            "ok": False,
            "action": "verify",
            "backup_file": str(target),
            "error": f"sqlite error: {exc}",
        }

    ok = integrity.lower() == "ok"
    return {
        "ok": ok,
        "action": "verify",
        "backup_file": str(target),
        "bytes": target.stat().st_size,
        "sha256": actual_sha,
        "sha256_verified": sha_verified,
        "integrity_check": integrity,
        "table_counts": table_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Online WAL-consistent SQLite backup and verifier.")
    parser.add_argument("--db", type=Path, help="Path to source SQLite database (backup mode).")
    parser.add_argument("--out-dir", type=Path, help="Destination directory for backups (backup mode).")
    parser.add_argument("--prefix", default="jobs.sqlite", help="Backup filename prefix.")
    parser.add_argument("--keep", type=int, default=14, help="Number of newest backups to retain (0 keeps all).")
    parser.add_argument("--verify", type=Path, help="Verify an existing backup file and its .sha256 sidecar.")
    parser.add_argument(
        "--allow-missing-sha256",
        action="store_true",
        help="Allow --verify when no .sha256 sidecar exists.",
    )
    args = parser.parse_args(argv)

    if args.verify is not None:
        report = verify_backup(args.verify, require_sha256=not args.allow_missing_sha256)
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 1

    if args.db is None or args.out_dir is None:
        parser.error("Provide either --verify <backup> or both --db <path> and --out-dir <dir>.")

    try:
        report = create_backup(args.db, args.out_dir, prefix=args.prefix, keep=args.keep)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "action": "backup", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

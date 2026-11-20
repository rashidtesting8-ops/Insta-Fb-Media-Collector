"""Phase 2 website media collector adapter.

Bridges the legacy builder's proven crawler into the job architecture:

- Crawls via `app/legacy_collector.py` (byte-for-byte vendored from
  `reference/source/pet-gromming-agent-factory-main (1).zip`; SHA-256
  5fe260e2db08…). All SSRF defence (safe_url, verified-socket dialing, redirect
  hop cap), robots enforcement, same-site scoping, bounded page/media limits,
  per-file size caps, sha256 de-duplication, SVG sanitisation and
  prompt-injection neutralisation are inherited unchanged from that module.
- Relocates downloaded media to `<job_dir>/website/` (Phase 2 approval #6).
- Preserves legacy provenance (pages.json, snapshots, social link records)
  under `<job_dir>/research/`.
- Produces the normalized manifest at `<job_dir>/manifest.json` via
  `app/manifest.py` and re-validates every downloaded file on disk.

The Phase 1 `collect_stub` placeholder was removed: real extraction replaced
it (tests moved to tests/test_website_collector.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from app import legacy_collector
from app.manifest import (
    DOWNLOADED,
    build_manifest,
    validate_against_disk,
    write_manifest,
)

DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_MEDIA = 30

CollectFn = Callable[..., dict[str, Any]]


@dataclass
class WebsiteExtractionResult:
    job_id: str
    website_url: str
    status: str  # completed | partial_success | failed
    pages_crawled: int = 0
    asset_candidates: int = 0
    assets_downloaded: int = 0
    duplicates: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0
    manifest_path: str | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _absorb_research(project_root: Path, target: Path) -> None:
    """Relocate legacy provenance (pages/snapshots/social-sources/run-summary)
    to <job>/research/, moving (not copying) so the legacy tree can be pruned."""
    src = project_root / "research"
    if not src.is_dir():
        return
    if not target.exists():
        os.replace(src, target)
        return
    for file in src.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(src)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            os.replace(file, dst)
    shutil.rmtree(src)


def _prune_if_empty(root: Path) -> bool:
    remaining = [p for p in root.rglob("*") if p.is_file()]
    if not remaining:
        shutil.rmtree(root)
        return True
    return False


def run_website_extraction(
    job_id: str,
    job_dir: str | Path,
    website_url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_media: int = DEFAULT_MAX_MEDIA,
    allow_private: bool = False,
    collector: CollectFn | None = None,
) -> WebsiteExtractionResult:
    """Run the website crawl for one job and produce the normalized manifest.

    Never raises for crawl-level failures; they are reported in `issues` and
    reflected in `status` so the caller can record them truthfully.
    (Invalid input URLs raise ValueError from `safe_url` before any request.)
    """
    collector = collector or legacy_collector.collect
    job_dir = Path(job_dir)
    (job_dir / "website").mkdir(parents=True, exist_ok=True)

    summary = collector(
        website_url,
        job_dir,
        max_pages=max_pages,
        max_media=max_media,
        allow_private=allow_private,
    )
    project_root = Path(str(summary["project_folder"]))
    rows = _load_json(project_root / "research" / "asset-manifest.json", [])
    pages = _load_json(project_root / "research" / "pages.json", [])
    issues = list(summary.get("issues") or [])

    # Relocate media from the legacy layout to <job>/website/.
    moved, missing = 0, 0
    for row in rows:
        if row.get("status") != DOWNLOADED or not row.get("file"):
            continue
        src = project_root / str(row["file"])
        if not src.is_file():
            missing += 1
            continue
        dst = job_dir / "website" / src.name
        os.replace(src, dst)
        moved += 1

    _absorb_research(project_root, job_dir / "research")
    pruned = _prune_if_empty(project_root)

    bytes_downloaded = sum(int(r.get("bytes") or 0) for r in rows if r.get("status") == DOWNLOADED)
    manifest = build_manifest(
        job_id=job_id,
        website_url=website_url,
        rows=rows,
        pages=pages,
        issues=issues,
        limits={"max_pages": max_pages, "max_media": max_media},
        pages_crawled=int(summary.get("pages_crawled") or 0),
        asset_candidates=int(summary.get("asset_candidates") or 0),
        bytes_downloaded=bytes_downloaded,
    )

    # Belt-and-braces: re-hash every downloaded file on disk; downgrade on mismatch.
    problems = validate_against_disk(job_dir, manifest)
    for item in manifest["assets"]:
        if item["extraction_status"] == DOWNLOADED and any(p.startswith(item["asset_id"] + ":") for p in problems):
            item["extraction_status"] = "integrity_mismatch"
            item["local_path"] = None
    if problems:
        issues = issues + [{"reason": "manifest_validation", "detail": p} for p in problems[:20]]
        manifest["coverage"]["issues"] = issues[:70]

    manifest_path = write_manifest(job_dir, manifest)
    counts = manifest["counts"]

    notes = [
        f"media relocated to website/ ({moved} files moved; {missing} listed but missing on disk)",
        f"legacy project folder pruned: {pruned}",
    ]
    if counts["pages_crawled"] == 0:
        notes.append("No pages crawled; site may be unreachable or blocked.")
    if problems:
        notes.append(f"{len(problems)} manifest validation problem(s); see coverage.issues.")

    return WebsiteExtractionResult(
        job_id=job_id,
        website_url=website_url,
        status=manifest["source_status"],
        pages_crawled=counts["pages_crawled"],
        asset_candidates=counts["asset_candidates"],
        assets_downloaded=counts["downloaded"],
        duplicates=counts["duplicates"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        bytes_downloaded=counts["bytes_downloaded"],
        manifest_path=str(manifest_path),
        issues=issues[:50],
        notes=notes,
    )


def sha256_file(path: str | Path) -> str:
    """Small helper kept for tests/debugging; the manifest hashes via streams."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

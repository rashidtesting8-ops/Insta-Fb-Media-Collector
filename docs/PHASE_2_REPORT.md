# Phase 2 Report — Website Media Collector and Real Manifest Integration

**Date:** 20 November 2026
**Scope delivered:** legacy crawler vendored into the worker behind the existing job architecture; website-only extraction live; normalized manifest replacing the stub; duplicates and partial failures handled; SSRF/robots/rate/size protections inherited unchanged; automated offline tests; one authorized low-volume live smoke test.
**Out of scope (unchanged):** Instagram/Facebook, gallery-dl/Instaloader/yt-dlp integration, AI selection, Google Drive, frontend, Docker cleanup.

---

## 1. Changed files

**Worker code (services/media-worker/):**
| File | Change |
|---|---|
| `app/legacy_collector.py` | **NEW — vendored byte-for-byte** from `reference/source/pet-gromming-agent-factory-main (1).zip` (`src/engine/collector.py`); SHA-256 `5fe260e2db08dba502311fb780bad5501c380df0202108852e852580f92835dc` verified identical to the ZIP entry and the read-only reference copy. Zero code drift; all SSRF/robots/limits logic inherited as-is. |
| `app/manifest.py` | **NEW** — normalized manifest builder: asset_id, source_url, local_path, type, filename, dimensions, bytes, sha256, captured_at, `post_date: null` + `date_status: "unknown"` (spec §01 — website dates are never invented), `rights_status: "unverified"` (spec §14), per-item `extraction_status` + detail; atomic writes; deterministic `validate_against_disk` (path safety, size, re-hash). |
| `app/collector.py` | **REWRITTEN** — `run_website_extraction()`: runs the vendored crawler under `<job>/`, relocates media to `<job>/website/`, **moves** legacy provenance to `<job>/research/` and prunes the legacy tree, builds + validates the manifest, classifies `completed / partial_success / failed` honestly. Phase-1 `collect_stub` removed (superseded). |
| `app/service.py` | `_process_job` now performs real website extraction; social URLs recorded honestly as `not_implemented` (Phase 3) instead of fake "queued"; `max_pages` (1–25, default 10) + `max_media` (1–80, default 30) added to `POST /jobs`; `GET /jobs/{id}/manifest` now serves the real manifest (graceful note for pre-Phase-2 jobs). `/health`, `/status`, `/jobs`, `/jobs/{id}` unchanged. |
| `requirements.txt` | Added `beautifulsoup4`, `Pillow`, `requests` (crawler dependencies), `pytest` (tests ship in the image; host has no Python). |
| `Dockerfile` | `COPY tests ./tests` added; otherwise unchanged (non-root, healthcheck). |
| `tests/test_website_collector.py` | **NEW** — 10 tests (below). |
| `tests/test_phase1_smoke.py` | `collect_stub` test removed; social stub tests retained. |

**Untouched:** pristine ZIP, `reference/legacy-extracted/` (verified hash-identical after the work), legacy containers/volumes/images, both n8n instances, `.env`, all backups, old C: storage folder, old job records.

## 2. Tests (in-container, offline; `python -m pytest tests -q` → **10 passed in 2.98s**)

Ported from the legacy suite + new coverage, all against an in-process `ThreadingHTTPServer` fixture on 127.0.0.1 (`allow_private=True`, exactly the legacy test pattern; no external network):

1. `test_utilities` — srcset/same-site/slugify (legacy port).
2. `test_safe_url_fail_closed_for_private_targets` — loopback/LAN literals and credential URLs rejected with no network calls.
3. `test_prompt_injection_neutralization` — legacy port.
4. `test_website_extraction_end_to_end` — 2 pages crawled, 3 identical PNGs → 1 downloaded + 1 duplicate; manifest schema/counts/unique asset IDs; downloaded file exists on disk with **matching sha256**, dimensions 320×240; `local_path` under `website/`; `post_date` null / `date_status` unknown / rights unverified; duplicate carries `same_as`; `validate_against_disk` clean; provenance kept under `research/`; legacy layout pruned; social links only recorded.
5. `test_partial_failure_is_classified_honestly` — one broken image → `partial_success`, failed item keeps error detail, disk validation clean.
6. `test_unreachable_site_is_not_marked_successful` — 404-everything handler → `failed`, never faked.
7. `test_private_network_target_rejected_without_allow_private` — loopback URL refused.
8. `test_manifest_holds_unknown_dates_only` — every item: unknown date, unverified rights.

Deviation found during test bring-up (fixed): the first adapter version *copied* legacy `research/` and failed to prune the legacy tree; now it moves it (`os.replace`) — the e2e test caught it. Also corrected two test-fixture bugs of my own (missing `tmp_path` arg; the "unreachable" fixture initially served a 200 directory listing).

## 3. Authorized live smoke test (low volume, after tests passed)

Target selection note (honest deviation): several real groomer candidates were unreachable or refused from this network (`puppalacesalon.com` — never existed; `barkbus.com`, `thegroomroom.co.uk` — transport/503 refusals; websearch unavailable). To keep the smoke controlled, rights-clean and robots-permissive, the crawl ran against a real, in-domain article: **`https://en.wikipedia.org/wiki/Dog_grooming`** with `max_pages=5`, `max_media=12`. Business-site crawling remains the production use under operator authorization.

**Job `14fec3250eba4844a98954275e35e238`** — `queued → running → partial_success` in ~10 s:

| Metric | Value |
|---|---|
| Pages crawled | 3 (article + same-site links) |
| Media candidates discovered | 57 |
| Downloaded | 3 files, 142,460 bytes (139 KB) — all on **D:** |
| Duplicates | 0 |
| Rejected honestly | 7 × `blocked_by_robots` (thumb.wikimedia.org + `/w/index.php` edit links — robots enforcement proven live) + 2 × `invalid_image` (non-decodable `File:` page payloads) |
| Page-level issues | 2 `robots_disallow` recorded |
| Manifest | `D:\pgm-media-storage\jobs\<job>\manifest.json` (schema `2.0-website`, 12 items, per-item statuses + details) |

**Verification evidence:**
- Files exist on the D: bind mount: `website\` (3 SVGs), `research\` (pages.json, 3 HTML snapshots, social-sources.json, run-summary.json, legacy asset-manifest), `manifest.json`.
- Integrity: manifest sha256 `36e4c7c54b31…` == disk re-hash `36e4c7c54b31…` (**match: True**); adapter-level `validate_against_disk` clean at write time.
- Dates/rights: `post_date=null`, `date_status=unknown`, `rights_status=unverified`, `captured_at` recorded.
- Pre-Phase-2 jobs `22fef997…` and `26c48d32…` still HTTP 200, untouched; their manifest endpoint degrades gracefully.
- Old C: `storage\jobs` untouched (no new writes since D4); all backups intact.
- Containers: `pgm-media-collector-1` (rebuilt+recreated), `pgm-media-n8n-1` and legacy `website-builder-testing-main-n8n-1` untouched and healthy; probe webhook **HTTP 200, `auth=verified`**, sentinel readable.
- Disk: C: 29.60 GB free / D: 75.79 GB free.

## 4. Limitations (truthful)

1. **`failed` count in the job record is aggregated** (robots-blocked + invalid + errors); the manifest keeps exact per-item statuses. A Phase-3 refinement could split the job-level counters.
2. **Candidate ordering is discovery-order** (legacy behavior): the smoke's 12-attempt cap hit icons/logos before article photos; several real photos were additionally refused by `thumb.wikimedia.org`'s robots policy. This is honest limitation of bounded first-N extraction, recorded per item.
3. **No wall-clock cap around the whole job** beyond the crawler's internal 210 s crawl deadline + per-file caps.
4. **Social sources are recorded, never scraped**; their job status is `not_implemented` (Phase 3).
5. SVGs carry no width/height (raster-only; matches the legacy module).
6. The smoke target was not a real grooming business (network refusals above); business-site end-to-end validation remains to be exercised with an operator-authorized client site.

## 5. Phase 3 readiness

Ready: website extraction live end-to-end with honest statuses and a real, disk-validated manifest on D:; job API extended (bounded `max_pages`/`max_media`); credential/webhook/probe patterns proven; legacy crawler fully reusable with zero drift. Phase 3 (n8n WF01/WF02/WF05 orchestration + social adapters + per-source dispatch) can proceed on approval; the worker's `sources.instagram/facebook` fields are already wired for truthful `not_implemented` states.

**Phase 2 complete. Stopping here per instruction — Phase 3 not started.**

# Phase 0 — Read-Only Engineering Audit
**Project:** AI-Powered Pet Grooming Media Intelligence Platform
**Date:** 20 November 2026
**Mode:** Plan / read-only. No source edits, no scraping, no dependency installs, no Docker start/stop, no secrets accessed, no network calls.

---

## 0. Scope compliance and critical workspace finding

| Constraint | Status |
|---|---|
| Read `AGENTS.md`, `docs/PROJECT_SPEC.md`, `docs/AI_VISUAL_DIRECTOR_V2.md`, original prompt | Done — **read from inside `Pet_Grooming_OpenCode_Starter_Kit_V2.zip` as read-only streams** |
| Read `docs/Developer_Guide_V2.pdf` | **Could not be parsed by this model.** Per `PROJECT_SPEC.md` §4 and your instruction, the Markdown spec was treated as authoritative. The standalone PDF `Pet_Grooming_AI_Media_Automation_Developer_Guide_V2_OpenCode.pdf` is byte-identical in size (124,499 B) to the one in the starter kit. |
| Inspect legacy source under `reference/legacy-extracted/` | **Path does not exist.** No bootstrap has occurred. Audited the canonical source directly from `pet-gromming-agent-factory-main (1).zip` instead. |
| No source file modification | Respected. This audit document is the only file written. |
| No live scraping / network / Docker / installs | Respected. |
| No secrets | Respected. `website-builder-testing-main/.env` and the nested `(1)/.../.env` were deliberately not opened. |

### Workspace reality
`C:\Users\CZ\Downloads\Insta and Fb Scrpaer Intellegence` is **empty** (0 entries). None of the paths named in your prompt exist locally yet. The starter kit ZIP contains exactly:

```
README_FIRST.md, AGENTS.md, OpenCode_Phase_0_Audit_Prompt.txt,
docs/PROJECT_SPEC.md (32 KB), docs/Developer_Guide_V2.pdf,
docs/AI_VISUAL_DIRECTOR_V2.md, docs/AI_VISUAL_DIRECTOR_ORIGINAL.txt
```

### Which source is authoritative?
`README_FIRST.md` and `PROJECT_SPEC.md` both name **`pet-gromming-agent-factory-main (1).zip`** as the baseline. Hash comparison of key files shows the extracted working copies have **drifted** and must not be mistaken for the baseline:

| File | ZIP `(1)` | `website-builder-testing-main` | `website-builder-testing-push` |
|---|---|---|---|
| `requirements.txt` | `861ebb68…` | `caf16718…` | `9a264a07…` |
| `Dockerfile` | `e2b20a82…` | `752e8f1d…` | `face9878…` |
| `docker-compose.yml` | `3aeee0f0…` | `ade9a6fa…` | `e21de509…` |
| `social_collect.py` | `328ebcc0…` | `ec52df8b…` | `79a049ef…` |
| `llm/adapter.py` | `583c7705…` | `1534bee1…` | `f1c9283f…` |
| `app/rights.py` | `d45bcf22…` | `3adbfa94…` | `4403d6a4…` |

Also present but **not usable as `reference/legacy-extracted/`**:
- `pet-gromming-agent-factory-main/` — contains only 2 SQLite files (`projects/editvista.sqlite3`, `projects/_system/jobs.sqlite`).
- `pet-gromming-agent-factory-main (1)/pet-gromming-agent-factory-main/` — a full but nested copy containing a real `.env`, `diag_bundle*.zip`, `.cache` LLM outputs and Python caches (9,756 files). Do not use as-is; it carries secrets/runtime artifacts.
- The two `website-builder-testing-*` trees are divergent git working copies (one has a `.env`).

**Recommendation:** perform the README_FIRST bootstrap from the pristine ZIP `(1)` in Phase 1, after your approval.

**Also important:** the pre-existing `Senior Engineering Audit Pet Grooming Agent Factory.md` audits an older commit (`2436c0c`). Several of its findings are **already fixed** in the baseline ZIP `(1)` — e.g. it claims no perceptual dedupe and no image-bomb guard, but `app/curator.py` `compute_perceptual_hash()` and `Image.MAX_IMAGE_PIXELS = MAX_SAFE_IMAGE_PIXELS` are present; it claims the Dockerfile runs as root, but the baseline uses `USER appuser`. Actual source wins; that document is useful as a risk history, not as truth.

---

## 1. Executive assessment and architecture fit

**Assessment:** We are not starting from nothing. We have a disciplined Python prototype with genuinely good security engineering (verified-socket SSRF defence, DNS-rebinding tests, robots enforcement, arg-array subprocesses, append-only rights audit, Pydantic v2 contracts, durable SQLite job records, an LLM adapter with vision-capability checks and hard budget ceilings, and a non-root Dockerfile). Reuse is clearly the right strategy.

**Fit gap:** it is a **website builder**, not a media-only platform. The baseline contains no Google Drive integration, no n8n AI/vision nodes, no Instaloader, no yt-dlp, no FFmpeg, no frame extraction, no image/video pixel payload builder, no job manifest matching the spec contract, no secure worker media-stream endpoint, and a Compose file whose n8n service cannot authenticate to or share media with the worker. The four n8n JSONs are website intake, telemetry, GHL export and a demo — all inactive (`"active": false`) and all outside the media MVP.

**Verdict:** The spec's target architecture is sound and achievable. Phase 1 should be a thin Docker/n8n foundation that wraps the existing worker; Phase 2+ should add adapters incrementally. Do not rewrite the collector, curator, rights or LLM adapter.

---

## 2. Actual repository inventory and component reuse table

Baseline ZIP `(1)` tree (canonical): `app/` (pipeline layers), `src/engine/` (collection + LLM), `n8n/` (4 JSONs), `tests/` (39 test modules), `docs/`, `examples/`, `scripts/`, plus Docker/CI/lockfiles.

| Component | Actual file | Decision | Why / target phase |
|---|---|---|---|
| Website crawler | `src/engine/collector.py` (~32 KB) | **REUSE / EXTEND** | `safe_url`, `validate_ip_address`, `dial_verified_socket`, `SafeHTTPAdapter`, robots, `grab_media`, `download_one`, sha256 + dedupe, size caps. Emit spec manifest in Phase 2/4. |
| JS gallery | `src/engine/browser_gallery.py` | **REUSE / EXTEND** | Playwright + per-request SSRF route guard. Coupled to `research/pages.json`; keep or decouple. |
| Social collector | `src/engine/social_collect.py` (~24 KB) | **REUSE / REWORK** | gallery-dl arg-array subprocess, dry-run `--dump-json`, failure classification, originals preserved separately from stripped copies. Rework `--range`/date handling and manifest fields. |
| LLM adapter | `src/engine/llm/adapter.py` (~40 KB) | **REUSE AS REFERENCE / OPTIONAL VALIDATOR** | Vision capability detection, budget/cache/ledger/redaction are excellent. Spec assigns AI calls to **n8n**, so port prompt + strict schemas + deterministic validation into n8n; keep adapter for offline validation/tests. |
| Image curation | `app/curator.py` | **REUSE / EXTEND** | pHash + Hamming dedupe, blur checks, focal point, banner-text detection, `Image.MAX_IMAGE_PIXELS`, image-bomb guard. Add video frame handling externally. |
| API + state | `src/engine/service.py` | **REUSE / REWORK** | FastAPI `/health`, `/jobs`, `/jobs/{id}`, `/pipeline/run`, crash recovery, bounded queue, auth middleware. Add manifest/stream endpoints; drop website-release endpoints from media path. |
| HTTP auth | `src/engine/auth.py` | **REUSE** | Constant-time bearer/`X-API-Key`, origin + host checks. |
| Stage contracts | `src/engine/contracts/__init__.py` | **REUSE / EXTEND** | `Asset`, `StageInput/Output`, path-traversal validator, SHA-256 validation. Extend with platform/type/dates/rights/visual fields. |
| Job/asset DB | `app/db.py`, `app/schema.sql` | **REUSE / ADAPT** | SQLite WAL, idempotency, provenance + `right_confirmed_for_preview`, phash column. Website-specific `business_facts` table can be ignored. |
| Rights | `app/rights.py` | **REUSE / ADAPT** | Append-only `rights-audit.jsonl`, SHA verification, human operator/basis/evidence. Map to `rights_status`. |
| Approval gate | `app/approval.py` | **REFERENCE / EXCLUDE FROM MVP** | Website-release gate; not the media rights review. Contains two silent `except … pass` blocks (lines 54, 71). |
| Cleanup | `app/cleanup.py` | **REFERENCE** | Good fail-closed precedent (blocks prune without approved archive + `verify_release`). |
| Cockpit/templates | `app/cockpit.py`, `app/templates/*` | **REFERENCE ONLY** | New frontend is coded in the IDE. |
| Website generation | `app/generator.py`, `reviser.py`, `ghl_exporter.py`, `qa_engine.py`, `fact_extractor.py`, `pilot.py`, `mvf_gate.py`, `claim_scanner.py` | **KEEP ARCHIVED / EXCLUDE** | Out of media-only scope. |
| n8n 01 / 02 | `n8n/01-…json`, `n8n/02-…json` | **REFERENCE ONLY** | Correct `http://collector:8787` DNS pattern; wrong payloads and status schema. |
| n8n 03 / 04 | `n8n/03-…json`, `n8n/04-…json` | **OUT OF MVP** | GHL export / luxury demo. 04 uses a 2-second busy-wait poll loop. |
| Docker | `Dockerfile`, `docker-compose.yml` | **REWORK** | See §4. |
| Tests | 39 modules under `tests/` | **REUSE** | Offline unit tests available; see §6. |

### External tooling availability (searched across all text files in baseline ZIP)

| Tool | Present? | Note |
|---|---|---|
| `gallery-dl` | **Yes** | `requirements.txt` (pinned `1.30.0` in `requirements-social.txt`), `pyproject.toml` `social` extra, `social_collect.py`, tests. |
| Playwright / Chromium | **Yes** | `requirements-browser.txt` `1.55.0`, `browser` extra, Dockerfile installs Chromium. |
| Instaloader | **No** | Must be added (optional fallback). |
| yt-dlp | **No** | Must be added (optional video fallback). |
| FFmpeg / ffprobe | **No** | Must be added for frame extraction. |
| Google Drive / gdrive | **No** | Must be implemented in n8n. |

**Dependency contradiction:** `requirements.txt` lists `playwright` and `gallery-dl` in the base list, but `pyproject.toml` puts them in `browser`/`social` extras and the Dockerfile runs `uv sync --frozen --no-dev --extra browser` only. **gallery-dl will not be in the built image.** This matches the spec's predicted finding.

---

## 3. Working vs broken vs not-yet-integrated

### Working (evidenced in source + tests present)
- **SSRF / URL safety** — `collector.py`: `validate_ip_address`, `_validate_hostname`, `safe_url`, `dial_verified_socket`, `SafeHTTPAdapter`, redirect hop cap (6), robots enforcement, `MAX_SITE_BYTES=140 MB`. Tests: `test_ssrf_rebinding.py`, `test_collector.py`.
- **Download integrity** — `download_one()` records status, sha256, duplicates, MIME/extension checks, `invalid_image`, `too_large`, `empty`, `blocked_by_robots`.
- **Browser gallery SSRF guard** — `browser_gallery.py` re-validates every route via `verify_browser_url()`.
- **Social collection safety** — `social_collect.py`: permission gate (default off), HTTPS/host allowlist, arg-array `subprocess.Popen(..., shell=False)`, `--dump-json` dry run, `--download-archive` idempotency, bounded bytes (25 MB item / 150 MB staging / 300 MB project), classified failures (`login_required`, `rate_limited`, `extractor_broken`, `permission_required`, `gallery_dl_not_installed`, `project_storage_limit`, `corrupt_or_empty_media`), secret scrubbing, original blob kept in `assets/<platform>/` separate from metadata-stripped `assets/optimized/`.
- **Rights audit** — `rights.py` append-only, SHA-checked, requires operator/basis/evidence; `check_used_assets_audit_signatures()` detects tampering.
- **Contracts** — `Asset` defaults `rights_confirmed_for_preview=False`; path validator rejects absolute and `..` paths; sha256 strict regex.
- **Job durability** — `service.py` SQLite WAL, `running/queued → interrupted` on restart, bounded executor (2 workers) + semaphore (4).
- **LLM adapter** — `supports_vision` check raises on vision input to text-only provider; `get_default_adapter(require_vision=True)` skips Groq's default `openai/gpt-oss-120b`; disk cache, usage ledger, SQLite budget caps, secret redaction.
- **Image curation** — pHash, Hamming dedupe, sharpness, focal point, image-bomb guard.
- **Dockerfile** — pinned uv 0.6.6, frozen lockfile, Chromium, non-root `appuser`, healthcheck.

### Broken / risky as-is (with file paths)
1. **n8n cannot authenticate to the worker.** `docker-compose.yml` passes `EDITVISTA_API_TOKEN` only to `collector`/`cockpit`, not to `n8n`. All four workflows send `X-API-Key: {{ $env.EDITVISTA_API_TOKEN || '' }}`; the worker's `enforce_api_security()` requires a matching token, so calls resolve to empty and get **401**.
2. **No shared media mount between n8n and worker.** Compose mounts `./projects` only into `collector`/`cockpit`. n8n gets `./n8n:/data/n8n-workflows:ro`. `Read/Write Files from Disk` in n8n cannot see downloaded media.
3. **n8n file access restrictions absent.** No `N8N_RESTRICT_FILE_ACCESS_TO`, no `N8N_ENCRYPTION_KEY` in Compose.
4. **Old n8n image** `n8nio/n8n:1.76.1` (spec says pin a current compatible release).
5. **Duplicated `EDITVISTA_API_TOKEN` line** in both `collector` and `cockpit` environments (Compose parses the last one, but it is drift).
6. **`.env.example` port mismatch** — `COCKPIT_BASE_URL=http://127.0.0.1:8080` vs Compose publishing `8888`. No Google credentials, no `MEDIA_WORKER_TOKEN`, no `N8N_ENCRYPTION_KEY`.
7. **Coverage truncation** — `social_collect.py::_build_gallery_dl_cmd` uses `--range 1-{max_files}` and `--date-after`. Pinned/non-chronological posts can be missed; `--date-after` can early-exit. There is a useful post-hoc per-file date check, but range still caps discovery.
8. **Permission gate is opt-in** — `collect_social(i_have_permission=None)` returns `review_needed/permission_required` unless explicitly `True`. Must be wired to a real operator consent step, never silently defaulted.
9. **No video pipeline** — `ALLOWED` includes `.mp4/.webm/.mov`, but there is no ffprobe validation, no duration, no frame extraction, no `thumbnail_only` vs `full_video_reviewed` distinction.
10. **`collect()` returns only a summary** (`pages_crawled`, `asset_candidates`, `assets_saved`, `issues`, `notes`); the detailed manifest is side-effect files under `research/`. Does not match spec §8.
11. **Social collector is coupled to website-builder layout** — requires an existing project with a `research/` folder.
12. **Silent exception blocks** remain in `app/approval.py` (lines 54, 71) and across website-generation modules; do not import that behaviour into the media path.
13. **No endpoints** for `/manifest`, `/media/{asset_id}` streaming, `/frames`, or per-source status.
14. **All n8n workflows are inactive** and target the wrong product; none contain AI/vision, Google Drive, or file-read nodes.
15. **No AI prompt injection control in n8n yet** — the V2 prompt says to treat captions/pages as data, but the workflow must enforce it structurally.

### Not-yet-integrated (must be built)
- `compose.media.yml` (2 services, shared storage, persistent n8n state).
- Worker Dockerfile with `social` extra + Instaloader + yt-dlp + FFmpeg.
- Manifest / selection / coverage contracts and deterministic validator.
- n8n WF01–WF05 media-only workflows, including strict MODE A / MODE B JSON schemas.
- Google Drive folder create/reuse, binary upload, verification ledger, rights gate.
- Frontend (URL form + polling + previews + Drive link).
- All acceptance gates M2–M8.

---

## 4. Docker / n8n networking and storage findings

**Current Compose:** 3 services (`n8n`, `collector`, `cockpit`); ports bound to `127.0.0.1` (good); `n8n_data` volume for `/home/node/.n8n` (good persistence pattern); `./n8n` mounted read-only into n8n; `./projects` mounted RW into worker; healthchecks present; `cap_drop: ALL`, `no-new-privileges`, memory limits.

**Problems:** n8n→worker auth broken (§3.1), no shared media mount, no n8n file-access allowlist, no encryption key, outdated image, cockpit service out of scope, token duplicated, no Google credentials wiring, no Docker network explicitly named (default works but should be explicit), no resource limits tuned to the laptop.

**Target `compose.media.yml` (Phase 1):**
- Services: `n8n`, `collector` only.
- `n8n`: pinned current compatible image (verify release first), `127.0.0.1:5678:5678`, `N8N_ENCRYPTION_KEY` from `.env`, `N8N_RESTRICT_FILE_ACCESS_TO=/data/media`, `GENERIC_TIMEZONE`/`TZ`, `n8n_data:/home/node/.n8n`, `./storage/jobs:/data/media:ro`, `depends_on: collector: service_healthy`.
- `collector`: build from adapted Dockerfile, `127.0.0.1:8787:8787`, `PROJECTS_DIR=/data/projects`, `MEDIA_WORKER_TOKEN` from `.env`, `./storage/jobs:/data/projects:rw`, healthcheck, CPU/memory limits.
- Explicit bridge network; no public port exposure; credentials in `.env` / n8n Credentials only.
- Note: `local_path` in the worker (`/data/projects/...`) must be mapped to n8n's `/data/media/...` with validation — never naive string replacement on untrusted paths (spec §8). Prefer a secure worker media-stream endpoint or a strict path allowlist in n8n.

---

## 5. Security, compliance, source-access and rights issues

- **Keep** the collector's verified-socket SSRF defence, redirect cap, robots, domain scoping, exec allowlist, arg-array subprocesses and resource caps.
- **`check_social_url()` only validates hostname strings** — no DNS/redirect verification of the URL passed to gallery-dl. Add resolution + post-redirect checks for social URLs too (gallery-dl's own fetching is a separate concern).
- **Never** bypass login walls, CAPTCHA, checkpoints, rate limits, or use stolen cookies/sessions. The baseline correctly refuses cookie/session features.
- **365 days:** use verified publication timestamps; record `date_unknown`; do not trust `--date-after` alone; detect pinned/non-chronological cases; report `partial_success` truthfully.
- **Public visibility ≠ reuse permission.** Keep `rights_status` separate from `quality_score`; default `unverified`; require human rights review before Drive upload. The baseline's `rights_confirmed_for_preview=False` default and append-only audit are the right foundation.
- **Prompt injection:** website text, captions, filenames and frame content are untrusted data. The V2 prompt already instructs ignoring them; enforce structurally with fixed system prompt + strict schemas + deterministic validator outside the model.
- **Secrets:** never commit `.env`; baseline `.env.example` has only Groq placeholders. Avoid the real `.env` files found in the two extracted trees. Put Google OAuth in n8n Credentials; `.env` for tokens/encryption key; `.gitignore` both.
- **No production claims.** This is a prototype; no real end-to-end business run has been evidenced. Do not claim complete social coverage or full-video review.

---

## 6. Offline tests available and which can safely be run next

39 test modules exist. Likely offline-safe (mocks/fakes/tmp paths, no browser): `test_contracts.py`, `test_social_collect.py`, `test_llm_adapter.py`, `test_llm_pipeline_integration.py`, `test_curator_art_director.py`, `test_db_concurrency.py`, `test_rights_and_approval.py`, `test_approval_archival.py`, `test_ssrf_rebinding.py` (fake resolver), `test_v2.py`, `test_import_meta_export.py`, `test_ghl_export.py`, `test_claim_scanner.py`, `test_critic_and_reviser.py`.

Browser/live-environment dependent (defer; may need `playwright install chromium`): `test_qa_engine.py`, `test_ghl_embed_live.py`, `test_local_preview.py`, `test_audit_invariants.py`, `test_n8n_service_pipeline.py`, `test_security_review_fixes.py`, `test_section_generator_and_reviser.py`, `test_dod_acceptance.py`.

**I ran nothing.** Propose, after your explicit approval and after bootstrap:
1. `python -m compileall -q src app` (syntax only).
2. `ruff check .` and `pyright` (static; venv already exists in `website-builder-testing-push\.venv`, but prefer a clean Phase 1 image).
3. `pytest -q tests/test_contracts.py tests/test_social_collect.py tests/test_llm_adapter.py tests/test_curator_art_director.py tests/test_db_concurrency.py tests/test_rights_and_approval.py tests/test_ssrf_rebinding.py`
Ask before running any command.

---

## 7. Proposed target folder / API / manifest contracts

### Folder layout (adapted from spec §5)
```
project-root/
  AGENTS.md                      # new media-only rules (starter kit)
  docs/  PROJECT_SPEC.md, AI_VISUAL_DIRECTOR_V2.md,
         AI_VISUAL_DIRECTOR_ORIGINAL.txt, Developer_Guide_V2.pdf,
         AUDIT_PHASE_0.md, DECISIONS.md
  reference/source/              # untouched baseline ZIP (1)
  reference/legacy-extracted/    # extracted copy; legacy AGENTS.md -> LEGACY_AGENTS.md
  services/media-worker/         # thin adaptation of existing worker
  n8n-media/  WF01..WF05 .json
  frontend/
  storage/jobs/<job_id>/  website/ instagram/ facebook/ frames/ selected/ metadata/ logs/
                          manifest.json  selection.json  source-coverage.json
  compose.media.yml
  .env.example   .gitignore
```

### Worker API (minimal)
- `GET /health` → `{"ok": true}` (reuse).
- `POST /jobs` → `{"job_id", "status":"queued"}` (reuse; extend payload with optional social URLs + lookback + limits).
- `GET /jobs/{job_id}` → status, per-source status, progress, counts, errors (extend).
- `GET /jobs/{job_id}/manifest` → validated media manifest (new).
- `GET /jobs/{job_id}/media/{asset_id}` → stream original bytes with strict path allowlist (new; preferred over raw path mapping).

### n8n webhooks
- `POST /webhook/media-intake` → immediate `{"job_id","status":"queued"}`.
- `GET /webhook/media-status?job_id=…` → truthful state, per-source errors, Drive link.

### Manifest item (extend spec §8 + baseline `Asset`)
```json
{
  "asset_id": "ig_post_0001_photo_01",
  "job_id": "<uuid>",
  "platform": "instagram|facebook|website",
  "type": "image|video",
  "source_url": "https://…",
  "local_path": "/data/projects/<job_id>/instagram/image_01.jpg",
  "post_date": null,
  "date_verification": "verified|unknown",
  "hash_sha256": "<64 hex>",
  "phash": "<optional>",
  "width": 0, "height": 0, "mime": "image/jpeg",
  "captured_at": "<UTC ISO>",
  "visual_status": "not_analyzed|analyzed|unavailable",
  "rights_status": "unverified|review_required|confirmed",
  "rights_basis": null, "rights_operator": null, "rights_evidence": null
}
```

### Selection manifest
Include `schema_version`, `prompt_version`, `model`, `analysis_coverage`, per-category selections, `selected_asset_ids`, `rights_review_required_ids`, `rejected_assets`, scores 0..100. Deterministic validator: IDs must exist in current job manifest; bounds and enums enforced; selection limits respected; failures explicit.

---

## 8. Ordered milestone roadmap (M0–M8)

- **M0 (this document):** static audit, reuse matrix, risk register, Phase 1 plan. No destructive edits.
- **M1:** Docker Compose for n8n + worker; persistent n8n state; shared storage; `config`/`ps`/health; n8n→worker HTTP via service DNS; n8n reads permitted mount.
- **M2:** one permitted website download; manifest with URL/hash/error proof.
- **M3:** public social source — success **or** classified access limitation; never fake success.
- **M4:** 365-day processing — in-range / old / unknown / pinned categories.
- **M5:** image batch AI — real pixels to model, strict schema, no hallucinated IDs.
- **M6:** video AI — frames + timestamps; `thumbnail_only` never `full_video_reviewed`.
- **M7:** Drive — folder create/reuse, verified file IDs, idempotent retries, rights gate.
- **M8:** full E2E + nontechnical UI; accurate partial failures; handover.

---

## 9. Exact Phase 1 implementation plan (awaiting approval)

**Goal:** local foundation only. No scraping, no AI, no Drive, no frontend.

**Bootstrap (documentation/setup, after approval)**
1. Create `docs/`, `reference/source/`, `reference/legacy-extracted/`, `n8n-media/`, `services/`, `frontend/`, `storage/jobs/` in the workspace.
2. Copy `pet-gromming-agent-factory-main (1).zip` → `reference/source/` unchanged.
3. Extract it → `reference/legacy-extracted/`; rename only the extracted `AGENTS.md` → `LEGACY_AGENTS.md`.
4. Copy starter-kit docs into `docs/`; keep root `AGENTS.md` from the starter kit.

**New/changed files (all new; existing source untouched)**
- `compose.media.yml` — target design from §4.
- `services/media-worker/Dockerfile` — adapt baseline: `uv sync --frozen --no-dev --extra browser,social`, add Instaloader + yt-dlp + FFmpeg, non-root, healthcheck.
- `services/media-worker/` — copy/adapt `src/engine` + `app` as needed; keep `service.py` health/jobs endpoints; add `GET /manifest` stub returning `[]`.
- `.env.example` — `N8N_IMAGE`, `N8N_ENCRYPTION_KEY`, `MEDIA_WORKER_TOKEN`, `PROJECTS_DIR`; no real values.
- `.gitignore` — `.env`, `storage/`, caches, `reference/` outputs.
- `n8n-media/00-health-probe.json` — temporary M1 test workflow (HTTP Request → `http://collector:8787/health` + Read/Write Files from Disk on `/data/media/sentinel.txt`), deleted or superseded in Phase 3.

**Acceptance tests (M1) — all after approval**
1. `docker compose -f compose.media.yml config` → valid, no warnings; confirm no `latest` tag.
2. `docker compose -f compose.media.yml build` → succeeds; image contains `gallery-dl`, `yt-dlp`, `instaloader`, `ffmpeg -version`, and runs as non-root.
3. `docker compose -f compose.media.yml up -d` then `docker compose ps` → both healthy.
4. Host: `Invoke-WebRequest http://localhost:8787/health` → `{"ok":true}`.
5. n8n: health-probe workflow returns 200 from `http://collector:8787/health` (proves service DNS + token).
6. n8n: `Read/Write Files from Disk` reads `/data/media/sentinel.txt` (proves bind mount + `N8N_RESTRICT_FILE_ACCESS_TO`).
7. n8n UI reachable at `http://127.0.0.1:5678`; credentials/encryption key persist after `down`/`up`.
8. No scraper invoked, no Drive node, no AI call, no external network request during tests.

**Explicitly not in Phase 1:** adapters for gallery-dl/Instaloader/yt-dlp/FFmpeg logic, n8n WF01–WF05, AI schemas, Drive, frontend, dependency upgrades beyond the worker image.

---

## 10. Questions / blockers (genuine, not answerable from docs)

1. **Approve the bootstrap** (extract baseline ZIP into `reference/`, rename legacy `AGENTS.md`)? Required before real source work; not done in this audit.
2. **Confirm canonical source** = `pet-gromming-agent-factory-main (1).zip` (the drifted `website-builder-testing-*` copies are ignored).
3. **n8n image pin:** which current compatible n8n version should replace `1.76.1`? (Need your approval/verification; no internet call made in audit.)
4. **Vision model/provider for AI stage** plus where credentials live — spec says n8n owns AI; confirm provider/model supporting vision and budget cap. Blocks Phase 5, not Phase 1.
5. **Google Drive OAuth strategy** — n8n Credentials vs service account; blocks Phase 6, not Phase 1.
6. **Operator consent mechanism** for social collection (explicit `--i-have-permission` equivalent) — product/legal decision for Phase 2.
7. **Laptop capacity** (RAM/CPU/disk) to set Compose limits safely.
8. The human-readable PDF could not be parsed by this model; confirm that relying on `docs/PROJECT_SPEC.md` is acceptable (recommended) or supply a text export.

---

**End of Phase 0 audit. No implementation performed.**

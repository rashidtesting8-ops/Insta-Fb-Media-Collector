# Pet Grooming AI Media Intelligence Platform — Developer Guide v2.0

**Date:** 20 November 2026  
**Status:** Approved development architecture with Collection V1.1 requirements. Implementation status is tracked separately.

**Audience:** OpenCode agent, developer, n8n engineer, QA reviewer  
**Source baseline:** User-supplied `pet-gromming-agent-factory-main (1).zip`  
**Creative baseline:** Premium Pet Grooming Website Visual Director V2.0  
**Target:** Any publicly accessible pet grooming business website and publicly visible Facebook/Instagram pages, subject to legitimate access and reuse permissions.

> **North star:** A non-technical operator enters the business website, Instagram and Facebook URLs. The platform collects accessible and permitted images and videos published in the preceding 365 days, verifies coverage, checks quality and removes duplicates before Groq selection, and delivers only approved selections.

## 01 — Product scope and hard boundaries

**Inputs** include the required website, Instagram profile and Facebook Page URLs, desired maximum selected images and videos, optional creative notes, and permission and rights review status. Selection limits apply after collection. The master Creative Director V2.0 prompt remains versioned.

### Collection V1.1 — OWNER CONFIRMED

1. All three source URLs are mandatory.
2. Collect images and videos published in the 365 days preceding each job run, subject to legitimate access and collection permissions.
3. Incomplete or unverified collection can proceed to Groq selection only with explicit owner approval for that job.
4. Quality checks and deduplication precede Groq selection.
5. Final selection and delivery follow the Complete Project Flow Infographic and existing rights controls. Partial-collection approval leaves per-asset rights status and delivery eligibility unchanged.

**Milestone 1 implementation.** New submissions require all three source URLs at the dashboard and worker. Canonical n8n intake delegates validation to the authenticated worker validator and returns HTTP 400 on invalid input before job creation. Direct worker creation rejects invalid input with HTTP 422. The new requirement applies to new jobs. Trusted resume by existing job ID remains available without resupplying source URLs. The existing social permission gate still governs whether collection can run.

**Outputs:** source-separated original local media, metadata/coverage manifests, image and video analysis, ranked final selection, selected original asset IDs, permission review flags, Google Drive business folder, transfer report, job status/history, machine-readable errors.

**Out of scope for the media-only MVP:** generating a new website; publishing HTML or a GHL site; private profile access; CAPTCHA or account-checkpoint bypass; scraping media unavailable to a normal permitted viewer; guaranteeing every post from the last year; assigning ownership/copyright rights based solely on public visibility.

**Milestone 2 date policy — OWNER CONFIRMED.** Use UTC backend timestamps. Store original job creation as `created_at` and `window_end_utc`. Set `window_start_utc` exactly 365 × 24 hours earlier. Both endpoints are inclusive. Retries of an existing job, resume and status reads preserve the stored window. The existing idempotency expiration still applies to new submissions. Historical records without a frozen window remain readable.

**Milestone 2 implementation.** The manifest stores attributable publication evidence, original timestamp and offset where supplied, and normalized UTC separately from capture time. Website evidence comes from an unambiguous media-specific JSON-LD `datePublished` matched to `contentUrl`. Social evidence requires an unambiguous matched post record or extractor sidecar. Positional matches, naive dates, upload dates and capture times do not qualify. Unknown dates appear in the manifest’s `unknown_date_review` list. Out-of-window media remains available for audit. Both are excluded from automatic AI, as are jobs without a verified frozen window. The guarded adapter applies the same rule. The unpublished standalone synthetic fixture has no publication evidence and cannot be sent by the acceptance runner under this rule.

**Proposed details for final owner review.** Coverage includes website galleries and videos, Instagram feed posts, carousels, Reels and accessible Stories, and Facebook posts, albums, photos, videos and Reels. Only calculate a coverage percentage against a trustworthy independent inventory. Otherwise display **Unknown** for the percentage.

**USER INPUT REQUIRED.** Confirm the proposed content list (including expired Stories and off-site embeds), acceptable inventory evidence for each source and how manual review of unknown dates is resolved. Supply the three authorized pilot URLs, available CMS/platform inventories or supported access, and permissions for collection, retention and later external processing. Confirm the owner approval mechanism and handling of stale approvals after resume or inventory changes, and agree resource, retention and cost limits.

**Quality bar:** An excellent but unrelated image is not acceptable. Never fabricate a founder portrait, service offered, before/after pair, animal identity, or salon location. Visual selection is separate from legal permission to republish.

## 02 — Architecture: who does what

```text
Windows laptop, Docker Desktop running

Next.js/React frontend (localhost:3000)
  | POST /api/jobs (same-origin server route; no secrets exposed in browser)
  v
n8n local (localhost:5678)  [workflow controller / job status / AI / Drive]
  | HTTP requests on Docker network, e.g. http://collector:8787
  v
Python Media Worker (original FastAPI collector adapted in Docker)
  |- website crawler (existing collector + browser_gallery / Playwright)
  |- social primary: gallery-dl (external CLI)
  |- Instagram fallback: Instaloader (eligible/permitted cases)
  |- video fallback: yt-dlp (eligible/permitted supported URLs)
  |- FFmpeg + Pillow: file validation, image metrics, video frames
  v
Host bind-mounted storage: ./storage/jobs/<job_id>/
  |- website/, instagram/, facebook/, metadata/, frames/, selected/
  |- manifest.json, status.json, logs/
  ^                         |
  |  read-only mount         v
n8n image/video AI curation -> selection manifest -> n8n Google Drive nodes
  |  selected binaries read only as needed, not bulk-stored in n8n execution data
  v
Google Drive: Pet Grooming Media / <business-domain> / Best Images, Best Videos, Reports
  |
  v
Frontend displays job_id, source statuses, coverage, selected totals, Drive link
```

**Keep a single controller:** n8n runs the business process. Python does crawling, extraction, file operations, frame generation, optional small validation endpoints. Do not build a second complex orchestration system unless tests prove it needed. The existing FastAPI worker is a useful basis; do not create a fresh full backend without checking it.

**Local networking:** Host browser uses `http://localhost:5678` and `http://localhost:8787`. From the n8n Docker container, `localhost` means n8n itself; use Docker Compose service name such as `http://collector:8787` or whichever service is actually configured. Verify with a real health call.

**n8n file access:** Bind the same host folder to worker and n8n. Example: worker `/data/projects` read-write; n8n `/data/media` read-only. n8n 2.x has file access restrictions: explicitly authorize the mounted path (`N8N_RESTRICT_FILE_ACCESS_TO=/data/media` as applicable). Validate with a real Read/Write Files from Disk node. Mount a persistent `/home/node/.n8n` volume; keep project media out of the n8n internal database volume.

**State:** For one-user local MVP, durable SQLite from the ZIP may suffice; use a bounded worker, persistent job records and crash recovery. Test concurrent runs and disk pressure before considering PostgreSQL/Redis. Never call a job complete just because its n8n execution ended.

## 03 — Original ZIP inspection and reuse guide

Before using the ZIP, inspect *actual* contents. The prior archive included original website-builder logic and a media collector. There is **no verified end-to-end media-only pipeline yet**.

| Existing code | Intended action | Why |
|---|---|---|
| `src/engine/collector.py` | REUSE/EXTEND | Website crawl, safe URLs, bounded media discovery |
| `src/engine/browser_gallery.py` | REUSE/EXTEND | JavaScript-rendered galleries |
| `src/engine/social_collect.py` | REUSE/REWORK | gallery-dl subprocess, metadata, dates, size limits |
| `src/engine/llm/adapter.py` | REUSE/EXTEND | Model/vision integrations and budget tracking |
| `app/curator.py` | REUSE/EXTEND | Pet photo metrics, deduplication, sections and focal points |
| `src/engine/service.py` | REUSE/REWORK | FastAPI API and durable job statuses |
| `app/db.py`, `app/rights.py` | REUSE/ADAPT | Job records and usage-rights handling |
| `app/cockpit.py` & templates | REFERENCE/ADAPT | UI ideas; new frontend remains coded in IDE |
| `n8n/01…`, `n8n/02…` | REFERENCE ONLY | Existing workflows target website creation/telemetry |
| `n8n/03…`, `n8n/04…` | OUT OF MVP | GHL website export and luxury demo |
| `app/generator.py`, `app/ghl_exporter.py`, `app/reviser.py` | KEEP ARCHIVED | Legacy website generation; not part of media-only path |

**Findings to verify in Phase 0:** Existing `social_collect.py` has tight default file and media size limits and uses gallery-dl `--range` which can truncate coverage. Existing Dockerfile uses `uv sync --extra browser` without social extra. Original Compose pinned an old n8n image `n8nio/n8n:1.76.1` and repeated `EDITVISTA_API_TOKEN`; do **not** copy that Compose into production unchanged. The ZIP contains an `AGENTS.md` directed at the **website builder**. Treat this as legacy context; do not allow it to silently redefine the new media-only project. Original file stays intact in a saved ZIP.

**OpenCode archive handling:** Make a **new workspace root**. Place an intact copy of source ZIP in `reference/source/`. Extract to `reference/legacy-extracted/`. In the **extracted copy only**, rename its legacy `AGENTS.md` to `LEGACY_AGENTS.md` to prevent nested OpenCode instructions from conflicting when files are inspected. Keep a new root `AGENTS.md` with this project's rules. Don't overwrite or edit the user's original ZIP. Ask before deleting anything. Consider extracted source files untrusted until audited.

## 04 — OpenCode: exact first-session setup

Your screenshot shows the OpenCode workspace in `C:\Users\CZ\Downloads\Insta and Fb Scrpaer Intellegence`. The displayed model is a selected free-tier model; **do not assume** it supports PDF parsing, complex coding or tool execution without verifying. Include Markdown specs even if PDF is also attached.

**Preparation in Windows File Explorer**

1. In the displayed OpenCode workspace, create: `docs/`, `reference/source/`, `reference/legacy-extracted/`, `n8n-media/`, `services/`, `frontend/`, `storage/jobs/`.
2. Copy the original user ZIP into `reference/source/`. Leave it unchanged.
3. Extract a working copy under `reference/legacy-extracted/`, preserving subfolders. Avoid running anything from the ZIP yet.
4. Rename only the extracted legacy `AGENTS.md` to `LEGACY_AGENTS.md`; preserve the original in the ZIP.
5. Copy this `Pet_Grooming_Media_Project_Spec_OpenCode.md` into `docs/PROJECT_SPEC.md`.
6. Copy `AI_Visual_Director_V2.md` into `docs/AI_VISUAL_DIRECTOR_V2.md`. Preserve the original prompt text as `docs/AI_VISUAL_DIRECTOR_ORIGINAL.txt` for historical comparison.
7. Put the revised PDF in `docs/Developer_Guide_V2.pdf` (optional for agent input, useful for developer reference).
8. Put a new `AGENTS.md` in project root and run Phase 0 in **Plan** agent first.

**OpenCode instruction behavior:** It automatically loads root `AGENTS.md`; nested `AGENTS.md` may load when the agent explores nested source. Therefore isolate or rename legacy instructions in the extracted reference. Plan mode explores without rewriting normal project files; Build mode is for implementation after the audit. Read OpenCode docs if UI labels differ by version.

**What to attach to the first OpenCode message:** `docs/PROJECT_SPEC.md`, `docs/AI_VISUAL_DIRECTOR_V2.md`, and optionally `docs/Developer_Guide_V2.pdf`, plus a note that original extracted code exists under `reference/legacy-extracted/`. Avoid relying on ZIP attachment alone: binary ZIP is not the same as a browsable source tree in the model context. If PDF isn't understood, the Markdown is authoritative machine-readable context.

**First instruction:** Use `OpenCode_Phase_0_Audit_Prompt.txt`. Ask it to read the files and **audit only**, generate audit report, architecture and plan; no network scraping, no Docker changes, no mass refactors or deletions. Only after acceptance switch to **Build** for Phase 1.

**Workspace `AGENTS.md` rules** appear in the companion `OpenCode_Project_AGENTS.md`; copy it to root as `AGENTS.md`. OpenCode can read it automatically.

## 05 — Repo and data structure target

```text
project-root/
  AGENTS.md                           # project rules, NOT legacy builder directions
  docs/
    PROJECT_SPEC.md
    AI_VISUAL_DIRECTOR_V2.md
    AI_VISUAL_DIRECTOR_ORIGINAL.txt
    Developer_Guide_V2.pdf
    AUDIT_PHASE_0.md
    DECISIONS.md
  reference/
    source/pet-gromming-agent-factory-main.zip    # untouched
    legacy-extracted/pet-gromming-agent-factory-main/
  services/
    media-worker/                     # adapt existing FastAPI code, not rewrite blindly
      adapters/website.py
      adapters/instagram.py
      adapters/facebook.py
      adapters/video.py
      processing/manifest.py
      processing/quality.py
      processing/frames.py
      api.py
      tests/
  frontend/                           # Next.js or existing UI scaffold after audit
  n8n-media/
    WF01-intake.json
    WF02-source-extraction.json
    WF03-ai-curation.json
    WF04-drive-delivery.json
    WF05-status-and-errors.json
  storage/jobs/<job_id>/
    website/ instagram/ facebook/ frames/ selected/ metadata/ logs/
    manifest.json
    selection.json
  compose.media.yml
  .env.example
  .gitignore
```

These are **target files**, not claims about what exists in the original ZIP. Phase 0 may suggest a smaller layout. Prefer a thin adapter around tested existing code to wholesale copying.

## 06 — Local Docker milestone

**Minimum services:** n8n, media worker. The frontend can run as IDE development server (`npm run dev`). Persist n8n user data, keep worker output in host bind-mounted storage; avoid storing downloaded media inside n8n's own SQLite volume.

**Illustrative compose pattern (review and adapt, do not paste over existing configs blindly):**

```yaml
services:
  n8n:
    image: ${N8N_IMAGE:?set_supported_pinned_n8n_image}
    ports: ["127.0.0.1:5678:5678"]
    environment:
      - TZ=Asia/Karachi
      - GENERIC_TIMEZONE=Asia/Karachi
      - N8N_RESTRICT_FILE_ACCESS_TO=/data/media
      - N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY:?set_key}
    volumes:
      - n8n_data:/home/node/.n8n
      - ./storage/jobs:/data/media:ro
    depends_on: [collector]

  collector:
    build: ./services/media-worker
    ports: ["127.0.0.1:8787:8787"]
    environment:
      - PROJECTS_DIR=/data/projects
      - MEDIA_WORKER_TOKEN=${MEDIA_WORKER_TOKEN:?set_token}
    volumes:
      - ./storage/jobs:/data/projects:rw
    # Define container CPU/memory and process limits based on local laptop capacity.

volumes:
  n8n_data:
```

This snippet intentionally **omits** a working Dockerfile and source mount for the worker: OpenCode must inspect the legacy Dockerfile, verify build context and generate a tested implementation. It is architecture guidance, not copy-paste runnable production config. Do not run n8n with public exposed ports. Pin a current compatible n8n image after verifying the release; preserve your credentials and encryption key.

**Connectivity tests:**

```powershell
docker version
docker compose version
docker compose -f compose.media.yml config
docker compose -f compose.media.yml ps
# From host: test service only after container is running:
Invoke-WebRequest http://localhost:8787/health
```

Within n8n use `http://collector:8787/health`; verify before adding any scraping logic. In production use authenticated internal endpoints, not a generic shell executor for arbitrary user strings.

## 07 — Downloader strategy and permissions

1. **Website:** existing crawl engine first; browser_gallery/Playwright only where standard fetch cannot render media. Crawl only target domain or narrowly allowed domains; respect site restrictions and use resource/time budgets. Link discovery from pages/sitemap. Website asset timestamps are often unknown.
2. **Instagram:** gallery-dl is primary for supported and permitted public content; Instaloader is an optional alternate on supported public sources. Neither guarantees access to an arbitrary public profile over 365 days; do not try to defeat login walls, CAPTCHA, or rate limits.
3. **Facebook:** gallery-dl primary supported extractor; yt-dlp only for supported/permitted individual videos; API only where authorized permission exists. A Facebook URL does not grant API permissions.
4. **yt-dlp:** video fallback and supported embedded media. It is not a website crawler.
5. **FFmpeg:** worker produces sample frames from videos, records timestamps and duration. Do not claim full-video review from a thumbnail.
6. **Fail safely:** record `not_accessible`, `permission_required`, `rate_limited`, `date_unknown`, `partial_success`, `unsupported_url`, or `source_failed` with evidence.

**V1.1 required capabilities.** Follow permitted historical pagination and persist per-source checkpoints, page or cursor progress, stable post and attachment identities, stop reasons, and verified file checksums. Enumerate carousel and album children separately. Preserve original files, source URLs, publication timestamps and collection metadata. A lone old or pinned post cannot establish the cutoff. Reconcile expected items against collected, duplicate, out-of-window, missing, blocked and failed items. Record verified coverage only when the permitted historical boundary is established and every in-scope attachment is accounted for as present and verified. A cap, access gate, unresolved failure or unknown boundary cannot establish complete coverage.

**Remaining implementation gaps.** The website crawler follows bounded HTML links. The social extractors yield bounded samples, lack verified historical pagination checkpoints and an independent attachment inventory, and have observed anonymous access gaps. The AI path enforces date eligibility. Verified coverage and per-job partial approval remain separate, unimplemented gates. See the [Phase 10 acceptance matrix](PHASE_10_LOCALHOST_QA_MATRIX.md) for the V1.1 acceptance baseline.

**Milestone 3A offline implementation.** The opt-in checkpoint engine stores the original frozen window, source identity, cursor, discovered pages or posts, attachment identities and transfer receipts in a job-local `collection/` directory. The engine commits a page and its continuation together. The injected website adapter discovers permitted sitemap indexes, archive links and explicit next-page links. Simulated social listings use the same interface with separate post and attachment IDs. Thumbnail and missing-child records remain distinct from downloadable media. An interrupted transfer resumes only when the adapter confirms the same revision and range, or restarts from byte zero. File publication is an atomic rename after length and checksum checks, with recovery from a saved checksum receipt. Local checksums are recorded even when a fixture supplies no independent source digest.

M3A uses explicit fixture budgets and an injected permission policy. It is outside the normal worker job path. Listing exhaustion alone leaves coverage unknown, and recorded gaps leave it incomplete. Existing collectors still run their bounded paths. M3B requires owner-approved sources, access methods, content scope, inventories and operating budgets before wiring a real transport or validating platform coverage.

**Milestone 3B-A fixture worker entry point.** From the worker directory, run `python -m app.fixture_collection RECORDING.json OUTPUT_ROOT`. Supply a recording marked `fixture_only`, a `fixture-...` job ID with its original frozen M2 timestamps, per-source budgets, recorded website documents and robots policy, recorded social pages with explicit cursors and child IDs, and inline binary fixtures. A repeated command for the same marked job and window reopens its checkpoint. The worker writes `collection/fixture-manifest.json` under that fixture job. It records source URLs, asset IDs, publication evidence, cursor outcomes and unknown-date review entries in the existing manifest shape. It never enters normal job intake, publishes a normal job manifest or invokes AI or delivery. Fixture budgets do not establish live operating limits.

**M3B-B default-off integration.** The worker can opt into the checkpoint path using operator-configured source URLs, approved origins and explicit budgets. It connects guarded website discovery and downloads and accepts local public social snapshots without launching an extractor. Unverified collection is held before AI and delivery. The [Lily’s Club pilot](M3B_B_PUBLIC_PILOT.md) has designated URLs and still requires live collection approval, source inventory evidence and operating limits. The M4 owner-approval requirement remains separate.

**M3B-C offline extractor handoff.** The same authenticated worker job endpoint can enter a recorded-only mode via operator-set `RECORDED_EXTRACTOR_HANDOFF=1` and `RECORDED_EXTRACTOR_CONFIG` pointing to local JSON explicitly marked `offline_only`. All three URLs remain mandatory and the configuration must match each one. Recorded website documents and binary fixtures are local. Instagram accepts gallery-dl URL records and Instaloader post sidecars. Facebook accepts gallery-dl URL records and yt-dlp video info records. The mode takes precedence over the public HTTP flag and cannot use HTTP or launch an extractor. An interrupted recorded job can reuse its idempotency key within the existing retry window, with its original URLs, job ID and frozen UTC window.

Each recording identifies its extractor and profile. Continuations and child expectations retain source attribution. An unattributed token, child count or generic extractor date does not establish pagination, a missing attachment or a verified publication instant. Asset outcomes and source issues are separate from overall coverage, which stays unknown without an independent inventory. Duplicate checksums keep separate provenance and rights flags. Recorded jobs stay held at both curation and delivery. [Pilot and remaining live prerequisites](M3B_B_PUBLIC_PILOT.md)

**Never** construct a shell command from untrusted URL text. Use `subprocess.run([binary, option, ..., validated_url], timeout=...)`, a strict allowlist of domains and executable paths, and resource limits. Validate URLs after redirects and DNS resolution to block localhost/private IP SSRF. Publicly visible does not imply copyright permission.

## 08 — Job API and manifest contracts

**Input to n8n (illustrative):**

```json
{
  "website_url": "https://example-pet-groomer.com",
  "instagram_url": "https://www.instagram.com/examplegroomer/",
  "facebook_url": "https://www.facebook.com/examplegroomer",
  "lookback_days": 365,
  "max_selected_images": 30,
  "max_selected_videos": 10,
  "user_selection_notes": "Prefer hero shots and grooming transformations"
}
```

**Immediate response:** `{"job_id":"<uuid>","status":"queued"}`. Return promptly; do not hold a browser HTTP request open while downloading hundreds of files. n8n internally calls existing/adapted worker endpoints; inspect real routes before designing incompatible paths.

**Required state transitions.** Validate all three URLs before starting collection. After collection, record each source as verified complete, incomplete, blocked or coverage unknown. For incomplete, blocked or unverified collection, enter `awaiting_partial_collection_approval` and keep Groq disabled. An explicit owner decision tied to the job, coverage evidence and eligible subset can record `partial_collection_approved` or `partial_collection_declined`. Declined or pending jobs issue no Groq request. Approved partial jobs retain their incomplete coverage status. Eligible jobs proceed through quality checks and deduplication, Groq batch analysis and final selection, independent per-asset rights review, and approved delivery. Keep source coverage distinct from job status and delivery outcome.

**Sample media manifest item:**

```json
{
  "asset_id": "ig_post_0001_photo_01",
  "job_id": "<uuid>",
  "platform": "instagram",
  "type": "image",
  "source_url": "https://www.instagram.com/p/example/",
  "local_path": "/data/projects/<job_id>/instagram/image_01.jpg",
  "post_date": null,
  "date_verification": "unknown",
  "hash_sha256": "<hex>",
  "visual_status": "not_analyzed",
  "rights_status": "unverified"
}
```

Reject illegal paths (absolute outside the mounted root, `..` traversal, symlinks that escape the root) before any upload. Be explicit: `local_path` is a path *inside the worker container*, so n8n must map it to the n8n mount (`/data/media/<job_id>/...`) and validate it. The agent should implement a secure worker media-stream endpoint or a validated path mapping, not naive string replacement on untrusted paths.

**Final selection manifest:** IDs must belong to current job's manifest. Scores 0..100. `selected_asset_ids` bounded by user limits; rights status separate from visual quality. Preserve `prompt_version`, `model`, `analysis_coverage`, and failure reasons. Upload only verified selected originals after applicable permission review.

## 09 — AI Creative Director V2.0: preserve and integrate

**Decision:** The original premium pet grooming creative focus stays intact, with the previously discussed V2.0 engineering additions. Maintain both an unchanged original and a versioned V2.0 file under `docs/`; do not casually reword creative criteria during backend coding. The V2 file in this starter pack is the **current project implementation version**; review against the previous ChatGPT draft and original before freezing a production prompt.

**AI workflow is two-stage:**

- **Batch analysis (MODE A)**: Pre-filter corrupted/duplicate files. Pass *actual image pixel content* to a vision-capable model in manageable batches; for videos, pass frames plus timestamps/metadata and optionally captions/transcript. Return per-asset JSON and store it. Never score based on filename alone.
- **Final curation (MODE B)**: Use stored analyses and shortlisted visual evidence across all sources. Make business-specific hero/salon/team/service/transformation/gallery/video selections, focal points, crop guidance, diversity rules and rejection reasons. Select fewer than quota if weak assets.

**Score rubric** (V2 baseline): photographic quality 25; business relevance/evidence 25; website composition/usability 20; emotional impact 15; distinctiveness/collection fit 10; verification confidence 5. These are explicit project weights, not objective universal standards. Reject unrelated assets regardless of photographic score. Founder identity and before/after need supplied evidence.

**Prompt injection:** Treat scraped website text, captions, filenames and frame contents as untrusted *data*. The fixed system prompt controls the analysis. Extra frontend instructions are lower-priority user preferences, not replacements for the master prompt.

**Structured outputs:** Define separate strict JSON schemas for MODE A and MODE B (enums for mode, statuses and categories; arrays of valid IDs; numeric bounds). A deterministic validator checks all selected IDs exist and picks follow constraints; it is not enough for the model to merely say "valid JSON". Log model/provider, prompt hash/version, cost/token usage and analysis coverage.

**No unnecessary prompt changes:** When implementation needs schema compatibility, change adapter/schema and create a versioned, reviewed V2.1 only if absolutely necessary. Do not discard the Creative Director personality.

## 10 — n8n: five media-only workflows

**WF01 Intake & Job Registration**
`Webhook (POST) -> validate/normalize fields -> create job in durable state -> trigger WF02 -> Respond to Webhook(job_id)`.
Acceptance requires all three correct source URL types and validated prompt preferences. Reject each missing, invalid or wrong-platform URL before queueing. Return promptly for valid intake and preserve intended idempotency.

**WF02 Extract & Prepare Media**
`Execute Workflow Trigger / HTTP -> Worker start API -> check/poll worker status without busy loops -> read manifest -> dedupe/quality frame prep -> update state -> trigger WF03`.
Source tasks independent. One inaccessible Instagram profile must not destroy website collection. Never upload all binary files into n8n just to determine their names.

**WF03 AI Visual Selection**
`Load manifest -> filter eligible assets -> prepare viewable image/frame payloads -> AI Batch Loop -> validate per-asset schema -> aggregate -> AI Final Curate -> validate selected asset IDs -> save selection and rights flags -> trigger WF04 or rights-review state`.
Use project `AI_VISUAL_DIRECTOR_V2.md`, not the old site-builder design prompt baked into existing n8n flows.

**WF04 Google Drive Delivery**
`Check permission review -> normalize domain -> Google Drive search folder -> IF absent create folder -> create Best Images/Best Videos/Reports -> get selected binary files through read-only mounted volume -> Google Drive upload -> verify file IDs/counts -> save Drive folder URL -> completed/partial`.
Use a dedicated parent folder and idempotency keys. Large videos may require a resumable upload route if n8n binary processing proves unreliable; don't silently upload corrupt/truncated files.

**WF05 Status & Error API**
`Webhook GET status/<job_id> -> authenticate/validate job_id -> query persisted state -> Respond JSON`.
Return state, progress, per-source count/errors, AI results summary, Drive folder URL, timestamps, and failure classification. UI polls every few seconds or uses server-sent events later; keep workflow executions bounded.

**Build order:** Start with WF01 + mocked WF02; then actual Website adapter; then Social adapters; then AI; then Drive; then progress UI. Export tested workflows to `n8n-media/*.json` and back up n8n credentials securely (never commit secrets).

## 11 — Google Drive and binary delivery

Google Drive nodes are appropriate for creating business folders and uploading selected media. But the n8n node needs the **file binary**, not only a string path. `Read/Write Files from Disk` can load a file if its path exists *inside n8n container*. On self-hosted n8n 2.x explicitly permit the shared file-access path. Process selected files one at a time or in bounded batches; retain checksum and upload ledger to handle retries.

Folder convention:

```text
Pet Grooming AI Media/
  example-pet-groomer.com/
    Best Images/
    Best Videos/
    Reports/
      selection.json
      source-coverage.json
      upload-report.csv
```

For a repeat run, locate the business folder and reuse it; create a new dated report or run subfolder when appropriate. Verify returned Drive file ID and folder ID before marking each asset uploaded. Error state `partial_upload` rather than false `completed` when some files fail.

## 12 — Frontend specification

**Primary screen** requires website, Instagram and Facebook URLs and shows the rolling 365-day scope. Show auto-detected links for confirmation, selection limits, optional creative instructions, per-source progress and stop reasons, coverage and partial-approval status, selected previews, the Drive link and job history.

**No raw credentials in browser.** Frontend should use a server-side proxy/API route to the protected n8n webhook. The `/api/jobs` response returns job ID immediately. UI displays `queued`, `collecting website`, `collecting social`, `analyzing`, `uploading`, `completed`, or `partial` truthfully. Don't claim 365-day complete when sources are inaccessible.

**First runnable frontend target:** URL form -> mocked job ID -> poll mock statuses -> display mock result; then replace mocks with real WF01 and WF05 endpoints. Styling follows after functionality.

## 13 — Testing and acceptance gates

| Gate | Test | Pass criteria |
|---|---|---|
| M0 | Static source audit + offline tests | Dependencies/routes/legacy instructions documented; no destructive edits |
| M1 | Docker health and shared mount | Both containers start; n8n HTTP calls worker using service DNS; n8n reads permitted mount |
| M2 | One website download | At least one real permitted media file, manifest with URL/hash/error proof |
| M3 | Public social source | Downloader success **or** explicit, classified access limitation; never fake success |
| M4 | Rolling 365-day collection across three mandatory sources | Reject each missing or invalid URL. Traverse historical pages through the verified boundary, reconcile individual images and videos and their provenance, handle in-range, old and unknown dates and pinned/out-of-order posts, and resume the same window. Caps, access blocks or missing evidence cannot produce verified complete status. |
| M5 | Image batch AI | Pixels reach model; score schema valid; no hallucinated IDs |
| M6 | Video AI | Frames with timestamps analyzed; `thumbnail_only` never marked `full_video_reviewed` |
| M7 | Drive | Folder created/reused, file IDs verified, retries do not duplicate files |
| M8 | Full E2E + UI | Dashboard and worker agree on per-source coverage and job ID. Pending and declined partial jobs make zero Groq calls. Approved partial jobs preserve coverage gaps and run quality checks and deduplication before Groq. Rights-unverified assets cannot be delivered. Verify final selection and delivery against the supplied infographic and rights evidence. |

**Negative tests:** wrong URLs, private/restricted profiles, blocked pages, non-image files, oversized media, disk exhaustion, worker restart, duplicate downloads, invalid AI JSON, hallucinatory asset IDs, expired Google OAuth, duplicate Drive uploads, simultaneous jobs, unsafe redirects, malicious captions. Test each milestone against a controlled or explicitly authorized sample before broader usage.

## 14 — Security and policy rules

- Only collect media when access and collection are permitted; do not bypass authentication, CAPTCHA, account checkpoint, technical restrictions, or rate limits. No stolen cookies/sessions.
- Implement domain allowlisting, DNS/IP checks, redirect validation, no requests to loopback/RFC1918/link-local/metadata endpoints, and subprocess array arguments to prevent injection.
- Store all credentials in Docker `.env` (excluded from Git) or n8n Credentials, not in source, prompt files or client code.
- Keep worker APIs on a private Docker network and bind host debug ports to 127.0.0.1.
- Limit crawl depth, pages, download count, storage bytes, file sizes, timeout, concurrency, AI spend and temp retention. Log failures; no silent auto-approval.
- Maintain separate `rights_status` and `quality_score`. Public content is not automatically licensed for use on generated sites or ads. Require relevant permission before publishing/downstream reuse.
- Keep original uploaded ZIP and legacy code untouched. Commit only user-approved changes; back up working state.

## 15 — Development milestone roadmap and exit criteria

**Phase 0 (read-only audit):** Inspect ZIP and v1 guide; reconcile real routes, dependencies, assets, tests, existing AGENTS.md and Docker Compose. Deliver `docs/AUDIT_PHASE_0.md`, reuse matrix, risk register and Phase 1 plan. No code edits beyond documentation explicitly permitted.

**Phase 1 (local foundation):** Build tested Docker Compose for n8n + worker with persistent n8n state and shared storage. `docker compose config`, `ps`, health tests, n8n-to-worker HTTP and file mount pass.

**Phase 2 (scraper adapters):** Reuse website collector. Add gallery-dl primary, Instaloader and yt-dlp optional adapters, immutable media manifest, source error classifications. Work with a bounded test batch before expanding limits.

**Phase 3 (job orchestration):** WF01, WF02 and WF05; durable statuses, 3-source dispatch and per-source partial success. Test from a local curl/Postman call.

**Phase 4 (media processing):** Hashes, image resolution/blur, video frames, date categories, dedupe and representative visual inputs, before AI call.

**Phase 5 (AI Creative Director V2):** MODE A and MODE B, model capability verification, structured output validation, 1- and 100-image tests, no invented media.

**Phase 6 (Google Drive):** WF04 selected originals only, folder creation/reuse, verification, retry, rights gate, upload report.

**Phase 7 (nontechnical frontend):** URL form, optional discovery, options, job state, selected previews, Drive link, errors and history.

**Phase 8 (productionization):** End-to-end QA, security checks, concurrency and disk tests, cleaning up staging, user acceptance, backup/handover.

**Do not jump phases** because a screenshot shows a nice frontend or n8n indicates a successful execution. Every phase needs proof of actual file outputs and tests.

## 16 — Debug and recovery runbook

| Symptom | Likely class / action |
|---|---|
| n8n cannot reach worker | Docker service-name DNS vs localhost, port, health, network |
| n8n cannot read media path | Wrong container mount, n8n file restriction, permissions, path mapping |
| gallery-dl no media | Extractor limitation, login requirement, access restrictions, unsupported link; classify |
| Instaloader/yt-dlp fails | Unsupported or restricted source; do not automatically evade constraints |
| 365-day results incomplete | Pagination/range limit, pinned posts, invalid dates, inaccessible posts |
| AI returns superficial selection | Actual pixels/frames not passed, batch design insufficient, wrong model capability |
| AI JSON parsing fails | Schema mismatch; retry bounded times, log raw output securely and fail explicitly |
| Drive upload invalid binary | n8n file-read failed or wrong binary property; check size and file ID |
| n8n files grow unbounded | Avoid binary blobs in execution data; use shared bind mount; retention policy |
| Wrong system prompt used | Ensure active workflow loads versioned V2 file and correct MODE; log prompt version |

## 17 — What to give OpenCode (and in which order)

**First OpenCode message, in Plan agent:**

1. Explain actual workspace root and point to `docs/PROJECT_SPEC.md`.
2. Say ZIP source is extracted under `reference/legacy-extracted/`, original ZIP in `reference/source/`.
3. Point to `docs/AI_VISUAL_DIRECTOR_V2.md`, and the original prompt for historical reference.
4. Ask read-only project audit, real file inventory, compatibility review and runnable Phase 1 plan.
5. Explicitly prohibit package installation, live social scraping, secret access, API calls, code generation/refactors and Docker destructive commands in this first pass.

**After reading audit:** Authorize one specific Build-phase milestone at a time. Ask for changed files, commands run, test results, blockers and next task. Don't give a giant "build entire app" instruction.

**OpenCode recommended permanent rules:** Use the provided `OpenCode_Project_AGENTS.md`. OpenCode docs: https://opencode.ai/docs/instructions, https://opencode.ai/docs/agents, https://opencode.ai/docs/attachments.

## 18 — Documentation references and maintenance

- User original ZIP is the **only source of truth about existing code**. Code inventory and risk claims should be reverified against the current extracted version.
- User original Creative Director prompt and V2.0 draft are the creative baseline. Preserve originals and record version/hash when the prompt changes.
- gallery-dl: https://github.com/mikf/gallery-dl (active development moved to https://codeberg.org/mikf/gallery-dl)
- yt-dlp: https://github.com/yt-dlp/yt-dlp
- Instaloader: https://instaloader.github.io/
- OpenCode: https://opencode.ai/docs/
- n8n self-hosted file node: https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.readwritefile/
- n8n docs: https://docs.n8n.io/
- Docker Compose: https://docs.docker.com/compose/
- Google Drive: https://developers.google.com/drive/api/guides/about-sdk

**Document change log**
- v1.0 — Initial local-first developer blueprint; primarily VS Code/Cursor oriented; creative prompt original preserved.
- **v2.0 (20 Nov 2026)** — OpenCode-first launch, root/nested AGENTS.md risk, extracted ZIP workflow, strict source-vs-plan distinction, Creative Director V2.0 two-stage integration, verified n8n 2.x shared file access restriction, fuller job/status contracts, milestone gates, safer Compose baseline and OpenCode first prompt.

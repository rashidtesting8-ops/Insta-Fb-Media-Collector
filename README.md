# Atelier Media — Pet Grooming Media Intelligence (V2.1)

A 100% open-source, self-hosted media collection, AI visual curation, rights-verification, and private Google Drive delivery platform for pet grooming businesses.

An operator enters a business's **Website URL** and **Facebook Page URL** (Instagram is optional and parked; **zero Meta API dependency**). The platform collects publicly accessible, permitted media from a frozen **365-day UTC window**, supports secure **client-supplied Facebook Export ZIP / media uploads** when platform CDNs block automated bots, runs **Groq Vision AI (`Visual Director V2`)** on real image pixels and FFmpeg video keyframes after independent Two-Admin approval, enforces per-asset commercial rights review, and delivers only rights-cleared originals and reports to a **private Google Drive folder** via n8n.

> **Engineering honesty first.** This repository never fabricates coverage, publication dates, or legal reuse rights. Platform blocks (`robots_disallowed`, `login_required`, `rate_limited`), undated website media (`unknown_date`), and unverified usage rights (`rights_status: "unverified"`) are recorded explicitly and never silently bypassed. No CAPTCHA evasion, no `robots.txt` bypass, no cookie/session smuggling, and no fake completion states.

---

## 1. Current Status — Verified Truth vs. Honest Limits

Everything below reflects what is **actually built, tested, and proven** in this repository (including V2.1, PR #21–#23, Milestone OS-1, and Milestone OS-2), alongside real-world external constraints.

| Capability | Current Status | Verified Evidence & Honest Notes |
| --- | --- | --- |
| **Website Media Collection** | **Live-verified & hardened** | SSRF-hardened crawler (`website_inventory.py`, `legacy_collector.py`, `public_collection.py`) with vetted HTTPS-only allowlist for major website builders (Wix, Squarespace, Shopify, Webflow, WordPress, Cloudinary, GoDaddy, Contentful, Sanity, Bunny) plus lazy image/video parsing. Most sites lack media-specific JSON-LD `datePublished`, so undated website assets stay truthfully labeled `unknown_date` and enter AI curation only when an operator explicitly enables the per-job **"Include undated website media"** toggle (`Website · Date unverified`). |
| **Facebook Public Discovery** | **Live-verified (metadata)** | `gallery-dl 1.32.20` (+ `yt-dlp` fallback) reliably discovers public Facebook posts/photo URLs and timestamps. **Empirical finding:** Meta's `scontent.*.fbcdn.net/robots.txt` explicitly serves `User-agent: *` → `Disallow: /`, so anonymous automated binary downloads are faithfully refused and logged as `robots_disallowed` / `access_blocked`. We do not bypass `robots.txt`. |
| **Client Facebook Export ZIP & Direct Media Upload** | **Built & offline-tested (OS-2)** | Zero-Meta-API, 100% compliant path for full 365-day Facebook photos and videos. Authenticated Named Admins drop a client's official **Facebook Export ZIP** or direct photo/video files in the job UI (`POST /api/jobs/[id]/media-upload`). Validates size caps (`100 MB` total / `25 MB` file / `150 MB` uncompressed / `60` files), blocks zip-slip/symlinks/encrypted entries, verifies magic bytes, parses official Facebook Export JSON `creation_timestamp` and captions, enforces the 365-day UTC window, deduplicates by SHA-256, runs Pillow + FFmpeg preprocessing, and atomically publishes a new M4 immutable collection revision. |
| **Instagram Collection** | **Intentionally parked (optional)** | Anonymous Instagram extraction is consistently gated by platform `429`/login walls. `instagram_url` is optional in V2.1 intake; existing parser/normalizers remain dormant in code without claiming active anonymous coverage. |
| **Preprocessing (Dedupe, Quality, Video Frames)** | **Live-verified** | Deterministic SHA-256 cross-source deduplication, Pillow image dimension/blur metrics, and bounded FFmpeg video keyframe extraction with timestamps. Videos reviewed via sample frames are never mislabeled as full-video review. |
| **Two-Admin M4 Governance & Auth** | **Live-verified** | Argon2id (`libsodium23`) Named Admin accounts, HttpOnly signed session cookies, 30-second single-use HMAC assertions (`X-Named-Assertion`), and an immutable SQLite approval & rights ledger enforcing Two-Admin separation of duties before Groq Vision AI can execute on partial/unverified collections. |
| **Groq Vision AI (`Visual Director V2`)** | **Live-verified** | Two-stage pipeline (`MODE A` per-asset visual inspection on real pixels/keyframes → `MODE B` ranked collection selection) using `qwen/qwen3.8-27b` and versioned [`docs/AI_VISUAL_DIRECTOR_V2.md`](docs/AI_VISUAL_DIRECTOR_V2.md). Proven live (`mock: false`, ~$0.023 on 3-asset pilot). **Honest throughput limit:** Defaults in `.env.example` are tuned safely for Groq's `on_demand` tier (`8K TPM / 1K OTPM`), which paces at ~45s/asset (~15–20 min for 25 assets). Upgrade to **Groq Dev Tier** (env config change only) for 1–2 min full client runs. |
| **Per-Asset Rights Gate & Private Google Drive Delivery** | **Live-verified (Videos + Reports); Images tested via contract suite** | Every asset starts `rights_status: "unverified"`. Only explicitly cleared assets are read by n8n from the read-only mount and uploaded via Google Drive v3 OAuth (`06-google-drive-delivery.json`) into a **Restricted / Private** folder (`Best Images/`, `Best Videos/`, `Reports/`). Live pilot verified `Best Videos/` + `Reports/` in Drive; `Best Images/` upload is covered by automated Drive v3 contract tests and ready for a quick photo-heavy live pilot. |
| **Atelier Media Non-Technical Studio UI** | **Built & live on localhost** | Next.js 16 (`React 19`, `Tailwind CSS 4`) operator dashboard with guided first-run onboarding (`/setup-admin`), plain-English 4-step workflow, visual gallery & video player, Facebook Export ZIP dropzone, and zero secrets exposed to the browser. |
| **Public Production Hosting** | **Prepared, not yet deployed publicly** | Hardened [`deploy/compose.staging.yml`](deploy/compose.staging.yml), [`deploy/Caddyfile.prod`](deploy/Caddyfile.prod) (scoped `105MB` media upload cap, `64KB` default cap), WAL-consistent SQLite backup (`sqlite_backup.py`), readiness probe (`readiness_probe.py`), and [`docs/PRODUCTION_LAUNCH_RUNBOOK.md`](docs/PRODUCTION_LAUNCH_RUNBOOK.md) are committed. Currently running locally on Docker + `localhost:3000`; public launch still requires a VPS with HTTPS domain, production Google Drive OAuth, scheduled off-host backups, and Groq Dev Tier. |

---

## 2. Architecture & Strict Layer Ownership

The platform enforces clean separation of responsibilities with **no split-brain orchestration**:

```text
Browser (Non-Technical Operator)
  │  Same-origin HTTPS / HttpOnly signed cookies (zero API keys in browser)
  ▼
Atelier Media Frontend — Next.js 16 (frontend/, :3000)
  │  • Server-side API proxies with Origin/CSRF validation
  │  • Signed 30s single-use HMAC assertions (X-Named-Assertion) for admin mutations
  │  • Multipart media upload proxy (100 MB cap) -> framed binary stream to worker
  ├──────────────────────────────────────────────┐
  │ POST /webhook/pipeline-intake                │ Authenticated read & admin actions
  │ POST /webhook/drive-delivery                 │ (/jobs, /named/*, /file)
  ▼                                              ▼
n8n Orchestration (n8n-media/, :5679)     Python Media Worker (services/media-worker/, :8788 / :8787)
  │  • Intake validation & status polling   • FastAPI + SQLite (WAL) durable job & M4 ledger
  │  • Google Drive v3 OAuth delivery       • Multi-tenant 365-day historical checkpoint engine
  │  • Read-only mount: /data/media         • SSRF-hardened website crawler + vetted builder CDNs
  │  • Never runs Vision AI directly        • gallery-dl / yt-dlp / Instaloader subprocesses
  │                                         • Secure owner Facebook Export ZIP & media ingest
  │                                         • Pillow metrics + FFmpeg video keyframe extraction
  │                                         • Async Groq Vision AI Visual Director V2 runner
  │                                         • Read-write mount: /data/projects
  └───────────────────┬──────────────────────────┘
                      ▼
        Shared Host Bind Mount (${MEDIA_STORAGE_DIR:-./storage/jobs})
          ├── <job_id>/website/  facebook/  instagram/  frames/  collection/
          └── <job_id>/manifest.json  curation.json  delivery.json
                      │
                      │ Rights-cleared originals + sanitized reports only (read by n8n RO)
                      ▼
        Private Google Drive (Restricted Sharing)
          Pet Grooming AI Media / <business-domain>-<short-id>/
            ├── Best Images/
            ├── Best Videos/
            └── Reports/  (selection.json, source-coverage.json)
```

---

## 3. End-to-End Operator Workflow

1. **Sign In / Admin Onboarding:** On first local run, visit `/setup-admin` to bootstrap Named Admin accounts (automatically hard-disabled when `DASHBOARD_ORIGIN` uses `https://` in production; use `scripts/named_bootstrap.py` in production).
2. **Start a Collection Job (`/new`):** Enter the client's **Website URL** and **Facebook Page URL** (Instagram optional), set selection quotas and optional creative notes, and confirm collection permission. The worker freezes an immutable **365-day UTC window** at job creation.
3. **Review Collection & Optional Client Facebook Upload (`/jobs/[id]`):**
   - Inspect per-source coverage, website previews, and any platform diagnostics (such as `robots_disallowed` on Facebook CDN).
   - If the client supplied a **Facebook Export ZIP** or direct Facebook photos/videos, drop them into the **Add Client Facebook Media** panel to ingest, date-filter, preprocess, and create a new M4 revision.
   - Optionally enable **Include undated website media in AI curation** if the business website lacks structured publish dates.
   - Record **Two-Admin M4 approval** (two distinct Named Admins) when collection is partial or coverage is unverified.
4. **AI Selection, Rights Verification & Google Drive Delivery:**
   - Start **Groq Vision AI (`Visual Director V2`)** and monitor live progress.
   - Inspect ranked hero, salon, grooming, team, before/after, and video picks, then explicitly mark commercial **Rights Verified** per asset.
   - Confirm rights release and deliver cleared originals + reports straight to a private **Google Drive** folder.

---

## 4. Repository Structure

```text
.
├── compose.media.yml                # Local Docker stack (n8n + collector + optional --profile frontend)
├── .env.example                     # Sanitized V2.1 environment template (OTPM-safe Groq defaults)
├── AGENTS.md                        # Project engineering & compliance rules
├── frontend/                        # Atelier Media Next.js 16 operator studio & server API proxies
│   ├── src/app/                     # Pages (/login, /setup-admin, /, /new, /jobs/[id], /settings) & /api/*
│   ├── src/components/              # AppShell, Icons, LogoutButton
│   ├── src/lib/                     # Signed sessions, Named Admin HMAC client, security helpers, types
│   └── tests/                       # Unit, behavior, and live integration tests
├── services/media-worker/           # Python 3.12 FastAPI worker, collector, preprocessor & AI runner
│   ├── app/                         # service.py, public_collection.py, social_upload.py, named_auth.py,
│   │                                # security.py, ai/* (Groq V2 pipeline/prompts/schemas), etc.
│   ├── scripts/                     # named_bootstrap.py, sqlite_backup.py, readiness_probe.py,
│   │                                # groq_bounded_acceptance.py
│   └── tests/                       # Full offline pytest suite (SSRF, OS-1/OS-2, M4, AI, ops, security)
├── n8n-media/                       # Exported n8n workflow JSONs & Node contract tests
│   ├── 00-health-probe.json
│   ├── 05-ai-curation-probe.json
│   ├── 06-google-drive-delivery.json
│   └── 07-pipeline-orchestration.json
├── deploy/                          # Staging/Production HTTPS Compose, Caddyfile.prod & workflow sanitizer
├── docs/                            # V2.1 spec, Visual Director V2 prompt, launch/cutover & phase reports
├── .github/workflows/tests.yml      # 5-job CI pipeline (frontend, n8n, pytest, compose, gitleaks)
└── reference/                       # Git-ignored local read-only legacy archive (never executed/redistributed)
```

---

## 5. Technology Stack

- **Frontend:** Next.js `16.4.6` (standalone output), React `19.3.0`, TypeScript `5`, Tailwind CSS `4.3.3`, ESLint `9`.
- **Media Worker:** Python `3.12-slim` (non-root `appuser`), FastAPI, Uvicorn, Pydantic v2, BeautifulSoup4, Pillow, Requests, SQLite (WAL mode), `libsodium23` (Argon2id), `FFmpeg`.
- **External Media CLIs (argument-array subprocesses only; zero vendored CLI source):** `gallery-dl==1.32.20`, `yt-dlp>=2025.1.15`, `instaloader>=4.14,<5`.
- **Orchestration:** Self-hosted `n8n` (pinned `n8nio/n8n:2.48.7`), read-only media mount (`N8N_RESTRICT_FILE_ACCESS_TO=/data/media`), `$env` expression blocking enabled.
- **Vision AI:** OpenAI-compatible multimodal endpoint — verified with **Groq** (`qwen/qwen3.8-27b`) executing [`docs/AI_VISUAL_DIRECTOR_V2.md`](docs/AI_VISUAL_DIRECTOR_V2.md).
- **Reverse Proxy (Production):** Caddy `2.8-alpine` ([`deploy/Caddyfile.prod`](deploy/Caddyfile.prod)) with automatic HTTPS, security headers, `64KB` default request body limit, and `105MB` scoped strictly to `/api/jobs/*/media-upload`.

---

## 6. Local Setup & Running the Stack

### Prerequisites
- **Docker Desktop** (WSL2 backend on Windows) with ~2 GB RAM headroom.
- **Node.js 24+** (for running/testing the Next.js frontend and n8n contract tests locally; host Python is **not** required because Python 3.12 and `pytest` ship inside the worker image).
- Optional for live AI & Drive runs: a **Groq API key** (`AI_API_KEY`) and a **Google Drive OAuth2** credential configured in n8n.

### Step 1: Configure Environment
Copy `.env.example` to `.env` (never commit `.env`) and set independent random secrets:

```powershell
Copy-Item .env.example .env
```

Key settings in `.env`:
- **Storage & n8n:** `MEDIA_STORAGE_DIR`, `N8N_IMAGE=n8nio/n8n:2.48.7`, `N8N_ENCRYPTION_KEY`, `MEDIA_WORKER_TOKEN`.
- **V2.1 Engine & Governance:** `COLLECTION_ENGINE=historical`, `PUBLIC_COLLECTION_CHECKPOINTS=1`, `M4_COLLECTION_APPROVAL_ENABLED=1`, `NAMED_ASSERTION_SECRET` (32+ random bytes shared only between frontend server and worker).
- **Groq Vision AI:** `AI_PROVIDER=openai_compatible`, `AI_MODEL=qwen/qwen3.8-27b`, `AI_ENDPOINT=https://api.groq.com/openai/v1/chat/completions`, `AI_API_KEY`. Default token/spacing budgets (`850`/`950`, `AI_MAX_IMAGES_PER_REQUEST=1`) are sized safely for Groq's `8K TPM / 1K OTPM` `on_demand` tier; raise them only on **Groq Dev Tier** or higher.
- **Dashboard & Webhooks (server-side only; never `NEXT_PUBLIC_`):** `MEDIA_WORKER_DASHBOARD_TOKEN`, `N8N_PIPELINE_TOKEN`, `N8N_ADMIN_WEBHOOK_TOKEN`, `GOOGLE_DRIVE_READY` (`1` only after OAuth2 is connected in n8n), `DASHBOARD_ORIGIN`, `DASHBOARD_SESSION_SECRET`, `DASHBOARD_PASSWORD`.

> **n8n 2.x credential note:** `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` is enabled by design. Worker authentication inside n8n uses an n8n `httpHeaderAuth` Credential named `Media Worker API Token` matching `MEDIA_WORKER_TOKEN` — never `$env`.

### Step 2: Start Worker & n8n in Docker

```powershell
docker compose -f compose.media.yml config -q
docker compose -f compose.media.yml up -d --build
docker compose -f compose.media.yml ps
```

In the n8n editor (`http://localhost:5679`), import and activate:
- [`n8n-media/07-pipeline-orchestration.json`](n8n-media/07-pipeline-orchestration.json)
- [`n8n-media/06-google-drive-delivery.json`](n8n-media/06-google-drive-delivery.json)

Bind the `Media Worker API Token`, webhook header auth credentials, and (for live Drive uploads) your Google Drive OAuth2 credential.

### Step 3: Start Atelier Media Frontend

Option A — Run in Docker alongside worker and n8n:
```powershell
docker compose -f compose.media.yml --profile frontend up -d --build
```

Option B — Run locally on Node.js (with `frontend/.env.local` pointing `WORKER_URL=http://127.0.0.1:8788` and `N8N_URL=http://127.0.0.1:5679`):
```powershell
Set-Location frontend
npm ci
npm run build
# copy static assets for standalone mode, then start:
node --env-file=.env.local .next/standalone/server.js
```

### Step 4: Verify Full Stack Readiness

```powershell
docker run --rm --add-host=host.docker.internal:host-gateway pgm-media-collector:latest `
  python /srv/scripts/readiness_probe.py `
  --worker-url http://host.docker.internal:8788/ready `
  --n8n-url http://host.docker.internal:5679/healthz/readiness `
  --dashboard-url http://host.docker.internal:3000/api/ready
```

### Local Service Ports

| Service | Host URL | Internal Docker URL | Purpose |
| --- | --- | --- | --- |
| **Atelier Media Dashboard** | `http://localhost:3000` | `http://dashboard:3000` | Non-technical operator UI & server API proxies |
| **Project n8n** | `http://localhost:5679` | `http://n8n:5678` | Intake webhooks, status polling, Google Drive v3 delivery |
| **Python Media Worker** | `http://localhost:8788` | `http://collector:8787` | Collection, ZIP ingest, FFmpeg/Pillow, M4 ledger, Groq V2 AI |

All host ports are bound to `127.0.0.1` in `compose.media.yml`. Do not expose n8n or the worker directly to the public internet.

---

## 7. API & Route Overview

- **Frontend Server Routes (`frontend/src/app/api/`):**
  - Health/Readiness: `GET /api/health`, `GET /api/ready`
  - Auth & Onboarding: `POST /api/auth/login`, `POST /api/auth/logout`, `POST /api/auth/setup-admin`, `POST /api/auth/named/[action]`
  - Jobs & Collection: `POST /api/intake/validate`, `GET|POST /api/jobs`, `GET /api/jobs/[id]`, `GET /api/jobs/[id]/status`, `GET /api/jobs/[id]/manifest`, `GET /api/jobs/[id]/file`, `POST /api/jobs/[id]/resume`, `POST /api/jobs/[id]/media-upload`, `POST /api/jobs/[id]/website-policy`
  - Governance, AI & Delivery: `GET|POST /api/jobs/[id]/collection-approval`, `POST /api/jobs/[id]/collection-approval/decisions`, `POST /api/jobs/[id]/ai-start`, `POST /api/jobs/[id]/ai-execute`, `GET /api/jobs/[id]/curation`, `POST /api/jobs/[id]/rights`, `GET|POST /api/jobs/[id]/delivery`
- **Media Worker (`services/media-worker/app/service.py`):**
  - Probes: `GET /health`, `GET /ready`, `GET /status`
  - Orchestration & Jobs (scoped bearer tokens): `POST /intake/validate`, `POST /jobs`, `GET /jobs`, `GET /jobs/{job_id}`, `GET /jobs/{job_id}/pipeline-status`, `POST /jobs/{job_id}/orchestration`, `POST /jobs/{job_id}/resume-collection`, `GET /jobs/{job_id}/manifest`, `GET /jobs/{job_id}/file`, `GET /jobs/{job_id}/curation`, `GET /jobs/{job_id}/delivery-plan`, `POST /jobs/{job_id}/delivery-result`, `GET /jobs/{job_id}/delivery`
  - Named Admin HMAC Actions: `POST /named/{action}` (including `collection-media-upload`, collection approval decisions, website undated policy, AI execution, rights verification, and admin auth/session endpoints)

---

## 8. Automated Verification & CI Gates

Every PR and push to `main` runs 5 automated GitHub Actions jobs defined in [`.github/workflows/tests.yml`](.github/workflows/tests.yml). All suites run **fully offline** without external scraping, paid AI calls, or real credentials:

```powershell
# 1. Offline Python worker test suite (in-container, zero host Python needed)
docker compose -f compose.media.yml exec collector python -m pytest tests -q

# 2. Frontend unit tests, lint, production build, TypeScript & integration test
Set-Location frontend
npm test
npm run lint
npm run build
npx tsc --noEmit
npm run test:integration
Set-Location ..

# 3. n8n workflow contract & Google Drive v3 tests + staging sanitizer check
node --test n8n-media/canonical-qa-workflow.test.mjs n8n-media/approval-qa-workflow.test.mjs n8n-media/private-drive-delivery.test.mjs n8n-media/sprint4-workflows.test.mjs
node deploy/staging-workflow.mjs > $null

# 4. Validate local & staging/production Docker Compose manifests
docker compose -f compose.media.yml --profile frontend config --quiet

# 5. Gitleaks secret scan
docker run --rm -v "${PWD}:/repo:ro" zricethezav/gitleaks:latest dir /repo --no-banner --redact
```

For WAL-consistent SQLite backups in staging/production:
```powershell
docker compose -f compose.media.yml exec collector python /srv/scripts/sqlite_backup.py --help
```

---

## 9. Security, Privacy & Compliance Controls

- **SSRF & Network Safety:** Rejects loopback, RFC1918, link-local, metadata, IPv4-mapped IPv6, and non-canonical IP literals with verified-socket dialing, redirect hop limits, per-file byte caps, and HTTPS-443 enforcement on vetted builder CDN suffixes.
- **`robots.txt` Compliance:** Page and media fetches strictly honor `robots.txt` and record `robots_disallowed` honestly per source/item.
- **Upload & Subprocess Safety:** External CLIs run exclusively via argument arrays (never shell strings). Owner media/ZIP uploads enforce strict byte/file/uncompressed limits, reject path traversal (`..`), symlinks, and encrypted ZIP entries, and verify file magic headers before saving.
- **Authentication & Two-Admin Separation:** Named Admin passwords use Argon2id (`libsodium23`); admin mutations require 30-second HMAC assertions (`X-Named-Assertion`) with single-use replay nonces. Production `https://` origins automatically enable `__Host-` secure cookies and disable `/setup-admin`.
- **Prompt-Injection Hygiene:** Scraped website copy, filenames, and social captions are treated strictly as untrusted data; system prompt authority stays locked to [`docs/AI_VISUAL_DIRECTOR_V2.md`](docs/AI_VISUAL_DIRECTOR_V2.md).
- **Rights ≠ Public Visibility:** Every asset defaults to `rights_status: "unverified"`. Publicly visible website or social media is never treated as commercially licensed; delivery blocks until explicit human rights verification is recorded.
- **Secret & Data Hygiene:** `.env`, social cookies, SQLite databases, and downloaded client media are git-ignored and never committed; `gitleaks` scans every PR in CI.

---

## 10. Remaining Steps to Public Production Launch

1. **Merge OS-1 & OS-2 PRs into `main`** after all 5 GitHub Actions CI checks pass.
2. **Photo + Facebook Export ZIP Live Pilot:** Run one small photo-heavy job on `http://localhost:3000` (Website URL + Facebook URL + client Facebook ZIP upload → Two-Admin Approve → Groq AI → Rights Verify → Google Drive) to visually confirm **`Best Images/`** alongside **`Best Videos/`** and **`Reports/`** in Google Drive.
3. **Groq Tier Upgrade:** Upgrade the Groq account to **Dev Tier** and raise `AI_MAX_IMAGES_PER_REQUEST` / token budgets in production `.env` so 25–40 asset client jobs complete in ~1–2 minutes.
4. **Public HTTPS Deployment:** Follow [`docs/PRODUCTION_LAUNCH_RUNBOOK.md`](docs/PRODUCTION_LAUNCH_RUNBOOK.md) to deploy [`deploy/compose.staging.yml`](deploy/compose.staging.yml) + [`deploy/Caddyfile.prod`](deploy/Caddyfile.prod) on a VPS with a dedicated production domain, production Google Drive OAuth app, rotated tokens, scheduled off-host `sqlite_backup.py`, and uptime alerts on `/api/ready`, `/ready`, and `/healthz/readiness`.
5. **Optional Future V2.2 (Only If Needed):** If clients ever require fully automated 365-day Facebook downloads without exporting a ZIP, an official Meta Graph API Page OAuth design is documented separately in [`docs/V2_2_META_GRAPH_API_PLAN.md`](docs/V2_2_META_GRAPH_API_PLAN.md).

---

## 11. Key Documentation Index

- [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) — Authoritative V2.1 scope, architecture, and contracts
- [`docs/AI_VISUAL_DIRECTOR_V2.md`](docs/AI_VISUAL_DIRECTOR_V2.md) — Versioned Creative Director V2 system prompt
- [`docs/PRODUCTION_LAUNCH_RUNBOOK.md`](docs/PRODUCTION_LAUNCH_RUNBOOK.md) — Public HTTPS deployment, backups, OAuth, and cutover checklist
- [`docs/CUTOVER_RUNBOOK_V2_1.md`](docs/CUTOVER_RUNBOOK_V2_1.md) — V2.1 cutover & operational procedures
- [`docs/V2_2_META_GRAPH_API_PLAN.md`](docs/V2_2_META_GRAPH_API_PLAN.md) — Optional future Meta Graph API plan (not required for open-source V2.1)
- [`docs/AUDIT_PHASE_0.md`](docs/AUDIT_PHASE_0.md) — Original read-only baseline audit & reuse matrix

---

## 12. Upstream Dependencies & Provenance

This repository invokes the following open-source tools strictly as external subprocesses and redistributes **none of their source code**. Operators must respect each upstream license and applicable platform terms:

- [gallery-dl](https://codeberg.org/mikf/gallery-dl) — public social extractor
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — supported single-video fallback
- [Instaloader](https://instaloader.github.io/) — dormant/optional Instagram fallback
- [n8n](https://n8n.io/) — workflow orchestration & Google Drive v3 integration (fair-code)
- [Next.js](https://nextjs.org/), [FastAPI](https://fastapi.tiangolo.com/), [FFmpeg](https://ffmpeg.org/), [Pillow](https://python-pillow.org/), [Caddy](https://caddyserver.com/) — per upstream licenses

**Vendored legacy baseline provenance:** `services/media-worker/app/legacy_collector.py` and `services/media-worker/app/legacy_social_collect.py` originate from the owner's prior baseline archive (`pet-gromming-agent-factory-main (1).zip`, original SHA-256 baseline `5fe260e2db08dba502311fb780bad5501c380df0202108852e852580f92835dc` and `328ebcc00943d72d168237fa785f14f35264c02205888e039910f777afb9b4a7`, with `legacy_collector.py` subsequently extended in OS-1 for lazy media attributes). The original archive stays strictly local and read-only under git-ignored `reference/` and is never redistributed.

---

## 13. License

Not yet chosen — all rights reserved pending the repository owner's decision.

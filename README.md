# Pet Grooming Media Intelligence — Insta & Fb Media Collector

An AI-assisted media collection and curation pipeline for pet grooming
businesses. An operator supplies a business website URL (required) plus
optional public Instagram/Facebook URLs; a bounded, permission-gated worker
collects **publicly accessible, permitted** media, records honest provenance
(sha256, source URLs, dates or explicit `unknown`, unverified rights), and
produces a unified, disk-validated manifest. The pipeline includes AI-assisted
visual selection and Google Drive delivery, orchestrated by n8n.

> **Honesty first.** This project never fabricates coverage. Social platforms
> heavily gate anonymous automated access; every source reports truthful
> per-item statuses (`downloaded`, `blocked_by_robots`, `login_required`,
> `rate_limited`, `gallery_dl_error_*`, `unknown` dates) and a job is never
> marked successful without verifiable evidence.

---

## 1. Project overview

A nontechnical operator's goal: enter business URLs, get the best real,
permitted media for that business. The platform separates concerns:

- **n8n** (self-hosted, Docker) owns orchestration: job intake, status polling
  and Google Drive delivery. The worker calls the vision provider when opted in.
- **Python/Docker media worker** owns collection & processing: a vendored,
  SSRF-hardened website crawler; gallery-dl (primary social tool) with
  Instaloader / yt-dlp fallbacks; and deterministic Phase 4 media
  preprocessing (cross-source dedupe, technical quality metrics, bounded
  FFmpeg frame extraction, verified-date categorization).
- **Shared bind-mounted storage** holds per-job media and manifests.

## 2. Current implementation status

| Capability | Status | Evidence |
|---|---|---|
| Legacy audit & architecture plan (Phase 0) | ✅ Done | `docs/AUDIT_PHASE_0.md` |
| Docker + n8n infrastructure, shared storage, credential auth (Phase 1) | ✅ Done | `docs/PHASE_1_REPORT.md` |
| Website media collector + unified manifest (Phase 2) | ✅ Done | 10 offline tests + bounded live smoke (`docs/PHASE_2_REPORT.md`) |
| Instagram/Facebook collection engine (Phase 3) | ✅ Done | 13 offline mocked tests + bounded consent-gated live smoke (`docs/PHASE_3_REPORT.md`) |
| Media preprocessing: dedupe, quality metrics, video frames, date categories (Phase 4) | ✅ Done | 14 offline tests + live end-to-end evidence (`docs/PHASE_4_REPORT.md`) |
| AI Visual Director V2 (Phase 5) | Implemented and mock-tested. Live Groq MODE A/B acceptance pending. | [Report](docs/PHASE_5_REPORT.md) |
| Drive delivery (Phase 6) | Implemented and fixture-tested. OAuth and live upload pending. | [Report](docs/PHASE_6_REPORT.md) |
| n8n orchestration (Phase 7) | Intake, bounded polling, rights checks and status. | [Report](docs/PHASE_7_REPORT.md) |
| Dashboard (Phase 8) | Local Next.js dashboard for collection and job review. | [Report](docs/PHASE_8_REPORT.md) |
| Security and staging (Phase 9) | Signed dashboard sessions, internal API roles, safe intake and private HTTPS Compose configuration. | [Runbook](docs/PHASE_9_REPORT.md) |

## 3. Architecture

The browser calls the authenticated Next.js dashboard. Dashboard submissions go to
n8n's authenticated intake webhook, which validates and starts a worker job,
responds with a job ID and continues polling. Dashboard reads use a scoped worker
token. The worker stores jobs in SQLite and writes media and manifests on the shared
volume. n8n gets read-only access to selected originals for rights-gated Drive delivery.

## 5. Technology stack

- **Worker:** Python 3.12, FastAPI, uvicorn, Pydantic v2, BeautifulSoup4,
  Pillow, requests.
- **External media tools (invoked as argument-array subprocesses; no source
  incorporated):** gallery-dl 1.32.20 (primary social), Instaloader
  (Instagram fallback), yt-dlp (Facebook single-video fallback), FFmpeg
  for bounded frame extraction.
- **Orchestration:** n8n (self-hosted Docker image, pinned in `.env`).
- **Storage:** host bind mount shared worker-RW / n8n-RO.
- **Testing:** pytest (offline, in-process HTTP fixtures, mocked tool
  runners — no network needed).

## 6. Requirements and prerequisites

- Docker Desktop (WSL2 backend) with ~2 GB RAM headroom for two containers.
- Python 3.12 **only inside the worker image** (no host Python required; the
  test suite runs in-container).
- n8n credentials: an n8n **Credential** of type `httpHeaderAuth` named
  `Media Worker API Token` (its value must match `MEDIA_WORKER_TOKEN` in
  `.env`) for authenticated workflow calls.
- AI and Drive credentials are optional for local fixture tests. Live provider and Drive acceptance remain pending.

## 7. Local Docker setup

```powershell
# 1) configure environment
Copy-Item .env.example .env      # then edit values (see next section)

# 2) validate + start
docker compose -f compose.media.yml config -q
docker compose -f compose.media.yml up -d
docker compose -f compose.media.yml ps       # both services healthy

# 3) Import the workflow JSONs from n8n-media/ in the project n8n editor.
# Bind credentials and publish the health and intake workflows per the Phase 9 runbook.

# 4) probe end-to-end (webhook → worker health → auth → file mount)
Invoke-WebRequest http://127.0.0.1:5679/webhook/phase1-probe
```

## 8. Environment variable setup

For Phase 9 local development, configure the worker and dashboard with
a shared read-scoped `MEDIA_WORKER_DASHBOARD_TOKEN`. Configure the frontend
server environment with `DASHBOARD_ORIGIN` (exact origin), `DASHBOARD_PASSWORD`,
`DASHBOARD_SESSION_SECRET` (independent random key of 32+ characters) and
`N8N_PIPELINE_TOKEN`. Bind the n8n workflow credentials before testing intake.
Use a separate staging instance for the rollout described in the
[Phase 9 staging runbook](docs/PHASE_9_REPORT.md).


Copy `.env.example` → `.env` and set:

| Variable | Purpose |
|---|---|
| `N8N_IMAGE` | Pinned n8n image (e.g. `n8nio/n8n:2.48.7`) |
| `N8N_ENCRYPTION_KEY` | n8n credential encryption key (generate randomly) |
| `MEDIA_WORKER_TOKEN` | Worker auth token; also stored as the n8n `httpHeaderAuth` credential |
| `TZ` / `GENERIC_TIMEZONE` | Timezone for n8n |

Never commit `.env`. In n8n 2.x, `$env` access in expressions is blocked by
default — worker authentication uses an **n8n Credential**, not env vars.

## 9. Ports

| Service | URL | Notes |
|---|---|---|
| **Project n8n** | `http://localhost:5679` | this repo's stack (host port 5679 → container 5678) |
| **Python worker** | `http://localhost:8788` | host 8788 → container 8787 |
| Dashboard | `http://localhost:3000` | run in `frontend/` |
| Legacy n8n (pre-existing, unrelated) | `http://localhost:5678` | a separate older instance on this machine — **not part of this repository** |

Host ports 5679/8788 were chosen because 5678/8787 were already occupied
locally; other machines may use whatever is free (edit the `ports:` block).

## 10. Media storage architecture

- Worker writes job data to `/data/projects/<job_id>/` (RW); n8n mounts the
  **same host folder** at `/data/media` (RO) with
  `N8N_RESTRICT_FILE_ACCESS_TO=/data/media`.
- The committed `compose.media.yml` points this bind at
  `D:/pgm-media-storage/jobs` — **that is a machine-specific path from our
  local setup, not a requirement.** Adjust the two `volumes:` lines to any
  path with adequate free space on your machine.
- Per-job layout: `website/`, `instagram/`, `facebook/` (originals),
  `research/` (provenance: page snapshots, social metadata rows,
  metadata-stripped copies, download archives), `manifest.json`
  (unified, multi-source, sha256-verified).
- Runtime storage is git-ignored; the repository contains no media, no job
  databases, and no customer data.

## 11. Local development instructions

```powershell
# code lives in services/media-worker (app/ + tests/)
docker compose -f compose.media.yml build collector   # rebuild after edits
docker compose -f compose.media.yml up -d              # recreate worker

# quick checks
Invoke-WebRequest http://localhost:8788/health
Invoke-WebRequest http://localhost:8788/ready
```

`POST /jobs` requires `website_url`, `instagram_url` and `facebook_url` for every new job.
New jobs freeze a 365-day UTC window at creation. The legacy `lookback_days` input is accepted for compatibility and does not change that window.
The endpoint also accepts `max_pages` (1–25),
`max_media` (1–80), `max_social_files` (1–60), and
**`social_permission_confirmed`** — an operator acknowledgement that intended
collection/use is permitted. It is *not* platform authorization, *not* a
copyright licence, and it never bypasses login walls or rate limits; when
false, no social collection is attempted at all.

## 12. Running automated tests

```powershell
docker compose -f compose.media.yml exec collector python -m pytest tests -q
# Workflow JSON tests need the repository layout available in the test runtime.
```

All tests are offline: in-process HTTP fixtures and mocked
gallery-dl/Instaloader/yt-dlp runners. No network access, no Docker services
beyond the worker itself, no secrets. CI runs the same suite on Python 3.12.

## 13. Current API endpoints

- Dashboard with a signed session: `POST /api/jobs` returns the accepted job ID;
  `GET /api/jobs` and per-job routes return status, previews and manifests.
- Worker: `GET /health` and `GET /ready` are probe endpoints. The dashboard token
  may read jobs and call `POST /intake/validate`. Job creation, operations
  status, delivery planning and updates require the n8n worker token.
- n8n: `POST /webhook/pipeline-intake` and `GET /webhook/pipeline-status` use
  header authentication. `POST /webhook/drive-delivery` uses an admin credential
  and requires explicit rights approval.

Frontend checks run from `frontend/` with `npm test`, `npm run lint`,
`npm run build`, `npx tsc --noEmit` and `npm run test:integration` after building.

## 14. Security and privacy considerations

- **SSRF-hardened crawler:** hostname/IP validation (loopback, RFC1918,
  link-local, IPv4-mapped IPv6, non-canonical literals rejected),
  verified-socket dialing, redirect hop cap, per-file byte caps, crawl
  deadline.
- **Robots respected:** page and media fetches honour `robots.txt`
  (disallowures recorded per item — never silently ignored).
- **Subprocess safety:** external tools invoked with argument arrays, never
  shell strings; size/staging caps; secret scrubbing in logs.
- **Consent gating:** social collection requires the operator acknowledgement
  flag; failures (`login_required`, `rate_limited`, access gates) are
  reported, never bypassed. **No CAPTCHA bypass, no credential theft, no
  cookie/session smuggling, no restrictions circumvention.**
- **Prompt-injection hygiene:** page text is sanitized before storage;
  scraped content is treated strictly as data.
- **Secret hygiene:** secrets live only in `.env` and n8n Credentials;
  `.env` is git-ignored; the public repo contains none (scanned with
  gitleaks pre-publish).
- **Rights vs. visibility:** every asset carries
  `rights_status: "unverified"` — public visibility is not a licence; a
  human rights review gate precedes any planned publication use.

## 15. Known limitations

1. Anonymous social extraction is **partial and low-volume by design**;
   Instagram/Facebook gate anonymous automated access aggressively
   (error envelopes, login walls, rate limits). Full, timestamped history
   requires platform-authorized access (deliberately not implemented).
2. Website asset publication dates are generally unverifiable → recorded as
   `unknown`, never invented.
3. Live Groq MODE A/B and Google Drive OAuth/upload acceptance remain open.
4. Commercial distribution requires explicit rights approval.

## 16. Development roadmap

Phases 0–8 delivered the existing pipeline and dashboard. Phase 9 provides
local hardening and the staging runbook. Phase 10 covers live end-to-end
acceptance after approval. Phase 11 completes documentation and handover.

## 17. Upstream dependencies and attribution

This repository calls the following tools as external subprocesses and ships
**none of their source code**. Users must respect each project's license and
each platform's terms:

- [gallery-dl](https://github.com/mikf/gallery-dl) (also mirrored at
  [Codeberg](https://codeberg.org/mikf/gallery-dl)) — primary social extractor.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — fallback for supported single
  Facebook post/video URLs.
- [Instaloader](https://instaloader.github.io/) — Instagram fallback.
- [n8n](https://n8n.io/) — workflow automation (fair-code).
- FFmpeg, FastAPI, BeautifulSoup, Pillow, requests, Pydantic, pytest —
  per their upstream licenses.

**Vendored legacy modules:** `services/media-worker/app/legacy_collector.py`
and `app/legacy_social_collect.py` are byte-for-byte vendored from the
project's own prior baseline archive
(`pet-gromming-agent-factory-main (1).zip`, the user's earlier
`pet-gromming-agent-factory` project). SHA-256 provenance:
`5fe260e2db08dba502311fb780bad5501c380df0202108852e852580f92835dc`
(collector) and
`328ebcc00943d72d168237fa785f14f35264c02205888e039910f777afb9b4a7`
(social collector). That archive is retained **locally only**
(`reference/`, git-ignored) and is intentionally not redistributed here;
`docs/AUDIT_PHASE_0.md` documents the full reuse analysis.

## 18. License

Not yet chosen — all rights reserved pending the owner's decision.

# Phase 8 Report — Frontend Dashboard

**Date:** 21 November 2026
**Branch:** `feat/phase8-frontend-dashboard` (from `main` @ `ce7e0c52…`)
**Status:** Complete. Next.js dashboard builds and runs locally; backend regression suite green. No live AI or Google Drive operations.

## Frontend architecture

- Next.js 16 (App Router) + React 19 + TypeScript + Tailwind CSS 4 under `frontend/`.
- Server-side API routes under `frontend/src/app/api/*` proxy to the worker (`WORKER_URL`, default `http://127.0.0.1:8788`) using `MEDIA_WORKER_TOKEN` (server-only, never shipped to browser).
- n8n health checked via `N8N_URL` (default `http://127.0.0.1:5679`).
- Optional password gate via `DASHBOARD_PASSWORD` (cookie-based, MVP only).
- Client uses `fetch` to same-origin `/api/*`; no internal tokens in browser bundles.

## Screens implemented

1. **Dashboard** (`/`) — totals for submitted/running/completed/failed from real `/api/jobs`; recent job activity.
2. **New Media Collection** (`/new`) — validated form (website required, URLs, ranges, AI/social flags) → `/api/intake/validate` → `/api/jobs`.
3. **Job Progress / Details** (`/jobs/[id]`) — stage-based progress bar, source outcomes, preprocessing/AI status, errors, media thumbnails via protected file proxy, curation (mock curation never shown as genuine), delivery ledger, manifest metadata. Bounded polling every 10s until terminal.
4. **Settings / System Status** (`/settings`) — worker/n8n connectivity, AI provider & Drive readiness (no secrets).
5. **Login** (`/login`) — password gate when `DASHBOARD_PASSWORD` set.

## Actual API integrations

- `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/pipeline-status`, `GET /jobs/{id}/manifest`, `GET /jobs/{id}/curation`, `GET /jobs/{id}/delivery`, `GET /jobs/{id}/file?path=`, `POST /intake/validate`, `POST /jobs`, `GET /health` (worker), `GET /healthz` (n8n).

## Tests and build results

- Frontend: `npm run build` → **Compiled successfully** (exit 0); `npm run lint` → 0 errors (5 warnings).
- Dev server: `npm run dev` → HTTP 200 on `/`.
- Backend regression: **113 passed, 3 skipped**; new endpoints verified live (`/jobs`, `/jobs/{id}/file` → 200).
- n8n `:5679`, worker `:8788`, legacy n8n `:5678` healthy.

## Screenshots / UI evidence

- Dev server responded HTTP 200 on `/` (12993 bytes). Full visual capture not automated; pages render the documented layout.

## Security and external blockers

- Media files served only through validated relative paths inside the job directory (traversal guard).
- No secrets in browser bundles; internal tokens server-side only.
- Auth is MVP password gate, not production-grade; multi-user isolation not implemented.
- Blocked stages remain honest: AI curation and Drive delivery show blocked/awaiting configuration because genuine MODE B and Google Drive OAuth are not yet validated.

## Local startup

```
cd frontend
npm install
npm run dev   # http://localhost:3000
```
Set `WORKER_URL`, `N8N_URL`, `MEDIA_WORKER_TOKEN`, optional `DASHBOARD_PASSWORD` in the environment (or `.env.local`).

## Remaining Phase 9 deployment requirements

- Production auth (real users/roles), HTTPS, CORS allowlist, rate limiting, deployment of frontend + worker + n8n, backup/monitoring, and completion of Phase 5/6 external validations.

## GitHub PR

Branch `feat/phase8-frontend-dashboard`; PR to be opened into `main`. CI to be verified after push.

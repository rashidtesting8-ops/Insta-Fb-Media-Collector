# Phase 7 Report — n8n End-to-End Orchestration

**Date:** 21 November 2026
**Branch:** `feat/phase7-n8n-orchestration` (from `main` @ `eb12318d…`)
**Status:** Complete. Real n8n bounded execution validated (invalid input + resume existing job + idempotent re-submit + status webhook). No live AI or Google Drive operations performed. Workflow imported and published on project n8n (`:5679`).

## Features implemented

- **`n8n-media/07-pipeline-orchestration.json`** — single controller workflow with two webhooks:
  - `POST /webhook/pipeline-intake`: validate → create/resume worker job → bounded poll loop (Wait 10s, max 120 attempts) → rights-approval gate → Drive delivery (folder create + binary upload loop + ledger persist) → final status.
  - `GET /webhook/pipeline-status?job_id=`: read-only reconciled status.
- **Worker additions (additive, no contract breaks):**
  - `app/orchestration.py`: `validate_intake`, `derive_stage`, `find_idempotent_job`, `poll_decision`, `STAGES`, `MAX_POLL_ATTEMPTS`.
  - `POST /intake/validate`, `GET /jobs/{id}/pipeline-status`, `POST /jobs/{id}/orchestration`.
  - `idempotency_key` on `POST /jobs` (reuse existing job on duplicate key).
  - `pipeline_stage` checkpoints in `_process_job` (collecting → preprocessing → ai_processing).
- **`app/delivery.py`** retry-safety: plan carries `uploaded_keys`/`pending_uploads`/`drive_folder_id` from prior `delivery.json`; `finalize_delivery` merges prior ledger; `post_delivery_result` rebuilds plan when missing.
- Status reconciliation maps worker statuses (`queued/running/extraction_complete/partial_success/failed/interrupted`) to the 10 Phase 7 stages, including `awaiting_rights_approval`, `delivering`, `blocked`.

## Tests

- Offline suite: **113 passed, 3 skipped** in-container (skipped = n8n workflow-structure tests that need the repo layout; they run in CI from the repo).
- Phase 7 tests cover: stage derivation for all statuses/delivery overrides, valid/invalid/multi-source/website-only intake, resume mode, preferences, idempotency lookup, poll decisions, workflow JSON structure + no secrets + bounded polling + two webhooks.
- Security scan: no secret-like patterns; `.env`/`reference`/storage ignored.

## n8n workflows created

- `n8n-media/07-pipeline-orchestration.json` (35 nodes, 28 connections, `active: false` in repo; published for the local test).
- Reuses existing `06-google-drive-delivery.json` semantics via inline Drive nodes (credential referenced by name only).

## Verified local execution results

- n8n `:5679` and worker `:8788` healthy; legacy n8n `:5678` untouched.
- Invalid intake: `POST /webhook/pipeline-intake {"website_url":"not-a-url"}` → `{"ok":false,"stage":"failed","errors":[...]}` (400).
- Resume existing terminal job: `{"job_id":"14fec325…","stage":"partial_success"}` (no new job, no AI/Drive calls).
- Idempotent re-submit: identical body returned the same `job_id` (worker `idempotent_reuse`).
- Status webhook: `GET /webhook/pipeline-status?job_id=…` → reconciled stage with terminal flag.
- No live AI (Groq) or Google Drive upload executed.

## External blockers

1. Google Drive OAuth2 credential not configured (Drive stage returns `blocked` / `drive_credentials_unavailable`).
2. Genuine Phase 5 MODE B curation still unverified — no real customer-approved selections released (fixture/mock curation remains blocked by default).
3. Operator must confirm `authorization_confirmed` for any commercial distribution.

## GitHub PR URL and CI status

Branch `feat/phase7-n8n-orchestration`, PR to be opened into `main`; CI `tests` workflow to be verified after push.

## Phase 8 readiness

Frontend (Phase 8) may begin against the stable worker + n8n endpoints (`/jobs`, `/jobs/{id}`, `/jobs/{id}/pipeline-status`, `/jobs/{id}/delivery-plan`, `/jobs/{id}/delivery`). Core orchestration and delivery contracts are in place. Stop after Phase 7 per instruction; Phase 8 not started.

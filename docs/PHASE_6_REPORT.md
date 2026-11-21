# Phase 6 Report — Google Drive Delivery Engine

**Date:** 21 November 2026
**Branch:** `feat/phase6-google-drive-delivery` (from `main` @ `edfce77e…`)
**Status:** Integration complete with fixture-only tests. **Live Drive upload blocked — no Google OAuth credentials configured. No real AI curation exists yet (Phase 5 MODE B unverified), so nothing was released as a customer deliverable.**

## Architecture / flow

```text
n8n webhook (POST /webhook/drive-delivery: job_id, business_name,
             explicit authorization_confirmed)
  -> HTTP GET worker /jobs/{id}/delivery-plan
       worker validates curation (blocks mock/fixture/test-only by default),
       resolves selected IDs against the manifest, checks safe relative paths,
       file existence and sha256, computes deterministic upload_key,
       returns sanitized folder name (<business>-<jobid8>) + n8n paths
  -> Google Drive OAuth2 credential: create business folder
  -> Read/Write Files from Disk: read each selected ORIGINAL from /data/media
  -> Google Drive upload (binary), no public sharing
  -> HTTP POST worker /jobs/{id}/delivery-result (uploads ledger, folder IDs,
       authorization flag) -> persisted delivery.json
Frontend/status: GET /jobs/{id}/delivery-plan | /delivery-result | /delivery
```

## Implementation

- `app/delivery.py`: `sanitize_folder_name`, `build_delivery_plan`, `finalize_delivery`. Strict safety: absolute/`..` paths rejected, missing files/sha mismatch rejected, unknown selected IDs rejected, mock/fixture curation blocked unless explicitly `allow_fixture=True` (tests only). Rights gating: without `authorization_confirmed=True`, delivery status `blocked_rights_review`. Idempotency: deterministic `upload_key = asset_id:sha256`; duplicate retries ignored. Partial failures recorded per asset. No public-link API usage anywhere.
- `app/service.py`: three additive endpoints (plan / result / delivery), all existing endpoints unchanged.
- `n8n-media/06-google-drive-delivery.json`: webhook → request validation → worker plan → plan-ready gate → Drive folder create → binary read → Drive upload → ledger persist → response; Google Drive credential referenced **by name only**, no secrets in the workflow.
- `tests/test_phase6_delivery.py`: all curation is explicitly labeled `FIXTURE-ONLY-PHASE6-INTEGRATION`; fixtures are used only in `allow_fixture=True` integration tests and can never become customer deliverables.

## Test evidence

Folder naming (URL/domain and fallback) · fixture curation blocked by default · asset mapping with sha/rights/n8n path · path traversal and missing files rejected · sha mismatch detected · rights gate (`blocked_rights_review`) · upload ledger and persisted `delivery.json` schema · retry idempotency (first successful upload wins) · partial upload failure · n8n workflow JSON imports with credential-by-name. Full offline suite after rebuild: **98 passed, 1 skipped** in the worker image (the n8n-workflow-file test skips there because workflow assets aren't copied into the worker; CI runs from the repo where the file exists).

## Live Drive validation

**BLOCKED:** no Google OAuth credentials configured (`.env` has no `GOOGLE_*`/Drive vars; n8n credential not created). Per the approval, no live upload was attempted. Additionally, Phase 5 genuine MODE B remains unverified — no real curation exists, and fixture/mock selections must not be released. No media was made public.

## Files changed

`app/delivery.py` (new), `app/service.py`, `n8n-media/06-google-drive-delivery.json` (new), `tests/test_phase6_delivery.py` (new), this report. No secrets, media, DBs, backups or runtime artifacts committed.

## Blockers / Phase 7 readiness

Blockers: (1) configure Google Drive OAuth2 credential in n8n and bind it to the workflow; (2) complete genuine Phase 5 MODE A/B curation; (3) operator confirms `authorization_confirmed` for any commercial distribution. **Frontend (Phase 7) not started.**

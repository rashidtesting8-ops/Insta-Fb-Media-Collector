# Pet Grooming Media Intelligence — V2.1 Production Cutover Runbook

This runbook defines the exact, reversible operator steps to cut over the live local Docker stack (`pgm-media`) from the pre-Sprint-1 build to the hardened V2.1 Website + Facebook Media Collection SaaS.

> [!IMPORTANT]
> Do not execute these commands until you have reviewed and approved the cutover window. All existing downloaded media on `MEDIA_STORAGE_DIR` and all n8n credentials inside the `n8n_media_data` volume are preserved.

---

## 1. Pre-Cutover Backup & Environment Check

1. Verify current container health and record existing storage path:
   ```powershell
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   ```
2. Back up the live SQLite database before any schema migration (replace `D:/pgm-media-storage/jobs` with your actual `MEDIA_STORAGE_DIR`):
   ```powershell
   Copy-Item "D:/pgm-media-storage/jobs/_system/jobs.sqlite" "D:/pgm-media-storage/jobs/_system/jobs.sqlite.pre-v2.1.bak"
   ```
3. Confirm your local `.env` (never committed) defines all required V2.1 variables from `.env.example`:
   - `MEDIA_STORAGE_DIR=D:/pgm-media-storage/jobs` (or your chosen host storage directory)
   - `COLLECTION_ENGINE=historical`
   - `PUBLIC_COLLECTION_CHECKPOINTS=1`
   - `M4_COLLECTION_APPROVAL_ENABLED=1`
   - `NAMED_ASSERTION_SECRET=<independent 32+ char secret>`
   - `MEDIA_WORKER_TOKEN=<admin token>` and `MEDIA_WORKER_DASHBOARD_TOKEN=<separate read token>`
   - `AI_PROVIDER=groq`, `AI_MODEL=meta-llama/llama-4-scout-17b-16e-instruct`, `AI_ENDPOINT=https://api.groq.com/openai/v1/chat/completions`, `AI_API_KEY=<groq key>`, `AI_MAX_IMAGES_PER_REQUEST=5`
   - `N8N_PIPELINE_TOKEN=<pipeline webhook token>` and `N8N_ADMIN_WEBHOOK_TOKEN=<separate admin webhook token>`
   - `GOOGLE_DRIVE_READY=1` (only after confirming Google Drive OAuth2 credential in n8n)
4. Validate rendered Compose config without starting containers:
   ```powershell
   docker compose -f compose.media.yml config --quiet
   ```

---

## 2. Rebuild & Restart `pgm-media` Services

1. Rebuild the hardened worker image and recreate `collector` + `n8n` (preserves `n8n_media_data` and `MEDIA_STORAGE_DIR`):
   ```powershell
   docker compose -f compose.media.yml up -d --build collector n8n
   ```
   *(Optional — to also run the containerized Next.js dashboard on `http://localhost:3000`:)*
   ```powershell
   docker compose -f compose.media.yml --profile frontend up -d --build
   ```
2. Verify container health and worker readiness:
   ```powershell
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   Invoke-RestMethod http://127.0.0.1:8788/ready
   ```

---

## 3. One-Time M4 Database Migration & Named Admin Bootstrap

1. Apply the additive M4 collection approval & rights ledger migration inside `collector`:
   ```powershell
   docker exec pgm-media-collector-1 python -m app.collection_approval --migrate-db /data/projects/_system/jobs.sqlite
   ```
2. Bootstrap the first Named Admin account (if not already provisioned):
   ```powershell
   docker exec -it pgm-media-collector-1 python /srv/scripts/named_bootstrap.py
   ```
3. Sign in to the operator dashboard (`/login` -> **Named Admin**), open `/settings`, verify:
   - **Worker:** `OK` (`collection_engine: historical`, `M4 Approval & Rights Gate: Enabled`)
   - **n8n:** `OK`
   - **AI provider configured (worker):** `Yes (groq · ...)`
   - **Drive delivery webhook token:** `Configured`
4. Create a second independent Named Admin account in `/settings` (required for two-admin separation between job submitter and approver).

---

## 4. Import Repaired n8n Workflows

Open `http://127.0.0.1:5679` and import (or update) from `/opt/media-workflows` (repo `n8n-media/`):
1. `07-pipeline-orchestration.json` — verify credentials `Media Pipeline Webhook Token`, `Media Worker API Token`, and `Google Drive account` (OAuth2); activate.
2. `06-google-drive-delivery.json` — verify credentials `Media Admin Webhook Token` (header `X-Admin-Key`, matching `N8N_ADMIN_WEBHOOK_TOKEN`), `Media Worker API Token`, and `Google Drive account` (OAuth2); activate.
3. Keep `00-health-probe.json` and `05-ai-curation-probe.json` inactive unless explicitly running probes.

---

## 5. First Guarded End-to-End Smoke Verification

1. Submit a new Website + Facebook collection from `/new` (leaving Instagram blank or optional) while signed in as Named Admin #1.
2. Once collection + preprocessing completes, inspect the job at `/jobs/<id>`:
   - Review the **Media gallery** (verify images, `.media` files, `<video>` previews, and date provenance badges).
   - Optionally toggle **Include undated website media** if the salon's website gallery should be included in AI curation.
3. Sign in as Named Admin #2:
   - Click **Approve** (with a recorded reason) -> **Start AI** -> check consent -> **Run AI Curation (Async)** (`202 Accepted`).
   - Confirm `curation.json` completes with real Groq Vision scores, hero ranks, and crop guidance.
   - Verify commercial rights per selected asset (`evidence_ref` + `reason` -> **Verify**).
   - Check commercial release confirmation and click **Deliver Approved Media to Google Drive**.
   - Open the returned private Google Drive folder link and confirm original binaries uploaded with private visibility.

---

## 6. Rollback Procedure

If any cutover check fails:
1. Stop recreated containers without deleting volumes:
   ```powershell
   docker compose -f compose.media.yml stop collector n8n
   ```
2. Restore the backed-up SQLite file (`jobs.sqlite.pre-v2.1.bak`) if needed, check out the prior git tag/commit, and rebuild.

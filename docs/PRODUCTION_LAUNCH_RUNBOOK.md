# Pet Grooming Media Intelligence — Public Production Launch Runbook

This runbook covers Priority 3 of the post-pilot launch plan: hosting the hardened V2.1 stack behind public HTTPS, running automated daily WAL-consistent SQLite backups with restore verification, monitoring service readiness, provisioning isolated production Google Drive OAuth, and rotating all secrets cleanly.

> [!IMPORTANT]
> Never reuse localhost pilot credentials, local `.env` secrets, or the `Google Drive – Local Test` OAuth credential in production. Keep `.env` outside Git at all times.

---

## 1. Public HTTPS Deployment (Caddy 2 + `deploy/compose.staging.yml`)

The hardened Compose stack in [`deploy/compose.staging.yml`](../deploy/compose.staging.yml) isolates `collector` and `n8n` on an internal `worker` network and exposes only the `dashboard` through the `https` Caddy reverse proxy on `edge`.

### Required Production Environment Settings (in host-only `.env.prod`, `chmod 600`)
- **Canonical HTTPS Origin & Hostname:**
  - `STAGING_HOST=app.yourdomain.com` *(hostname without scheme or port)*
  - `DASHBOARD_ORIGIN=https://app.yourdomain.com` *(automatically enables `__Host-` `Secure` cookies and permanently disables `/setup-admin` inside Next.js)*
  - `ACME_EMAIL=ops@yourdomain.com`
  - `CADDYFILE_PATH=./Caddyfile.prod`
- **Pinned Images:**
  - `N8N_IMAGE=n8nio/n8n:2.48.7`
  - `CADDY_IMAGE=caddy:2.8-alpine`
- **Isolated Production Storage:**
  - `STAGING_MEDIA_DIR=/srv/pgm-media-prod/jobs` *(fresh dedicated volume/disk with monitoring for disk pressure)*
- **Security & M4 Separation (all independent 32+ byte random secrets):**
  - `COLLECTION_ENGINE=historical`
  - `PUBLIC_COLLECTION_CHECKPOINTS=1`
  - `M4_COLLECTION_APPROVAL_ENABLED=1`
  - `MEDIA_WORKER_TOKEN`, `MEDIA_WORKER_DASHBOARD_TOKEN`, `NAMED_ASSERTION_SECRET`, `DASHBOARD_PASSWORD`, `DASHBOARD_SESSION_SECRET`, `N8N_PIPELINE_TOKEN`, `N8N_ADMIN_WEBHOOK_TOKEN`, `N8N_ENCRYPTION_KEY`
- **AI Budget & Rate-Limit Controls:**
  - For Groq `on_demand` (`1,000` OTPM / `8,000` TPM): keep `AI_MODE_A_MAX_OUTPUT_TOKENS=850`, `AI_MODE_B_MAX_OUTPUT_TOKENS=950`, `AI_BATCH_SIZE=1`, `AI_MAX_IMAGES_PER_REQUEST=1`, `AI_MIN_REQUEST_SPACING_S=5`, `AI_MAX_RETRIES=3`.
  - After upgrading to Groq Developer Tier: see [`docs/V2_2_META_GRAPH_API_PLAN.md`](./V2_2_META_GRAPH_API_PLAN.md) for safe production throughput tuning.

### Publishing Ports 80 & 443 Safely
By default, `deploy/compose.staging.yml` binds `https` to loopback `127.0.0.1:8443:443` so staging is never accidentally public. For public production hosting on a hardened Linux VPS, use a small host-local override file (e.g., `compose.prod.override.yml`) publishing `80:80` and `443:443` on `https` ONLY (never expose `collector:8787`, `dashboard:3000`, or `n8n:5678` publicly; access n8n admin strictly via SSH tunnel to `127.0.0.1:5680`):

```yaml
services:
  https:
    ports: !override
      - "80:80"
      - "443:443"
```

Validate before starting:
```bash
docker compose --env-file .env.prod -f deploy/compose.staging.yml -f compose.prod.override.yml config --quiet
docker compose --env-file .env.prod -f deploy/compose.staging.yml -f compose.prod.override.yml up -d --build
```

### Production Admin Provisioning (No Browser Bootstrap)
Because `DASHBOARD_ORIGIN=https://...` AND [`deploy/Caddyfile.prod`](../deploy/Caddyfile.prod) both block `/setup-admin*` (`404`), bootstrap the first Named Admin exclusively via authenticated server SSH + CLI:

```bash
docker exec pgm-media-staging-collector-1 python -m app.collection_approval --migrate-db /data/projects/_system/jobs.sqlite
docker exec -it pgm-media-staging-collector-1 python /srv/scripts/named_bootstrap.py \
  --db /data/projects/_system/jobs.sqlite --verified-operator <admin1-operator-id>
```

Then sign in as Admin #1 over HTTPS (`https://app.yourdomain.com/login` -> **Named Admin**) and create Admin #2 in `/settings` so independent two-admin approval separation is active from day one.

---

## 2. Automated Daily WAL-Consistent SQLite Backups & Off-Host Sync

Never use raw `cp`, `rsync`, or `Copy-Item` on a live WAL-mode SQLite file. Use [`services/media-worker/scripts/sqlite_backup.py`](../services/media-worker/scripts/sqlite_backup.py), which uses Python's `sqlite3.Connection.backup()` API, validates `PRAGMA integrity_check == "ok"`, generates a `.sha256` sidecar, and prunes old backups cleanly.

### Manual Test & Verification
```bash
# 1. Create a WAL-consistent verified backup (retains 14 newest copies)
docker exec pgm-media-staging-collector-1 python /srv/scripts/sqlite_backup.py \
  --db /data/projects/_system/jobs.sqlite \
  --out-dir /data/projects/_system/backups \
  --keep 14

# 2. Verify backup integrity + SHA-256 checksum
docker exec pgm-media-staging-collector-1 python /srv/scripts/sqlite_backup.py \
  --verify /data/projects/_system/backups/<filename>.bak
```

### Linux Daily Cron Example (`/etc/cron.d/pgm-sqlite-backup`)
```cron
# Run daily at 02:15 UTC, then sync encrypted backups off-host (e.g., rclone/S3/B2)
15 2 * * * root docker exec pgm-media-staging-collector-1 python /srv/scripts/sqlite_backup.py --db /data/projects/_system/jobs.sqlite --out-dir /data/projects/_system/backups --keep 14 >> /var/log/pgm-backup.log 2>&1 && rclone sync /srv/pgm-media-prod/jobs/_system/backups remote-encrypted:pgm-prod-backups/sqlite >> /var/log/pgm-backup.log 2>&1
```

### Windows Task Scheduler Equivalent (Local Host Pilot / Staging)
```powershell
docker exec pgm-media-collector-1 python /srv/scripts/sqlite_backup.py --db /data/projects/_system/jobs.sqlite --out-dir /data/projects/_system/backups --keep 14
```

### Monthly Restore Drill
1. Download the newest off-host `.bak` and `.bak.sha256` to a temporary directory.
2. Run `python services/media-worker/scripts/sqlite_backup.py --verify /tmp/restore-test/<file>.bak`.
3. Confirm exit code `0`, `integrity_check: "ok"`, `sha256_verified: true`, and non-zero expected `table_counts` (`jobs`, `named_accounts`, `collection_approvals`, `asset_rights_decisions`).

---

## 3. Uptime & Readiness Monitoring

Use [`services/media-worker/scripts/readiness_probe.py`](../services/media-worker/scripts/readiness_probe.py) from cron, systemd timer, or an external monitor (returns exit `0` when all services report ready, `1` if any endpoint is degraded or unreachable):

```bash
# Localhost / host-network probe
python services/media-worker/scripts/readiness_probe.py \
  --worker-url http://127.0.0.1:8788/ready \
  --n8n-url http://127.0.0.1:5679/healthz/readiness \
  --dashboard-url http://127.0.0.1:3000/api/ready \
  --timeout 5

# Production edge HTTPS probe (plus internal collector/n8n checks via container network)
python services/media-worker/scripts/readiness_probe.py \
  --worker-url "" \
  --n8n-url "" \
  --dashboard-url https://app.yourdomain.com/api/ready \
  --timeout 5
```

Recommended alert thresholds:
- Alert immediately on 2 consecutive probe failures (2-minute interval).
- Alert when host disk usage on `STAGING_MEDIA_DIR` exceeds `80%`.
- Alert when daily Groq spend approaches the operator budget threshold.

---

## 4. Production Google Drive OAuth & Secret Rotation Checklist

### Separate Production Google Drive OAuth
1. Create a dedicated Google Cloud project / OAuth2 client (**Production**) — do **not** reuse the `Google Drive – Local Test` credential from localhost testing.
2. Restrict OAuth scopes strictly to Google Drive file creation/upload (`drive.file` or scoped Drive folder management as required by n8n Drive v3 nodes).
3. Configure the authorized redirect URI for your production n8n instance (`https://<n8n-admin-tunnel-or-domain>/rest/oauth2-credential/callback`).
4. Inside production n8n, bind ONLY the two active V2.1 production workflows:
   - `07-pipeline-orchestration.json` (`pgmPipeV21Orch07`)
   - `06-google-drive-delivery.json` (`pgmDriveV21Deliv`)
   - Keep `00-health-probe.json` and `05-ai-curation-probe.json` **unpublished**.
5. Confirm `GOOGLE_DRIVE_READY=1` only after verifying a real private folder write + `public_sharing: false` check.

### Secret Rotation Order (Zero Data Loss)
Generate fresh 32+ byte random tokens (`openssl rand -hex 32`) before public launch and rotate every 90 days:
1. **`MEDIA_WORKER_TOKEN`** — update `.env.prod`, recreate `collector`, and update n8n `Media Worker API Token` credential.
2. **`MEDIA_WORKER_DASHBOARD_TOKEN`** — update `.env.prod`, recreate `collector` and `dashboard`.
3. **`N8N_PIPELINE_TOKEN` & `N8N_ADMIN_WEBHOOK_TOKEN`** — update `.env.prod`, recreate `dashboard`, and update corresponding n8n Header Auth credentials (`X-Pipeline-Key`, `X-Admin-Key`).
4. **`NAMED_ASSERTION_SECRET` & `DASHBOARD_SESSION_SECRET`** — update `.env.prod`, recreate `collector` and `dashboard` (existing browser sessions cleanly re-login).
5. **`AI_API_KEY`** — provision a dedicated production Groq key with monthly spend cap, update `.env.prod`, recreate `collector`, and revoke the old key.
6. **`N8N_ENCRYPTION_KEY`** — **WARNING:** rotating `N8N_ENCRYPTION_KEY` invalidates stored n8n credentials unless you first export/re-import credentials or re-enter them in the production n8n instance. Always generate a brand-new `N8N_ENCRYPTION_KEY` when provisioning a fresh production instance, and back it up in an encrypted password manager / vault (never Git).

---

## 5. Production Launch Go / No-Go Gate

Do not onboard paying clients publicly until **all 7 gates** pass:
1. [ ] `fix/p3-live-uat-hardening` and `ops/production-launch-readiness` merged to `main` with all GitHub Actions jobs green (`frontend`, `n8n-workflows`, `offline-pytest`, `compose-validate`, `secret-scan`).
2. [ ] Public HTTPS live via `Caddyfile.prod` with valid Let's Encrypt certificate, HSTS header verified, and `/setup-admin` returning `404`.
3. [ ] Two independent Named Admins provisioned via CLI + `/settings`; self-approval confirmed blocked.
4. [ ] Automated daily `sqlite_backup.py` cron + off-host sync active, with 1 verified restore drill (`--verify` = `ok`).
5. [ ] Readiness probes active on `/api/ready`, `/ready`, and `/healthz/readiness`.
6. [ ] Production Google Drive OAuth configured and tested on 1 bounded multi-asset (photo + video) job confirming `Best Images/`, `Best Videos/`, `Reports/`, and `visibility: private`.
7. [ ] Groq Developer Tier enabled (or bounded pilot job limits communicated honestly to operators) with hard per-job cost cap configured.

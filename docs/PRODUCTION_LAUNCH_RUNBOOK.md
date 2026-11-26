# Pet Grooming Media Intelligence — Public Production Launch Runbook

This runbook covers **Milestone P4 (Public HTTPS VPS Deployment & Production Operations Hardening)** for the 100% open-source V2.1 stack (no Meta Graph API): hosting behind public HTTPS via Caddy 2, running fail-closed pre-flight security validation, scheduling daily online WAL-consistent SQLite backups with SHA-256 restore drills, monitoring service readiness & disk pressure, provisioning isolated production Google Drive OAuth, and rotating all secrets cleanly.

> [!IMPORTANT]
> Never reuse localhost pilot credentials, local `.env` secrets, or the `Google Drive – Local Test` OAuth credential in production. Keep `.env.prod` strictly outside Git with `chmod 600`.

---

## 1. Fresh Ubuntu 24.04 VPS Bootstrap (10-Minute Rollout)

1. Provision an Ubuntu 24.04 LTS VPS (`>= 2 vCPU, 4 GB RAM, 40+ GB SSD` — use `4+ GB RAM` if enabling optional Playwright Chromium fallback) and point your DNS `A` record for `app.yourdomain.com` to the VPS public IP.
2. Install Docker Engine + Docker Compose v2 plugin and clone this repository on the VPS.
3. Run the idempotent VPS bootstrap helper (preview first with `--dry-run`):
   ```bash
   sudo bash deploy/vps-bootstrap.sh --dry-run --domain app.yourdomain.com
   sudo bash deploy/vps-bootstrap.sh --domain app.yourdomain.com --alert-webhook https://your-alert-webhook.example/hook
   ```
   This configures UFW (`22/tcp`, `80/tcp`, `443/tcp` ONLY), creates `/srv/pgm-media-prod/jobs` (`chmod 750`), writes `/etc/pgm-media/ops.env` (`chmod 600`), and enables `pgm-backup.timer` (daily `02:15 UTC`) and `pgm-readiness.timer` (every 2 minutes).

---

## 2. Production Configuration & Fail-Closed Pre-Flight Check

Create host-only `.env.prod` from [`deploy/.env.prod.example`](../deploy/.env.prod.example):

```bash
cp deploy/.env.prod.example .env.prod
chmod 600 .env.prod
# Generate 8 independent 32+ byte secrets (never reuse across keys!):
for i in 1 2 3 4 5 6 7 8; do openssl rand -hex 32; done
```

Key production settings:
- **Public HTTPS & Edge Security:** `STAGING_HOST=app.yourdomain.com`, `DASHBOARD_ORIGIN=https://app.yourdomain.com`, `ACME_EMAIL=ops@yourdomain.com`, `CADDYFILE_PATH=./Caddyfile.prod` (auto-enables `__Host-` `Secure` cookies and blocks `/setup-admin*` at both Caddy and Next.js with `404`).
- **AI Throughput Preset (`AI_TIER_PRESET`):**
  - Keep `AI_TIER_PRESET=on_demand` while on Groq Free / On-Demand OTPM limits (`batch=1`, `mode_b=950`, `spacing=5s`, `cap=$0.25`).
  - Switch to `AI_TIER_PRESET=dev` once your Groq account is upgraded to Developer Tier (`batch=3`, `mode_b=3500`, `spacing=0.5s`, `max_requests=60`, `cap=$1.50`). Leave the 7 individual `AI_*` override vars blank so `AI_TIER_PRESET` controls them cleanly.
- **Optional Playwright JS Fallback:** Set both `INSTALL_PLAYWRIGHT=1` and `PLAYWRIGHT_FALLBACK_ENABLED=1` only if you want SSRF-hardened Chromium fallback for heavily JS-rendered Wix/React salon galleries.

Validate Compose + run the fail-closed pre-flight security audit BEFORE launching:

```bash
docker compose --env-file .env.prod -f deploy/compose.staging.yml -f deploy/compose.prod.yml config --quiet
docker compose --env-file .env.prod -f deploy/compose.staging.yml -f deploy/compose.prod.yml build

docker run --rm \
  -v "$PWD/.env.prod:/tmp/.env.prod:ro" \
  -v "/srv/pgm-media-prod/jobs:/srv/pgm-media-prod/jobs:rw" \
  pgm-media-prod-collector \
  python /srv/scripts/prod_preflight.py --env-file /tmp/.env.prod --check-host --max-disk-pct 80
```

Launch production containers (ONLY Caddy publishes `80` and `443`; `collector`, `dashboard` stay on internal networks; `n8n` binds strictly to `127.0.0.1:5680`):

```bash
docker compose --env-file .env.prod -f deploy/compose.staging.yml -f deploy/compose.prod.yml up -d
```

---

## 3. Production Admin Provisioning (No Browser Bootstrap)

Because `DASHBOARD_ORIGIN=https://...` AND [`deploy/Caddyfile.prod`](../deploy/Caddyfile.prod) both block `/setup-admin*` (`404`), bootstrap Admin #1 exclusively over SSH:

```bash
docker exec pgm-media-prod-collector-1 python -m app.collection_approval --migrate-db /data/projects/_system/jobs.sqlite
docker exec -it pgm-media-prod-collector-1 python /srv/scripts/named_bootstrap.py \
  --db /data/projects/_system/jobs.sqlite --verified-operator <admin1-operator-id>
```

Then sign in as Admin #1 at `https://app.yourdomain.com/login` (**Named Admin**) and create Admin #2 in `/settings` so independent two-admin approval separation is active from day one.

---

## 4. Automated Daily WAL-Consistent SQLite Backups & Restore Drills

Never use raw `cp`, `rsync`, or `Copy-Item` on a live WAL-mode SQLite database. [`services/media-worker/scripts/sqlite_backup.py`](../services/media-worker/scripts/sqlite_backup.py) uses `sqlite3.Connection.backup()`, validates `PRAGMA integrity_check == "ok"`, writes a `.sha256` sidecar, and retains the newest 14 copies.

Systemd timer [`deploy/systemd/pgm-backup.timer`](../deploy/systemd/pgm-backup.timer) runs daily at `02:15 UTC`. For off-host encrypted backups, configure `rclone` and uncomment `ExecStartPost` in [`deploy/systemd/pgm-backup.service`](../deploy/systemd/pgm-backup.service).

### Manual Backup & Monthly Restore Verification Drill
```bash
# 1. Create a WAL-consistent verified backup
docker exec pgm-media-prod-collector-1 python /srv/scripts/sqlite_backup.py \
  --db /data/projects/_system/jobs.sqlite \
  --out-dir /data/projects/_system/backups \
  --keep 14

# 2. Verify SHA-256 + SQLite PRAGMA integrity_check + table counts
docker exec pgm-media-prod-collector-1 python /srv/scripts/sqlite_backup.py \
  --verify /data/projects/_system/backups/<filename>.bak
```

Confirm exit code `0`, `integrity_check: "ok"`, `sha256_verified: true`, and expected row counts (`jobs`, `named_accounts`, `collection_approvals`, `asset_rights_decisions`).

---

## 5. Readiness, Disk-Pressure & Alert Monitoring

Systemd timer [`deploy/systemd/pgm-readiness.timer`](../deploy/systemd/pgm-readiness.timer) runs [`services/media-worker/scripts/readiness_probe.py`](../services/media-worker/scripts/readiness_probe.py) every 2 minutes across:
- Collector `/ready`
- n8n `/healthz/readiness`
- Public HTTPS Dashboard `https://app.yourdomain.com/api/ready`
- Host storage disk pressure (`/data/projects` fails closed at `>= 80%` used)
- Optional sanitized HTTPS alert webhook (`ALERT_WEBHOOK_URL` in `/etc/pgm-media/ops.env`) firing **only** when readiness or disk status degrades.

Verify live status anytime:
```bash
sudo systemctl status pgm-readiness.timer pgm-backup.timer
sudo journalctl -u pgm-readiness.service -n 20 --no-pager
```

---

## 6. Production Google Drive OAuth & Secret Rotation

### Isolated Production Google Drive OAuth (via SSH Tunnel to n8n)
1. Never expose n8n publicly. Access production n8n admin strictly via SSH port forward:
   `ssh -L 5680:127.0.0.1:5680 ops@<vps-ip>` → open `http://127.0.0.1:5680`.
2. Create a dedicated Google Cloud project / OAuth2 client (**Production**) — never reuse localhost test credentials.
3. Import and activate ONLY the two V2.1 production workflows:
   - `n8n-media/07-pipeline-orchestration.json` (`pgmPipeV21Orch07`)
   - `n8n-media/06-google-drive-delivery.json` (`pgmDriveV21Deliv`)
   - Keep `00-health-probe.json` and `05-ai-curation-probe.json` **unpublished**.
4. Flip `GOOGLE_DRIVE_READY=1` in `.env.prod` and recreate `dashboard` only after verifying 1 real private folder write (`public_sharing: false`).

### 90-Day Secret Rotation Order (Zero Data Loss)
Generate fresh 32+ byte random tokens (`openssl rand -hex 32`):
1. `MEDIA_WORKER_TOKEN` → update `.env.prod`, recreate `collector`, update n8n `Media Worker API Token` credential.
2. `MEDIA_WORKER_DASHBOARD_TOKEN` → update `.env.prod`, recreate `collector` and `dashboard`.
3. `N8N_PIPELINE_TOKEN` & `N8N_ADMIN_WEBHOOK_TOKEN` → update `.env.prod`, recreate `dashboard`, update n8n Header Auth credentials (`X-Pipeline-Key`, `X-Admin-Key`).
4. `NAMED_ASSERTION_SECRET` & `DASHBOARD_SESSION_SECRET` → update `.env.prod`, recreate `collector` and `dashboard` (existing browser sessions re-login cleanly).
5. `AI_API_KEY` → provision new Groq production key with monthly spend cap, update `.env.prod`, recreate `collector`, revoke old key.
6. `N8N_ENCRYPTION_KEY` → generate brand new per fresh instance and back up in an encrypted password manager / vault (rotating an existing key invalidates stored n8n credentials unless re-entered).

---

## 7. Production Launch Go / No-Go Gate

Do not onboard paying clients publicly until **all 7 gates** pass:
1. [ ] All GitHub Actions CI checks (`frontend`, `n8n-workflows`, `offline-pytest`, `compose-validate`, `secret-scan`) are green on `main` including P1–P4.
2. [ ] `prod_preflight.py --env-file .env.prod --check-host` exits `0` (`ok: true`, zero errors).
3. [ ] Public HTTPS is live via `deploy/compose.prod.yml` + `deploy/Caddyfile.prod` with valid Let's Encrypt cert, HSTS verified, and `/setup-admin` returning `404`.
4. [ ] Two independent Named Admins are provisioned via SSH CLI + `/settings`; self-approval confirmed blocked.
5. [ ] `pgm-backup.timer` + off-host `rclone` sync active with 1 verified restore drill (`sqlite_backup.py --verify` = `ok`).
6. [ ] `pgm-readiness.timer` active with `/api/ready`, `/ready`, `/healthz/readiness`, and disk threshold `< 80%` reporting `overall: "ok"`.
7. [ ] Isolated production Google Drive OAuth verified on 1 combined photo + video job confirming `Best Images/`, `Best Videos/`, `Reports/`, `visibility: private`, and Groq tier (`AI_TIER_PRESET=dev` or bounded `on_demand`) clearly configured.

#!/usr/bin/env bash
# Atelier Media — Idempotent Ubuntu 24.04 VPS Production Bootstrap
#
# Prepares a fresh hardened Linux VPS for public HTTPS hosting:
#   1. Verifies Docker Engine + Compose v2 plugin are available.
#   2. Configures UFW firewall (SSH 22, HTTP 80, HTTPS 443 ONLY; denies all other inbound).
#   3. Creates isolated storage (/srv/pgm-media-prod/jobs) with restricted permissions.
#   4. Installs systemd timers for daily online WAL SQLite backups and 2-minute readiness + disk probes.
#
# Usage:
#   sudo bash deploy/vps-bootstrap.sh --dry-run
#   sudo bash deploy/vps-bootstrap.sh --domain app.yourdomain.com [--alert-webhook https://...]

set -euo pipefail

DRY_RUN=0
DOMAIN=""
MEDIA_DIR="/srv/pgm-media-prod/jobs"
ALERT_WEBHOOK=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --domain)
      DOMAIN="${2:-}"
      shift 2
      ;;
    --media-dir)
      MEDIA_DIR="${2:-}"
      shift 2
      ;;
    --alert-webhook)
      ALERT_WEBHOOK="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== 1. Checking Docker & Compose v2 ==="
if [[ "$DRY_RUN" -eq 0 ]]; then
  command -v docker >/dev/null 2>&1 || { echo "Docker Engine not installed. Install Docker first." >&2; exit 1; }
  docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 plugin not installed." >&2; exit 1; }
else
  echo "[dry-run] verify docker and docker compose v2"
fi

echo "=== 2. Hardening UFW Firewall (22/tcp, 80/tcp, 443/tcp only) ==="
if [[ "$DRY_RUN" -eq 1 ]] || command -v ufw >/dev/null 2>&1; then
  run_cmd ufw default deny incoming
  run_cmd ufw default allow outgoing
  run_cmd ufw allow 22/tcp comment "SSH admin"
  run_cmd ufw allow 80/tcp comment "Caddy HTTP ACME + redirect"
  run_cmd ufw allow 443/tcp comment "Caddy Public HTTPS"
  run_cmd ufw --force enable
else
  echo "WARNING: ufw not found; configure your cloud firewall to allow ONLY TCP 22, 80, 443."
fi

echo "=== 3. Creating Isolated Production Media & Systemd Ops Directories ==="
run_cmd install -d -m 0750 "${MEDIA_DIR}" "${MEDIA_DIR}/_system" "${MEDIA_DIR}/_system/backups"
run_cmd install -d -m 0750 /etc/pgm-media

OPS_ENV="/etc/pgm-media/ops.env"
PUBLIC_READY="http://dashboard:3000/api/ready"
if [[ -n "$DOMAIN" ]]; then
  PUBLIC_READY="https://${DOMAIN}/api/ready"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] write ${OPS_ENV} (chmod 600) with PUBLIC_READY_URL=${PUBLIC_READY}"
else
  cat > "${OPS_ENV}" <<EOF
PUBLIC_READY_URL=${PUBLIC_READY}
MAX_DISK_PCT=80
ALERT_WEBHOOK_URL=${ALERT_WEBHOOK}
EOF
  chmod 600 "${OPS_ENV}"
fi

echo "=== 4. Installing Daily WAL Backup & 2-Minute Readiness Systemd Timers ==="
for unit in pgm-backup.service pgm-backup.timer pgm-readiness.service pgm-readiness.timer; do
  run_cmd install -m 0644 "${SCRIPT_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
done
run_cmd systemctl daemon-reload
run_cmd systemctl enable --now pgm-backup.timer pgm-readiness.timer

echo "=== Bootstrap Complete ==="
echo "Next steps:"
echo "  1. Copy deploy/.env.prod.example -> .env.prod && chmod 600 .env.prod (fill independent 32+ byte secrets)."
echo "  2. Validate: docker compose --env-file .env.prod -f deploy/compose.staging.yml -f deploy/compose.prod.yml config --quiet"
echo "  3. Build & preflight: docker compose --env-file .env.prod -f deploy/compose.staging.yml -f deploy/compose.prod.yml build"
echo "     docker run --rm -v \"\$PWD/.env.prod:/tmp/.env.prod:ro\" -v \"${MEDIA_DIR}:${MEDIA_DIR}:rw\" pgm-media-prod-collector python /srv/scripts/prod_preflight.py --env-file /tmp/.env.prod --check-host"
echo "  4. Start: docker compose --env-file .env.prod -f deploy/compose.staging.yml -f deploy/compose.prod.yml up -d"
echo "  5. Bootstrap Admin #1 over SSH: docker exec -it pgm-media-prod-collector-1 python /srv/scripts/named_bootstrap.py --db /data/projects/_system/jobs.sqlite --verified-operator <admin1-id>"

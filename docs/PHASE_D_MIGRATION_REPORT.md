# Phase D — Docker Storage Migration Report

**Date:** 20 November 2026
**Method:** Docker Desktop supported GUI relocation (per approval).
**Status:** IN PROGRESS — backup copy + checksum running.

---

## 1. Pre-migration state (recorded before any change)

- Health proof (all green before shutdown): worker `/health` `{"ok":true}`; pgm n8n `/healthz` `{"status":"ok"}`; **legacy n8n `/healthz` `{"status":"ok"}`**; probe webhook HTTP 200 (`auth: verified`, sentinel readable).
- Full inventory saved to `D:\DockerBackup\pre-state.txt`: 9 containers (3 running healthy), 4 named volumes, 12 images, `settings-store.json` contents, free-space readings.
- Old disk image location (default, no override in settings): `C:\Users\CZ\AppData\Local\Docker\wsl\disk\docker_data.vhdx` — 18.86 GB.
- Free space before: C: 9.87 GB / D: 114.40 GB.

## 2. Workspace preservation (D1.2)

- Full workspace backed up to `D:\DockerBackup\workspace\` (2.15 MB; verified to include `.env` — the pgm n8n encryption key + worker token — and `reference\source\pet-gromming-agent-factory-main (1).zip`).
- Note: the workspace backup copy contains `.env` secrets — keep `D:\DockerBackup` private and delete it when the retention window ends.

## 3. Graceful stop (D1.3)

- `docker stop` issued for the 3 running containers (they exited 137 after the SIGTERM grace period — identical to this machine's legacy history; SQLite WAL recovery expected, integrity re-verified below).
- Docker Desktop UI + backend processes terminated; `wsl --shutdown` executed; `wsl -l -v` shows `docker-desktop` **Stopped**; **exclusive-open lock test on the VHDX passed ("UNLOCKED — safe to copy")** before any file operation.

## 4. VHDX backup (D1.4–D1.5) — COMPLETE, VERIFIED

- Copy: `C:\Users\CZ\AppData\Local\Docker\wsl\disk\docker_data.vhdx` → `D:\DockerBackup\docker_data.vhdx` (18:49:21 → 18:52:51, robocopy exit 1 = copied OK).
- Size match: **True** (20,227,031,040 bytes both).
- SHA-256: source `2141CE43C83D642E061EBAA29CF168A3B3C70DA078F42FC1648140F9898A8FF5`; backup identical → **CHECKSUM MATCH: True**.
- D: free after backup: 94.72 GB.
- Backup retained until ≥48 h stable + your separate approval (per safeguard).

## 5. Migration (D2) — restart complete; GUI relocation handed to operator

- Docker Desktop restarted; engine UP.
- Note discovered: containers stopped manually via `docker stop` are **not** auto-restarted by `restart: unless-stopped` (correct Docker semantics) — all three were started explicitly and returned healthy:
  - `pgm-media-collector-1` healthy (127.0.0.1:8788), `pgm-media-n8n-1` healthy (127.0.0.1:5679), `website-builder-testing-main-n8n-1` healthy (127.0.0.1:5678).
- Backup-source health re-verified before the irreversible step: worker `/health` `{"ok":true}`; pgm `/healthz` `{"status":"ok"}`; legacy `/healthz` `{"status":"ok"}`; probe HTTP 200 `auth: verified` + sentinel OK. This also proves SQLite integrity after the graceful-stop SIGKILLs.
- Docker Desktop CLI has no settings setter (verified via `docker desktop --help`: only start/stop/restart/status/diagnose/…), so the **supported GUI relocation is performed by the operator**: Settings → Resources → Advanced → Disk image location → `D:\DockerData` → Apply & Restart. No manual VHDX moves performed.

## 5. Migration (D2) — COMPLETE

- Docker Desktop restarted; engine UP (29.9.2).
- Note discovered: containers stopped manually via `docker stop` are **not** auto-restarted by `restart: unless-stopped` (correct Docker semantics) — all three were started explicitly and returned healthy before the relocation.
- Docker Desktop CLI has no settings setter (verified via `docker desktop --help`), so the supported GUI relocation was performed by the operator.
- **Result:** disk image relocated to `D:\DockerDesktopWSL` (operator's chosen folder name on D:; equivalent to the proposed `D:\DockerData`). `settings-store.json` now records `"CustomWslDistroDir": "D:\\DockerDesktopWSL"`. Both VHDX files moved: `D:\DockerDesktopWSL\disk\docker_data.vhdx` (18.84 GB) + `D:\DockerDesktopWSL\main\ext4.vhdx` (0.09 GB). The old C: VHDX was removed by Docker Desktop after the successful move. No manual VHDX operations were performed at any step.

## 6. Post-migration verification (D3) — ALL PASS

| Check | Result |
|---|---|
| Docker disk image location on D: | `CustomWslDistroDir = D:\DockerDesktopWSL`; VHDX present on D:; old C: file gone |
| Engine | server 29.9.2; WSL `docker-desktop` Running |
| All 3 containers healthy | `pgm-media-collector-1`, `pgm-media-n8n-1`, `website-builder-testing-main-n8n-1` — all Up (healthy); auto-restarted by their policies after the migration engine restart |
| All 4 named volumes intact | `pgm-media_n8n_media_data`, `website-builder-testing-main_n8n_data`, `pet-gromming-agent-factory-main_n8n_data`, `website-builder-testing-main_worker_state` |
| Legacy n8n port | 127.0.0.1:5678 → `/healthz` `{"status":"ok"}` |
| New n8n port | 127.0.0.1:5679 → `/healthz` `{"status":"ok"}` |
| Worker port | 127.0.0.1:8788 → `/health` `{"ok":true}` |
| n8n state | published workflow still registered (`00 - Phase 1 Health Probe`); credential intact |
| Authenticated probe | **HTTP 200**: `worker_status.auth = "verified"`, `token_configured: true` |
| Sentinel | `cat /data/media/sentinel.txt` OK inside n8n; probe `shared_mount_sentinel` OK |
| Backup preserved | `D:\DockerBackup\docker_data.vhdx` (18.84 GB) untouched; retained until ≥48 h stable + separate approval |
| C: free | **30.08 GB** (was 9.87 GB pre-migration → ≈ +20.2 GB) |
| D: free | **75.79 GB** (consumed: migrated VHDX 18.84 + backup 18.84) |

## 7. Failures & recovery actions

1. **Probe webhook 404 after migration** (`Cannot GET /webhook/phase1-probe`): the published workflow's webhook registration did not re-apply automatically across the engine-restart sequence. **Recovery (executed):** re-applied `n8n publish:workflow --id=mN20QUV6UGZyOX0T`; webhook re-registered; probe then returned HTTP 200 with full evidence. No data loss; no other workflow affected.
   - Operational note for future restarts: if a published webhook 404s after a manual engine restart, re-run the publish command for that workflow.
2. **Containers did not auto-start after the D2.1 manual stop** (expected Docker `unless-stopped` semantics after `docker stop`): started explicitly, re-verified healthy before the irreversible step. Not a failure of the migration itself.

## 8. Retention & cleanup status

- Backup `D:\DockerBackup\docker_data.vhdx` + workspace copy `D:\DockerBackup\workspace\` + `pre-state.txt` — **preserved**; deletion only after ≥48 h stable operation and your separate approval.
- No containers, volumes, or images were deleted or pruned at any point. No legacy environment was modified beyond being stopped/restarted alongside the migration (state preserved and verified healthy).

## 9. Phase 2 readiness

Storage risk resolved: Docker now grows on D: (75.79 GB free). The stack is fully verified post-migration (all D3 checks green, one recovered webhook registration). **Remaining before Phase 2 scraping:** your approval of the D4 media-storage plan (§10 below) — bind `storage\jobs` → `D:\pgm-media-storage\jobs` via an explicit Compose bind mount — since Phase 2 downloads must not grow on C:. Phase 2 (Website Media Collector + real manifest) is otherwise ready to begin on your instruction.

---

## 10. D4 — Media storage relocation — EXECUTED AND VERIFIED (approved separately)

**Target:** `D:\pgm-media-storage\jobs`, via an **explicit Compose bind mount** (no junction).

**Executed steps (each as approved):**
1. **Pre-flight/inspection:** recorded `storage\jobs` contents (sentinel 311 B + `_system` job DB 12 KB with WAL/SHM) and live container mounts.
2. **Backup first:** copied all existing media/job files to `D:\DockerBackup\media-jobs-backup\` (4 files) — untouched since.
3. **Target created:** `D:\pgm-media-storage\jobs`.
4. **Copy without deletion:** sentinel + job DB copied to the target; C: originals retained intact.
5. **Compose change (2 lines + comment):** `./storage/jobs` → `D:/pgm-media-storage/jobs` for both services; `N8N_RESTRICT_FILE_ACCESS_TO=/data/media` unchanged; worker RW `/data/projects`, n8n RO `/data/media` unchanged.
6-8. **Recreated only the pgm-media containers** (config validation exit 0; both healthy); **legacy n8n untouched** (Up and healthy throughout, never restarted).

**Verification results:**
| Check | Result |
|---|---|
| New live mounts | worker: `D:/pgm-media-storage/jobs -> /data/projects (rw=true)`; n8n: `/run/desktop/mnt/host/d/pgm-media-storage/jobs -> /data/media (rw=false)` |
| Sentinel from n8n via new mount | `cat /data/media/sentinel.txt` OK |
| Worker `/health` (8788) | `{"ok":true}` |
| pgm n8n `/healthz` (5679) | `{"status":"ok"}` |
| Authenticated probe | **HTTP 200** — `auth: verified`, `token_configured: true`, sentinel read OK (webhook re-registered automatically after this restart; no manual re-publish needed) |
| Job persistence / data continuity | old job `22fef997…` (created on C:) readable from the relocated DB (HTTP 200) |
| Actual D: file writes | new job `26c48d32…` POSTed → `GET /jobs/{id}` HTTP 200; D: `jobs.sqlite-wal` modified at the job's exact creation time (7:33:51 PM, 20,656 bytes) — main-file mtime deferred by SQLite WAL checkpointing, as expected |
| C: untouched since switch | C: `jobs.sqlite` mtime unchanged (7:16:33 PM); only the old sentinel + DB remain, retained intact |
| Old C: media folder | present, retained — deletion only on your explicit approval |
| Docker backups | `D:\DockerBackup\docker_data.vhdx` + `media-jobs-backup\` intact |
| Disk space | C: 29.96 GB free / D: 75.79 GB free |

**Original plan retained below for reference; it was executed as written.**

**Target:** move downloaded media off C: to `D:\pgm-media-storage\jobs`, using an **explicit Compose bind mount** (per your preference — no Windows junction).

**Proposed steps (each reversible; nothing deleted until verified):**
1. **Pre-flight:** record current `storage\jobs` contents (currently just the sentinel, KBs). Create `D:\pgm-media-storage\jobs`.
2. **Backup:** copy existing `storage\jobs\*` → `D:\pgm-media-storage\jobs\` (tiny today; will grow in Phase 2 — this becomes the permanent location).
3. **Compose change (one line × 2 services):** in `compose.media.yml`, replace both `./storage/jobs` bind sources with `D:/pgm-media-storage/jobs` (worker `/data/projects:rw`; n8n `/data/media:ro`). Keep everything else identical. Compose accepts Windows absolute paths on any drive.
4. **Recreate + verify:** `docker compose -f compose.media.yml up -d` (recreates both containers with the new mount) → verify `docker compose ps` healthy; sentinel readable from inside n8n (`/data/media/sentinel.txt`); probe webhook HTTP 200 with `shared_mount_sentinel`; run one `POST /jobs` → confirm the job DB is written to the **new** location (worker `_system/jobs.sqlite` appears under `D:\pgm-media-storage\jobs\_system\`).
5. **Only after verification passes:** the old C: `storage\jobs` folder is retained (unused) until your explicit approval to delete it.
6. **.gitignore note:** the git-ignored `storage/jobs/*/_system/` rule becomes moot for the D: copy; nothing else changes in the workspace.

**Downtime:** ~1–2 minutes (one compose recreate).
**Rollback:** revert the two lines in `compose.media.yml`, `up -d`, verify sentinel — the old C: folder is untouched until step 5.

---

**Phase D complete. Stopping here per instruction — Phase 2 not started.**

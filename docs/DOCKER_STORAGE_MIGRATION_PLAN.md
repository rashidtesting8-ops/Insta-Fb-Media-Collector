# Docker Storage Migration Assessment & Plan (read-only; awaits approval)

**Date:** 20 November 2026
**Mode:** Read-only assessment. No Docker stop, no file moves, no prunes, no config changes were performed.
**Goal:** Relocate Docker Desktop's disk image from C: (critical space) to D: (114 GB free) with zero loss to both n8n environments (legacy `website-builder-testing-main-n8n-1` and new `pgm-media-n8n-1`).

---

## 1. Current state (inspected)

### Disk image location
- Docker Desktop 4.98, engine 29.9.2, **WSL2 backend**, single `docker-desktop` distro, WSL 3.0.5.
- `settings-store.json` contains **no explicit disk-image location** → Docker uses the default:
  - `C:\Users\CZ\AppData\Local\Docker\wsl\disk\docker_data.vhdx` — **18.86 GB** (all images, containers, named volumes)
  - `C:\Users\CZ\AppData\Local\Docker\wsl\main\ext4.vhdx` — 0.10 GB (WSL system; stays on C:, negligible)
- Free space: **C: 9.87 GB / 121 GB**, **D: 114.40 GB / 117 GB**.

### Containers (3 running, 6 exited legacy)
| Container | Image | Status | Notes |
|---|---|---|---|
| `pgm-media-n8n-1` | n8nio/n8n:2.48.7 | Up, healthy | port 127.0.0.1:5679 |
| `pgm-media-collector-1` | pgm-media-collector | Up, healthy | port 127.0.0.1:8788 |
| `website-builder-testing-main-n8n-1` | n8nio/n8n:2.47.3 | Up, healthy | **live legacy**, port 127.0.0.1:5678 — preserve |
| 6 × legacy collector/cockpit/n8n | mixed | Exited | untouched |

### Named volumes (all inside docker_data.vhdx; all tiny)
| Volume | Size | Content |
|---|---|---|
| `pgm-media_n8n_media_data` | 6.5 MB | new n8n state: owner account, workflow, worker-token credential |
| `website-builder-testing-main_n8n_data` | 7.3 MB | **live legacy n8n state** — preserve |
| `pet-gromming-agent-factory-main_n8n_data` | 6.4 MB | dormant legacy n8n state |
| `website-builder-testing-main_worker_state` | 236 KB | legacy worker state |

### Bind mounts (host folders — NOT inside the VHDX; unaffected by the move)
- `pgm-media-collector-1`: `C:\...\Insta and Fb Scrpaer Intellegence\storage\jobs` → `/data/projects` (rw)
- `pgm-media-n8n-1`: same folder → `/data/media` (ro)
- The live legacy n8n container currently has **no bind mounts** (volume only) → it is fully self-contained in the VHDX.
- ⚠️ `storage\jobs` currently holds only the sentinel (KBs) but **will grow with downloaded media in Phase 2** — a separate placement decision (junction to D: or workspace relocation) is recommended, see §5.

### Images (12; the VHDX holds them all)
Relevant: `n8nio/n8n:2.48.7` (2.18 GB), `n8nio/n8n:2.47.3` (1.94 GB, used by live legacy), `pgm-media-collector` (876 MB), legacy builder images (3.86–3.91 GB ×4), `n8nio/n8n:1.76.1` (1.28 GB), `n8nio/n8n:latest` (1.94 GB), `docker.n8n.io/n8nio/n8n:stable` (2.18 GB), `instaandfbscrpaerintellegence-collector` (221 MB), `alpine` (~8 MB — pulled by me during this assessment for read-only volume measurement; disclosed).

### Backup requirements
1. **Primary:** the single `docker_data.vhdx` file (18.86 GB) — contains both n8n instances' full state.
2. **Host files:** the workspace (especially `.env` with `N8N_ENCRYPTION_KEY` + `MEDIA_WORKER_TOKEN` — required to decrypt the pgm n8n credential; never committed), `reference/source/` ZIP, legacy project folders referenced by exited legacy containers.
3. The legacy n8n's encryption key is auto-generated and stored **inside** its volume → moves with the VHDX automatically.

### n8n owner account (verification requested)
- n8n log: **"Owner was set up successfully"**.
- `GET http://127.0.0.1:5679/` → **HTTP 200**, no `/setup` redirect (instance initialized).
- Probe webhook after setup → **HTTP 200**, `worker_status.auth = "verified"`, sentinel read OK.
- **Conclusion: owner account is operational.**

---

## 2. Recommended procedure (supported path)

**Option A — Docker Desktop GUI move (recommended, officially supported).**
Docker Desktop → Settings → Resources → Advanced → **Disk image location** → `D:\DockerData` → **Apply & Restart**. Docker Desktop itself relocates `docker_data.vhdx` and re-registers the WSL disk. No export/import, no data transformation; volumes move as-is.

**Option B — WSL-native move (fallback, works with WSL 3.0.5).**
`wsl --shutdown` then `wsl --manage docker-desktop --move D:\WSL\docker-desktop`. Viable, but less aligned with Docker Desktop's own management of the disk; use only if the GUI path fails.

**Option C — manual file move.** Not recommended (unsupported, registration/lock risk).

---

## 3. Exact execution plan (Phase D — pending your approval)

**D0 — Pre-flight (no downtime; mostly already done in this assessment).**
Confirm `D:\DockerData` target and `D:\DockerBackup` folder; record health snapshot: `docker ps`, `docker volume ls`, probe webhook 200, legacy n8n healthy on 5678. (Done — captured above.)

**D1 — Single downtime window begins.** Quit Docker Desktop (all containers stop; `restart: unless-stopped` will bring them back on start).

**D2 — Safety backup.** Copy `C:\Users\CZ\AppData\Local\Docker\wsl\disk\docker_data.vhdx` → `D:\DockerBackup\docker_data.vhdx` and verify size matches (≈10–15 min for 18.86 GB).

**D3 — Restart + verify the backup source was healthy.** Start Docker Desktop; confirm the 3 containers return healthy and probe = 200. (Optional but cheap insurance before the irreversible step; +3–5 min downtime.)

**D4 — Move.** Settings → Resources → Advanced → Disk image location → `D:\DockerData` → Apply & Restart (≈10–25 min). Docker moves the VHDX and restarts itself.

**D5 — Post-migration health checks (exact):**
```
docker version
docker volume ls            # expect the same 4 volumes
docker ps -a                # 3 running + healthy (pgm ×2 + legacy n8n)
Invoke-WebRequest http://127.0.0.1:8788/health          # {"ok":true}
Invoke-WebRequest http://127.0.0.1:5679/healthz         # {"status":"ok"}
Invoke-WebRequest http://127.0.0.1:5678/healthz         # legacy n8n ok
Invoke-WebRequest http://127.0.0.1:5679/webhook/phase1-probe   # 200 + auth verified + sentinel
```
Plus: you log into n8n at `http://127.0.0.1:5679` (manual confirmation), and C: free space should gain ≈18.9 GB (→ ~28.8 GB).

**D6 — Cleanup (only after 24–48 h stable).** Delete `D:\DockerBackup\docker_data.vhdx` (or keep longer if you prefer). Optional separate approval: prune stale images (`1.76.1`, `latest`, `docker.n8n.io/n8n:stable`, `instaandfbscrpaerintellegence-collector`, duplicate legacy builder pairs — ≈11–12 GB; after migration these live on D:, so this becomes optional housekeeping rather than urgent).

---

## 4. Downtime, risks, rollback

- **Expected downtime:** one window, **~25–45 min** (backup 10–15 + move 10–25 + restarts/health 3–5). Both n8n instances are local dev; no external users affected. The legacy n8n on 5678 is down only during the same window.
- **Data-loss risk: very low.** All n8n state (both instances) is inside the one VHDX and moves as a file; the pre-move backup copy plus auto-restart policies cover the failure modes.
- **Rollback steps (if anything fails):**
  1. GUI move fails midway → the original C: VHDX normally remains in place; restart Docker Desktop, verify health, retry later.
  2. Docker broken after the move → Quit Docker Desktop; copy `D:\DockerBackup\docker_data.vhdx` back to `C:\Users\CZ\AppData\Local\Docker\wsl\disk\docker_data.vhdx`; start Docker; re-run the D5 checks (this restores the exact pre-migration state of BOTH n8n instances).
  3. pgm n8n credential issue (not expected — its encryption key is in `.env`, which does not move) → the credential re-import procedure is already proven and documented in the Phase 1 report.
- **Preservation guarantee:** nothing is deleted at any step; the original file is only removed by Docker Desktop after a successful move, and the backup copy is retained until your explicit sign-off.

---

## 5. Follow-up recommendation (separate decision, not part of the move)

After the move, Docker growth lands on D:, but **media downloads bind-mount to `storage\jobs` on C:** and will grow fast in Phase 2. Options:
- **(a)** Junction: `storage\jobs` → `D:\pgm-media-storage\jobs` (`mklink /J`), then `docker compose up -d --force-recreate` for both services, verify via the probe sentinel. Zero code changes.
- **(b)** Relocate the whole workspace to D: later (compose uses relative paths, so it moves cleanly — but the OpenCode session root and any tooling would need repointing, as we saw in today's round-trip).

Recommend deciding (a) vs (b) together with Phase 2 start; not required for the VHDX move itself.

---

## 6. Assessment side-effect disclosure

- `alpine:latest` (~8 MB) was pulled during read-only volume size measurement (no other images/volumes/containers were touched).
- No Docker stop/start, no file moves, no configuration changes were performed in this assessment.

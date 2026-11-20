# Phase 1 Report — Local Infrastructure (M1)

**Date:** 20 November 2026
**Scope executed:** controlled bootstrap, safe project structure, Docker Compose (n8n + worker), local n8n service, lightweight worker skeleton, shared media storage, `/health` + authenticated `/status`, internal Docker networking, environment templates, connectivity checks, persistent n8n volume, minimal documentation.
**Explicitly NOT done (per approval):** frontend UI, real social scraping, AI selection, Google Drive uploads, AI/Drive credentials, touching any legacy environment.

---

## 1. Pre-execution inspection (safe, read-only)

| Item | Finding |
|---|---|
| RAM | 7.9 GB total (0.5 GB free at inspection) — constrained |
| CPU | Intel i5-4300U, 2 cores / 4 threads — constrained |
| OS | Windows 10 Pro build 19045 |
| Disk | C: 121 GB total, **7.0 GB free** (critical); D: 117 GB free (unused) |
| WSL2 | Default distro `docker-desktop`, version 2, Running |
| Docker | Docker Desktop 4.98.0, engine 29.9.2, Compose v5.6.1 — running |
| Port 5678 | **Occupied** by live healthy legacy n8n (`website-builder-testing-main-n8n-1`, n8n 2.47.3) — must not touch |
| Port 8787 | **Occupied** by a local `python` process (pid 18584) — must not touch |
| Existing volumes | `website-builder-testing-main_n8n_data`, `pet-gromming-agent-factory-main_n8n_data` — untouched by design |
| Legacy containers | 6 containers across two legacy compose projects — untouched |

Consequent safety decisions: host ports **5679** (n8n) and **8788** (worker) instead of 5678/8787; distinct compose project name `pgm-media`; new volume `pgm-media_n8n_media_data`; all host binds on `127.0.0.1` only.

## 2. n8n version resolution (user decision #2)

- GitHub latest stable release (non-prerelease): **n8n@2.48.7**, published 2026-11-20.
- Docker tag verified: `n8nio/n8n:2.48.7` (amd64 digest `sha256:8c66e93c3f69…`; index digest `sha256:fabce3e9678a…`), ~378 MB compressed.
- **Deviation recorded (DECISIONS.md #12):** `docker.n8n.io` is DNS-unresolvable on this network (`no such host`), so the identical official image was pulled from Docker Hub as `n8nio/n8n:2.48.7`. Version pin unchanged. Old `1.76.1` not used.

## 3. Bootstrap evidence

- Pristine ZIP copied to `reference/source/pet-gromming-agent-factory-main (1).zip`; SHA-256 verified identical to the original (never modified).
- Extracted to `reference/legacy-extracted/pet-gromming-agent-factory-main/` — all 163 files present; 161 existing entries verified byte-identical to the ZIP, 1 missing entry completed, 0 mismatches.
- Extracted legacy `AGENTS.md` → `LEGACY_AGENTS.md` (extracted copy only).
- Starter-kit docs materialized at `docs/` (PROJECT_SPEC.md, AI_VISUAL_DIRECTOR_V2.md, AI_VISUAL_DIRECTOR_ORIGINAL.txt, Developer_Guide_V2.pdf) plus root `AGENTS.md`, `README_FIRST.md`.
- During bootstrap I discovered the user had already prepared `AGENTS.md`, `.env` (all 5 required keys), `.env.example`, `.gitignore`, `compose.media.yml`, `DECISIONS.md` and a partial extraction. I verified my starter-kit copies were byte-identical to those files (no content lost), completed their partial extraction non-destructively, and **kept their compose design**, adding only: explicit `name: pgm-media`, the n8n `MEDIA_WORKER_TOKEN` pass-through (later removed again in favour of a credential — #14), and a mem-limit correction (#13).

## 4. Files created or modified

**Modified (pre-existing, user-created or mine — surgical edits only):**
- `compose.media.yml` — added `name: pgm-media`; n8n `mem_limit` 512m→1g, `mem_reservation` 256m→384m; token-env line added then replaced with a NOTE comment (credential pattern instead).
- `.env` — single non-secret line: `N8N_IMAGE` → `n8nio/n8n:2.48.7` (values never displayed; secrets untouched).
- `.env.example` — image line + explanatory comment.
- `DECISIONS.md` — appended decisions #11–#18.
- `services/media-worker/app/service.py` (user-authored, adopted) — container entrypoint; surgical addition of the token-protected `GET /status` (constant-time compare, fails closed; pattern from legacy `src/engine/auth.py`) plus its docstring entry.
- `services/media-worker/Dockerfile` — CMD switched from the agent's interim `app.api:app` to the user's `app.service:app`.
- `docs/PHASE_1_REPORT.md` (this file), `README.md` (quickstart + endpoint list).

**Created (new files only):**
- `services/media-worker/Dockerfile` — python:3.12-slim, ffmpeg + ca-certificates, pinned pip deps, non-root `appuser`, healthcheck (pattern from legacy Dockerfile).
- `services/media-worker/requirements.txt` — fastapi/uvicorn/pydantic + gallery-dl 1.30.0, yt-dlp, instaloader.
- `services/media-worker/app/__init__.py`, `app/api.py` — interim skeleton (api.py later removed after adopting the user's `service.py`; the agent's own file).
- `n8n-media/00-health-probe.json` — temporary probe workflow (webhook → worker health → credential-auth worker status → Read/Write Files sentinel → evidence response).
- `storage/jobs/sentinel.txt` — shared-mount sentinel.
- `docs/AUDIT_PHASE_0.md` (Phase 0 deliverable, unchanged this phase).

**Adopted unchanged (user-authored, appeared mid-session):**
- `services/media-worker/app/service.py` (job API skeleton), `app/collector.py` + `app/social_collect.py` (stdlib-only stubs), `tests/test_phase1_smoke.py` (offline smoke tests), `n8n-media/README.md` (placeholder doc). Verified intact after the workspace relocation round-trip (see §8.6).

**Removed:**
- `services/media-worker/app/api.py` — the agent's own interim file from this session, superseded by the adopted `service.py` (which now also carries `/status`).

**Untouched:** original ZIP in Downloads, both `reference/` copies of legacy content beyond completion, all legacy containers/volumes/images/networks, the local python process on 8787, the live legacy n8n on 5678.

## 5. Containers configured and started

```
pgm-media-collector-1  pgm-media-collector  Up (healthy)  127.0.0.1:8788->8787/tcp   CMD: uvicorn app.service:app
pgm-media-n8n-1        n8nio/n8n:2.48.7     Up (healthy)  127.0.0.1:5679->5678/tcp
```

- Worker image: `pgm-media-collector:latest`, ~876 MB, non-root `appuser`, Python 3.12.16, **runs the adopted user-authored `app/service.py`**.
- n8n 2.48.7 persistent state: named volume `pgm-media_n8n_media_data` (new; no legacy volume reused or modified). Persistence proven: imported credential + workflow survived multiple container recreations and a full Docker Desktop restart.
- Shared storage: host `./storage/jobs` → worker `/data/projects` (rw) and n8n `/data/media` (ro).
- n8n file access restriction: `N8N_RESTRICT_FILE_ACCESS_TO=/data/media` (verified inside container).
- Internal network: `pgm-media_media-net` (bridge).
- Legacy environments untouched; the legacy n8n instance on 5678 remained healthy throughout (via its own restart policies).

## 6. Actual health-check and connectivity results (final image)

| # | Test | Result |
|---|---|---|
| 1 | `docker compose -f compose.media.yml config` | exit 0; correct model |
| 2 | Worker image build (final, adopted skeleton) | built; healthy |
| 3 | Both containers healthy | yes (`docker compose ps`) |
| 4 | Host → worker `http://127.0.0.1:8788/health` | `{"ok":true}` |
| 5 | Host → n8n `http://127.0.0.1:5679/healthz` | `{"status":"ok"}` |
| 6 | `POST /jobs` (spec §8 shape) | `{"job_id":"22fef997b7ef…","status":"queued"}` — immediate response |
| 7 | `GET /jobs/{job_id}` | durable record; per-source statuses (`website: queued`, `instagram/facebook: not_requested`); advanced to `awaiting_extraction` |
| 8 | `GET /jobs/{job_id}/manifest` | stub manifest `{"assets":[],"note":"Phase 1 stub…"}` |
| 9 | Negative: `/status` without token | **HTTP 401 Unauthorized** (fail-closed verified) |
| 10 | User's offline smoke tests, run inside the container | **PASS × 3** (`test_collector_stub_returns_queued`, `test_social_stub_queued_when_url_given`, `test_social_stub_requires_url`) |
| 11 | Worker tools | `gallery-dl 1.30.0`, `yt-dlp 2026.10.26`, `instaloader 4.16`, `ffmpeg 7.1.5`, non-root `appuser` |

## 7. n8n-to-worker communication (workflow-level, end-to-end)

Probe workflow `00 - Phase 1 Health Probe` (imported via CLI, published) executed through a **real production webhook**, final verified output:

```
GET http://127.0.0.1:5679/webhook/phase1-probe  →  HTTP 200
{
  "worker_health": {"ok": true},
  "worker_status": {"ok": true, "service": "media-worker", "phase": 1, "auth": "verified", "token_configured": true},
  "shared_mount_sentinel": {"file_name": "sentinel.txt", "mime_type": "text/plain"}
}
```

This proves inside one n8n execution: webhook trigger, container-DNS HTTP to the worker, **credential-based** authentication (n8n Credential `Media Worker API Token` / `httpHeaderAuth`), the `Read/Write Files from Disk` node working through `N8N_RESTRICT_FILE_ACCESS_TO=/data/media`, and the response node. Note: a raw `node -e fetch()` from the n8n container now correctly receives **401** — the token is deliberately absent from n8n's environment (least privilege, DECISIONS #14); the credential is the only auth path and it works.

## 8. Issues hit and resolutions

1. **`docker.n8n.io` unresolvable** → switched to identical Docker Hub image (DECISIONS.md #12).
2. **n8n OOM group-kills at 512m** (5 restarts during CLI import; silent shell deaths) → raised to 1g; 0 restarts since (DECISIONS.md #13).
3. **n8n 2.x blocks `$env` in expressions** (`access to env vars denied`; breaking-change rule `process-env-access`; no allowlist in 2.48.7) — the legacy workflows' `$env.TOKEN` pattern is dead on 2.x. Fixed by importing an n8n **Credential** (`httpHeaderAuth`, id `pgm-worker-token-cred`) and referencing it in the HTTP Request node (DECISIONS.md #14). Temp file with the token was deleted immediately after import; the token never appeared in logs or this report.
4. **Webhook path conflict** between old and new probe versions → old workflow unpublished; new one published.
5. **BuildKit spurious exit-1** on provenance export — image verified built; no action needed.
6. **Workspace relocated mid-session (user action):** the project folder was moved to `D:\Downloads\…` and later restored to the original C: path during your C: disk-space reorganization. This killed one in-flight background rebuild (compose file temporarily missing) and briefly broke the host bind mount; the running containers and the named n8n volume were unaffected, and every file was verified intact after the round-trip (DECISIONS.md #18).
7. **Host Python is not installed**, so the user's `tests/test_phase1_smoke.py` runs inside the worker container (no pytest in the image — executed via a manual function runner).
8. **C: hit 0.00 GB free; an OpenCode server restart cancelled a background build.** You compacted the Docker VHDX (~9 GB recovered, C: → ~9.2 GB) and I restarted Docker Desktop to complete Phase 1; all containers (including the legacy n8n) returned via their restart policies.
9. **Live stack survived the entire turbulence:** after the workspace round-trip, the probe webhook again returned HTTP 200 with full evidence (health, credential-authenticated status, sentinel read) without recreating any container.

## 9. Disk situation (resolved during the session — follow-up recommended)

- C: (121 GB) fell to **0.00 GB free** during Phase 1 (n8n 2.48.7 pull 2.18 GB + worker image 876 MB + prior pressure). Windows itself was at risk of random write failures; an OpenCode server restart also interrupted one background rebuild.
- **You resolved it:** the Docker WSL2 VHDX (`docker_data.vhdx`) was compacted (~9 GB returned; file now 18.83 GB) and Docker Desktop was restarted. C: recovered to **~9.2 GB free**. I restarted Docker Desktop to complete Phase 1 (all containers came back via `restart: unless-stopped`, including the legacy n8n — untouched content-wise).
- Earlier in the session I pruned all Docker build cache (7.29 GB freed inside the VM), which reduced VHDX pressure but could not return space to Windows without compaction.
- **Recommended follow-up (needs your approval, stops all Docker briefly):** move Docker Desktop's disk image location to D: (~116 GB free) — C: at ~9 GB free with an 18.8 GB VHDX remains fragile as media downloads begin in Phase 2. Optional: remove stale images `n8nio/n8n:1.76.1` (1.28 GB) / `n8nio/n8n:latest` (1.94 GB) — legacy-owned, only with explicit approval.

## 10. Known small items (non-blocking)

- The n8n instance has **no owner account yet**: on first visit to `http://127.0.0.1:5679` the setup screen will ask you to create the owner user. This did not affect CLI import, publish, or webhook execution (all proven above).
- One stale **unpublished draft** workflow (`luw40ZyN6fwZ1rbx`, the first env-based probe) remains in n8n; there is no delete CLI in 2.48.7 — delete it in the UI in one click if you wish.
- n8n log shows a deprecation notice for internal task-runner mode (Python runner unavailable); harmless for the probe workflow; revisit when WF03 uses Code nodes.
- The busybox `wget --header` in the n8n image ignores headers; use node-fetch or HTTP Request nodes for authenticated checks from that container.

## 11. Phase 2 readiness

Ready and proven: bootstrap ✅, compose ✅, n8n 2.48.7 with restricted file access ✅, worker running the adopted spec-§8 skeleton (`/health`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/manifest`, token-protected `/status`) ✅, shared mount ✅, credential pattern proven end-to-end ✅, smoke tests passing ✅, social/video tool binaries present and versioned ✅, legacy environments untouched ✅.

Phase 2 (website adapter + real manifest contract) can vendor the legacy `src/engine/collector.py` + `browser_gallery.py` behind the worker's `_process_job` and replace the stub manifest. Phase 3 will reuse the proven webhook + credential patterns for WF01–WF05 (and add an n8n-side auth gate for `POST /jobs`).

Before heavy media work: decide on relocating Docker's disk image to D: (C: has ~9.2 GB free; the media volume will grow quickly).

**Phase 1 complete. Stopping here per instruction.**

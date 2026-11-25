# V2.2 Engineering Plan — Meta Graph API (Client Facebook Page OAuth), Groq Tier Sizing & Photo Pilot

**Status:** Proposal / Plan Only (awaiting owner approval before any V2.2 code build).  
**Baseline:** V2.1 Bounded Localhost E2E Pilot (`b06090323c54471091d272de87ce6007`) passed `GO` for Website media downloads + Facebook public discovery + Two-Admin M4 approval + real Groq Vision curation + private Google Drive delivery.

---

## 1. Strategic Problem: Anonymous Facebook Binary Downloads

During live V2.1 pilot testing against authorized client pages (`lilys-club.co.uk`):
- Public Facebook discovery (`gallery-dl`) reliably extracts dated `in_window` post metadata, IDs, and captions (`20` + `3` verified records).
- Anonymous binary media downloads from Meta CDN consistently hit `access_blocked` (`0` FB binaries downloaded).
- Per project policy (`AGENTS.md` Rule 7), the platform **never** bypasses Meta login walls, anti-bot controls, or rate limits, and never stores user browser cookies.

### Recommended V2.2 Solution: Authorized Meta Graph API (Client Facebook Page OAuth)
For paying pet-grooming clients who want guaranteed 365-day Facebook photos and videos included alongside website media, introduce an optional, 100% policy-compliant **Client Facebook Page Connection** via official Meta Graph API OAuth.

#### Architecture & Layer Ownership (Preserving V2.1 Boundaries)
1. **Frontend (`frontend/`) — Client Page Authorization UI:**
   - Adds an optional **"Connect Client Facebook Page (Meta OAuth)"** flow on `/new` or a scoped one-time client authorization link.
   - Displays explicit scope disclosure (`pages_show_list`, `pages_read_engagement`) and the connected Page Name / Page ID.
   - Never exposes raw Page Access Tokens to browser JavaScript or Git.
2. **n8n (`n8n-media/`) — OAuth Token Custody & Refresh:**
   - Owns the Meta OAuth2 callback, exchanges short-lived user token -> long-lived token -> **Page Access Token** scoped to the single authorized client Page.
   - Stores encrypted credentials inside n8n's encrypted vault (never in SQLite plaintext or job manifests).
   - Passes a short-lived, job-scoped read request or fetches authorized Graph API media URLs cleanly within n8n before handing off permitted media URLs to the worker (or passes a strictly scoped, in-memory token header never logged or persisted to disk).
3. **Python Media Worker (`services/media-worker/`) — Bounded Graph API Collector Adapter:**
   - New adapter `app/meta_graph_collect.py` (invoked only when an explicit authorized Page connection is attached to the job; anonymous jobs stay on the existing honest public discovery path).
   - Queries official Graph API endpoints with strict pagination and 365-day frozen window filtering (`since` / `until` matching `[window_start_utc, window_end_utc]`):
     - `GET /{page-id}/photos?type=uploaded&fields=id,created_time,images,name,permalink_url`
     - `GET /{page-id}/videos?fields=id,created_time,source,length,description,permalink_url,thumbnails`
   - Downloads highest-resolution photo/video binaries through existing SSRF-checked, size-capped, SHA-256 + MIME + FFmpeg preprocessing pipeline.
   - Records exact provenance (`date_status: "verified"`, `date_source: "meta_graph_api"`, `created_time` preserved verbatim) while **keeping `rights_status: "unverified"`** until a Named Admin verifies commercial release rights under M4!

#### Meta App Review & Prerequisites (Human / Business Steps)
- Meta Developer App in **Business** category with verified business account.
- Permissions to request in Meta App Review: `pages_show_list`, `pages_read_engagement` (read-only; zero publishing permissions).
- Privacy Policy URL & Data Deletion Callback URL hosted on your production HTTPS domain (`docs/PRODUCTION_LAUNCH_RUNBOOK.md`).
- **Estimated Build Effort (once Meta App is created):** 2–3 small approved milestones (~2–3 engineering days including offline mocked Graph API tests + M4 ledger verification).

---

## 2. Groq Tier Upgrade & Throughput Sizing (Full 20–40 Asset Client Jobs)

Live telemetry from pilot `b06090323c54471091d272de87ce6007` on Groq `qwen/qwen3.8-27b`:

| Parameter | Current `on_demand` Tier (Verified Live) | Recommended Groq Developer Tier |
|---|---|---|
| Rate Limits | `1,000` OTPM · `8,000` TPM · `1,000` RPD | Higher TPM/OTPM (check Groq Console limits) |
| `AI_MODE_A_MAX_OUTPUT_TOKENS` | `850` | `1200` |
| `AI_MODE_B_MAX_OUTPUT_TOKENS` | `950` | `1800` |
| `AI_BATCH_SIZE` | `1` | `2`–`3` |
| `AI_MAX_IMAGES_PER_REQUEST` | `1` | `3` |
| `AI_MIN_REQUEST_SPACING_S` | `5` | `1`–`2` |
| `AI_MAX_REQUESTS_PER_JOB` | `25` | `40` |
| `AI_MAX_COST_USD_PER_JOB` | `0.25` | `0.75`–`1.00` |
| Estimated Wall Time (3 assets) | ~2.5–3 min | ~15–25 sec |
| Estimated Wall Time (25 assets) | ~18–22 min | ~1.5–2.5 min |
| Estimated Cost (25 assets) | ~$0.18–$0.22 | ~$0.18–$0.22 *(same token pricing)* |

**Action:** Keep the `on_demand` OTPM-safe defaults in `.env.example` and `deploy/compose.staging.yml` until the Groq billing tier is upgraded, then raise the `.env` knobs above without any code changes.

---

## 3. Quick Photo-Only Localhost Pilot Checklist (`Best Images/` Proof)

Pilot `b0609032` proved `Best Videos/` + `Reports/` end-to-end (AI selected 1 video, 0 images). To also verify live `Best Images/` subfolder creation and JPEG/PNG upload on your local stack (`< $0.03` Groq spend):

1. **Admin #1 (`ahsan-rashid`)** opens `http://localhost:3000/new`:
   - Enter an authorized grooming website URL rich in pet photography (e.g., `https://www.lilys-club.co.uk/` or a second authorized test salon).
   - Set bounds: `max_pages=5`, `max_media=6`, `max_social_files=1`, `max_selected_images=3`, `max_selected_videos=0` (so Mode B focuses on image selection).
   - Submit and wait for collection + preprocessing to finish.
2. **Admin #2 (`ismail-sajid`)** opens the new `/jobs/<id>`:
   - Review downloaded photos in the gallery; enable **Include undated website media** if website images are undated.
   - Click **Approve** -> **Start AI** -> check consent -> **Run AI Curation (Async)**.
   - Wait for `AI curation: completed` and confirm `Selected: N (N img / 0 vid)`.
   - Enter `evidence_ref` + `reason` and click **Verify** on 1–3 AI-selected photos.
   - Check commercial release confirmation and click **Deliver Approved Media to Google Drive**.
3. **Verify Live Private Drive Folder:**
   - Open the returned private folder URL and confirm:
     - `Best Images/` exists and contains ONLY rights-verified original images.
     - `Reports/ai-curation-report.json` and `Reports/media-inventory-summary.json` list only those released image IDs.
     - Visibility is **Restricted / Private** (`public_sharing: false`).

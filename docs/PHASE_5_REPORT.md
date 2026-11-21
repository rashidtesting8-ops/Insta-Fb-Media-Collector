# Phase 5 Report — AI Visual Director V2.0 Pipeline

**Date:** 21 November 2026
**Branch:** `feat/phase5-ai-visual-director` (from `main` @ `d68fd38`, Phase 4 PR #3 merged)
**Scope delivered:** provider-agnostic two-mode AI media analysis and curation engine implementing the authoritative `docs/AI_VISUAL_DIRECTOR_V2.md` contract: MODE A batch analysis of real pixels/frames, strict schema validation outside the model, MODE B business-specific final curation, persisted curation manifest, worker/n8n integration, 25 new offline tests (suite total 68), honest mock-provider smoke on real D: assets, and full documentation.
**Critical status:** **no AI provider credentials are configured** in this environment. All AI execution was performed by an explicitly-labeled deterministic mock. **Live AI provider validation is pending — no genuine model analysis or aesthetic judgment has occurred or is claimed.**

---

## 1. Architecture

```text
Phase 4 enriched manifest (website/instagram/facebook assets + quality, phash,
dedupe groups, frames, dates, rights)
   |
   v
app/ai/payload.py    select AI-ready assets
                     - exclude corrupted/unsupported/missing; cap per-job count
                     - analyze duplicate-group REPRESENTATIVES only, members inherit
                     - convert to bounded EXIF-oriented JPEG copies (originals untouched)
                     - video -> extracted frames with labels "@1.50s"
   |
   v
app/ai/prompts.py    MODE A / MODE B prompts embedding the V2.0 contract,
                     sanitized captions strictly as DATA (injection guard)
   |
   v
app/ai/provider.py   AIProviderConfig from env (AI_PROVIDER/MODEL/ENDPOINT/API_KEY)
                     - OpenAICompatibleProvider: image_url data URLs, bounded
                       exponential retries on 429/5xx/timeout, usage/cost tracking
                     - MockVisionProvider: deterministic, clearly labeled, offline
                     - UsageTracker: per-job request/cost ceilings, pre-flight enforcement
   |
   v
app/ai/pipeline.py   MODE A batches -> strict Pydantic validation (outside model)
                     - hallucinated ids/score violations -> bounded retry -> honest
                       `visual_analysis_status="unavailable"`, never invented data
                     - duplicate members inherit representative analyses (recorded)
                     MODE B -> strict V2 §9 schema -> cross-manifest id validation
                     - selection limits (hero+gallery <= max images; videos <= max videos)
                     - rights_review_required_ids DERIVED from manifest (model cannot hide)
   |
   v
curation.json (atomic) next to manifest.json on the D: shared storage
GET /jobs/{id}/curation serves it; job record carries `ai_curation` status
```

## 2. Provider adapter design and configuration

`AI_PROVIDER` = `mock` (deterministic, offline) or `openai_compatible` (any
OpenAI-style vision chat/completions endpoint); `AI_MODEL`, `AI_ENDPOINT`,
`AI_API_KEY` (secret; env/n8n credential only), `AI_TIMEOUT_S`, `AI_MAX_RETRIES`,
`AI_MAX_REQUESTS_PER_JOB`, `AI_MAX_COST_USD_PER_JOB`, `AI_PRICE_IN_PER_1K`,
`AI_PRICE_OUT_PER_1K`. All documented in `.env.example`; no real values in
source or GitHub. Secrets are scrubbed from errors/logs; usage records carry
only counts/token estimates. Budget exhaustion raises `BudgetExceededError`
before the next call. `build_provider()` **never** falls back to mock silently:
unset → `disabled`; incomplete real config → `provider_unavailable`.

## 3. Batch and final curation flows

- **MODE A**: bounded batches (6 assets default); per batch, actual image bytes
  (EXIF-oriented JPEG conversion, ≤1024 px, ≤1.5 MB) or Phase 4 frames with
  timestamps reach the provider via image_url data URLs; response strict-JSON
  validated against the V2 §9 MODE A field list, the 25/25/20/15/10/5 breakdown
  (must sum to the reported score within 1), enums, normalized focal coords,
  and the batch's own asset ids. Failures retry within `AI_MAX_RETRIES`, then
  become honest `unavailable` analyses.
- **MODE B**: consumes only validated analyses + operator-supplied verified
  business context; strict V2 §9 MODE B field list; `selected_asset_ids` derived
  from post-truncation placements; hero ≤3 and total images/videos within the
  job's configured maxima; before/after states restricted to the three V2
  states; video recommendations carry `frames_reviewed` (never
  `full_video_reviewed` from sampled frames); rights flags derived from the
  manifest; prompt version/model/provider/mock/usage recorded in
  `analysis_coverage`.

## 4. Files changed

| File | Change |
|---|---|
| `app/ai/__init__.py`, `schemas.py`, `provider.py`, `payload.py`, `prompts.py`, `pipeline.py` | **NEW** — the complete two-mode AI engine (new package) |
| `app/service.py` | `run_ai_curation` + `business_context` request fields, AI stage in job flow, `GET /jobs/{id}/curation`, honest status downgrade on AI failure/disablement |
| `tests/test_ai_visual_director.py` | **NEW** — 25 offline tests |
| `n8n-media/05-ai-curation-probe.json` | **NEW** — read-only webhook probe (job status + JSON curation summary; no binary storage) |
| `.env.example` | AI provider/budget placeholders (no secrets) |
| `docs/PHASE_5_REPORT.md` | this report |
| `README.md`, `DECISIONS.md` (#33–#36) | status/decision updates |

`docs/AI_VISUAL_DIRECTOR_ORIGINAL.txt` **untouched** (SHA-256
`661D5F38…`, size 18,160 bytes, unchanged from the starter kit); V2 contract
file also unchanged and embedded by reference in the prompt builders.

## 5. Example outputs (mocked — not genuine vision)

MODE A (one item, trimmed):

```json
{"asset_id":"fb_0001_67c48d0bed10","filename":"facebook-67c48d0bed10.jpg",
 "source":"facebook","media_type":"image",
 "detected_content":"mock-detected content for fb_0001_67c48d0bed10",
 "possible_categories":["gallery","service"],"quality_score":76.6,
 "score_breakdown":{"technical_quality":19.2,"business_relevance_authenticity":19.2,
 "composition_usability":15.3,"emotion_aesthetics":11.5,
 "distinctiveness_collection_fit":7.7,"evidence_confidence":3.8},
 "verification_status":"unverified","visual_analysis_status":"analyzed",
 "recommended_use":"gallery","selection_status":"considered",
 "selection_reason":"deterministic mock derived from supplied metadata"}
```

MODE B (curation.json `curation` section, trimmed, from the real D: smoke job):

```json
{"schema_version":"visual-director-v2.0","job_id":"a46ad72cf17e467ca3258e78f023353d",
 "mode":"FINAL_CURATE","selected_asset_ids":["fb_0001_...","fb_0002_...", "..."],
 "rights_review_required_ids":["<all selected — rights_status unverified>"],
 "analysis_coverage":{"assets_supplied":4,"assets_analyzed":4,"mock":true,
 "prompt_version":"V2.0","provider":"mock","model":"mock-vision-v1",
 "usage":{"requests":1,"input_tokens":480,"output_tokens":320}},
 "final_quality_assessment":{"meets_premium_standard":false,
 "overall_assessment":"mock assessment; no genuine vision occurred"}}
```

## 6. Test evidence

`python -m pytest tests -q` → **68 passed** (29 Phase 1–3 + 14 Phase 4 + 25 Phase 5).
Phase 5 coverage: MODE A/MODE B schema validity + strict extra-field rejection;
score breakdown consistency; hallucinated ids (MODE A batch and MODE B selection)
rejected; malformed JSON → honest failure after bounded retries; rate-limit
429 → exponential backoff then success (scripted transport); budget pre-flight;
provider-unavailable/disabled honesty (never silently mocked); partial batch
failure preserves successful batches; video frames supplied with timestamps →
`frames_reviewed`; duplicate members inherit representative analyses; image
conversion applies EXIF orientation and dimension cap without touching the
original; corrupted/unsupported/missing assets excluded; selection caps (hero
included); rights flags derived; unknown dates never fabricated; prompt
injection stripped from captions; mock always labeled; curation.json persisted
and consistent with the manifest.

Three real bugs found and fixed during testing: stale pre-truncation references
re-expanded `selected_asset_ids`; a 3-hero list exceeded a 2-image cap; the
mock provider did not record usage. Strict V2 §9 compliance verified by removing
an extra top-level MODE B field (emotional storytelling folded into gallery).

## 7. Live provider test status

**PENDING / BLOCKED:** no `AI_API_KEY` or vision endpoint is configured (`.env`
holds only infrastructure + GitHub keys). Per the approval, the pipeline was
fully exercised with the deterministic mock: bounded run against the real
Phase 4 enriched manifest on D: (job `a46ad72…`) produced `curation.json`
(9,523 bytes) with 4 analyzed assets, 1 request, 4 selected, all 4 rights-review
flagged, `mock: true`; served correctly by `GET /jobs/{id}/curation`. A live
job explicitly requesting AI with no provider configured correctly returned
`ai_curation.status="disabled"` and `partial_success` while preserving all
extraction results. **No genuine model response, visual observation or
aesthetic judgment has been claimed.**

## 8. Usage/cost safeguards

Per-job request ceiling (40), cost ceiling ($2 default), price-per-1k env knobs,
pre-flight checks before every call, bounded retries (2, exponential 2s/4s),
request timeout (120 s), per-job asset cap (40), payload dimension/byte caps,
and recorded usage (requests, retries, tokens, estimated cost). The mock records
usage too so CI exercises the accounting path.

## 9. Rights and authenticity protections

`rights_status` flows from the manifest into `rights_review_required_ids`
(derived, not model-supplied); all selected assets remain rights-unverified in
practice. Before/after states restricted to V2's three values; founder identity
defaults `unverified`; only verified timestamps may categorize dates; captions
are sanitized and embedded as DATA; every selected id and every file reference
is validated against the actual manifest; visual-analysis claims require pixels
to have been supplied (mock analyses are explicitly labeled, never passed off
as real vision); video review is `frames_reviewed` for sampled frames.

## 10. Known limitations

1. No live multimodal provider validated — all outputs are mock-labeled; real
   aesthetic quality is unproven until credentials are configured.
2. MODE A/B are sequential (bounded concurrency knob reserved for a later
   phase); per-job asset cap 40, batch size 6.
3. Mock analysis derives scores from technical metadata — deliberately a
   placeholder, not a vision judgment.
4. n8n workflow asset is a read-only curation probe; n8n does not itself call
   the vision model (the worker does, per the architecture).
5. Duplicate members inherit representative analyses (recorded); near-duplicate
   distinctions rely on Phase 4's conservative dHash threshold.
6. Cost estimation depends on operator-supplied price env vars; when absent it
   records 0 rather than guessing.

## 11. Phase 6 readiness

`curation.json` provides the exact selected asset ids, original-file references,
rights-review flags and ranked placements a Google Drive delivery workflow
needs. Phase 6 (n8n Drive folders, binary upload of selected ORIGINALS,
verification ledger, rights gate) can consume it directly. No Drive, frontend,
or production deployment work was performed here.

---

## 12. Live vision-provider acceptance test — **BLOCKED (no credentials)**

**Attempted:** 21 November 2026, after pulling latest `main` (`595058c520bceddb693ba3e3838c0913b0506f5a`, PR #4 merged; GitHub Actions run **#12 on main: success**).

**Result:** No live vision-provider test was run. Inspection found:
- `.env`: **no `AI_*` keys at all** (only infrastructure + GitHub keys).
- Running worker container: **no `AI_*` environment variables**.
- Therefore `AIProviderConfig.from_env()` resolves `provider=""` → `build_provider()` returns `disabled`. A job requesting `run_ai_curation=true` correctly reports `ai_curation.status="disabled"` and `partial_success` while preserving extraction (live check, job `24ecb32b…`).

**Do not treat the mock smoke (§7) as live validation** — it was explicitly labeled `mock: true`; no genuine model id, usage or cost exists to record.

### Exact configuration required to unblock

| Variable | Purpose | Compatibility requirement |
|---|---|---|
| `AI_PROVIDER` | Select provider | `openai_compatible` for a real vision endpoint (`mock` is test-only) |
| `AI_MODEL` | Vision model id | Must accept OpenAI-style chat/completions with `image_url` data URLs (a vision-capable model id exposed by your endpoint) |
| `AI_ENDPOINT` | Full chat/completions URL | HTTPS URL reachable from the worker container |
| `AI_API_KEY` | Provider credential | Secret; authorized for `AI_MODEL`; store only in local `.env` or n8n credentials — never in Git |
| `AI_TIMEOUT_S` | Per-request timeout | default 120 s |
| `AI_MAX_RETRIES` | Bounded retries (429/5xx/timeout) | default 2, exponential backoff |
| `AI_MAX_REQUESTS_PER_JOB` | Request ceiling | default 40 |
| `AI_MAX_COST_USD_PER_JOB` | Cost ceiling | default 2.0 USD |
| `AI_PRICE_IN_PER_1K` / `AI_PRICE_OUT_PER_1K` | Optional price inputs for cost estimates | 0 by default → cost recorded as 0 when unavailable |

Add these to the **local** `.env` (use `.env.example` as the template), then recreate the worker so the container receives them:
`docker compose -f compose.media.yml up -d` (no container deletion, no volume changes).

### Pixel-delivery verification (static, not live)

- `app/ai/payload.py::convert_for_payload` produces real JPEG **bytes** from the on-disk original (EXIF-oriented, ≤1024 px, ≤1.5 MB); `build_image_payloads` attaches them to `ImagePayload.data` with labels (`primary image` or `frame @<timestamp>s`).
- `app/ai/provider.py::OpenAICompatibleProvider.complete` builds `content` with `{"type":"image_url","image_url":{"url":"data:<mime>;base64,<base64 of actual bytes>"}}` — filenames/metadata are **not** the payload; captions are text-context data only.
- Offline tests prove file-backed selection, labeled frame payloads, and that only supplied assets can be analyzed.
- **Live receipt of pixels by a real model remains UNVERIFIED until credentials are configured.**

### What will be executed once credentials are set

1. One small, cost-bounded job: `run_ai_curation=true`, ≤1–2 assets, `max_images=2`, a lowered per-job request ceiling, explicit business context; no Drive/frontend involvement.
2. Verify MODE A items (real model id, `visual_analysis_status`, range-valid weighted breakdown, no invented ids), MODE B strict V2 §9 schema, `curation.json` persisted on D:, selected ids all present in the manifest, rights-review flags derived/intact, and actual provider usage (requests/tokens) + cost info recorded truthfully.
3. Update this section with the real model id, usage and results — or with an honest auth/access failure. No mock output will be relabelled as live success.

**Current status (first check, before credentials): BLOCKED — no `AI_*` variables.**

---

## 13. Live vision-provider acceptance test — second attempt with Groq credentials: **BLOCKED (API 403/1010 from container)**

**Prepared and verified (no secret values displayed):**
- Pulled latest `main` `595058c…` (PR #4 merged, Actions run #12 success).
- All four required variables present in local `.env`: `AI_PROVIDER`, `AI_MODEL`, `AI_ENDPOINT`, `AI_API_KEY` (configured flags only).
- **Model availability confirmed via Groq models API** (from host): 11 models returned; exact id `qwen/qwen3.8-27b` present; metadata declares `input_modalities: text,image` (genuine vision input), `context_window: 131072`, `max_completion_tokens: 16384`, `supported_features: tools,json_mode,reasoning`, pricing prompt `0.0000008`/token, completion `0.000004`/token.
- Accurate cost inputs added locally: `AI_PRICE_IN_PER_1K=0.0008`, `AI_PRICE_OUT_PER_1K=0.004`; request ceiling 3, cost ceiling $0.25, retries 1 from `.env`.
- `compose.media.yml` now forwards **only** the ten `AI_*` variables to the media worker (least privilege). `docker compose config --quiet` valid; **`--no-deps` recreate of `collector` only** — both n8n containers and legacy resources untouched; all `AI_*` keys verified present in the container by name only.
- Provider hardened for this model's declared capabilities: JSON mode request + bounded `max_tokens=4000`; offline suite re-run **68/68 passed** after the change.

**Live attempt:** one bounded script with a **single shared `UsageTracker` capped at 3 requests** — (1) a raw-pixel probe (synthetic red PNG, ask dominant color), then (2) MODE A + (3) MODE B on the real Phase 4 enriched manifest on D: for job `a46ad72…`.

**Result:** the first request (pixel probe) was rejected before reaching the model:
`urllib.error.HTTPError: HTTP Error 403: Forbidden` — provider body `error code: 1010` (Cloudflare access-control rejection). **No tokens consumed; cost $0.00; no MODE A/MODE B response; no live `curation.json` produced.** The budget cap of 3 requests was not exceeded (one failed attempt only).

**Read-only diagnosis (no second inference call, no model switch):**
- The identical GET to `https://api.groq.com/openai/v1/models` **from the host succeeds** (11 models, key valid, model available).
- The same GET **from inside the worker container returns HTTP 403 / code 1010** with the same HTTP stack.
- Therefore the rejection is **container egress / WAF-level** (the container's network path or client signature is blocked), **not** an invalid key, unavailable model, or missing vision support.
- I did **not** spoof a browser signature or try another model: both would violate the project's anti-bypass / no-automatic-model-switch rules.

**Guardrails confirmed:** request cap 3 + $0.25 cost ceiling with real provider pricing configured; budget checked before every call; no credentials in logs/manifests/Git; no legacy n8n, volume, backup, or container deletion.

**Blocker / required next step (operator decision):** provide an authorized network path for the worker's Groq traffic (e.g. an approved proxy/egress allowlist for the container, or provider-side allowlisting of the container egress IP), then rerun the same 3-request bounded test. Until then live vision validation remains **PENDING/BLOCKED**, and the existing mock `curation.json` (job `a46ad72…`, `mock: true`) must not be relabelled as live success. Phase 6 not started.

**Local uncommitted changes from this validation attempt** (preserved, not published): `compose.media.yml` AI env forwarding; `.env` price variables (secrets local-only); `app/ai/provider.py` JSON-mode/token cap; this report section.

---

## 14. Groq transport remediation + live attempt outcome (21 November 2026)

**403/1010 resolved.** Option A (approved) replaced `urllib.request` with `requests.post` in `OpenAICompatibleProvider._http_post` (TLS verification ON, no UA override, same endpoint/auth/body/timeout/retry/usage/cost logic). The retry classifier now handles `requests.HTTPError` (429/5xx only) and `requests.Timeout`/`RequestException`/malformed responses. Secret scrubbing was tightened to redact the token after `Bearer` (a test caught the old partial redaction). With the fix, a read-only unauthenticated models GET from the worker returns **HTTP 401 with `cf-ray`** (Groq app reached) instead of 403/1010 — confirmed twice (rays `a5de98b6ac3fd4b0-KHI`, `a5de9d2f8819c4c3-KHI`).

**Live attempt (3 provider requests total, cost ceiling $0.25):**
1. A pixel-probe call was rejected by Groq's app with **400** — `"'messages' must contain the word 'json' ... to use response_format json_object"` (prompt-shape rule for this model, not an image/network problem). No tokens billed.
2. The bounded pipeline call (MODE A on the real D: manifest for job `a46ad72…`) was rejected by Groq's app with **400** — `"Too many images provided. This model supports up to 3 images"`; with `max_retries=1` this used 2 attempts. No tokens billed.

**Result:** **$0.00 cost, 0 tokens, no successful inference, no non-mock `curation.json` produced.** The earlier mock curation file was preserved on D: as `curation.mock.json` and was **not** relabelled. Total provider attempts = 3, exactly the authorized ceiling — **no further live call was made**.

**Fix for the next bounded run:** MODE A batch size is now env-configurable (`AI_BATCH_SIZE`, conservative default **3** to match the model's documented image limit) and the worker service uses it. Offline suite after all changes: **78 passed** (68 prior + 10 transport/config tests). Worker recreated with `--no-deps`; both n8n instances and all data untouched.

**Remaining blocker:** live MODE A/B validation needs a **fresh authorization for another ≤3-request run** with `AI_BATCH_SIZE=3` (4 assets → 2 MODE A batches + MODE B). 403/1010 is gone; the outstanding item is purely the model's per-request image limit and the exhausted request budget. Phase 6 not started.

---

## 15. FINAL bounded live Groq test (after PR #5 merge) — **BLOCKED: 413 payload size + 429 rate limit**

**Setup verified:** latest `main` pulled at merge commit `0e4b0a2d79797fe079cf78e91a3e543a27533c84` (PR #5 merged; Actions run #14 passed). `requests.post` transport with `verify=True` active in the worker (no `urllib.request`); read-only unauthenticated models GET from the worker returned **401 with `cf-ray: a5deac29bfbf374e-KHI`** — no 403/1010. Worker-only rebuild/recreate (`--no-deps`); both n8n instances intact. Config: `AI_PROVIDER=openai_compatible`, `AI_MODEL=qwen/qwen3.8-27b`, request cap **3**, cost ceiling **$0.25**, prices configured (`0.0008`/`0.004` per 1k), `batch_size=3` and `max_retries=0` enforced by the test script. Prompts verified to contain "STRICT JSON" (Groq `json_object` requirement).

**Job:** `a46ad72…` on the D: mount — 12 manifest rows, exactly **4 processed image assets** (the rest are honest failure/unsupported rows). MODE A batches planned 3+1 images, then MODE B = **3 provider requests total**.

**Actual outcomes (verbatim error prefixes, no secret values):**
1. MODE A batch 1 → **HTTP 413**: `Request too large for model qwen/qwen3.8-27b in organization … service tier …` (3 image data-URL payloads + prompt exceeded the model's request-size limit).
2. MODE A batch 2 → **HTTP 429**: `Rate limit reached for model qwen/qwen3.8-27b in organization … service tier …`.
3. `max_retries=0` → **no retry**; pipeline status `failed`, `mode_a_count: 0`, no MODE B call (budget preserved).

**Genuine results:** model `qwen/qwen3.8-27b`, provider `openai_compatible`, `mock: false` on the attempted run; **2 provider requests attempted, 0 tokens billed, $0.00 cost** (both rejections pre-inference). No MODE A analyses, no MODE B curation, no non-mock `curation.json` produced. The earlier mock curation file on D: remains intact and **labeled `mock: true`** — it was not relabelled as live success. No alternate model, signature, proxy, or repeated retry was attempted.

**Root causes of this attempt (both provider-side constraints, not auth/network):**
- 413: payload-size limit for this model. Remediation candidate (requires fresh approval): reduce image dimensions/byte cap further (e.g. 512 px / ≤400 KB per image) and/or `AI_BATCH_SIZE=1`.
- 429: Groq rate limit for this model/service tier. Remediation candidate (requires fresh approval): pace requests and/or raise the account tier limit; the adapter already honors 429 with bounded backoff when retries are enabled.

**Regression/health after the attempt:** offline suite **78 passed**; worker `/health`, new n8n and legacy n8n `/healthz` OK; authenticated probe HTTP 200 `auth=verified`; volumes/jobs/backups untouched. **Live AI acceptance remains BLOCKED on provider-side payload/rate constraints; Phase 6 not started.**

---

## 16. Payload optimization + rate-limit hardening and minimal live run (branch `fix/phase5-groq-payload-rate-limits`)

**Provider limits reviewed (Groq docs):** `qwen/qwen3.8-27b` base limits 30 RPM / 1K RPD / 8K TPM / 200K TPD; documented headers `retry-after`, `x-ratelimit-remaining-requests` (RPD), `x-ratelimit-remaining-tokens` (TPM), reset headers; 429 is the rate-limit signal. A read-only models GET with the configured key returned **200** (account permitted). Smaller images alone are not assumed to solve all limits — batch size and pacing were changed too.

**Implemented:**
- Configurable `AI_MAX_IMAGE_DIM` (512), `AI_MAX_IMAGE_BYTES` (300 KB encoded target), `AI_MAX_REQUEST_BYTES` (1 MB full JSON body including base64 expansion), `AI_MIN_REQUEST_SPACING_S` (2 s), `AI_BATCH_SIZE` (set to **1** locally), all forwarded to the worker in `compose.media.yml` and documented in `.env.example`.
- JPEG optimization with adaptive quality/downscale to the byte target; EXIF orientation applied on a copy (original `exif_orientation` stays intact); originals never overwritten.
- **Local preflight** rejects oversized bodies with `RequestTooLargeError` before any HTTP call (no repeated 413s); 413 is never retried.
- 429 handling parses `Retry-After` (bounded), records a non-secret rate-limit header snapshot, honors short retry windows; explicit `RateLimitedError` for quota exhaustion; no key/proxy/account rotation; 401/403 remain authoritative.
- Pipeline now preserves MODE A results, coverage and usage on MODE B failure (honest partial bookkeeping), and drops model-invented `missing_assets[].category` values with a recorded warning instead of silently relabeling them.

**Minimal live run (1 real D: image derived from job `a46ad72…`, no new scraping, budget 3 requests / $0.25):**
- **MODE A SUCCEEDED with real pixels**: the real vision model accepted the 512 px / ≤300 KB image payload and the pipeline advanced to MODE B. (MODE A analyses were in-memory; they are no longer discarded on MODE B failure after this branch's fix.)
- **MODE B failed strict schema validation**: the model emitted `missing_assets[].category` outside the V2 enum (`ModeBResponse` rejected it). With `max_retries=0` there was no uncontrolled retry. **2 of 3 provider requests used; no further live call was made.** Token/cost figures from that run were not persisted by the old early-return path (now fixed for future runs).
- **No non-mock `curation.json` produced.** The earlier mock curation on D: remains labeled `mock: true` and was not relabelled.

**Offline regression suite:** new tests cover 512 px resizing, JPEG byte target, base64 expansion, full-body preflight, 413 no-retry, 429 Retry-After, quota exhaustion, request pacing, and the two new MODE B honesty fixes, plus all Phase 1–5 regressions (final count recorded after the rebuild). **Phase 6 not started; MODE B live curation remains pending a fresh bounded run with the fixed validator.**

---

## 17. FINAL live MODE A/B acceptance (merged `d5396cd…`) — **BLOCKED: provider payload-size rejection returned as 429**

**Environment verified:** clean tree pulled to `d5396cdd90b73acb9271922516320bccb366caee`; worker rebuilt no-cache from merged main; model `qwen/qwen3.8-27b` available (models GET 200); config active: `AI_BATCH_SIZE=1`, `AI_MAX_IMAGE_DIM=512`, `AI_MAX_IMAGE_BYTES=300000`, `AI_MAX_REQUEST_BYTES=1000000`, `AI_MIN_REQUEST_SPACING_S=2`, request cap 3, cost ceiling $0.25. Minimal job `phase5-live-one` holds one real D: image (`fb_0001_67c48d0bed10`, `rights_status: unverified`).

**Attempt 1 (before score-normalization fix):** MODE A reached MODE B but the model's `quality_score` disagreed with its own 25/25/20/15/10/5 breakdown; strict validation rejected it. Fix applied: derive the score deterministically from the model's breakdown (no invented values) and record a warning; all-batch-failure now preserves usage/coverage instead of returning empty results. Offline suite: **90 passed**.

**Attempt 2 (with fix):** MODE A was rejected by Groq with **HTTP 429** whose body begins `"Request too large for model qwen/qwen3.8-27b in organization … service tier on_demand"` — a payload-size rejection delivered with a rate-limit status. Rate headers showed full quota (`x-ratelimit-remaining-requests: 1000`, `x-ratelimit-remaining-tokens: 8000`, reset `1ms`), so this was **not quota exhaustion**, but it *was* a 429: per the approval I stopped immediately. **No third request, no model switch, no proxy/UA workaround, no mock substitution.**

**Genuine results:** provider `openai_compatible`, model `qwen/qwen3.8-27b`, `mock: false`; job `phase5-live-one`; **1 request in this attempt, 0 tokens billed, $0.00 cost**; `visual_analysis_status: unavailable` for the single asset; no MODE B call, no `curation.json` produced. The pre-existing mock curation on D: remains labeled `mock: true`.

**Remaining blocker:** this model/service tier rejects the multimodal request size even with 512 px / 300 KB local limits (prompt + image tokens exceed its on-demand allowance). A fresh approval would be needed to test a substantially smaller payload (e.g. 256 px / ≤100 KB and a shorter MODE A prompt). **Live MODE A/B success is NOT demonstrated; Phase 6 remains unstarted.** Offline suite 90/90; worker + both n8n healthy; all data preserved.

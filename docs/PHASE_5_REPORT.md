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

# Phase 3 Report — Social Media Collection Engine (Instagram + Facebook)

**Date:** 20 November 2026 (hardening addendum same day — §7)
**Scope delivered:** gallery-dl-primary social extraction integrated into the worker (separate Instagram and Facebook adapters), Instaloader Instagram fallback, yt-dlp fallback for supported single Facebook video/post URLs, 365-day lookback with honest date verification, configurable bounded limits, operator-consent gate, unified multi-source manifest, offline mocked tests, and bounded authorized live smoke tests with truthful outcomes.
**Out of scope (unchanged):** AI Visual Director, Google Drive, frontend, production deployment, Docker cleanup.

---

## 1. Changed files (services/media-worker/ unless noted)

| File | Change |
|---|---|
| `app/legacy_social_collect.py` | **NEW — vendored byte-for-byte** from the pristine ZIP (`src/engine/social_collect.py`); SHA-256 `328ebcc00943d72d168237fa785f14f35264c02205888e039910f777afb9b4a7` verified identical to ZIP + reference copy. Contributes: arg-array subprocess, `--dump-json` dry-run, date-after + post-hoc verified-date filtering, `--download-archive` idempotency, `--whitelist platform`, size caps (25 MB item / 150 MB staging / 300 MB project), failure classification, secret scrubbing, originals kept separately from metadata-stripped copies. |
| `app/social_adapter.py` | **NEW** — per-platform extraction: gallery-dl via the vendored module; **Instaloader fallback** for Instagram (anonymous, `--count` bounded, `--post-filter` cutoff, `--abort-on=429,403`); **yt-dlp fallback** only for supported single Facebook post/video URLs (`fb.watch`, `/posts`, `/videos`, `/watch`, `/reel`, `/photo`); permission gate (`social_permission_confirmed`), URL allowlist, media relocated to `<job>/instagram/` + `<job>/facebook/`, optimized copies to `research/optimized/`, sha-dedupe within job, dims computed from disk, honest statuses (`completed`/`partial_success`/`failed`/`permission_required`/`invalid_url`), refined gallery-dl error-code classification. Fallbacks are injectable for tests and **never** fire on rate limits. |
| `app/manifest.py` | Unified multi-source contract: `SCHEMA_VERSION="3.0-unified"`, `build_social_item` (asset IDs `ig_…`/`fb_…`, `date_status` verified-only, rights unverified, origin/author detail), `merge_social_into_manifest` (website preserved, per-source summaries, lookback + cutoff + consent recorded, honest overall status). Phase-2 top-level fields kept for compatibility. |
| `app/service.py` | `social_permission_confirmed` (default **false** — collection is never attempted without operator acknowledgement) + `max_social_files` (1–60, default 20) added to `POST /jobs`; `_process_job` runs each source **isolated** (social failure never destroys website results), stores per-source progress, merges the unified manifest, and computes overall status from the final merged sources (**bug found live and fixed**: the first version computed job status from a dict where the stale `running` placeholder could override the real website result, mislabeling a `partial_success` job as `failed`). |
| `requirements.txt` | `gallery-dl==1.30.0 → 1.32.20` (see §4). |
| `tests/test_social_collector.py` | **NEW** — 13 offline mocked tests. |
| `tests/test_website_collector.py` | One assertion updated: manifest schema `2.0-website` → `3.0-unified` (contract evolution; all other website tests unchanged and passing = regression green). |
| `README.md`, `DECISIONS.md` | Updated (live social collection, decisions #24–#27). |

**Untouched:** pristine ZIP + reference copy (hash-verified identical after all work), legacy containers/volumes, both n8n instances (their state/credentials intact; probe verified), all backups, old C: storage, all pre-Phase-3 job records.

## 2. Test evidence (in-container, fully offline, mocked tool runners)

`python -m pytest tests -q` → **23 passed** (10 website regression + 13 social):

Social coverage: permission gate blocks everything with zero invocations; invalid URL rejected; gallery-dl success with **verified date**, bounded flags asserted (`--whitelist`, `--date-after`, `--range 1-N`, `--filesize-max`); login_required reported honestly; **rate limit never triggers a fallback**; Instaloader fallback success (verified date, shortcode URL, bounded `--count`, `--abort-on` flags); yt-dlp fallback for a single FB video (type=video, upload_date verified); FB *page* URLs never invoke yt-dlp; unified merge preserves website assets when a social source fails (source_status `partial_success`); unique asset IDs across platforms; full-disk validation clean; undated items stay `unknown`; single-URL regex gates; each platform runs independently.

Bring-up fixes found by the tests/offline cycle: a `src.stat()`-after-move crash path in the adapter; a whitespace-damaged edit in `manifest.py`; the `fb.watch/<id>` regex miss.

## 3. Authorized live smoke tests (bounded, consent-gated)

Targets: website `https://en.wikipedia.org/wiki/Dog_grooming` (controlled, robots-permissive, rights-clean), plus the real public pet-grooming business sources `https://www.instagram.com/barkbus/` and `https://www.facebook.com/barkbus`. Bounds: 5 pages / 12 website media; **6 social files**; 365-day lookback; operator consent flag set. Two runs:

**Run 1 (job `f01368f7…`, gallery-dl 1.30.0):** website 3 files; both social sources failed with reason `unknown` (no fallback fired) — diagnosis revealed gallery-dl 1.30.0 emits **JSON error envelopes with exit 0** (Instagram access-gate code 6) and produces **empty output** for the Facebook page. This run also exposed the job-status merge bug (§1), fixed immediately.

**Run 2 (job `d29fc1a1…`, after fixes + gallery-dl 1.32.20): overall `partial_success` with real media on D:**

| Source | Status | Outcome |
|---|---|---|
| website | partial_success | 3 files, 139 KB (same honest profile as Phase 2) |
| instagram | partial_success | gallery-dl access-gated (`gallery_dl_error_6` + diagnostic recorded) → **Instaloader fallback delivered 1 file** (150×150 profile picture, 3.5 KB) |
| facebook | partial_success | **gallery-dl 1.32.20 delivered 3 real photos** (1080×1080, 1170×1100, 1170×962; 480 KB total) before hitting the photos-album access gate |

Unified manifest (`schema 3.0-unified`): 16 assets (12 website rows incl. honest failures + 4 social), per-source statuses, `lookback_days=365`, `cutoff_date=2025-11-20`, consent recorded; **all 7 downloaded files re-hashed on D: — 0 mismatches**; full provenance (social-media-manifest, download archive, metadata-stripped copies, snapshots). All social dates honestly `unknown` — anonymous extraction provided no trustworthy post timestamps, so none were invented.

Post-run battery: worker/n8n(×2) healthy; probe HTTP 200 `auth=verified` + sentinel; old jobs `22fef997`/`26c48d32`/`14fec325` intact and readable; C: old storage + all backups untouched; C: 29.59 GB / D: 75.79 GB free.

## 4. Failed cases & how they are handled (nothing is faked)

1. **gallery-dl 1.30.0 pinned version was categorically broken for 2026 IG/FB** → pin bumped to **1.32.20** (current stable; documented in requirements + DECISIONS #26). Even current versions face heavy anonymous access-gating; the extractor is a fast-moving target and the pin must be re-reviewed periodically.
2. **Anonymous Instagram post access is gated** (error envelope code 6; `NotFoundError` on the posts endpoint): classified `gallery_dl_error_6` with the diagnostic tail recorded; the bounded Instaloader fallback (anonymous) obtained only the profile picture. No login, no cookies, no bypass — by design.
3. **Facebook page photos partially gated**: 3 photos downloaded, then the album extractor hit the access gate → `partial_success`, never marked complete.
4. **No post timestamps available anonymously** → all social items carry `date_status: unknown`; the 365-day rule only filters *verified* out-of-range dates.
5. **Full 365-day history is not retrievable anonymously** for either platform — a permanent, documented coverage gap, not a bug.

## 5. Coverage gaps & limitations

- Anonymous (no-login) collection yields partial, low-volume results by design; platform authorization (Business/Graph APIs) is the only route to complete, timestamped, permitted history and is deliberately not implemented (needs credentials/approval).
- The consent gate is an operator acknowledgement, **not** platform authorization or a copyright licence (recorded in every manifest).
- Social items may include low-value assets (e.g., a profile picture) — AI selection (Phase 5) will judge relevance; nothing is pre-filtered on filename.
- Cross-platform dedupe (same photo on website + social) is not applied yet; `sha256` fields make it a Phase-4 preprocessing step.
- The legacy module's upstream Pillow deprecation warning (`getdata`) remains — the vendored file stays byte-identical by policy.

## 6. Phase 4 readiness

Ready: website + social sources live and honest; unified manifest on D: with sha-verified assets; per-source isolation proven; consent + bounds wired end-to-end; both n8n instances and the credential/probe path intact. Phase 4 (media processing: hashes/dedupe across sources, video frames via FFmpeg, quality metrics, date categorization — preparing AI payloads) can proceed on approval; the manifest already carries everything Phase 4/5 need (real pixels on disk, sha256, dims, dates, rights flags).

## 7. Acceptance hardening (same-day addendum)

The refined Phase 3 acceptance requirements were implemented and re-verified:

1. **No first-N-as-coverage proof:** every social section now carries a
   `coverage` block —
   `discovery_selection: "bounded_sample_not_full_coverage"`,
   `full_coverage_claimed: false`, `cutoff_date`, `discovery_limit_files`,
   and the date-filter semantics. Nothing anywhere claims complete 365-day
   extraction.
2. **Six-way source statuses:** `completed`, `partial_success`,
   `restricted` (login walls / access-gate envelopes), `rate_limited`
   (never retried, never fallback-hammered), `inaccessible`
   (not-found/unreachable), `failed` (tool/adapter errors), plus
   `permission_required` / `invalid_url` / `not_requested`. Job-level
   aggregation was extended to the new statuses.
3. **Fallbacks stay appropriate:** Instaloader (Instagram only) and yt-dlp
   (single Facebook post/video URLs only) fire only on access-gate or
   extractor-broken reasons; never on rate limits or not-found. No login,
   cookies, CAPTCHA bypass, or restriction circumvention exists anywhere.
4. **Dates:** verified platform timestamps are preserved with
   `date_status: "verified"`; everything else is `unknown`; only *verified*
   out-of-range dates are filtered (regression-tested at the boundary).
5. **Disk-backed manifest:** social items are now validated against disk at
   write time (sha + size + path safety), and the **final merged manifest**
   is re-validated in the service before persistence — any problem is
   recorded in `sources.manifest` and in the manifest's coverage issues.
6. **Independent sources:** unchanged and regression-covered (a restricted
   Facebook never disturbs website/Instagram results).
7. **New regressions (6):** access-gate envelope → `restricted`;
   not-found → `inaccessible` (no fallback); files+gate → `partial_success`
   (the live Facebook pattern); date boundary (in-range kept, out-of-range
   dropped, undated kept); duplicate fallback posts deduped; coverage
   metadata recorded and no full-coverage language present.
8. **Tool execution ledger:** each section records `tools_executed` —
   tool name, live-captured version (offline/mocked runs skip the version
   probe), and per-tool outcome.
9. **Coverage honesty:** as (1) — enforced structurally, asserted by test.
10. **Final regression:** **29/29 offline tests pass** (10 website
    Phase-2 + 19 social), plus the live Phase-1 battery (worker `/health`,
    both n8n `/healthz`, authenticated probe HTTP 200) and a fresh live
    Phase-2 website-only job (`partial_success`, 3 files — unchanged
    honest profile).

**Post-hardening live evidence (job `f8ff89faa0ad48738e5f92542177fbec`,
consent-gated, max 3 social files):** Instagram — `gallery-dl(1.32.20,
gallery_dl_error_6)` → `instaloader(4.16, downloaded_1)` → 1 file,
`partial_success`; Facebook — `gallery-dl(1.32.20, gallery_dl_error_6)` →
3 files, `partial_success`; website 3 files; overall job `partial_success`;
no source falsely marked complete; no fallback fired after a rate limit.

**Note:** these hardening changes are local-first and **not yet pushed** to
the GitHub baseline (committing them is the recommended next git step, e.g.
`feat/phase3-acceptance-hardening` → PR → `main`).

**Phase 3 complete. Stopping here per instruction — Phase 4 (AI integration / Google Drive) not started.**

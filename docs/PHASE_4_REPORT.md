# Phase 4 Report — Media Preprocessing Engine

**Date:** 20 November 2026
**Branch:** `feat/phase4-media-preprocessing` (from `main` @ `005bd18`)
**Scope delivered:** deterministic media preprocessing between collection and the future AI stage — cross-source deduplication (exact + perceptual), image technical-quality analysis, bounded FFmpeg video frame extraction, verified-date categorization, and additive enrichment of the unified manifest — fully integrated into the worker job flow, with 14 new offline tests (suite total 43), live end-to-end evidence, and D:-verified outputs.
**Out of scope (unchanged):** AI Visual Director (Phase 5), Google Drive (Phase 6), frontend, production deployment.

---

## 1. Architecture and processing flow

```text
collection (website / instagram / facebook; consent-gated)
  -> unified manifest (schema 3.0-unified — UNCHANGED)
  -> app/preprocessing.py  (deterministic, offline, no AI, never modifies originals)
       1. per-asset technical analysis
            images  : integrity, dimensions, aspect, usable-size, EXIF orientation,
                      sharpness (gradient energy), mean luma, contrast std,
                      clipped shadows/highlights, centered crop windows
            videos  : ffprobe duration/resolution/codec, bounded frame sampling
                      via ffmpeg (argument arrays, per-call timeouts, ≤6 frames,
                      ≤640px frames, ≤600s videos, ≤45s per ffmpeg call),
                      middle-frame representative thumbnail
            other   : e.g. SVG -> honest "unsupported_format" (not an error)
       2. cross-source deduplication
            exact sha256 groups, then perceptual dHash groups (64-bit difference
            hash, conservative Hamming ≤6; exact-grouped pairs never double-grouped)
            representative = largest pixel area, tie-break larger bytes, then
            asset_id order — reason recorded per group; all provenance retained
       3. date categorization — verified timestamps only:
            in_lookback_verified | older_verified | unknown
            (never inferred from download/capture time)
  -> enriched manifest (atomic rewrite; purely additive fields)
  -> validate_against_disk (downloaded + extracted_frame items; sha + size + path)
  -> final job record: preprocessing {status, images, videos, duplicate_groups}
```

Job status integration: preprocessing failures never kill a job — they are
recorded; a failed preprocessing downgrades an otherwise `extraction_complete`
job to `partial_success` with an honest error note.

## 2. Files changed

| File | Change |
|---|---|
| `services/media-worker/app/preprocessing.py` | **NEW** — the Phase 4 engine (analysis, dedup, video frames, enrichment driver; ~430 lines) |
| `services/media-worker/app/manifest.py` | `validate_against_disk` extended to also verify `extracted_frame` assets (additive; old statuses unaffected) |
| `services/media-worker/app/service.py` | `_process_job` runs preprocessing after the merged manifest, records the `preprocessing` field, honest downgrade on failure; scraping behavior untouched |
| `services/media-worker/tests/test_media_preprocessing.py` | **NEW** — 14 offline tests |
| `docs/PHASE_4_REPORT.md` | this report |
| `README.md`, `DECISIONS.md` | flow/status updates, decision #30 |

**Untouched:** all collection modules (no scraping behavior change), vendored legacy modules (byte-identical), containers/volumes/credentials/backups, old jobs and their manifests (verified readable, original schema intact).

## 3. Implementation details worth noting

- **Technical metrics are engineering measurements only.** Sharpness = normalized mean absolute gradient on a ≤512px grayscale copy; exposure = mean luma + clipped percentages; contrast = luma std-dev. They are never labelled AI aesthetic judgments (the AI Visual Director is Phase 5 and will receive real pixels/frames).
- **False-positive protection:** conservative dHash threshold (≤6) plus an explicit regression test asserting two unrelated images never share a group; exact-sha pairs are excluded from perceptual double-grouping (a bug found by the first test run and fixed).
- **Video limits:** duration ≤600 s, ≤6 frames/video, frame longest edge ≤640 px (never upscaled), per-call ffprobe/ffmpeg timeouts (30 s/45 s), single-frame-per-invocation extraction so one slow frame can't stall the job; `video_too_long`, `video_probe_failed`, `video_frame_failed` are honest per-asset outcomes.
- **Frames are derived assets:** `type: "frame"`, `extraction_status: "extracted_frame"`, parent reference + timestamp in `detail`, `date_status: "unknown"` (a frame timestamp is NOT a publication date), their own sha256, written under `<job>/frames/<asset_id>/`.
- **Safety:** original media files are never modified; unsafe manifest paths (absolute/`..`) are reported as `missing_file`, never followed; corrupted images are caught by full-decode integrity checks.
- **Compatibility:** enrichment is purely additive — `schema_version` stays `3.0-unified`; all Phase 2/3 header/asset fields are preserved (asserted by tests, verified against a real pre-Phase-4 manifest).

## 4. Test results and evidence

`python -m pytest tests -q` → **43 passed** (29 Phase 1–3 regressions + 14 new):

Exact duplicates (explainable representative, deterministic tie-break) · near duplicates (resized/JPEG re-encode grouped perceptually with recorded Hamming distance) · **false-positive guard** (distinct images never grouped) · corrupted image (honest outcome, no crash) · unsupported SVG (expected, not an error) · low-resolution flagged · blur + exposure metrics deterministic · **video frames** (bounded count, on-disk frames, parent references, middle-frame thumbnail, disk validation clean) · video duration limit · invalid video → `video_probe_failed` · missing + unsafe paths · verified/unknown date categories · manifest compatibility (schema + original fields + partial status) · source isolation (corrupted social asset never blocks website analysis).

One real bug found by tests and fixed: identical-byte images were double-grouped (exact + perceptual); the perceptual pass now skips exact-grouped pairs.

## 5. Live smoke-test evidence (bounded, authorized)

**Real end-to-end job `a46ad72…`** (Wikipedia + consent-gated barkbus socials, max 4 social files): collection as in Phase 3 (website 3 files, IG 1 via fallback, FB 3 photos, honest gates) → **preprocessing `completed`**: 4 real images analyzed (the Facebook photos — e.g. 1080×1080, sharpness 0.0216, mean_luma 135.3, contrast 63.36, all crop windows feasible), 3 SVGs honestly `unsupported_format`, 0 problems, 0 duplicate groups, all dates `unknown` (no verified timestamps — never invented). Enriched `manifest.json` written on **D:**; `validate_against_disk` → **0 problems**.

**Project-owned synthetic video on D:** (FFmpeg `lavfi` testsrc, 3 s h264): probed (duration 3.0 s, 320×240), **6 bounded frames extracted to `D:\pgm-media-storage\jobs\phase4-video-smoke\frames\…`** (all verified on the host mount), representative thumbnail = middle frame, status `completed`.

**Infrastructure battery:** worker `/health` ok, new n8n `/healthz` ok, legacy n8n `/healthz` ok, authenticated probe HTTP 200 `auth=verified` + sentinel, containers healthy and untouched, pre-Phase-4 jobs readable with original manifests (backward compatible), all volumes/backups intact, C: old storage untouched.

## 6. Example enriched manifest (real live sample, trimmed)

```json
{
  "asset_id": "fb_0001_67c48d0bed10", "platform": "facebook",
  "local_path": "facebook/facebook-67c48d0bed10.jpg",
  "sha256": "67c48d0bed10…", "date_status": "unknown",
  "rights_status": "unverified",
  "quality": {
    "integrity": "ok", "width": 1080, "height": 1080, "aspect_ratio": 1.0,
    "megapixels": 1.166, "usable_size": true, "exif_orientation": null,
    "mean_luma": 135.3, "contrast_std": 63.36,
    "clipped_shadows_pct": 0.0, "clipped_highlights_pct": 3.61,
    "sharpness": 0.0216,
    "crop_windows": { "16:9": {"width":1080,"height":607,"x":0,"y":236,"feasible":true}, "…": "…" }
  },
  "phash": "…",
  "duplicate_group_id": "dg_0002", "representative": true,
  "date_category": "unknown",
  "preprocessing": { "outcome": "processed" }
}
```

Header addition: `"preprocessing": {"engine": "phase4-v1", "status": "completed",
"images": {...}, "videos": {...}, "duplicate_groups": [...], "notes": […], "warnings": []}`.

Video parent sample: `"video_probe": {"duration_s": 3.0, "width": 320, "height": 240, "codec": "h264"}`,
`"frames": [{"frame_asset_id": "web_0001_clip_f001", "timestamp_s": 0.43, "local_path": "frames/web_0001_clip/frame_001.jpg", …}]`,
`"representative_thumbnail": {"local_path": "frames/web_0001_clip/frame_004.jpg", …}`.

## 7. Performance and resource limits

| Limit | Value |
|---|---|
| Analysis copy | ≤512 px longest edge (memory bounded; originals untouched) |
| Video duration cap | 600 s (longer → honest `video_too_long`) |
| Frames per video | ≤6, evenly spaced, one ffmpeg call per frame |
| Frame size | ≤640 px longest edge, never upscaled, JPEG q=3 |
| ffprobe / ffmpeg timeouts | 30 s / 45 s per call |
| Pur-Python metrics | O(analyze 512²) per image (~tens of ms in-container) |
| Manifest writes | atomic temp-file + rename; recovery-safe |

## 8. Known limitations

1. Perceptual dedup uses a single conservative dHash (64-bit). Subtle re-crops or heavy filters beyond the threshold may not group; grouped results always record the distance, and unrelated images are guarded by test.
2. Videos longer than 600 s are skipped honestly (bounded by design); no audio analysis.
3. EXIF orientation is reported, not rewritten (originals are never modified) — consumers must honour `exif_orientation`.
4. SVGs (common on websites) are `unsupported_format` for raster quality metrics — deliberate honesty.
5. Social items in live runs carry no verified dates → `date_category: unknown` (platform anonymous access provides no timestamps).
6. Preprocessing is single-threaded per job (bounded by design; worker already caps concurrent jobs).

## 9. Phase 5 AI readiness

The enriched manifest now provides everything the Phase 5 AI Visual Director needs, deterministically and honestly: real pixel-backed assets + extracted frames on disk with sha256, technical quality metrics (sharpness/exposure/contrast usable as pre-filters), crop windows for placement reasoning, duplicate groups with representatives (fewer redundant AI calls), verified-only date categories, rights `unverified` flags for the rights gate, and honest per-asset outcomes. The two-stage MODE A/B workflow (per `docs/AI_VISUAL_DIRECTOR_V2.md`) can consume this manifest directly; no AI, Drive, or frontend functionality was implemented in Phase 4.

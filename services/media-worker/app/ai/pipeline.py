"""AI Visual Director pipeline orchestrator (MODE A -> MODE B).

Flow (per approved Step 9):
    Phase 4 enriched manifest
      -> payload selection (dedupe representatives, honest exclusions,
         bounded prioritization)
      -> MODE A batches (strict JSON validated OUTSIDE the model, bounded
         retries; failures become per-asset `unavailable`, never invented)
      -> inherited analyses for duplicate-group members
      -> MODE B final curation (validated: ids must exist in the manifest,
         selection limits enforced by deterministic truncation, rights flags
         derived from the manifest — a model can never hide a rights review)
      -> curation.json written atomically next to manifest.json with full
         usage, coverage, warnings and a clear `mock` marker when the
         provider is mocked.

Failure honesty: extraction results are never destroyed. Budget exhaustion,
provider unavailability or total MODE A failure marks the AI stage failed
with precise reasons; partial batch failures keep analyzed results and are
reported as partial.
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.ai.payload import (
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_IMAGE_DIM,
    Candidate,
    build_image_payloads,
    sample_candidate_frames,
    select_ai_candidates,
)
from app.ai.prompts import (
    PROMPT_VERSION,
    build_mode_a_prompt,
    build_mode_b_prompt,
    get_prompt_metadata,
    system_prompt_for_mode,
)
from app.ai.provider import (
    AIProviderConfig,
    BudgetExceededError,
    ImagePayload,
    MockVisionProvider,
    OpenAICompatibleProvider,
    UsageTracker,
    VisionProvider,
    build_provider,
    scrub_secrets,
)
from app.ai.schemas import (
    SCHEMA_VERSION,
    Category,
    ModeAResponse,
    ModeBResponse,
    breakdown_matches_score,
)
from app.manifest import atomic_write_json
from app.publication import date_eligibility, window_bounds
from app import ai_execution

HERO_MAX = 3
_JSON_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


@dataclass
class AICurationResult:
    status: str            # completed | partial | failed | disabled | provider_unavailable
    job_id: str
    provider: str = ""
    model: str = ""
    mock: bool = False
    mode_a_items: list[dict[str, Any]] = field(default_factory=list)
    mode_b: dict[str, Any] | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "mock": self.mock,
            "coverage": self.coverage,
            "usage": self.usage,
            "warnings": self.warnings[:30],
            "errors": self.errors[:30],
        }


def extract_json_text(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = _JSON_FENCE_RE.sub("", text).strip()
    return text


def _parse_mode_a(raw: str, batch_ids: set[str]) -> tuple[ModeAResponse | None, list[str], list[str]]:
    """Parse + strictly validate one MODE A response against the batch.

    Hard failures (hallucinated ids, duplicate items) are rejected. A model
    that reports a quality_score inconsistent with its own breakdown is
    repaired deterministically from that breakdown (no invented values) with
    a recorded warning.
    """
    problems: list[str] = []
    warnings: list[str] = []
    try:
        data = json.loads(extract_json_text(raw))
    except json.JSONDecodeError as exc:
        return None, [f"invalid JSON: {str(exc)[:120]}"], []
    try:
        parsed = ModeAResponse.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - validation failure detail
        return None, [f"schema violation: {str(exc)[:200]}"], []
    seen: set[str] = set()
    for item in parsed.items:
        if item.asset_id not in batch_ids:
            problems.append(f"hallucinated asset id: {item.asset_id}")
            continue
        if item.asset_id in seen:
            problems.append(f"duplicate response for asset: {item.asset_id}")
            continue
        seen.add(item.asset_id)
        if not breakdown_matches_score(item):
            b = item.score_breakdown
            total = round(
                b.technical_quality
                + b.business_relevance_authenticity
                + b.composition_usability
                + b.emotion_aesthetics
                + b.distinctiveness_collection_fit
                + b.evidence_confidence,
                1,
            )
            warnings.append(
                f"score breakdown normalized for {item.asset_id}: model reported "
                f"{item.quality_score}, breakdown sums to {total}"
            )
            item.quality_score = max(0.0, min(100.0, total))
        if item.visual_analysis_status == "analyzed" and item.video_review_status == "full_video_reviewed":
            problems.append(f"unearned full_video_reviewed: {item.asset_id}")
    if problems:
        return None, problems, warnings
    return parsed, [], warnings


def _rights_review_ids(manifest: dict[str, Any], selected: list[str]) -> list[str]:
    unverified = {
        a["asset_id"]
        for a in manifest.get("assets", [])
        if (a.get("rights_status") or "unverified") != "verified"
    }
    return [aid for aid in selected if aid in unverified]


def _category_refs(response: ModeBResponse) -> set[str]:
    """All asset ids actually placed in MODE B categories (post-truncation)."""
    return (
        {h.asset_id for h in response.hero_selection}
        | {i.asset_id for i in response.salon_images}
        | {i.asset_id for i in response.founder_and_team_images}
        | {i.asset_id for i in response.service_images}
        | {i.asset_id for i in response.grooming_transformations}
        | {i.asset_id for i in response.gallery_images}
        | {v.asset_id for v in response.video_recommendations}
    )


def _truncate_to_limits(
    response: ModeBResponse,
    *,
    max_images: int,
    max_videos: int,
) -> tuple[ModeBResponse, list[str]]:
    """Deterministic enforcement of configured selection maxima. Keeps
    ranked order (hero first); records every truncation."""
    warnings: list[str] = []
    # Hero candidates are selected assets too: enforce both HERO_MAX (3) and max_images.
    hero_cap = min(HERO_MAX, max_images)
    if len(response.hero_selection) > hero_cap:
        dropped = [h.asset_id for h in response.hero_selection[hero_cap:]]
        response.hero_selection = response.hero_selection[:hero_cap]
        for new_rank, hero in enumerate(response.hero_selection, start=1):
            hero.rank = new_rank
        warnings.append(
            f"hero_selection truncated to max={hero_cap}; dropped: {dropped}"
        )
    image_groups = [
        [h.asset_id for h in response.hero_selection],
        [i.asset_id for i in response.salon_images],
        [i.asset_id for i in response.founder_and_team_images],
        [i.asset_id for i in response.service_images],
        [i.asset_id for i in response.grooming_transformations],
        [i.asset_id for i in response.gallery_images],  # includes emotional-storytelling items
    ]
    flat: list[str] = []
    for group in image_groups:
        for aid in group or []:
            if aid not in flat:
                flat.append(aid)
    if len(flat) > max_images:
        keep = set(flat[:max_images])

        def cut(items: list, attr: str = "asset_id") -> list:
            return [x for x in items if getattr(x, attr) in keep]

        removed = [aid for aid in flat if aid not in keep]
        warnings.append(
            f"selection truncated to max_selected_images={max_images}; dropped: {removed}"
        )
        response.salon_images = cut(response.salon_images)
        response.founder_and_team_images = cut(response.founder_and_team_images)
        response.service_images = cut(response.service_images)
        response.grooming_transformations = cut(response.grooming_transformations)
        response.gallery_images = cut(response.gallery_images)
        # hero is never truncated (≤3 by schema); pairs may lose ids
        response.before_after_pairs = [
            p for p in response.before_after_pairs
            if p.before_asset_id in keep and p.after_asset_id in keep
        ]
    videos = [v.asset_id for v in response.video_recommendations]
    if len(videos) > max_videos:
        response.video_recommendations = response.video_recommendations[:max_videos]
        warnings.append(
            f"video recommendations truncated to max_selected_videos={max_videos}"
        )
    return response, warnings


def run_started_curation(job_dir, job_id, *, processing_consent, start_etag, on_claimed=None, **options):
    """Internal post-collection dispatch. No intake body supplies consent or ledger location."""
    with ai_execution.execution(job_dir, job_id, processing_consent, start_etag) as staging:
        if callable(on_claimed):
            on_claimed()
        result = run_ai_curation(staging, job_id, **options)
        with ai_execution.publication():
            if result.mode_b is not None and (staging / "curation.json").is_file():
                os.link(staging / "curation.json", Path(job_dir) / "curation.json")
            return result


def _pack_image_batches(
    candidates: list[Candidate],
    *,
    max_candidates: int,
    max_images: int,
) -> list[list[Candidate]]:
    """Pack candidates into batches bounded by BOTH candidate count and total image/frame count."""
    cand_cap = max(1, int(max_candidates))
    img_cap = max(1, int(max_images))
    batches: list[list[Candidate]] = []
    current: list[Candidate] = []
    current_imgs = 0
    for raw_cand in candidates:
        cand = sample_candidate_frames(raw_cand, img_cap)
        cost = max(1, len(cand.payload_files))
        if current and (len(current) >= cand_cap or current_imgs + cost > img_cap):
            batches.append(current)
            current = []
            current_imgs = 0
        current.append(cand)
        current_imgs += cost
    if current:
        batches.append(current)
    return batches


def run_ai_curation(
    job_dir: str | Path,
    job_id: str,
    *,
    business_context: dict[str, Any] | None = None,
    user_selection_notes: str | None = None,
    include_undated_website: bool = False,
    max_images: int = 30,
    max_videos: int = 10,
    max_assets: int = 40,
    batch_size: int = 6,
    max_images_per_request: int | None = None,
    config: AIProviderConfig | None = None,
    provider: VisionProvider | None = None,
    tracker: UsageTracker | None = None,
    max_image_dim: int | None = None,
    max_image_bytes: int | None = None,
    validated_mode_a: ModeAResponse | None = None,
) -> AICurationResult:
    job_dir = Path(job_dir)
    manifest_path = job_dir / "manifest.json"
    result = AICurationResult(job_id=job_id, status="failed")
    try:
        ai_execution.require_pipeline(job_dir, job_id)
    except ai_execution.InferenceHeld as exc:
        result.errors.append(str(exc))
        return result
    business_context = business_context or {}
    raw_manifest = manifest_path.read_bytes() if manifest_path.is_file() else b""
    ai_execution.check_inputs(raw_manifest)
    manifest = json.loads(raw_manifest) if raw_manifest else None

    cfg = config or AIProviderConfig.from_env()
    max_image_dim = max_image_dim if max_image_dim is not None else cfg.max_image_dim
    max_image_bytes = max_image_bytes if max_image_bytes is not None else cfg.max_image_bytes
    img_cap_per_req = (
        max_images_per_request
        if max_images_per_request is not None
        else cfg.max_images_per_request
    )
    if cfg.bounded_acceptance:
        max_image_dim = min(max_image_dim, cfg.max_image_dim)
        max_image_bytes = min(max_image_bytes, cfg.max_image_bytes)
        img_cap_per_req = 1
        if (job_dir / "curation.json").exists():
            result.errors.append("Bounded acceptance requires a fresh job directory")
            return result
    if provider is None:
        provider, status = build_provider(cfg)
        if provider is None:
            result.status = "disabled" if status == "disabled" else "provider_unavailable"
            result.errors.append(
                "AI provider not configured (set AI_PROVIDER/AI_MODEL/AI_ENDPOINT/AI_API_KEY "
                "or AI_PROVIDER=mock); no AI analysis was performed."
            )
            return result
    result.provider = provider.name
    result.model = cfg.model or getattr(provider, "model", "") or provider.name
    result.mock = isinstance(provider, MockVisionProvider)
    tracker = tracker or UsageTracker(cfg)

    if manifest is None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bounds = window_bounds(manifest)
    manifest_ids = {
        a["asset_id"]: a
        for a in manifest.get("assets", [])
        if a.get("extraction_status") in ("downloaded", "duplicate")
        and (
            date_eligibility(a, bounds) == "in_window"
            or (
                include_undated_website
                and a.get("platform") == "website"
                and date_eligibility(a, bounds) == "unknown_date"
            )
        )
    }

    selection = select_ai_candidates(
        manifest,
        job_dir,
        max_assets=max_assets,
        include_undated_website=include_undated_website,
    )
    allowed = {c.asset["asset_id"] for c in selection.candidates} | set(selection.inherited)
    ai_execution.check_inputs(raw_manifest, allowed)
    manifest_ids = {aid: asset for aid, asset in manifest_ids.items() if aid in allowed}
    for exc in selection.excluded:
        result.warnings.append(f"excluded {exc['asset_id']}: {exc['reason']}")
    for member, rep in selection.inherited.items():
        result.warnings.append(f"duplicate member {member} inherits analysis from {rep}")
    for aid in selection.overflow_cut:
        result.warnings.append(f"overflow cut (asset cap {max_assets}): {aid}")

    candidates: list[Candidate] = selection.candidates
    if cfg.bounded_acceptance and (
        len(manifest.get("assets", [])) != 1 or len(candidates) != 1 or candidates[0].kind != "image"
    ):
        result.errors.append("Bounded acceptance requires exactly one eligible image in the manifest")
        return result
    if not candidates:
        result.status = "failed"
        result.errors.append("no AI-ready assets (all excluded, corrupted, or unavailable)")
        return result

    if cfg.mode_b_only != (validated_mode_a is not None):
        result.errors.append("MODE B-only recovery requires a validated MODE A checkpoint")
        return result
    if validated_mode_a is not None and [
        (item.asset_id, item.visual_analysis_status) for item in validated_mode_a.items
    ] != [(candidates[0].asset["asset_id"], "analyzed")]:
        result.errors.append("Checkpoint analysis does not match the authorized manifest asset")
        return result

    use_system_prompt = isinstance(provider, OpenAICompatibleProvider) and not cfg.bounded_acceptance
    prompt_meta = get_prompt_metadata()

    # --- MODE A batches ---------------------------------------------------
    mode_a_items = [item.model_dump() for item in validated_mode_a.items] if validated_mode_a else []
    injection_hits: list[str] = []
    batch_failures = 0
    packed_batches = (
        []
        if cfg.mode_b_only
        else _pack_image_batches(candidates, max_candidates=batch_size, max_images=img_cap_per_req)
    )
    total_batches = len(packed_batches)
    for batch in packed_batches:
        batch_dicts = [{"asset": c.asset, "kind": c.kind, "frame_timestamps": c.frame_timestamps} for c in batch]
        prompt, hits = build_mode_a_prompt(
            job_id,
            batch_dicts,
            user_selection_notes=user_selection_notes,
            include_directive=not use_system_prompt,
        )
        injection_hits.extend(hits)
        payloads: list[ImagePayload] = []
        for cand in batch:
            try:
                payloads.extend(
                    build_image_payloads(
                        cand, max_dim=max_image_dim, max_bytes=max_image_bytes
                    )
                )
            except Exception as exc:  # noqa: BLE001 — per-candidate prep failure
                result.warnings.append(
                    f"payload prep failed {cand.asset['asset_id']}: {scrub_secrets(str(exc))[:120]}"
                )
        batch_ids = {c.asset["asset_id"] for c in batch}
        supplied_ids = {p.asset_id for p in payloads}
        missing_supply = batch_ids - supplied_ids
        for aid in sorted(missing_supply):
            mode_a_items.append(
                {
                    "asset_id": aid,
                    "visual_analysis_status": "unavailable",
                    "selection_reason": "pixels could not be supplied to the model",
                }
            )
        # Never call the provider for a MODE A batch with zero image payloads
        # (an empty image list would masquerade as text-only MODE B).
        if not payloads:
            batch_failures += 1
            result.warnings.append("MODE A batch skipped: zero valid image payloads prepared")
            continue

        parsed: ModeAResponse | None = None
        attempt_errors: list[str] = []
        if cfg.bounded_acceptance and len(payloads) != 1:
            result.errors.append("Could not prepare the one bounded acceptance image")
            return result
        for attempt in range(ai_execution.retry_limit(cfg.max_retries) + 1):
            try:
                with (nullcontext() if isinstance(provider, OpenAICompatibleProvider)
                      else ai_execution.attempt("A", supplied_ids, attempt_index=attempt)):
                    if use_system_prompt:
                        completion = provider.complete(
                            prompt,
                            payloads,
                            tracker,
                            system_prompt=system_prompt_for_mode("A"),
                            attempt_offset=attempt,
                        )
                    elif isinstance(provider, OpenAICompatibleProvider):
                        completion = provider.complete(prompt, payloads, tracker, attempt_offset=attempt)
                    else:
                        completion = provider.complete(prompt, payloads, tracker)
            except ai_execution.InferenceHeld as exc:
                result.errors.append(str(exc))
                return result
            except BudgetExceededError as exc:
                result.errors.append(str(exc))
                _finalize_unavailable(batch, supplied_ids, mode_a_items)
                batch_failures += 1
                attempt_errors = []
                break
            except Exception as exc:  # noqa: BLE001 — provider failure honesty
                attempt_errors.append(scrub_secrets(str(exc))[:160])
                if not ai_execution.can_retry_http(0):
                    break
                continue
            parsed, problems, score_warnings = _parse_mode_a(completion.text, supplied_ids)
            result.warnings.extend(score_warnings)
            if parsed is not None:
                if cfg.bounded_acceptance and not result.mock:
                    ai_execution.checkpoint_analysis(parsed, batch_ids, result.model, cfg.api_key, completion.usage)
                break
            attempt_errors.extend(problems)
        if parsed is None:
            batch_failures += 1
            result.warnings.append(
                f"MODE A batch failed after {ai_execution.retry_limit(cfg.max_retries) + 1} attempt(s): "
                + "; ".join(attempt_errors[:3])
            )
            for cand in batch:
                if cand.asset["asset_id"] in supplied_ids:
                    mode_a_items.append(
                        {
                            "asset_id": cand.asset["asset_id"],
                            "visual_analysis_status": "unavailable",
                            "selection_reason": "model response failed validation; no invented analysis",
                        }
                    )
            continue
        returned_ids: set[str] = set()
        for item in parsed.items:
            returned_ids.add(item.asset_id)
            mode_a_items.append(item.model_dump())
        for omitted_id in sorted(supplied_ids - returned_ids):
            result.warnings.append(f"MODE A omitted asset {omitted_id}; marked unavailable")
            mode_a_items.append(
                {
                    "asset_id": omitted_id,
                    "visual_analysis_status": "unavailable",
                    "selection_reason": "model omitted asset from batch analysis",
                }
            )

    analyzed = [i for i in mode_a_items if i.get("visual_analysis_status") == "analyzed"]
    if injection_hits:
        result.warnings.append(
            f"prompt-injection patterns scrubbed from captions: {len(injection_hits)}"
        )
    if not analyzed:
        result.status = "failed"
        result.errors.append("all MODE A batches failed; no curation possible")
        result.mode_a_items = mode_a_items
        result.usage = tracker.as_dict()
        result.coverage = {
            "assets_supplied": len(candidates),
            "assets_analyzed": 0,
            "batches_total": total_batches,
            "batches_failed": batch_failures,
            "mode_status": "failed",
            "prompt_version": PROMPT_VERSION,
            "provider": result.provider,
            "model": result.model,
            "mock": result.mock,
        }
        return result

    # --- duplicate members inherit the representative's analysis ----------
    inherited = 0
    by_id = {i["asset_id"]: i for i in analyzed}
    for member, rep in selection.inherited.items():
        if rep in by_id:
            inherited_item = dict(by_id[rep])
            inherited_item["asset_id"] = member
            inherited_item["selection_reason"] = (
                f"analysis inherited from duplicate representative {rep}"
            )
            inherited_item["analysis_inherited_from"] = rep
            mode_a_items.append(inherited_item)
            inherited += 1

    # --- MODE B ------------------------------------------------------------
    mode_b_prompt = build_mode_b_prompt(
        job_id,
        analyzed,
        business_context,
        max_images=max_images,
        max_videos=max_videos,
        asset_metadata=manifest_ids,
        compact_output=(not cfg.bounded_acceptance and cfg.mode_b_max_output_tokens <= 1000),
        user_selection_notes=user_selection_notes,
        include_directive=not use_system_prompt,
    )
    analyzed_valid_ids = {
        i["asset_id"]
        for i in mode_a_items
        if i.get("visual_analysis_status") == "analyzed" and i["asset_id"] in manifest_ids
    }
    mode_b: ModeBResponse | None = None
    mode_b_errors: list[str] = []
    for attempt in range(ai_execution.retry_limit(cfg.max_retries) + 1):
        try:
            with (nullcontext() if isinstance(provider, OpenAICompatibleProvider)
                  else ai_execution.attempt("B", [], attempt_index=attempt)):
                if use_system_prompt:
                    completion = provider.complete(
                        mode_b_prompt,
                        [],
                        tracker,
                        system_prompt=system_prompt_for_mode("B"),
                        attempt_offset=attempt,
                    )
                elif isinstance(provider, OpenAICompatibleProvider):
                    completion = provider.complete(mode_b_prompt, [], tracker, attempt_offset=attempt)
                else:
                    completion = provider.complete(mode_b_prompt, [], tracker)
        except ai_execution.InferenceHeld as exc:
            result.errors.append(str(exc))
            return result
        except BudgetExceededError as exc:
            result.errors.append(str(exc))
            break
        except Exception as exc:  # noqa: BLE001
            mode_b_errors.append(scrub_secrets(str(exc))[:160])
            if not ai_execution.can_retry_http(0):
                break
            continue
        try:
            data = json.loads(extract_json_text(completion.text))
            # Normalize harmless model wording drift (never inventing asset ids,
            # facts, or hiding rights) with recorded warnings.
            if not cfg.bounded_acceptance and isinstance(data, dict):
                if isinstance(data.get("missing_assets"), list):
                    allowed = set(Category.__args__)
                    invalid = [
                        m for m in data["missing_assets"]
                        if not isinstance(m, dict) or m.get("category") not in allowed
                    ]
                    if invalid:
                        data["missing_assets"] = [
                            m for m in data["missing_assets"] if m not in invalid
                        ]
                        result.warnings.append(
                            f"MODE B: dropped {len(invalid)} missing_assets item(s) with "
                            "a category outside the V2.0 enum"
                        )
                normalized_status = 0
                curated_fields = {
                    "asset_id", "reason", "recommended_section", "selection_status",
                    "focal_point", "crop_notes", "limitations",
                }
                team_fields = curated_fields | {"identity_verification"}
                hero_fields = {
                    "rank", "asset_id", "reason", "composition_notes", "desktop_crop",
                    "mobile_crop", "focal_point", "text_overlay_suitability", "limitations",
                }
                video_fields = {
                    "asset_id", "filename", "source", "video_review_status", "evaluated_frames",
                    "quality_score", "recommended_section", "selection_status", "reason", "limitations",
                }
                section_map = [
                    ("salon_images", curated_fields),
                    ("founder_and_team_images", team_fields),
                    ("service_images", curated_fields),
                    ("grooming_transformations", curated_fields),
                    ("gallery_images", curated_fields),
                    ("hero_selection", hero_fields),
                    ("video_recommendations", video_fields),
                ]
                for sec_name, allowed_keys in section_map:
                    sec_items = data.get(sec_name)
                    if not isinstance(sec_items, list):
                        continue
                    cleaned_items = []
                    for entry in sec_items:
                        if not isinstance(entry, dict):
                            continue
                        item_copy = {k: v for k, v in entry.items() if k in allowed_keys}
                        if item_copy.get("selection_status") == "selected":
                            item_copy["selection_status"] = "recommended"
                            normalized_status += 1
                        cleaned_items.append(item_copy)
                    data[sec_name] = cleaned_items
                if normalized_status:
                    result.warnings.append(
                        f"MODE B: normalized selection_status 'selected' -> 'recommended' on {normalized_status} item(s)"
                    )
                # Ensure truthful pipeline-computed analysis_coverage metadata.
                cov = data.get("analysis_coverage")
                if not isinstance(cov, dict):
                    cov = {}
                data["analysis_coverage"] = {
                    "assets_supplied": len(candidates),
                    "assets_analyzed": len(analyzed),
                    "inherited_from_duplicates": inherited,
                    "unavailable": sum(
                        1 for i in mode_a_items if i.get("visual_analysis_status") == "unavailable"
                    ),
                    "prompt_version": PROMPT_VERSION,
                    "provider": result.provider,
                    "model": result.model,
                    "mock": result.mock,
                    "usage": tracker.as_dict(),
                }
            candidate_response = ModeBResponse.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            mode_b_errors.append(f"MODE B validation: {str(exc)[:200]}")
            continue
        # Cross-manifest and analyzed-only validation
        if candidate_response.job_id != job_id:
            mode_b_errors.append(f"MODE B job_id mismatch: {candidate_response.job_id!r} != {job_id!r}")
            continue
        unearned_video = [
            v.asset_id for v in candidate_response.video_recommendations
            if v.video_review_status == "full_video_reviewed"
        ]
        if unearned_video:
            mode_b_errors.append(f"MODE B unearned full_video_reviewed: {unearned_video[:5]}")
            continue
        unknown_rejected = {r.asset_id for r in candidate_response.rejected_assets} - set(manifest_ids)
        if unknown_rejected:
            mode_b_errors.append(f"MODE B rejected unknown asset ids: {sorted(unknown_rejected)[:5]}")
            continue
        selected = set(candidate_response.selected_asset_ids)
        hallucinated = selected - analyzed_valid_ids
        if hallucinated:
            mode_b_errors.append(
                f"MODE B selected unknown asset ids (or unanalyzed): {sorted(hallucinated)[:5]}"
            )
            continue
        referenced = _category_refs(candidate_response)
        unknown_refs = referenced - analyzed_valid_ids
        if unknown_refs:
            mode_b_errors.append(
                f"MODE B referenced unknown asset ids (or unanalyzed): {sorted(unknown_refs)[:5]}"
            )
            continue
        pair_refs = set()
        for pair in candidate_response.before_after_pairs:
            pair_refs.update({pair.before_asset_id, pair.after_asset_id})
        unknown_pairs = pair_refs - analyzed_valid_ids
        if unknown_pairs:
            mode_b_errors.append(
                f"MODE B pair references unknown asset ids (or unanalyzed): {sorted(unknown_pairs)[:5]}"
            )
            continue
        candidate_response, cut_warnings = _truncate_to_limits(
            candidate_response, max_images=max_images, max_videos=max_videos
        )
        result.warnings.extend(cut_warnings)
        # Derive final selection from the POST-TRUNCATION placements only.
        # A stale pre-truncation reference set would re-expand the list past
        # the configured limits (found by the selection-limit regression).
        final_selected = sorted(_category_refs(candidate_response))
        if final_selected != sorted(candidate_response.selected_asset_ids):
            result.warnings.append(
                "selected_asset_ids normalized to actual category placements "
                "(no model-only extras retained)"
            )
        candidate_response.selected_asset_ids = final_selected
        candidate_response.rights_review_required_ids = _rights_review_ids(
            manifest, final_selected
        )
        candidate_response.analysis_coverage.usage = tracker.as_dict()
        candidate_response.analysis_coverage.mock = result.mock
        mode_b = candidate_response
        break

    if mode_b is None:
        result.status = "failed"
        result.errors.extend(mode_b_errors[:5] or ["MODE B failed for unknown reason"])
        # Honest partial bookkeeping: MODE A results and usage DID happen.
        result.mode_a_items = mode_a_items
        result.usage = tracker.as_dict()
        result.coverage = {
            "assets_supplied": len(candidates),
            "assets_analyzed": len(analyzed),
            "inherited_from_duplicates": inherited,
            "unavailable": sum(
                1 for i in mode_a_items if i.get("visual_analysis_status") == "unavailable"
            ),
            "batches_total": total_batches,
            "batches_failed": batch_failures,
            "mode_b_status": "failed",
            "prompt_version": PROMPT_VERSION,
            "provider": result.provider,
            "model": result.model,
            "mock": result.mock,
        }
        return result

    curation = mode_b.model_dump()
    envelope = {
        "status": "completed" if batch_failures == 0 else "partial",
        "job_id": job_id,
        "mode_a_items": mode_a_items,
        "curation": curation,
        "ai_envelope": {
            "generated_at": datetime.now(UTC).isoformat(),
            "prompt_version": prompt_meta["prompt_version"],
            "prompt_sha256": prompt_meta["prompt_sha256"],
            "prompt_source": prompt_meta["prompt_source"],
            "schema_version": SCHEMA_VERSION,
            "include_undated_website": bool(include_undated_website),
            "provider": result.provider,
            "model": result.model,
            "mock": result.mock,
            "usage": tracker.as_dict(),
            "warnings": result.warnings[:50],
            "errors": result.errors[:50],
            "excluded_assets": selection.excluded,
            "inherited_from_duplicates": selection.inherited,
            "overflow_cut": selection.overflow_cut,
        },
    }
    with ai_execution.publication():
        atomic_write_json(job_dir / "curation.json", envelope)
    result.mode_a_items = mode_a_items
    result.mode_b = curation
    result.status = "completed" if batch_failures == 0 else "partial"
    result.coverage = {
        "assets_supplied": len(candidates),
        "assets_analyzed": len(analyzed),
        "inherited_from_duplicates": inherited,
        "unavailable": sum(
            1 for i in mode_a_items if i.get("visual_analysis_status") == "unavailable"
        ),
        "batches_total": total_batches,
        "batches_failed": batch_failures,
        "prompt_version": PROMPT_VERSION,
        "provider": result.provider,
        "model": result.model,
        "mock": result.mock,
    }
    result.usage = tracker.as_dict()
    return result


def _finalize_unavailable(
    batch: list[Candidate],
    supplied_ids: set[str],
    mode_a_items: list[dict[str, Any]],
) -> None:
    for cand in batch:
        if cand.asset["asset_id"] in supplied_ids:
            mode_a_items.append(
                {
                    "asset_id": cand.asset["asset_id"],
                    "visual_analysis_status": "unavailable",
                    "selection_reason": "AI budget exhausted before analysis",
                }
            )

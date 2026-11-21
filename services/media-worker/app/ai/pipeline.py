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
    select_ai_candidates,
)
from app.ai.prompts import PROMPT_VERSION, build_mode_a_prompt, build_mode_b_prompt
from app.ai.provider import (
    AIProviderConfig,
    BudgetExceededError,
    ImagePayload,
    MockVisionProvider,
    UsageTracker,
    VisionProvider,
    build_provider,
    scrub_secrets,
)
from app.ai.schemas import (
    SCHEMA_VERSION,
    ModeAResponse,
    ModeBResponse,
    breakdown_matches_score,
)
from app.manifest import atomic_write_json

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


def _parse_mode_a(raw: str, batch_ids: set[str]) -> tuple[ModeAResponse | None, list[str]]:
    """Parse + strictly validate one MODE A response against the batch."""
    problems: list[str] = []
    try:
        data = json.loads(extract_json_text(raw))
    except json.JSONDecodeError as exc:
        return None, [f"invalid JSON: {str(exc)[:120]}"]
    try:
        parsed = ModeAResponse.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - validation failure detail
        return None, [f"schema violation: {str(exc)[:200]}"]
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
            problems.append(f"score breakdown mismatch: {item.asset_id}")
        if item.visual_analysis_status == "analyzed" and item.video_review_status == "full_video_reviewed":
            problems.append(f"unearned full_video_reviewed: {item.asset_id}")
    if problems:
        return None, problems
    return parsed, []


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
    # Hero candidates are selected assets too: they must respect the image cap.
    if len(response.hero_selection) > max_images:
        dropped = [h.asset_id for h in response.hero_selection[max_images:]]
        response.hero_selection = response.hero_selection[:max_images]
        for new_rank, hero in enumerate(response.hero_selection, start=1):
            hero.rank = new_rank
        warnings.append(
            f"hero_selection truncated to max_selected_images={max_images}; dropped: {dropped}"
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


def run_ai_curation(
    job_dir: str | Path,
    job_id: str,
    *,
    business_context: dict[str, Any] | None = None,
    max_images: int = 30,
    max_videos: int = 10,
    max_assets: int = 40,
    batch_size: int = 6,
    config: AIProviderConfig | None = None,
    provider: VisionProvider | None = None,
    tracker: UsageTracker | None = None,
    max_image_dim: int = DEFAULT_MAX_IMAGE_DIM,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> AICurationResult:
    job_dir = Path(job_dir)
    manifest_path = job_dir / "manifest.json"
    result = AICurationResult(job_id=job_id, status="failed")
    business_context = business_context or {}

    cfg = config or AIProviderConfig.from_env()
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

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_ids = {
        a["asset_id"]: a
        for a in manifest.get("assets", [])
        if a.get("extraction_status") in ("downloaded", "extracted_frame")
    }

    selection = select_ai_candidates(manifest, job_dir, max_assets=max_assets)
    for exc in selection.excluded:
        result.warnings.append(f"excluded {exc['asset_id']}: {exc['reason']}")
    for member, rep in selection.inherited.items():
        result.warnings.append(f"duplicate member {member} inherits analysis from {rep}")
    for aid in selection.overflow_cut:
        result.warnings.append(f"overflow cut (asset cap {max_assets}): {aid}")

    candidates: list[Candidate] = selection.candidates
    if not candidates:
        result.status = "failed"
        result.errors.append("no AI-ready assets (all excluded, corrupted, or unavailable)")
        return result

    # --- MODE A batches ---------------------------------------------------
    mode_a_items: list[dict[str, Any]] = []
    injection_hits: list[str] = []
    batch_failures = 0
    total_batches = (len(candidates) + batch_size - 1) // max(1, batch_size)
    for start in range(0, len(candidates), max(1, batch_size)):
        batch = candidates[start : start + max(1, batch_size)]
        batch_dicts = [{"asset": c.asset, "kind": c.kind, "frame_timestamps": c.frame_timestamps} for c in batch]
        prompt, hits = build_mode_a_prompt(job_id, batch_dicts)
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

        parsed: ModeAResponse | None = None
        attempt_errors: list[str] = []
        for attempt in range(cfg.max_retries + 1):
            try:
                completion = provider.complete(prompt, payloads, tracker)
            except BudgetExceededError as exc:
                result.errors.append(str(exc))
                _finalize_unavailable(batch, supplied_ids, mode_a_items)
                batch_failures += 1
                attempt_errors = []
                break
            except Exception as exc:  # noqa: BLE001 — provider failure honesty
                attempt_errors.append(scrub_secrets(str(exc))[:160])
                continue
            parsed, problems = _parse_mode_a(completion.text, batch_ids)
            if parsed is not None:
                break
            attempt_errors.extend(problems)
        if parsed is None:
            batch_failures += 1
            result.warnings.append(
                f"MODE A batch failed after {cfg.max_retries + 1} attempt(s): "
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
        for item in parsed.items:
            mode_a_items.append(item.model_dump())

    analyzed = [i for i in mode_a_items if i.get("visual_analysis_status") == "analyzed"]
    if injection_hits:
        result.warnings.append(
            f"prompt-injection patterns scrubbed from captions: {len(injection_hits)}"
        )
    if not analyzed:
        result.status = "failed"
        result.errors.append("all MODE A batches failed; no curation possible")
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
    )
    mode_b: ModeBResponse | None = None
    mode_b_errors: list[str] = []
    for attempt in range(cfg.max_retries + 1):
        try:
            completion = provider.complete(mode_b_prompt, [], tracker)
        except BudgetExceededError as exc:
            result.errors.append(str(exc))
            break
        except Exception as exc:  # noqa: BLE001
            mode_b_errors.append(scrub_secrets(str(exc))[:160])
            continue
        try:
            data = json.loads(extract_json_text(completion.text))
            candidate_response = ModeBResponse.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            mode_b_errors.append(f"MODE B validation: {str(exc)[:200]}")
            continue
        # Cross-manifest validation: every selected id must exist
        valid_ids = set(by_id) | {i["asset_id"] for i in mode_a_items}
        selected = set(candidate_response.selected_asset_ids)
        hallucinated = selected - set(manifest_ids)
        if hallucinated:
            mode_b_errors.append(
                f"MODE B selected unknown asset ids: {sorted(hallucinated)[:5]}"
            )
            continue
        referenced = (
            {h.asset_id for h in candidate_response.hero_selection}
            | {i.asset_id for i in candidate_response.salon_images}
            | {i.asset_id for i in candidate_response.founder_and_team_images}
            | {i.asset_id for i in candidate_response.service_images}
            | {i.asset_id for i in candidate_response.grooming_transformations}
            | {i.asset_id for i in candidate_response.gallery_images}
            | {v.asset_id for v in candidate_response.video_recommendations}
        )
        unknown_refs = referenced - set(manifest_ids)
        if unknown_refs:
            mode_b_errors.append(
                f"MODE B referenced unknown asset ids: {sorted(unknown_refs)[:5]}"
            )
            continue
        pair_refs = set()
        for pair in candidate_response.before_after_pairs:
            pair_refs.update({pair.before_asset_id, pair.after_asset_id})
        unknown_pairs = pair_refs - set(manifest_ids)
        if unknown_pairs:
            mode_b_errors.append(
                f"MODE B pair references unknown ids: {sorted(unknown_pairs)[:5]}"
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
        return result

    curation = mode_b.model_dump()
    envelope = {
        "status": "completed" if batch_failures == 0 else "partial",
        "job_id": job_id,
        "mode_a_items": mode_a_items,
        "curation": curation,
        "ai_envelope": {
            "generated_at": datetime.now(UTC).isoformat(),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
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

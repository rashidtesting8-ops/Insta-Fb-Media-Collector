"""Strict, typed MODE A / MODE B output schemas for the AI Visual Director V2.0.

These schemas implement the machine-readable output contract of
docs/AI_VISUAL_DIRECTOR_V2.md §9 and are enforced OUTSIDE the model
(the model never grades its own homework). Structural honesty rules:

- Every score is range-bound; the 25/25/20/15/10/5 weighted breakdown must
  sum to the reported quality score (±1 for model rounding).
- `visual_analysis_status="unavailable"` is a first-class outcome: an asset
  whose pixels were not successfully supplied to the model must never carry
  an analysis.
- Video review honesty: `full_video_reviewed` may only be claimed when full
  video was actually analyzed; frame-sampled analysis is `frames_reviewed`.
- All fields are strict (extra="forbid") so models cannot smuggle in
  invented provenance.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "visual-director-v2.0"

Category = Literal[
    "hero",
    "salon_facilities",
    "founder_team",
    "service",
    "grooming_transformation",
    "before_after",
    "gallery",
    "emotional_storytelling",
    "video",
    "not_recommended",
]

VideoReviewStatus = Literal["thumbnail_only", "frames_reviewed", "full_video_reviewed"]
VerificationStatus = Literal["verified", "needs_verification", "unverified"]
AnalysisStatus = Literal["analyzed", "unavailable"]
SelectionStatus = Literal["recommended", "considered", "rejected"]
PairState = Literal["confirmed", "needs_verification", "rejected"]
IdentityVerification = Literal["verified", "unverified"]


class ScoreBreakdown(BaseModel):
    """The fixed V2.0 weighted rubric (25/25/20/15/10/5)."""

    model_config = ConfigDict(extra="forbid")

    technical_quality: float = Field(ge=0, le=25)
    business_relevance_authenticity: float = Field(ge=0, le=25)
    composition_usability: float = Field(ge=0, le=20)
    emotion_aesthetics: float = Field(ge=0, le=15)
    distinctiveness_collection_fit: float = Field(ge=0, le=10)
    evidence_confidence: float = Field(ge=0, le=5)


class ModeAItem(BaseModel):
    """Per-asset analysis (docs/AI_VISUAL_DIRECTOR_V2.md §9, MODE A)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    source: str = Field(min_length=1)
    media_type: Literal["image", "video", "frame"]
    detected_content: str = Field(min_length=1)
    possible_categories: list[Category] = Field(min_length=1)
    quality_score: float = Field(ge=0, le=100)
    score_breakdown: ScoreBreakdown
    visual_strengths: list[str] = Field(min_length=1)
    visual_weaknesses: list[str] = Field(min_length=1)
    verification_status: VerificationStatus
    visual_analysis_status: AnalysisStatus
    recommended_use: Category
    selection_status: SelectionStatus
    selection_reason: str = Field(min_length=1)
    # Video honesty fields (optional for images)
    video_review_status: VideoReviewStatus | None = None
    evaluated_frames: list[float] = Field(default_factory=list)

    @field_validator("evaluated_frames")
    @classmethod
    def _frames_non_negative(cls, values: list[float]) -> list[float]:
        if any(v < 0 for v in values):
            raise ValueError("frame timestamps must be non-negative")
        return values


class ModeAResponse(BaseModel):
    """A MODE A batch response wrapper."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["ANALYZE_BATCH"] = "ANALYZE_BATCH"
    items: list[ModeAItem] = Field(min_length=1)


class FocalPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class HeroCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1, le=3)
    asset_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    composition_notes: str = Field(min_length=1)
    desktop_crop: str = Field(min_length=1)
    mobile_crop: str = Field(min_length=1)
    focal_point: FocalPoint
    text_overlay_suitability: Literal["suitable", "possible", "unsuitable"]
    limitations: str | None = None


class CuratedImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    recommended_section: Category
    selection_status: SelectionStatus
    focal_point: FocalPoint | None = None
    crop_notes: str | None = None
    limitations: str | None = None


class TeamImage(CuratedImage):
    identity_verification: IdentityVerification = "unverified"


class BeforeAfterPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_asset_id: str = Field(min_length=1)
    after_asset_id: str = Field(min_length=1)
    state: PairState
    evidence: str = Field(min_length=1)
    reason: str = Field(min_length=1)


VideoSection = Literal[
    "hero",
    "salon_facilities",
    "founder_team",
    "service",
    "grooming_transformation",
    "gallery",
    "emotional_storytelling",
    "video",
    "story",
    "transformation",
]


class VideoRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    source: str = Field(min_length=1)
    video_review_status: VideoReviewStatus
    evaluated_frames: list[float] = Field(default_factory=list)
    quality_score: float = Field(ge=0, le=100)
    recommended_section: VideoSection
    selection_status: SelectionStatus
    reason: str = Field(min_length=1)
    limitations: str | None = None


class BusinessVisualIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style_summary: str = Field(min_length=1)
    atmosphere: str = Field(min_length=1)
    brand_personality: str = Field(min_length=1)
    key_visual_strengths: list[str] = Field(min_length=1)
    recommended_design_direction: str = Field(min_length=1)


class AnalysisCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assets_supplied: int = Field(ge=0)
    assets_analyzed: int = Field(ge=0)
    inherited_from_duplicates: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    prompt_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    mock: bool
    usage: dict[str, Any] = Field(default_factory=dict)


class DesignArtDirection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str = Field(min_length=1)
    layout_guidance: str = Field(min_length=1)
    color_and_lighting: str = Field(min_length=1)
    typography_mood: str = Field(min_length=1)
    section_pairing: str = Field(min_length=1)
    diversity_notes: str = Field(min_length=1)


class MissingAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    note: str = Field(min_length=1)


class RejectedAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FinalQualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_assessment: str = Field(min_length=1)
    strengths: list[str] = Field(min_length=1)
    weaknesses: list[str] = Field(min_length=1)
    meets_premium_standard: bool
    notes: str | None = None


class ModeBResponse(BaseModel):
    """Final curation (docs/AI_VISUAL_DIRECTOR_V2.md §9, MODE B)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    mode: Literal["FINAL_CURATE"] = "FINAL_CURATE"
    business_visual_identity: BusinessVisualIdentity
    analysis_coverage: AnalysisCoverage
    hero_selection: list[HeroCandidate] = Field(default_factory=list)
    salon_images: list[CuratedImage] = Field(default_factory=list)
    founder_and_team_images: list[TeamImage] = Field(default_factory=list)
    service_images: list[CuratedImage] = Field(default_factory=list)
    grooming_transformations: list[CuratedImage] = Field(default_factory=list)
    before_after_pairs: list[BeforeAfterPair] = Field(default_factory=list)
    gallery_images: list[CuratedImage] = Field(default_factory=list)
    video_recommendations: list[VideoRecommendation] = Field(default_factory=list)
    design_art_direction: DesignArtDirection
    missing_assets: list[MissingAsset] = Field(default_factory=list)
    rejected_assets: list[RejectedAsset] = Field(default_factory=list)
    selected_asset_ids: list[str] = Field(default_factory=list)
    rights_review_required_ids: list[str] = Field(default_factory=list)
    final_quality_assessment: FinalQualityAssessment


def breakdown_matches_score(item: ModeAItem) -> bool:
    b = item.score_breakdown
    total = (
        b.technical_quality
        + b.business_relevance_authenticity
        + b.composition_usability
        + b.emotion_aesthetics
        + b.distinctiveness_collection_fit
        + b.evidence_confidence
    )
    return abs(total - item.quality_score) <= 1.0

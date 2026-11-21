"""Phase 5 AI Visual Director package (provider-agnostic, two-mode)."""

from app.ai.payload import (
    Candidate,
    SelectionReport,
    build_image_payloads,
    convert_for_payload,
    select_ai_candidates,
)
from app.ai.pipeline import AICurationResult, run_ai_curation
from app.ai.provider import (
    AIProviderConfig,
    BudgetExceededError,
    ImagePayload,
    MockVisionProvider,
    OpenAICompatibleProvider,
    ProviderUnavailableError,
    UsageTracker,
    VisionProvider,
    build_provider,
)
from app.ai.schemas import (
    ModeAItem,
    ModeAResponse,
    ModeBResponse,
    SCHEMA_VERSION,
)

__all__ = [
    "AICurationResult",
    "AIProviderConfig",
    "BudgetExceededError",
    "Candidate",
    "ImagePayload",
    "MockVisionProvider",
    "ModeAItem",
    "ModeAResponse",
    "ModeBResponse",
    "OpenAICompatibleProvider",
    "SCHEMA_VERSION",
    "SelectionReport",
    "UsageTracker",
    "VisionProvider",
    "build_image_payloads",
    "build_provider",
    "convert_for_payload",
    "run_ai_curation",
    "select_ai_candidates",
]

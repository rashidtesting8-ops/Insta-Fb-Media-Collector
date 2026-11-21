"""Provider-agnostic multimodal AI adapter for the Visual Director pipeline.

Design:
- Configuration is environment-driven (never hard-coded): AI_PROVIDER,
  AI_MODEL, AI_ENDPOINT, AI_API_KEY, plus bounded budget knobs. Secrets are
  read from the environment and never logged, stored in manifests, or echoed.
- `OpenAICompatibleProvider` talks to any OpenAI-style chat/completions
  endpoint that accepts image_url data URLs: bounded retries with
  deterministic exponential backoff on 429/5xx/timeouts, per-request timeout,
  usage recording (tokens when the endpoint reports them), estimated cost via
  optional per-1k price env vars.
- `MockVisionProvider` is an explicitly-labeled deterministic provider used
  for offline tests and CI. It derives analyses from the *actual* supplied
  payload metadata (never claims pixels it did not receive, never invents
  assets) and is always recorded with `mock: true` and model id
  `mock-vision-v1`. It is never a silent fallback for a missing real
  provider: without configuration the AI stage reports
  `provider_unavailable`.
- A per-job usage tracker enforces request and cost ceilings before every
  call. Budget exhaustion is an honest, recorded failure — never a silent
  downgrade.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from PIL import Image

GROQ_ACCEPTANCE_MODEL = "qwen/qwen3.8-27b"
GROQ_ACCEPTANCE_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
# Reserve the full model context as an input-token upper bound, including image tokens.
GROQ_ACCEPTANCE_INPUT_TOKEN_BOUND = 131_072
# Published Qwen rates in USD per 1,000 tokens. Use higher account rates if configured.
GROQ_ACCEPTANCE_INPUT_PRICE = 0.0008
GROQ_ACCEPTANCE_OUTPUT_PRICE = 0.004

SecretPattern = re.compile(r"(?i)(authorization|api[_-]?key|bearer|token)\s*[=:]\s*\S+")
BearerPattern = re.compile(r"(?i)\bBearer\s+\S+")
_SENSITIVE_DIAGNOSTIC = re.compile(
    r"https?://[^\s\"'<>]+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|"
    r"\b(?:gsk|org|proj|sk)[_-][A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)
_PROVIDER_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RATE_VALUE = re.compile(r"\d+(?:\.\d+)?|(?:\d+(?:\.\d+)?(?:ms|s|m|h|d)){1,5}")


def scrub_secrets(text: str) -> str:
    # Bearer values first: the generic rule would replace the "Bearer" marker
    # before the value could be matched, leaving the secret in the tail.
    cleaned = BearerPattern.sub("Bearer [REDACTED]", text or "")
    return SecretPattern.sub(r"\1=[REDACTED]", cleaned)


def _safe_provider_message(text: str, credential: str) -> str:
    if credential:
        text = text.replace(credential, "[REDACTED]")
    if re.search(r"data:[^\s]*;base64|ASSET_SPECS|VALIDATED_ANALYSES|[\"']messages[\"']\s*:", text, re.I):
        return "Provider message omitted because it echoed request content"
    return " ".join(_SENSITIVE_DIAGNOSTIC.sub("[REDACTED]", scrub_secrets(text)).split())[:240]


class ProviderUnavailableError(RuntimeError):
    """Raised when a real provider is selected but not configured."""


class BudgetExceededError(RuntimeError):
    """Raised when the per-job AI budget (requests/cost) is exhausted."""


class RequestTooLargeError(RuntimeError):
    """Raised locally before any HTTP call when the full JSON body is oversized."""


class RateLimitedError(RuntimeError):
    """Raised when the provider signals an exhausted quota window; no retries."""


@dataclass
class AIUsage:
    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    rate_limit_snapshot: dict[str, str] = field(default_factory=dict)
    provider_error: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }
        if self.rate_limit_snapshot:
            result["rate_limit_snapshot"] = self.rate_limit_snapshot
        if self.provider_error:
            result["provider_error"] = self.provider_error
        return result


@dataclass
class AIProviderConfig:
    provider: str = ""          # "mock" | "openai_compatible" | "" (disabled)
    model: str = ""
    endpoint: str = ""
    api_key: str = ""
    timeout_s: int = 120
    max_retries: int = 2
    # Conservative vision batch size: several real vision models (including
    # Groq's qwen/qwen3.8-27b) accept at most 3 images per request.
    batch_size: int = 3
    max_requests_per_job: int = 40
    max_cost_usd_per_job: float = 2.0
    max_image_dim: int = 512          # conservative multimodal payload
    max_image_bytes: int = 300_000    # encoded JPEG target per image
    max_request_bytes: int = 1_000_000  # FULL JSON body incl. base64 expansion
    min_request_spacing_s: float = 2.0  # gentle pacing for free-tier RPM/TPM
    price_in_per_1k: float = 0.0
    price_out_per_1k: float = 0.0
    mode_a_max_output_tokens: int = 4000
    mode_b_max_output_tokens: int = 4000
    bounded_acceptance: bool = False

    def __post_init__(self) -> None:
        if min(self.mode_a_max_output_tokens, self.mode_b_max_output_tokens) < 1:
            raise ValueError("Output-token caps must be positive")
        if not self.bounded_acceptance:
            return
        if (self.provider, self.model, self.endpoint) != (
            "openai_compatible", GROQ_ACCEPTANCE_MODEL, GROQ_ACCEPTANCE_ENDPOINT
        ):
            raise ValueError("Bounded acceptance requires the configured Groq Qwen endpoint and model")
        for value in (self.price_in_per_1k, self.price_out_per_1k, self.max_cost_usd_per_job):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Bounded acceptance requires positive finite pricing and cost limits")
        self.price_in_per_1k = max(self.price_in_per_1k, GROQ_ACCEPTANCE_INPUT_PRICE)
        self.price_out_per_1k = max(self.price_out_per_1k, GROQ_ACCEPTANCE_OUTPUT_PRICE)
        self.max_retries = 0
        self.max_requests_per_job = min(self.max_requests_per_job, 2)
        self.max_cost_usd_per_job = min(self.max_cost_usd_per_job, 0.25)
        self.max_image_dim = min(self.max_image_dim, 256)
        self.max_image_bytes = min(self.max_image_bytes, 100_000)
        self.batch_size = 1
        self.mode_a_max_output_tokens = min(self.mode_a_max_output_tokens, 1200)
        self.mode_b_max_output_tokens = min(self.mode_b_max_output_tokens, 1800)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AIProviderConfig":
        e = env if env is not None else dict(os.environ)
        return cls(
            provider=(e.get("AI_PROVIDER") or "").strip().lower(),
            model=(e.get("AI_MODEL") or "").strip(),
            endpoint=(e.get("AI_ENDPOINT") or "").strip(),
            api_key=(e.get("AI_API_KEY") or "").strip(),
            timeout_s=int(e.get("AI_TIMEOUT_S") or 120),
            max_retries=int(e.get("AI_MAX_RETRIES") or 2),
            batch_size=int(e.get("AI_BATCH_SIZE") or 3),
            max_requests_per_job=int(e.get("AI_MAX_REQUESTS_PER_JOB") or 40),
            max_cost_usd_per_job=float(e.get("AI_MAX_COST_USD_PER_JOB") or 2.0),
            max_image_dim=int(e.get("AI_MAX_IMAGE_DIM") or 512),
            max_image_bytes=int(e.get("AI_MAX_IMAGE_BYTES") or 300_000),
            max_request_bytes=int(e.get("AI_MAX_REQUEST_BYTES") or 1_000_000),
            min_request_spacing_s=float(e.get("AI_MIN_REQUEST_SPACING_S") or 2.0),
            price_in_per_1k=float(e.get("AI_PRICE_IN_PER_1K") or 0.0),
            price_out_per_1k=float(e.get("AI_PRICE_OUT_PER_1K") or 0.0),
            mode_a_max_output_tokens=int(e.get("AI_MODE_A_MAX_OUTPUT_TOKENS") or 4000),
            mode_b_max_output_tokens=int(e.get("AI_MODE_B_MAX_OUTPUT_TOKENS") or 4000),
            bounded_acceptance=e.get("AI_BOUNDED_ACCEPTANCE") == "1",
        )


@dataclass
class ImagePayload:
    asset_id: str
    mime: str
    data: bytes
    label: str = ""           # e.g. "frame @1.50s" or "primary image"


@dataclass
class CompletionResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)


class UsageTracker:
    """Per-job budget guard: requests, tokens, estimated cost."""

    def __init__(self, config: AIProviderConfig) -> None:
        self.config = config
        self.usage = AIUsage()

    def pre_flight(self) -> None:
        if self.usage.requests >= self.config.max_requests_per_job:
            raise BudgetExceededError(
                f"AI request budget exceeded ({self.usage.requests}/"
                f"{self.config.max_requests_per_job})"
            )
        if self.usage.estimated_cost_usd >= self.config.max_cost_usd_per_job:
            raise BudgetExceededError(
                f"AI cost budget exceeded (${self.usage.estimated_cost_usd:.4f}/"
                f"${self.config.max_cost_usd_per_job:.2f})"
            )

    def record(self, usage: dict[str, Any], retries: int) -> None:
        self.usage.requests += 1
        self.usage.retries += retries
        self.record_tokens(usage)

    def reserve_request(self, max_output_tokens: int, *, retry: bool) -> float:
        self.pre_flight()
        reservation = 0.0
        if self.config.bounded_acceptance:
            reservation = self._cost(GROQ_ACCEPTANCE_INPUT_TOKEN_BOUND, max_output_tokens)
            if self.usage.estimated_cost_usd + reservation > self.config.max_cost_usd_per_job:
                raise BudgetExceededError("Worst-case next request would exceed the AI cost budget")
        self.usage.requests += 1
        self.usage.retries += int(retry)
        self.usage.estimated_cost_usd += reservation
        return reservation

    def record_tokens(self, usage: dict[str, Any], *, reservation: float = 0.0) -> None:
        self.usage.input_tokens += int(usage.get("input_tokens") or 0)
        self.usage.output_tokens += int(usage.get("output_tokens") or 0)
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        self.usage.estimated_cost_usd += self._cost(in_tok, out_tok) - reservation

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.config.price_in_per_1k + output_tokens * self.config.price_out_per_1k) / 1000

    def as_dict(self) -> dict[str, Any]:
        return self.usage.as_dict()

    def record_headers(self, headers: Any) -> None:
        """Non-secret rate-limit header snapshot (Groq documents these)."""
        try:
            for key in (
                "retry-after",
                "x-ratelimit-limit-requests",
                "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-requests",
                "x-ratelimit-remaining-tokens",
                "x-ratelimit-reset-requests",
                "x-ratelimit-reset-tokens",
            ):
                value = headers.get(key) if headers else None
                if value is not None:
                    value = str(value).strip()
                    if value != self.config.api_key and len(value) <= 40 and _RATE_VALUE.fullmatch(value):
                        self.usage.rate_limit_snapshot[key] = value
        except (AttributeError, TypeError):
            pass

    def record_http_error(self, response: requests.Response | None) -> dict[str, Any]:
        details: dict[str, Any] = {"http_status": response.status_code if response is not None else 0}
        if response is None:
            details["message"] = "Provider HTTP response unavailable"
        else:
            self.record_headers(response.headers)
            try:
                data = response.json()
            except ValueError:
                data = None
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if isinstance(error, dict):
                for key in ("code", "type"):
                    value = error.get(key)
                    if isinstance(value, str) and _PROVIDER_CODE.fullmatch(value):
                        safe = _safe_provider_message(value, self.config.api_key)
                        if _PROVIDER_CODE.fullmatch(safe):
                            details[key] = safe
            if not isinstance(message, str):
                # Do not persist an unstructured response body. Retain only known
                # categories for retry decisions with providers that send plain text.
                raw = response.text.lower()
                message = next((label for marker, label in (
                    ("quota", "provider quota exhausted"),
                    ("daily", "provider daily limit"),
                    ("too large", "provider request too large"),
                    ("rate limit", "provider rate limited"),
                ) if marker in raw), "Provider returned an unstructured error")
            details["message"] = _safe_provider_message(message, self.config.api_key)
        self.usage.provider_error = details
        return details


class VisionProvider:
    name = "abstract"

    def complete(
        self,
        prompt: str,
        images: list[ImagePayload],
        tracker: UsageTracker,
    ) -> CompletionResult:
        raise NotImplementedError


class MockVisionProvider(VisionProvider):
    """Deterministic, explicitly-labeled mock for offline tests and CI.

    It reads the batch's embedded asset_specs JSON (written by the prompt
    builder) and derives a plausible, schema-valid MODE A/MODE B response
    from the ACTUAL supplied metadata. It never claims to have inspected
    assets that were not supplied, and never invents asset ids.
    """

    name = "mock"

    def __init__(self, model: str = "mock-vision-v1") -> None:
        self.model = model

    def complete(
        self,
        prompt: str,
        images: list[ImagePayload],
        tracker: UsageTracker,
    ) -> CompletionResult:
        tracker.pre_flight()
        specs = self._extract_specs(prompt)
        supplied = {p.asset_id for p in images}
        mode = "FINAL_CURATE" if "MODE B - FINAL_CURATE" in prompt else "ANALYZE_BATCH"
        if mode == "ANALYZE_BATCH":
            items = []
            for spec in specs:
                asset_id = spec.get("asset_id")
                if asset_id not in supplied:
                    continue  # never analyze pixels that were not supplied
                metrics = spec.get("technical_metrics") or {}
                sharpness = float(metrics.get("sharpness") or 0.0)
                usable = bool(metrics.get("usable_size", True))
                base = 55.0 + min(25.0, sharpness * 400.0)
                if not usable:
                    base -= 20.0
                score = round(max(0.0, min(100.0, base)), 1)
                items.append(
                    {
                        "asset_id": asset_id,
                        "filename": spec.get("filename") or asset_id,
                        "source": spec.get("platform") or "website",
                        "media_type": spec.get("media_type") or "image",
                        "detected_content": f"mock-detected content for {asset_id}",
                        "possible_categories": ["gallery", "service"],
                        "quality_score": score,
                        "score_breakdown": {
                            "technical_quality": round(score * 0.25, 1),
                            "business_relevance_authenticity": round(score * 0.25, 1),
                            "composition_usability": round(score * 0.20, 1),
                            "emotion_aesthetics": round(score * 0.15, 1),
                            "distinctiveness_collection_fit": round(score * 0.10, 1),
                            "evidence_confidence": round(min(5.0, score * 0.05), 1),
                        },
                        "visual_strengths": ["plausible technical quality (mock)"],
                        "visual_weaknesses": ["mock analysis; no genuine vision occurred"],
                        "verification_status": "unverified",
                        "visual_analysis_status": "analyzed",
                        "recommended_use": "gallery",
                        "selection_status": "considered",
                        "selection_reason": "deterministic mock derived from supplied metadata",
                    }
                )
                if (spec.get("media_type") == "video"):
                    items[-1]["video_review_status"] = "frames_reviewed"
                    items[-1]["evaluated_frames"] = spec.get("frame_timestamps") or []
            mode_a_usage = {
                "input_tokens": 120 * max(1, len(images)),
                "output_tokens": 80 * max(1, len(items)),
            }
            tracker.record(mode_a_usage, retries=0)
            return CompletionResult(
                text=json.dumps({"mode": "ANALYZE_BATCH", "items": items}),
                usage={"input_tokens": 120 * max(1, len(images)), "output_tokens": 80 * max(1, len(items))},
            )
        # MODE B: deterministic curation over the embedded analyses
        analyses = self._extract_analyses(prompt)
        ranked = sorted(
            analyses, key=lambda a: (-float(a.get("quality_score") or 0), a.get("asset_id"))
        )
        top = ranked[: min(3, max(1, len(ranked)))]
        hero = [
            {
                "rank": i + 1,
                "asset_id": a["asset_id"],
                "reason": "highest mock score",
                "composition_notes": "centered subject (mock)",
                "desktop_crop": "16:9 safe (mock)",
                "mobile_crop": "4:5 safe (mock)",
                "focal_point": {"x": 0.5, "y": 0.45},
                "text_overlay_suitability": "possible",
                "limitations": "mock curation; no genuine vision occurred",
            }
            for i, a in enumerate(top)
        ]
        gallery = [
            {
                "asset_id": a["asset_id"],
                "reason": "supporting gallery candidate (mock)",
                "recommended_section": "gallery",
                "selection_status": "considered",
            }
            for a in ranked[3:9]
        ]
        selected = [h["asset_id"] for h in hero] + [g["asset_id"] for g in gallery]
        return CompletionResult(
            text=json.dumps(
                {
                    "schema_version": "visual-director-v2.0",
                    "job_id": self._extract_job_id(prompt),
                    "mode": "FINAL_CURATE",
                    "business_visual_identity": {
                        "style_summary": "deterministic mock identity",
                        "atmosphere": "warm professional grooming (mock)",
                        "brand_personality": "caring and skilled (mock)",
                        "key_visual_strengths": ["consistent subject presence (mock)"],
                        "recommended_design_direction": "editorial layout with calm neutrals (mock)",
                    },
                    "analysis_coverage": {
                        "assets_supplied": len(analyses),
                        "assets_analyzed": sum(
                            1 for a in analyses if a.get("visual_analysis_status") == "analyzed"
                        ),
                        "inherited_from_duplicates": 0,
                        "unavailable": sum(
                            1 for a in analyses if a.get("visual_analysis_status") != "analyzed"
                        ),
                        "prompt_version": "V2.0",
                        "provider": "mock",
                        "model": self.model,
                        "mock": True,
                        "usage": tracker.as_dict(),
                    },
                    "hero_selection": hero,
                    "salon_images": [],
                    "founder_and_team_images": [],
                    "service_images": [],
                    "grooming_transformations": [],
                    "before_after_pairs": [],
                    "gallery_images": gallery,
                    "video_recommendations": [],
                    "design_art_direction": {
                        "narrative": "mock narrative",
                        "layout_guidance": "mock layout guidance",
                        "color_and_lighting": "mock notes",
                        "typography_mood": "mock mood",
                        "section_pairing": "mock pairing",
                        "diversity_notes": "mock diversity notes",
                    },
                    "missing_assets": [
                        {"category": "salon_facilities", "note": "no salon evidence analyzed (mock)"}
                    ],
                    "rejected_assets": [],
                    "selected_asset_ids": selected,
                    "rights_review_required_ids": selected,
                    "final_quality_assessment": {
                        "overall_assessment": "mock assessment; no genuine vision occurred",
                        "strengths": ["deterministic pipeline executed end to end"],
                        "weaknesses": ["all judgments are mocked"],
                        "meets_premium_standard": False,
                        "notes": "live provider validation pending",
                    },
                }
            ),
            usage={"input_tokens": 200, "output_tokens": 150},
        )
        tracker.record({"input_tokens": 200, "output_tokens": 150}, retries=0)

    @staticmethod
    def _extract_block(prompt: str, marker: str) -> str | None:
        start = prompt.find(marker)
        if start < 0:
            return None
        start = prompt.find("```", start)
        if start < 0:
            return None
        end = prompt.find("```", start + 3)
        if end < 0:
            return None
        return prompt[start + 3 : end].strip()

    def _extract_specs(self, prompt: str) -> list[dict[str, Any]]:
        block = self._extract_block(prompt, "ASSET_SPECS")
        if not block:
            return []
        try:
            data = json.loads(block)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _extract_analyses(self, prompt: str) -> list[dict[str, Any]]:
        block = self._extract_block(prompt, "VALIDATED_ANALYSES")
        if not block:
            return []
        try:
            data = json.loads(block)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _extract_job_id(prompt: str) -> str:
        match = re.search(r"job_id[\"':\s]+([A-Za-z0-9_-]+)", prompt)
        return match.group(1) if match else "unknown"


class OpenAICompatibleProvider(VisionProvider):
    """Vision calls against an OpenAI-style chat/completions endpoint.

    Retries (bounded, exponential) on 429, 5xx and transport timeouts.
    Credentials are only used in headers and are scrubbed from any error.
    """

    name = "openai_compatible"

    def __init__(
        self,
        config: AIProviderConfig,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._transport = transport or self._http_post
        self._last_request = -1e9  # pacing: no wait on first call

    def complete(
        self,
        prompt: str,
        images: list[ImagePayload],
        tracker: UsageTracker,
    ) -> CompletionResult:
        if self.config.bounded_acceptance:
            tracker.pre_flight()
            if tracker.config != self.config:
                raise ValueError("Bounded acceptance requires a matching usage tracker")
            if len(images) != (1 if tracker.usage.requests == 0 else 0):
                raise ValueError("Bounded acceptance requires one MODE A image and text-only MODE B")
            if images:
                image = images[0]
                if image.mime != "image/jpeg" or len(image.data) > self.config.max_image_bytes:
                    raise ValueError("Bounded acceptance image must be a JPEG within the byte cap")
                with Image.open(io.BytesIO(image.data)) as decoded:
                    if decoded.format != "JPEG" or max(decoded.size) > self.config.max_image_dim:
                        raise ValueError("Bounded acceptance image exceeds the JPEG dimension cap")
                    decoded.verify()
        max_output_tokens = (
            self.config.mode_a_max_output_tokens if images else self.config.mode_b_max_output_tokens
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            b64 = base64.b64encode(image.data).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image.mime};base64,{b64}"},
                }
            )
        body = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "temperature": 0.2,
            # The pipeline always requires strict JSON; this model declares
            # json_mode support. Bounded cap large enough for a full batch.
            "response_format": {"type": "json_object"},
        }
        if self.config.bounded_acceptance:
            body["max_completion_tokens"] = max_output_tokens
            body["reasoning_effort"] = "none"
        else:
            body["max_tokens"] = max_output_tokens
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        # LOCAL PREFLIGHT: reject oversized requests before any HTTP call.
        # The measurement includes base64-expanded pixels and the full body.
        estimated = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if int(len(estimated) * 1.05) > self.config.max_request_bytes:
            raise RequestTooLargeError(
                f"request body {len(estimated)} bytes exceeds local cap "
                f"{self.config.max_request_bytes}; reduce AI_MAX_IMAGE_DIM/"
                f"AI_MAX_IMAGE_BYTES or AI_BATCH_SIZE"
            )

        last_error = ""
        for attempt in range(self.config.max_retries + 1):
            # Gentle pacing between calls (free-tier RPM/TPM friendliness).
            due = self.config.min_request_spacing_s - (time.monotonic() - self._last_request)
            if due > 0:
                self._sleep(due)
            self._last_request = time.monotonic()
            reservation = tracker.reserve_request(max_output_tokens, retry=attempt > 0)
            try:
                response = self._transport(
                    {
                        "url": self.config.endpoint,
                        "headers": headers,
                        "body": body,
                        "timeout_s": self.config.timeout_s,
                    }
                )
                usage_raw = response.get("usage") or {}
                if self.config.bounded_acceptance and not all(
                    type(usage_raw.get(key)) is int and 0 <= usage_raw[key] <= cap
                    for key, cap in (("prompt_tokens", GROQ_ACCEPTANCE_INPUT_TOKEN_BOUND),
                                     ("completion_tokens", max_output_tokens))
                ):
                    raise ValueError("Missing or out-of-bounds provider token usage")
                usage = {
                    "input_tokens": usage_raw.get("prompt_tokens") or 0,
                    "output_tokens": usage_raw.get("completion_tokens") or 0,
                }
                tracker.record_tokens(usage, reservation=reservation)
                choices = response.get("choices") or []
                if not choices:
                    raise ValueError("empty choices in provider response")
                text = (choices[0].get("message") or {}).get("content") or ""
                if not text.strip():
                    raise ValueError("empty content in provider response")
                return CompletionResult(text=text, usage=usage)
            except requests.HTTPError as exc:
                # Retry ONLY rate limits (bounded, honoring retry-after) and
                # transient 5xx. 401/403/413 are authoritative: no retry.
                response = getattr(exc, "response", None)
                code = int(getattr(response, "status_code", 0) or 0)
                headers = getattr(response, "headers", {}) or {}
                reported = tracker.record_http_error(response)
                detail = reported["message"]
                if reported.get("code"):
                    detail = f"{reported['code']}: {detail}"
                if code == 429:
                    lowered = detail.lower()
                    if "quota" in lowered or "daily" in lowered:
                        raise RateLimitedError(f"provider quota exhausted: {detail}") from None
                    retry_after = 0.0
                    try:
                        retry_after = float(str(headers.get("retry-after") or "0").strip())
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    last_error = f"HTTP 429: {detail}"
                    if attempt < self.config.max_retries:
                        self._sleep(max(retry_after, 2.0 * (2**attempt)))
                        continue
                    raise RateLimitedError(f"provider rate limit: {detail}") from None
                if 500 <= code < 600:
                    last_error = f"HTTP {code}: {detail}"
                    if attempt < self.config.max_retries:
                        self._sleep(2.0 * (2**attempt))
                        continue
                raise RuntimeError(f"provider HTTP error: HTTP {code} {detail}") from None
            except (requests.Timeout, requests.RequestException, ValueError) as exc:
                last_error = _safe_provider_message(str(exc), self.config.api_key)
                if attempt < self.config.max_retries:
                    self._sleep(2.0 * (2**attempt))
                    continue
                raise RuntimeError(f"provider error: {last_error}") from None
        raise RuntimeError(f"provider failed after retries: {last_error}")

    @staticmethod
    def _http_post(request: dict[str, Any]) -> dict[str, Any]:
        """Standard `requests` transport (default headers; no UA spoofing).

        TLS verification stays ON; credentials remain in headers only and are
        never logged or persisted. Bounded timeout comes from configuration.
        """
        response = requests.post(
            request["url"],
            headers=request["headers"],
            json=request["body"],
            timeout=request["timeout_s"],
            verify=True,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            raise requests.HTTPError("Unexpected provider redirect", response=response)
        response.raise_for_status()
        return response.json()


def build_provider(
    config: AIProviderConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    transport: Callable[[Any], Any] | None = None,
) -> tuple[VisionProvider | None, str]:
    """Returns (provider, status). Never silently substitutes a mock."""
    if not config.provider:
        return None, "disabled"
    if config.provider == "mock":
        return MockVisionProvider(), "mock"
    if config.provider == "openai_compatible":
        if not config.endpoint or not config.model:
            return None, "provider_unavailable"
        if not config.api_key:
            return None, "provider_unavailable"
        return OpenAICompatibleProvider(config, sleep=sleep, transport=transport), "ready"
    return None, "provider_unavailable"

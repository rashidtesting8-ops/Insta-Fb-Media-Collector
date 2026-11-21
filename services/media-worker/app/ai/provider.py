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
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

SecretPattern = re.compile(r"(?i)(authorization|api[_-]?key|bearer|token)\s*[=:]\s*\S+")
BearerPattern = re.compile(r"(?i)\bBearer\s+\S+")


def scrub_secrets(text: str) -> str:
    # Bearer values first: the generic rule would replace the "Bearer" marker
    # before the value could be matched, leaving the secret in the tail.
    cleaned = BearerPattern.sub("Bearer [REDACTED]", text or "")
    return SecretPattern.sub(r"\1=[REDACTED]", cleaned)


class ProviderUnavailableError(RuntimeError):
    """Raised when a real provider is selected but not configured."""


class BudgetExceededError(RuntimeError):
    """Raised when the per-job AI budget (requests/cost) is exhausted."""


@dataclass
class AIUsage:
    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }


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
    price_in_per_1k: float = 0.0
    price_out_per_1k: float = 0.0

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
            price_in_per_1k=float(e.get("AI_PRICE_IN_PER_1K") or 0.0),
            price_out_per_1k=float(e.get("AI_PRICE_OUT_PER_1K") or 0.0),
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
        self.usage.input_tokens += int(usage.get("input_tokens") or 0)
        self.usage.output_tokens += int(usage.get("output_tokens") or 0)
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        self.usage.estimated_cost_usd += (
            in_tok / 1000.0 * self.config.price_in_per_1k
            + out_tok / 1000.0 * self.config.price_out_per_1k
        )

    def as_dict(self) -> dict[str, Any]:
        return self.usage.as_dict()


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

    def complete(
        self,
        prompt: str,
        images: list[ImagePayload],
        tracker: UsageTracker,
    ) -> CompletionResult:
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
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        last_error = ""
        for attempt in range(self.config.max_retries + 1):
            tracker.pre_flight()
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
                usage = {
                    "input_tokens": usage_raw.get("prompt_tokens") or 0,
                    "output_tokens": usage_raw.get("completion_tokens") or 0,
                }
                tracker.record(usage, retries=attempt)
                choices = response.get("choices") or []
                if not choices:
                    raise ValueError("empty choices in provider response")
                text = (choices[0].get("message") or {}).get("content") or ""
                if not text.strip():
                    raise ValueError("empty content in provider response")
                return CompletionResult(text=text, usage=usage)
            except requests.HTTPError as exc:
                # Retry ONLY rate limits and transient server errors; 401/403
                # are authoritative denials. Details are scrubbed of secrets.
                response = getattr(exc, "response", None)
                code = int(getattr(response, "status_code", 0) or 0)
                detail = scrub_secrets(str(getattr(response, "text", "") or "")[:200])
                if code == 429 or 500 <= code < 600:
                    last_error = f"HTTP {code}: {detail}"
                    if attempt < self.config.max_retries:
                        self._sleep(2.0 * (2**attempt))
                        continue
                raise RuntimeError(f"provider HTTP error: HTTP {code} {detail}") from exc
            except (requests.Timeout, requests.RequestException, ValueError) as exc:
                last_error = scrub_secrets(str(exc))
                if attempt < self.config.max_retries:
                    self._sleep(2.0 * (2**attempt))
                    continue
                raise RuntimeError(f"provider error: {last_error}") from exc
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
        )
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

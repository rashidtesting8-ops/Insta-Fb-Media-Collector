"""Open-source-first, discovery-only social inventory adapter for CollectionCheckpoint.

Uses installed open-source CLI extractors as external subprocesses (never imported,
never passing cookies, logins, proxies, or restriction-bypass flags):
- gallery-dl (primary for public Instagram & Facebook discovery via --dump-json)
- Instaloader (bounded metadata-only Instagram fallback on non-rate-limit failures)
- yt-dlp (bounded metadata-only fallback strictly for individual public video/Reel URLs)

Discovery is strictly separated from binary downloads: this adapter emits normalized
SourceItem + Attachment metadata only. Binary downloads happen solely when
CollectionCheckpoint.run_source runs with transfer_budget > 0 through a guarded
download transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from app.collection_checkpoint import InventoryPage, SourceItem, SourceStop
from app.legacy_social_collect import _execute_subprocess, check_social_url, classify_social_failure
from app.publication import aware_timestamp
from app.social_normalizers import (
    build_source_item,
    gallery_error_codes,
    gallery_posts,
    instaloader_posts,
    parse_gallery_json,
    ytdlp_posts,
)

RunnerFn = Callable[[list[str], Path], tuple[int, str]]
ClockFn = Callable[[], float]

_IG_PROFILE_RE = re.compile(
    r"^/(?!(?:p|tv|reel|reels|explore|stories|accounts|direct|login|challenge|checkpoint)(?:/|$))"
    r"([A-Za-z0-9._]{1,30})(?:/(?:posts|reels|photos)?)?/?$",
    re.IGNORECASE,
)
_IG_SINGLE_RE = re.compile(
    r"^/(?:(?:[A-Za-z0-9._]{1,30})/)?(?:p|tv|reels?)/([A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)
_FB_UNSUPPORTED_PATHS = ("/login", "/login.php", "/checkpoint", "/challenge", "/marketplace", "/messages", "/settings")
_FB_PEOPLE_RE = re.compile(r"^/people/[^/?#]+/(\d+)/?$", re.IGNORECASE)
_FB_PROFILE_ID_RE = re.compile(r"(?:^|&)id=(\d+)(?:&|$)")
_FB_SINGLE_VIDEO_RE = re.compile(
    r"(?:"
    r"(?:facebook\.com|fb\.watch)/(?:[^?#]*/)?(?:posts|videos|watch|reel|story\.php|permalink\.php)[/?]"
    r"|fb\.watch/[A-Za-z0-9]+/?"
    r")",
    re.IGNORECASE,
)
_FB_PAGE_RE = re.compile(
    r"^/(?!(?:media|photo|photo\.php|watch|permalink\.php|story\.php|login|login\.php|checkpoint|challenge)(?:/|$))"
    r"([A-Za-z0-9._-]{1,120})(?:/(?:photos|photos_by|photos_albums|posts|videos|reels)?)?/?$",
    re.IGNORECASE,
)

_CHALLENGE_PATTERNS = ("checkpoint", "challenge", "captcha", "cf-mitigated", "verify your identity")
_AUTH_ERROR_PATTERNS = ("authenticationerror", "authenticationrequired", "authorizationerror", "loginrequiredexception")
_EXTRACTOR_RUNTIME_ERRORS = ("unboundlocalerror", "indexerror", "valueerror", "runtimeerror", "parseerror")
_RATE_LIMIT_RE = re.compile(r"(?:\b429\b|too many requests|rate[_ ]?limit|ratelimit|please wait a few minutes)")
_NOT_FOUND_RE = re.compile(r"(?:\b404\b|not found|unsupported url)")
_GALLERY_ERROR_TEXT_RE = re.compile(r"\[\s*(-1|4|6)\s*,")
_ELIGIBLE_FALLBACK_REASONS = frozenset({"gallery_dl_error_6", "extractor_broken", "tool_unavailable"})
_HARD_STOP_REASONS = frozenset({"rate_limited", "restricted", "access_blocked", "request_budget", "time_budget"})


@dataclass
class DiscoveryBudget:
    """Shared request, item, and wall-clock budget across primary and fallback tools."""

    request_budget: int
    item_limit: int
    time_budget_seconds: int
    clock: ClockFn = time.monotonic
    requests_used: int = 0
    tried_tools: set[str] = field(default_factory=set)
    started_at: float = field(init=False)

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 1 for v in (self.request_budget, self.item_limit, self.time_budget_seconds)):
            raise ValueError("Discovery budgets must be positive integers")
        self.started_at = self.clock()

    def remaining_seconds(self) -> float:
        return self.time_budget_seconds - (self.clock() - self.started_at)

    def reserve_tool(self, tool: str) -> int:
        if tool in self.tried_tools:
            raise SourceStop("source_stopped")
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise SourceStop("time_budget")
        if self.requests_used >= self.request_budget:
            raise SourceStop("request_budget")
        self.tried_tools.add(tool)
        self.requests_used += 1
        return max(1, int(remaining))


def classify_social_url(platform: str, url: str) -> tuple[str, str | None]:
    """Validate URL shape against installed open-source extractor patterns.

    Returns (url_kind, target_hint) or raises SourceStop('access_blocked') for
    unsupported / login / checkpoint paths.
    """
    check_social_url(platform, url)
    parts = urlsplit(url)
    path = parts.path or "/"
    low_path = path.lower().rstrip("/") or "/"

    if platform == "instagram":
        single = _IG_SINGLE_RE.match(path)
        if single:
            return "single_post", single.group(1)
        profile = _IG_PROFILE_RE.match(path)
        if profile:
            return "profile", profile.group(1).lower()
        raise SourceStop("access_blocked")

    if platform == "facebook":
        if any(low_path == bad or low_path.startswith(bad + "/") for bad in _FB_UNSUPPORTED_PATHS):
            raise SourceStop("access_blocked")
        if _FB_SINGLE_VIDEO_RE.search(url):
            return "single_post", None
        people = _FB_PEOPLE_RE.match(path)
        if people:
            return "people_profile", people.group(1)
        if low_path == "/profile.php":
            match = _FB_PROFILE_ID_RE.search(parts.query)
            if match:
                return "profile_id", match.group(1)
            raise SourceStop("access_blocked")
        page = _FB_PAGE_RE.match(path)
        if page:
            return "page", page.group(1)
        raise SourceStop("access_blocked")

    raise SourceStop("access_blocked")


def _extract_diagnostic_text(tool: str, output: str, gallery_records: list[Any] | None = None) -> str:
    """Return only error envelopes and non-JSON CLI diagnostics, excluding untrusted media JSON."""
    text = (output or "").strip()
    if not text:
        return ""
    if tool == "gallery-dl":
        parts: list[str] = []
        for entry in gallery_records or ():
            if isinstance(entry, list) and entry and isinstance(entry[0], int) and entry[0] in (-1, 4, 6):
                parts.append(json.dumps(entry, ensure_ascii=False))
        try:
            json.loads(text)
        except json.JSONDecodeError:
            for line in text.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                if raw.startswith("[") or raw.startswith("{"):
                    try:
                        json.loads(raw)
                        continue
                    except json.JSONDecodeError:
                        pass
                parts.append(raw)
        return "\n".join(parts)
    if tool == "yt-dlp":
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return ""
        except json.JSONDecodeError:
            pass
        non_json: list[str] = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            if raw.startswith("{"):
                try:
                    if isinstance(json.loads(raw), dict):
                        continue
                except json.JSONDecodeError:
                    pass
            non_json.append(raw)
        return "\n".join(non_json)
    return text


def classify_cli_outcome(
    tool: str,
    exit_code: int,
    output: str,
    gallery_codes: list[str] = (),
    *,
    diagnostic_text: str | None = None,
) -> str | None:
    """Map CLI exit code + scrubbed diagnostic output into strict failure classes."""
    diag = diagnostic_text if diagnostic_text is not None else output
    low = (diag or "").lower()
    if exit_code == 0 and not low.strip() and not gallery_codes:
        return None
    if _RATE_LIMIT_RE.search(low):
        return "rate_limited"
    if any(token in low for token in _CHALLENGE_PATTERNS) or any(token in low for token in _AUTH_ERROR_PATTERNS):
        return "restricted"
    base = classify_social_failure(exit_code, diag)
    if base == "rate_limited":
        return "rate_limited"
    if base == "login_required":
        return "restricted"
    if base == "extractor_broken" or any(token in low for token in _EXTRACTOR_RUNTIME_ERRORS):
        return "extractor_broken"
    if tool == "gallery-dl":
        codes = list(gallery_codes) or sorted(set(_GALLERY_ERROR_TEXT_RE.findall(diag or "")))
        if "6" in codes:
            return "gallery_dl_error_6"
        if "4" in codes:
            return "access_blocked"
        if "-1" in codes:
            return "access_blocked" if _NOT_FOUND_RE.search(low) else "source_stopped"
    if exit_code == 124:
        return "time_budget"
    if exit_code != 0:
        return "access_blocked" if _NOT_FOUND_RE.search(low) else "source_stopped"
    return None


def _cutoff_date(window_start_utc: str | None) -> date:
    frozen = aware_timestamp(window_start_utc)
    if frozen is not None:
        return frozen.astimezone(UTC).date()
    return (datetime.now(UTC) - timedelta(days=365)).date()


def build_gallery_dl_discovery_cmd(
    *,
    platform: str,
    url: str,
    cutoff: date,
    item_limit: int,
    http_timeout: int = 20,
    sleep_request: float = 1.0,
    executable: str = "gallery-dl",
) -> list[str]:
    return [
        executable,
        "--config-ignore",
        "--no-input",
        "--no-colors",
        "--no-postprocessors",
        "--resolve-json",
        "--date-after",
        (cutoff - timedelta(days=1)).isoformat(),
        "--range",
        f"1-{item_limit}",
        "--http-timeout",
        str(http_timeout),
        "--retries",
        "0",
        "--sleep-request",
        str(sleep_request),
        "--whitelist",
        platform,
        url,
    ]


def build_instaloader_discovery_cmd(
    *,
    url_kind: str,
    target_hint: str,
    stage: Path,
    cutoff: date,
    item_limit: int,
    http_timeout: int = 20,
    executable: str = "instaloader",
) -> list[str]:
    _ = item_limit  # instaloader 4.16 ignores --count on profiles; bounded by item_limit/time_budget in adapter
    cmd = [
        executable,
        "--quiet",
        "--no-resume",
        "--no-pictures",
        "--no-videos",
        "--no-video-thumbnails",
        "--no-captions",
        "--no-compress-json",
        "--no-profile-pic",
        "--max-connection-attempts",
        "1",
        "--request-timeout",
        str(http_timeout),
        "--dirname-pattern",
        str(stage),
        "--abort-on=429,403,401",
    ]
    if url_kind == "single_post":
        cmd.extend(["--", f"-{target_hint}"])
    else:
        cmd.extend([
            "--post-filter",
            f"date_utc>=datetime({cutoff.year},{cutoff.month},{cutoff.day})",
            "--",
            target_hint,
        ])
    return cmd


def build_ytdlp_discovery_cmd(
    *,
    url: str,
    http_timeout: int = 20,
    executable: str = "yt-dlp",
) -> list[str]:
    return [
        executable,
        "--ignore-config",
        "--no-cache-dir",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--socket-timeout",
        str(http_timeout),
        "--retries",
        "0",
        "--sleep-requests",
        "1",
        "--no-warnings",
        url,
    ]


class OpenSourceSocialInventory:
    """Discovery-only Inventory adapter for public Instagram/Facebook URLs."""

    def __init__(
        self,
        platform: str,
        profile_url: str,
        *,
        permission_confirmed: bool,
        window_start_utc: str | None,
        item_limit: int,
        request_budget: int,
        time_budget_seconds: int,
        allow_fallbacks: bool = True,
        runner: RunnerFn | None = None,
        clock: ClockFn = time.monotonic,
        http_timeout: int = 20,
        sleep_request: float = 1.0,
    ) -> None:
        self.platform = platform
        self.profile_url = profile_url
        self.permission_confirmed = bool(permission_confirmed)
        self.cutoff = _cutoff_date(window_start_utc)
        self.allow_fallbacks = bool(allow_fallbacks)
        self.runner = runner
        self.http_timeout = http_timeout
        self.sleep_request = sleep_request
        self.budget = DiscoveryBudget(
            request_budget=request_budget,
            item_limit=item_limit,
            time_budget_seconds=time_budget_seconds,
            clock=clock,
        )
        self.tools_executed: list[dict[str, Any]] = []
        self._url_kind: str | None = None
        self._target_hint: str | None = None
        self._ran: bool = False
        self._last_stop: str | None = None

    def check_access(self) -> tuple[str, str | None]:
        if not self.permission_confirmed:
            raise SourceStop("access_blocked")
        if self._url_kind is None:
            self._url_kind, self._target_hint = classify_social_url(self.platform, self.profile_url)
        return self._url_kind, self._target_hint

    def _run_tool(self, tool: str, cmd: list[str], stage: Path) -> tuple[int, str]:
        timeout = min(self.http_timeout * 2, self.budget.reserve_tool(tool))
        if self.runner is None and shutil.which(cmd[0]) is None:
            return 127, "tool_unavailable"
        return _execute_subprocess(cmd, stage, timeout=timeout, runner=self.runner)

    def _normalize_posts(self, extractor: str, posts: list[dict[str, Any]], issues: list[str]) -> list[SourceItem]:
        items: list[SourceItem] = []
        total_attachments = 0
        for post in posts:
            item = build_source_item(
                self.platform,
                self.profile_url,
                extractor,
                post,
                attribution=f"public {extractor} discovery metadata",
                issues=issues,
            )
            if item is None:
                continue
            remaining = self.budget.item_limit - total_attachments
            if remaining <= 0:
                issues.append("listing_cap")
                break
            downloadable = [a for a in item.attachments if a.kind != "thumbnail"]
            if len(downloadable) > remaining:
                kept = []
                kept_dl = 0
                for att in item.attachments:
                    if att.kind == "thumbnail":
                        kept.append(att)
                    elif kept_dl < remaining:
                        kept.append(att)
                        kept_dl += 1
                item = SourceItem(
                    item.id, item.url, tuple(kept), item.expected_ids,
                    item.status, item.identity_status, item.expected_count, item.shortcode,
                )
                issues.append("listing_cap")
                total_attachments += kept_dl
                items.append(item)
                break
            total_attachments += len(downloadable)
            items.append(item)
        return items

    def _discover_gallery_dl(self, stage: Path, issues: list[str]) -> tuple[list[SourceItem], str | None]:
        cmd = build_gallery_dl_discovery_cmd(
            platform=self.platform,
            url=self.profile_url,
            cutoff=self.cutoff,
            item_limit=self.budget.item_limit,
            http_timeout=self.http_timeout,
            sleep_request=self.sleep_request,
        )
        code, output = self._run_tool("gallery-dl", cmd, stage)
        if code == 127 and output == "tool_unavailable":
            self.tools_executed.append({"tool": "gallery-dl", "outcome": "tool_unavailable"})
            return [], "tool_unavailable"
        records = parse_gallery_json(output)
        error_codes = gallery_error_codes(records)
        items = self._normalize_posts("gallery-dl", gallery_posts(records), issues)
        diag = _extract_diagnostic_text("gallery-dl", output, records)
        reason = classify_cli_outcome("gallery-dl", code, output, error_codes, diagnostic_text=diag)
        if "6" in error_codes:
            issues.append("gallery_dl_error_6")
        if "4" in error_codes:
            issues.append("gallery_dl_error_4")
        if "-1" in error_codes:
            issues.append("gallery_dl_error_envelope")
        self.tools_executed.append({
            "tool": "gallery-dl",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
        })
        return items, reason

    def _discover_instaloader(
        self, url_kind: str, target_hint: str, stage: Path, issues: list[str],
    ) -> tuple[list[SourceItem], str | None]:
        cmd = build_instaloader_discovery_cmd(
            url_kind=url_kind,
            target_hint=target_hint,
            stage=stage,
            cutoff=self.cutoff,
            item_limit=self.budget.item_limit,
            http_timeout=self.http_timeout,
        )
        code, output = self._run_tool("instaloader", cmd, stage)
        if code == 127 and output == "tool_unavailable":
            self.tools_executed.append({"tool": "instaloader", "outcome": "tool_unavailable"})
            return [], "tool_unavailable"
        sidecars: list[dict[str, Any]] = []
        for path in sorted(stage.glob("*.json")):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    sidecars.append(loaded)
            except (OSError, json.JSONDecodeError):
                continue
        items = self._normalize_posts("instaloader", instaloader_posts(sidecars), issues)
        reason = classify_cli_outcome("instaloader", code, output)
        self.tools_executed.append({
            "tool": "instaloader",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
        })
        return items, reason

    def _discover_ytdlp(self, stage: Path, issues: list[str]) -> tuple[list[SourceItem], str | None]:
        cmd = build_ytdlp_discovery_cmd(url=self.profile_url, http_timeout=self.http_timeout)
        code, output = self._run_tool("yt-dlp", cmd, stage)
        if code == 127 and output == "tool_unavailable":
            self.tools_executed.append({"tool": "yt-dlp", "outcome": "tool_unavailable"})
            return [], "tool_unavailable"
        docs: list[dict[str, Any]] = []
        for line in (output or "").splitlines():
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    docs.append(loaded)
            except json.JSONDecodeError:
                continue
        if not docs and (output or "").strip().startswith("{"):
            try:
                loaded = json.loads(output)
                if isinstance(loaded, dict):
                    docs.append(loaded)
            except json.JSONDecodeError:
                pass
        items = self._normalize_posts("yt-dlp", ytdlp_posts(docs), issues)
        diag = _extract_diagnostic_text("yt-dlp", output)
        reason = classify_cli_outcome("yt-dlp", code, output, diagnostic_text=diag)
        self.tools_executed.append({
            "tool": "yt-dlp",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
        })
        return items, reason

    def _map_stop(self, reason: str | None) -> str | None:
        if reason is None:
            return None
        if reason == "gallery_dl_error_6":
            return "restricted"
        if reason in ("rate_limited", "restricted", "access_blocked", "request_budget", "time_budget", "listing_cap"):
            return reason
        return "source_stopped"

    def page(self, cursor: str | None) -> InventoryPage:
        if cursor is not None:
            raise ValueError("Bounded open-source discovery emits a single page without synthetic cursors")
        if self._ran:
            raise SourceStop(self._last_stop or "source_stopped")
        url_kind, target_hint = self.check_access()
        self._ran = True
        issues: list[str] = ["bounded_public_discovery", "independent_inventory_unavailable"]

        with tempfile.TemporaryDirectory(prefix=f"pgm-social-disc-{self.platform}-") as tmp:
            stage = Path(tmp)
            gdl_stage = stage / "gallery-dl"
            gdl_stage.mkdir(parents=True, exist_ok=True)
            items, primary_reason = self._discover_gallery_dl(gdl_stage, issues)

            # Stop immediately on rate limit, login/challenge restriction, or when primary already found items.
            if items or primary_reason in _HARD_STOP_REASONS:
                stop = self._map_stop(primary_reason)
                self._last_stop = stop
                if not items and stop is not None:
                    raise SourceStop(stop)
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), stop)

            # Eligible non-rate-limit fallback within shared budget (no fallback loops).
            final_reason = primary_reason
            if self.allow_fallbacks and primary_reason in _ELIGIBLE_FALLBACK_REASONS:
                if self.platform == "instagram" and target_hint:
                    fb_stage = stage / "instaloader"
                    fb_stage.mkdir(parents=True, exist_ok=True)
                    try:
                        items, final_reason = self._discover_instaloader(url_kind, target_hint, fb_stage, issues)
                    except SourceStop as exc:
                        final_reason = str(exc)
                if (
                    not items
                    and url_kind == "single_post"
                    and final_reason not in _HARD_STOP_REASONS
                    and (self.platform == "facebook" or (self.platform == "instagram" and final_reason in _ELIGIBLE_FALLBACK_REASONS))
                ):
                    yt_stage = stage / "yt-dlp"
                    yt_stage.mkdir(parents=True, exist_ok=True)
                    try:
                        items, final_reason = self._discover_ytdlp(yt_stage, issues)
                    except SourceStop as exc:
                        final_reason = str(exc)

            stop = self._map_stop(final_reason)
            self._last_stop = stop
            if not items and stop is not None:
                raise SourceStop(stop)
            if not items:
                self._last_stop = "public_inventory_unavailable"
                raise SourceStop("public_inventory_unavailable")
            return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), stop)

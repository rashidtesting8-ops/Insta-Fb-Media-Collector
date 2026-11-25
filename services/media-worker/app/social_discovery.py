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
from app.legacy_social_collect import _execute_subprocess, check_social_url, classify_social_failure, scrub_sensitive_text
from app.publication import aware_timestamp
from app.social_normalizers import (
    build_source_item,
    gallery_album_queues,
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

_CHALLENGE_PATTERNS = (
    "checkpoint",
    "challenge",
    "challengeerror",
    "captcha",
    "cf-mitigated",
    "verify your identity",
)
_AUTH_ERROR_PATTERNS = (
    "authenticationerror",
    "authenticationrequired",
    "authorizationerror",
    "authrequired",
    "loginrequiredexception",
    "privateprofilenotfollowedexception",
    "queryreturnedforbiddenexception",
    "private but not followed",
    "you must be logged in",
    "must be logged in",
    "http redirect to login page",
    "redirected to login page",
    "account credentials required",
    "posts are private",
)
_LOGIN_REDIRECT_URL_RE = re.compile(
    r"http redirect from\s+\S+\s+to\s+https?://(?:www\.|i\.|m\.)?(?:instagram|facebook)\.com/(?:accounts/login|login)"
)
_HOME_REDIRECT_AUTH_RE = re.compile(
    r"(?:"
    r"http redirect to home page\s*\(\s*https?://(?:www\.|m\.|mobile\.|web\.)?(?:instagram|facebook)\.com/?\s*\)"
    r"|http redirect to home page[^\n]{0,160}\b(?:instagram|facebook|login|auth|account|cookie|session|sign\s*in|private|restricted)\b"
    r"|\b(?:instagram|facebook|login|auth|account|cookie|session|sign\s*in|private|restricted)\b[^\n]{0,160}http redirect to home page"
    r")"
)
_HTTP_RESTRICTED_RE = re.compile(
    r"(?:"
    r"\b(?:401\s*unauthorized|403\s*forbidden)\b"
    r"|(?:http(?:error|\s+error|\s+status)?|status(?:_code)?|response(?:\s+code)?|code)\s*[:=\[\('\"\\\s]{1,6}(?:401|403)\b"
    r"|responded\s+with\s+['\"]?(?:401|403)(?:\b|\s|['\"])"
    r"|\"error\"\s*:\s*\"httperror\"\s*,\s*\"message\"\s*:\s*\"'?(?:401|403)(?:\b|\s|')"
    r"|'(?:401|403)\s*[^']*'\s+for\s+'"
    r"|\b(?:401|403)\s+client\s+error\b"
    r"|\(\s*(?:401|403)\s*\)"
    r")"
)
_EXTRACTOR_RUNTIME_ERRORS = ("unboundlocalerror", "indexerror", "valueerror", "runtimeerror", "parseerror")
_RATE_LIMIT_RE = re.compile(
    r"(?:"
    r"\b429\b"
    r"|too many requests"
    r"|toomanyrequestsexception"
    r"|too many queries in the last time"
    r"|feedback_required"
    r"|rate[_ ]?limit"
    r"|ratelimit"
    r"|please wait a few minutes"
    r"|temporarily blocked(?: from viewing images)?"
    r")"
)
_URL_QUERY_STRIP_RE = re.compile(r"(https?://[^\s'\"<>?]+)\?[^\s'\"<>]+")
_NOT_FOUND_RE = re.compile(r"(?:\b404\b|not found|unsupported url)")
_GDL_USER_NOT_FOUND_RE = re.compile(r"notfounderror.{0,120}(?:requested user could not be found|user)|requested user could not be found")
_INSTALOADER_NOT_EXISTS_RE = re.compile(r"profilenotexistsexception|profile\s+\S+\s+does not exist|does not exist\.")
_GALLERY_ERROR_TEXT_RE = re.compile(r"\[\s*(-1|4|6)\s*,")
_ELIGIBLE_FALLBACK_REASONS = frozenset({"gallery_dl_error_6", "extractor_broken", "tool_unavailable", "user_lookup_not_found"})
_HARD_STOP_REASONS = frozenset({"rate_limited", "restricted", "access_blocked", "request_budget", "time_budget", "profile_not_found"})
_SAFE_CURSOR_RE = re.compile(r"^[A-Za-z0-9_.:=/+-]{4,512}$")
_GDL_CURSOR_RE = re.compile(
    r"(?:Use\s+'-o\s+cursor=([^'\s]+)'|^\s*\[[^\]]+\]\s*Cursor:\s*(\S+)|\bCursor:\s*([A-Za-z0-9_.:=/+-]{4,512}))",
    re.MULTILINE,
)


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

    def reserve_tool(self, tool: str, *, allow_repeat: bool = False) -> int:
        if not allow_repeat and tool in self.tried_tools:
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
    if tool == "instaloader":
        cleaned_lines: list[str] = []
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            sanitized = scrub_sensitive_text(_URL_QUERY_STRIP_RE.sub(r"\1", raw))
            cleaned_lines.append(sanitized[:240])
            if len(cleaned_lines) >= 20:
                break
        return "\n".join(cleaned_lines)
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
    if (
        any(token in low for token in _CHALLENGE_PATTERNS)
        or any(token in low for token in _AUTH_ERROR_PATTERNS)
        or _LOGIN_REDIRECT_URL_RE.search(low)
        or _HOME_REDIRECT_AUTH_RE.search(low)
        or _HTTP_RESTRICTED_RE.search(low)
    ):
        return "restricted"
    base = classify_social_failure(exit_code, diag)
    if base == "rate_limited":
        return "rate_limited"
    if base == "login_required":
        return "restricted"
    if tool == "gallery-dl" and _GDL_USER_NOT_FOUND_RE.search(low):
        return "user_lookup_not_found"
    if tool == "instaloader" and _INSTALOADER_NOT_EXISTS_RE.search(low):
        return "profile_not_found"
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


def _cutoff_timestamp(window_start_utc: str | None) -> datetime:
    frozen = aware_timestamp(window_start_utc)
    if frozen is not None:
        return frozen.astimezone(UTC)
    return datetime.now(UTC) - timedelta(days=365)


def extract_gallery_dl_cursor(output: str, records: list[Any]) -> str | None:
    """Extract a real continuation cursor emitted by gallery-dl; never synthesizes one."""
    for match in _GDL_CURSOR_RE.finditer(output or ""):
        token = next((g for g in match.groups() if g), None)
        if token and _SAFE_CURSOR_RE.fullmatch(token):
            return token
    for entry in reversed(records or ()):
        meta = None
        if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[-1], dict):
            meta = entry[-1]
        elif isinstance(entry, dict):
            meta = entry
        if isinstance(meta, dict):
            for key in ("_cursor", "next_cursor", "cursor"):
                val = meta.get(key)
                if isinstance(val, str) and _SAFE_CURSOR_RE.fullmatch(val):
                    return val
    return None


def build_gallery_dl_historical_cmd(
    *,
    platform: str,
    url: str,
    batch_limit: int,
    http_timeout: int = 20,
    sleep_request: float = 1.0,
    cursor: str | None = None,
    include: str | None = None,
    resolve: bool = True,
    disable_loop: bool = False,
    executable: str = "gallery-dl",
) -> list[str]:
    """Bounded historical gallery-dl command WITHOUT --date-after (avoids pinned/unordered abort)."""
    cmd = [
        executable,
        "--config-ignore",
        "--no-input",
        "--no-colors",
        "--no-postprocessors",
        "--resolve-json" if resolve else "--dump-json",
        "--range" if resolve else "--child-range",
        f"1-{batch_limit}",
        "--http-timeout",
        str(http_timeout),
        "--retries",
        "0",
        "--sleep-request",
        str(sleep_request),
        "--whitelist",
        platform,
    ]
    if disable_loop:
        cmd.extend(["-o", "loop=false"])
    if include:
        cmd.extend(["-o", f"include={include}"])
    if cursor:
        if not _SAFE_CURSOR_RE.fullmatch(cursor):
            raise SourceStop("invalid_continuation")
        cmd.extend(["-o", f"cursor={cursor}"])
    cmd.append(url)
    return cmd


def build_instaloader_historical_cmd(
    *,
    url_kind: str,
    target_hint: str,
    stage: Path,
    cutoff: date,
    http_timeout: int = 20,
    include_reels: bool = False,
    reels_only: bool = False,
    use_resume: bool = False,
    executable: str = "instaloader",
) -> list[str]:
    """Bounded metadata-only Instaloader command with optional --reels and native resume prefix.

    Intentionally omits --quiet so HTTP 401/403/429 and login-redirect diagnostics are not
    suppressed behind generic failure summaries (subprocess runs with stdin=DEVNULL and no login).
    """
    cmd = [
        executable,
        "--resume-prefix" if use_resume else "--no-resume",
    ]
    if use_resume:
        cmd.append("pgm_resume")
    cmd.extend([
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
    ])
    if url_kind == "single_post":
        cmd.extend(["--", f"-{target_hint}"])
    else:
        if reels_only:
            cmd.extend(["--no-posts", "--reels"])
        elif include_reels:
            cmd.append("--reels")
        cmd.extend([
            "--post-filter",
            f"date_utc>=datetime({cutoff.year},{cutoff.month},{cutoff.day})",
            "--",
            target_hint,
        ])
    return cmd


def _evaluate_batch_cutoff(
    paired: list[tuple[dict[str, Any], SourceItem]],
    cutoff_utc: datetime,
    consecutive_older: int,
    threshold: int,
) -> tuple[int, bool]:
    """Advance consecutive-older count only for non-pinned posts with verified timestamps.

    Any later in-window post resets consecutive_older to 0 and clears cutoff evaluation,
    so out-of-order listings cannot prematurely stop traversal.
    """
    for post, item in paired:
        if post.get("pinned"):
            continue
        verified_ts = [
            ts
            for att in item.attachments
            if att.kind != "thumbnail"
            and isinstance(att.publication_evidence, dict)
            and (ts := aware_timestamp(att.publication_evidence.get("timestamp_utc"))) is not None
        ]
        if not verified_ts:
            continue
        newest = max(verified_ts)
        if newest >= cutoff_utc:
            consecutive_older = 0
        else:
            consecutive_older += 1
    return consecutive_older, consecutive_older >= threshold


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
        historical_mode: bool = False,
        batch_size: int | None = None,
        cutoff_consecutive_older: int = 3,
        include_reels: bool = False,
        include_albums: bool = False,
        album_limit: int = 5,
        resume_dir: Path | None = None,
        min_batch_seconds: float | None = None,
    ) -> None:
        self.platform = platform
        self.profile_url = profile_url
        self.permission_confirmed = bool(permission_confirmed)
        self.cutoff = _cutoff_date(window_start_utc)
        self.cutoff_utc = _cutoff_timestamp(window_start_utc)
        self.allow_fallbacks = bool(allow_fallbacks)
        self.runner = runner
        self.http_timeout = http_timeout
        self.sleep_request = sleep_request
        self.historical_mode = bool(historical_mode)
        self.batch_size = min(item_limit, batch_size) if batch_size is not None else min(item_limit, 20)
        if self.batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if type(cutoff_consecutive_older) is not int or cutoff_consecutive_older < 1:
            raise ValueError("cutoff_consecutive_older must be a positive integer")
        if type(album_limit) is not int or album_limit < 1:
            raise ValueError("album_limit must be a positive integer")
        if min_batch_seconds is not None and (isinstance(min_batch_seconds, bool) or min_batch_seconds < 0):
            raise ValueError("min_batch_seconds must be a non-negative number")
        self.cutoff_consecutive_older = cutoff_consecutive_older
        self.include_reels = bool(include_reels)
        self.include_albums = bool(include_albums)
        self.album_limit = album_limit
        self.resume_dir = resume_dir
        self.min_batch_seconds = float(min_batch_seconds) if min_batch_seconds is not None else None
        self._observed_batch_seconds: list[float] = []
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

    def _run_tool(self, tool: str, cmd: list[str], stage: Path, *, allow_repeat: bool = False) -> tuple[int, str]:
        timeout = min(self.http_timeout * 2, self.budget.reserve_tool(tool, allow_repeat=allow_repeat))
        if self.runner is None and shutil.which(cmd[0]) is None:
            return 127, "tool_unavailable"
        return _execute_subprocess(cmd, stage, timeout=timeout, runner=self.runner)

    def _normalize_posts(
        self,
        extractor: str,
        posts: list[dict[str, Any]],
        issues: list[str],
        *,
        max_attachments: int | None = None,
        visited_ids: set[str] | None = None,
    ) -> tuple[list[SourceItem], list[tuple[dict[str, Any], SourceItem]], int]:
        items: list[SourceItem] = []
        paired: list[tuple[dict[str, Any], SourceItem]] = []
        total_attachments = 0
        cap = max_attachments if max_attachments is not None else self.budget.item_limit
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
            if visited_ids is not None and item.id in visited_ids:
                continue
            remaining = cap - total_attachments
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
                    item.caption,
                )
                issues.append("listing_cap")
                total_attachments += kept_dl
                items.append(item)
                paired.append((post, item))
                if visited_ids is not None:
                    visited_ids.add(item.id)
                break
            total_attachments += len(downloadable)
            items.append(item)
            paired.append((post, item))
            if visited_ids is not None:
                visited_ids.add(item.id)
        return items, paired, total_attachments

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
        items, _, _ = self._normalize_posts("gallery-dl", gallery_posts(records), issues)
        diag = _extract_diagnostic_text("gallery-dl", output, records)
        reason = classify_cli_outcome("gallery-dl", code, output, error_codes, diagnostic_text=diag)
        if "6" in error_codes:
            issues.append("gallery_dl_error_6")
        if "4" in error_codes:
            issues.append("gallery_dl_error_4")
        if "-1" in error_codes:
            issues.append("gallery_dl_error_envelope")
        if reason == "user_lookup_not_found":
            issues.append("gallery_dl_user_lookup_unverified")
        self.tools_executed.append({
            "tool": "gallery-dl",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
        })
        return items, reason

    def _discover_instaloader(
        self,
        url_kind: str,
        target_hint: str,
        stage: Path,
        issues: list[str],
        *,
        historical: bool = False,
        include_reels: bool = False,
        reels_only: bool = False,
        use_resume: bool = False,
        max_attachments: int | None = None,
        visited_ids: set[str] | None = None,
    ) -> tuple[list[SourceItem], list[tuple[dict[str, Any], SourceItem]], int, str | None]:
        if historical:
            cmd = build_instaloader_historical_cmd(
                url_kind=url_kind,
                target_hint=target_hint,
                stage=stage,
                cutoff=self.cutoff,
                http_timeout=self.http_timeout,
                include_reels=include_reels,
                reels_only=reels_only,
                use_resume=use_resume,
            )
        else:
            cmd = build_instaloader_discovery_cmd(
                url_kind=url_kind,
                target_hint=target_hint,
                stage=stage,
                cutoff=self.cutoff,
                item_limit=self.budget.item_limit,
                http_timeout=self.http_timeout,
            )
        code, output = self._run_tool("instaloader", cmd, stage, allow_repeat=historical)
        if code == 127 and output == "tool_unavailable":
            self.tools_executed.append({"tool": "instaloader", "outcome": "tool_unavailable"})
            return [], [], 0, "tool_unavailable"
        sidecars: list[dict[str, Any]] = []
        for path in sorted(stage.glob("*.json")):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    sidecars.append(loaded)
            except (OSError, json.JSONDecodeError):
                continue
        if use_resume and list(stage.glob("pgm_resume*.json*")):
            issues.append("instaloader_resume_state_saved")
        raw_posts = instaloader_posts(sidecars)
        items, paired, count = self._normalize_posts(
            "instaloader", raw_posts, issues, max_attachments=max_attachments, visited_ids=visited_ids,
        )
        diag = _extract_diagnostic_text("instaloader", output)
        reason = classify_cli_outcome("instaloader", code, output, diagnostic_text=diag)
        self.tools_executed.append({
            "tool": "instaloader",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
        })
        return items, paired, count, reason

    def _discover_ytdlp(
        self,
        stage: Path,
        issues: list[str],
        *,
        target_url: str | None = None,
        allow_repeat: bool = False,
        max_attachments: int | None = None,
        visited_ids: set[str] | None = None,
    ) -> tuple[list[SourceItem], list[tuple[dict[str, Any], SourceItem]], int, str | None]:
        cmd = build_ytdlp_discovery_cmd(url=target_url or self.profile_url, http_timeout=self.http_timeout)
        code, output = self._run_tool("yt-dlp", cmd, stage, allow_repeat=allow_repeat)
        if code == 127 and output == "tool_unavailable":
            self.tools_executed.append({"tool": "yt-dlp", "outcome": "tool_unavailable"})
            return [], [], 0, "tool_unavailable"
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
        items, paired, count = self._normalize_posts(
            "yt-dlp", ytdlp_posts(docs), issues, max_attachments=max_attachments, visited_ids=visited_ids,
        )
        diag = _extract_diagnostic_text("yt-dlp", output)
        reason = classify_cli_outcome("yt-dlp", code, output, diagnostic_text=diag)
        self.tools_executed.append({
            "tool": "yt-dlp",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
        })
        return items, paired, count, reason

    def _map_stop(self, reason: str | None) -> str | None:
        if reason is None:
            return None
        if reason == "gallery_dl_error_6":
            return "restricted"
        if reason == "profile_not_found":
            return "access_blocked"
        if reason == "user_lookup_not_found":
            return "source_stopped"
        if reason == "extractor_broken" and self.historical_mode:
            return "extractor_failure"
        if reason in (
            "rate_limited", "restricted", "access_blocked", "request_budget", "time_budget",
            "listing_cap", "cutoff_reached", "unsupported_pagination", "extractor_failure",
        ):
            return reason
        return "source_stopped"

    @staticmethod
    def validate_historical_cursor(platform: str, cursor: str) -> dict[str, Any]:
        """Validate a persisted historical cursor JSON strictly before resuming."""
        try:
            loaded = json.loads(cursor)
        except json.JSONDecodeError as exc:
            raise SourceStop("invalid_continuation") from exc
        if not isinstance(loaded, dict) or loaded.get("v") != 1 or loaded.get("platform") != platform:
            raise SourceStop("invalid_continuation")
        for list_key in ("visited_ids",):
            val = loaded.get(list_key)
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise SourceStop("invalid_continuation")
        for int_key in ("consecutive_older", "total_discovered"):
            val = loaded.get(int_key)
            if type(val) is not int or val < 0:
                raise SourceStop("invalid_continuation")
        if platform == "facebook":
            phase = loaded.get("phase")
            if phase not in ("photos", "albums", "album_set"):
                raise SourceStop("invalid_continuation")
            visited_sets = loaded.get("visited_sets")
            if not isinstance(visited_sets, list) or not all(isinstance(x, str) for x in visited_sets):
                raise SourceStop("invalid_continuation")
            next_url = loaded.get("next_url")
            if phase in ("photos", "album_set"):
                if not isinstance(next_url, str) or not next_url:
                    raise SourceStop("invalid_continuation")
                try:
                    check_social_url("facebook", next_url)
                except ValueError as exc:
                    raise SourceStop("invalid_continuation") from exc
                low_path = (urlsplit(next_url).path or "/").lower().rstrip("/")
                if any(low_path == bad or low_path.startswith(bad + "/") for bad in _FB_UNSUPPORTED_PATHS):
                    raise SourceStop("invalid_continuation")
            pending_albums = loaded.get("pending_albums")
            if not isinstance(pending_albums, list):
                raise SourceStop("invalid_continuation")
            for alb in pending_albums:
                if not isinstance(alb, dict) or not isinstance(alb.get("url"), str):
                    raise SourceStop("invalid_continuation")
                try:
                    check_social_url("facebook", alb["url"])
                except ValueError as exc:
                    raise SourceStop("invalid_continuation") from exc
                low_path = (urlsplit(alb["url"]).path or "/").lower().rstrip("/")
                if any(low_path == bad or low_path.startswith(bad + "/") for bad in _FB_UNSUPPORTED_PATHS):
                    raise SourceStop("invalid_continuation")
        elif platform == "instagram":
            queue = loaded.get("queue")
            if not isinstance(queue, list):
                raise SourceStop("invalid_continuation")
            for task in queue:
                if not isinstance(task, dict) or task.get("section") not in ("posts", "reels", "single"):
                    raise SourceStop("invalid_continuation")
                t_url = task.get("url")
                if not isinstance(t_url, str):
                    raise SourceStop("invalid_continuation")
                try:
                    check_social_url("instagram", t_url)
                except ValueError as exc:
                    raise SourceStop("invalid_continuation") from exc
                t_cur = task.get("cursor")
                if t_cur is not None and (not isinstance(t_cur, str) or not _SAFE_CURSOR_RE.fullmatch(t_cur)):
                    raise SourceStop("invalid_continuation")
        else:
            raise SourceStop("invalid_continuation")
        return loaded

    def _load_historical_state(self, cursor: str | None, url_kind: str, target_hint: str | None) -> dict[str, Any]:
        if cursor is not None:
            return self.validate_historical_cursor(self.platform, cursor)
        if self.platform == "facebook":
            can_albums = self.include_albums and url_kind in ("people_profile", "profile_id", "page") and bool(target_hint)
            return {
                "v": 1,
                "platform": "facebook",
                "phase": "photos",
                "next_url": self.profile_url,
                "visited_ids": [],
                "visited_sets": [],
                "pending_albums": [],
                "albums_checked": not can_albums,
                "consecutive_older": 0,
                "total_discovered": 0,
                "timeline_cutoff": False,
                "unsupported_continuation": False,
            }
        if url_kind == "profile" and target_hint:
            base = f"https://www.instagram.com/{target_hint}"
            queue = [{"section": "posts", "url": f"{base}/posts/", "cursor": None}]
            if self.include_reels:
                queue.append({"section": "reels", "url": f"{base}/reels/", "cursor": None})
        else:
            queue = [{"section": "single", "url": self.profile_url, "cursor": None}]
        return {
            "v": 1,
            "platform": "instagram",
            "queue": queue,
            "visited_ids": [],
            "seen_cursors": [],
            "consecutive_older": 0,
            "total_discovered": 0,
            "cutoff_sections": [],
            "unsupported_sections": [],
        }

    def _check_pre_batch_time_guard(self, batch_limit: int) -> None:
        """Stop cleanly before spawning a batch that cannot finish within remaining wall-clock budget."""
        remaining = self.budget.remaining_seconds()
        if remaining <= 0:
            raise SourceStop("time_budget")
        if self.min_batch_seconds is not None:
            if remaining < self.min_batch_seconds:
                raise SourceStop("time_budget")
            return
        min_polite = max(0.0, (batch_limit - 1) * self.sleep_request) if batch_limit > 1 and self.sleep_request > 0 else 0.0
        if self._observed_batch_seconds:
            required = max(min_polite, min(self._observed_batch_seconds) * 0.85)
            if remaining < required:
                raise SourceStop("time_budget")
        elif min_polite > 0 and remaining < min_polite:
            raise SourceStop("time_budget")

    def _run_gallery_historical(
        self,
        *,
        url: str,
        batch_limit: int,
        stage: Path,
        issues: list[str],
        cursor: str | None = None,
        resolve: bool = True,
        disable_loop: bool = False,
        visited_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[SourceItem], list[tuple[dict[str, Any], SourceItem]], int, list[Any], str | None, str | None]:
        self._check_pre_batch_time_guard(batch_limit if resolve else 1)
        cmd = build_gallery_dl_historical_cmd(
            platform=self.platform,
            url=url,
            batch_limit=batch_limit,
            http_timeout=self.http_timeout,
            sleep_request=self.sleep_request,
            cursor=cursor,
            resolve=resolve,
            disable_loop=disable_loop,
        )
        t0 = self.budget.clock()
        code, output = self._run_tool("gallery-dl", cmd, stage, allow_repeat=True)
        elapsed = max(0.0, self.budget.clock() - t0)
        if code == 127 and output == "tool_unavailable":
            self.tools_executed.append({"tool": "gallery-dl", "outcome": "tool_unavailable", "url": url})
            return [], [], [], 0, [], None, "tool_unavailable"
        records = parse_gallery_json(output)
        error_codes = gallery_error_codes(records)
        raw_posts = gallery_posts(records) if resolve else []
        items, paired, dl_count = (
            self._normalize_posts("gallery-dl", raw_posts, issues, max_attachments=batch_limit, visited_ids=visited_ids)
            if resolve
            else ([], [], 0)
        )
        if resolve and code == 0 and items and elapsed > 0:
            self._observed_batch_seconds.append(elapsed)
        real_cursor = extract_gallery_dl_cursor(output, records)
        diag = _extract_diagnostic_text("gallery-dl", output, records)
        reason = classify_cli_outcome("gallery-dl", code, output, error_codes, diagnostic_text=diag)
        if "6" in error_codes and resolve:
            issues.append("gallery_dl_error_6")
        if "4" in error_codes:
            issues.append("gallery_dl_error_4")
        if "-1" in error_codes:
            issues.append("gallery_dl_error_envelope")
        if reason == "user_lookup_not_found":
            issues.append("gallery_dl_user_lookup_unverified")
        self.tools_executed.append({
            "tool": "gallery-dl",
            "outcome": reason or f"discovered_{len(items)}",
            "items": len(items),
            "url": url,
        })
        return raw_posts, items, paired, dl_count, records, real_cursor, reason

    def _page_facebook_historical(self, cursor: str | None, url_kind: str, target_hint: str | None) -> InventoryPage:
        state = self._load_historical_state(cursor, url_kind, target_hint)
        issues: list[str] = ["bounded_historical_discovery", "independent_inventory_unavailable"]
        visited_ids: set[str] = set(state.get("visited_ids") or ())
        visited_sets: set[str] = set(state.get("visited_sets") or ())
        phase = state["phase"]

        with tempfile.TemporaryDirectory(prefix="pgm-fb-hist-") as tmp:
            stage = Path(tmp)
            if phase == "albums":
                albums_url = f"https://www.facebook.com/{target_hint}/photos_albums"
                _, _, _, _, records, _, reason = self._run_gallery_historical(
                    url=albums_url,
                    batch_limit=self.album_limit,
                    stage=stage,
                    issues=issues,
                    resolve=False,
                    disable_loop=True,
                )
                if reason in _HARD_STOP_REASONS:
                    raise SourceStop(self._map_stop(reason) or "access_blocked")
                albums = [
                    a for a in gallery_album_queues(records)
                    if not a["set_id"] or a["set_id"] not in visited_sets
                ][: self.album_limit]
                state["albums_checked"] = True
                if albums:
                    first_album = albums.pop(0)
                    state["phase"] = "album_set"
                    state["next_url"] = first_album["url"]
                    state["pending_albums"] = albums
                    state["consecutive_older"] = 0
                    return InventoryPage((), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)))
                if state.get("timeline_cutoff"):
                    return InventoryPage((), None, tuple(dict.fromkeys(issues)), "cutoff_reached")
                if state.get("unsupported_continuation"):
                    return InventoryPage((), None, tuple(dict.fromkeys([*issues, "unsupported_pagination"])), "unsupported_pagination")
                if state["total_discovered"] == 0:
                    raise SourceStop(self._map_stop(reason) or "public_inventory_unavailable")
                return InventoryPage((), None, tuple(dict.fromkeys(issues)))

            rem_total = self.budget.item_limit - int(state["total_discovered"])
            if rem_total <= 0:
                return InventoryPage((), None, tuple(dict.fromkeys([*issues, "listing_cap"])), "listing_cap")
            batch_limit = min(self.batch_size, rem_total)
            raw_posts, items, paired, dl_count, _, _, primary_reason = self._run_gallery_historical(
                url=state["next_url"],
                batch_limit=batch_limit,
                stage=stage,
                issues=issues,
                resolve=True,
                disable_loop=True,
                visited_ids=visited_ids,
            )

            # Single public Facebook video URL yt-dlp fallback on eligible primary failure.
            if (
                not items
                and url_kind == "single_post"
                and self.allow_fallbacks
                and primary_reason in _ELIGIBLE_FALLBACK_REASONS
            ):
                yt_stage = stage / "yt-dlp"
                yt_stage.mkdir(parents=True, exist_ok=True)
                try:
                    items, paired, dl_count, primary_reason = self._discover_ytdlp(
                        yt_stage,
                        issues,
                        target_url=state["next_url"],
                        allow_repeat=True,
                        max_attachments=batch_limit,
                        visited_ids=visited_ids,
                    )
                except SourceStop as exc:
                    primary_reason = str(exc)

            if not items and primary_reason in _HARD_STOP_REASONS:
                raise SourceStop(self._map_stop(primary_reason) or "access_blocked")
            if not items and primary_reason == "gallery_dl_error_6" and url_kind != "single_post":
                raise SourceStop("restricted")

            consecutive_older, set_cutoff = _evaluate_batch_cutoff(
                paired, self.cutoff_utc, int(state["consecutive_older"]), self.cutoff_consecutive_older,
            )
            state["total_discovered"] = int(state["total_discovered"]) + dl_count
            for post in raw_posts:
                if post.get("fb_set_id"):
                    visited_sets.add(str(post["fb_set_id"]))
            state["visited_ids"] = sorted(visited_ids)
            state["visited_sets"] = sorted(visited_sets)

            # Real Facebook continuation only via gallery-dl next_photo_id + set_id (&setextract).
            next_set_url = None
            if raw_posts:
                last_post = raw_posts[-1]
                next_pid = last_post.get("fb_next_photo_id")
                set_id = last_post.get("fb_set_id")
                if next_pid and set_id and next_pid not in visited_ids:
                    next_set_url = f"https://www.facebook.com/photo/?fbid={next_pid}&set={set_id}&setextract"

            if primary_reason in _HARD_STOP_REASONS and items:
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), self._map_stop(primary_reason))

            if state["total_discovered"] >= self.budget.item_limit:
                issues.append("listing_cap")
                if next_set_url and not set_cutoff:
                    state["next_url"] = next_set_url
                    state["consecutive_older"] = consecutive_older
                    return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)), "listing_cap")
                pending_albums_cap = list(state.get("pending_albums") or ())
                if pending_albums_cap:
                    next_album = pending_albums_cap.pop(0)
                    state["phase"] = "album_set"
                    state["next_url"] = next_album["url"]
                    state["pending_albums"] = pending_albums_cap
                    state["consecutive_older"] = 0
                    return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)), "listing_cap")
                if not state.get("albums_checked"):
                    state["phase"] = "albums"
                    state["consecutive_older"] = 0
                    return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)), "listing_cap")
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), "listing_cap")

            if not set_cutoff and next_set_url:
                state["next_url"] = next_set_url
                state["consecutive_older"] = consecutive_older
                return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)))

            if phase == "photos" and set_cutoff:
                state["timeline_cutoff"] = True
            if not set_cutoff and not next_set_url and dl_count >= batch_limit:
                state["unsupported_continuation"] = True

            # Move to pending albums or album discovery if configured.
            pending_albums = list(state.get("pending_albums") or ())
            if pending_albums:
                next_album = pending_albums.pop(0)
                state["phase"] = "album_set"
                state["next_url"] = next_album["url"]
                state["pending_albums"] = pending_albums
                state["consecutive_older"] = 0
                return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)))
            if not state.get("albums_checked"):
                state["phase"] = "albums"
                state["consecutive_older"] = 0
                return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)))

            if state.get("unsupported_continuation"):
                return InventoryPage(
                    tuple(items), None, tuple(dict.fromkeys([*issues, "unsupported_pagination"])), "unsupported_pagination",
                )
            if state.get("timeline_cutoff") or (phase == "photos" and set_cutoff):
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), "cutoff_reached")
            if not items and primary_reason:
                raise SourceStop(self._map_stop(primary_reason) or "source_stopped")
            if state["total_discovered"] == 0:
                raise SourceStop("public_inventory_unavailable")
            return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), self._map_stop(primary_reason))

    def _page_instagram_historical(self, cursor: str | None, url_kind: str, target_hint: str | None) -> InventoryPage:
        state = self._load_historical_state(cursor, url_kind, target_hint)
        issues: list[str] = ["bounded_historical_discovery", "independent_inventory_unavailable"]
        visited_ids: set[str] = set(state.get("visited_ids") or ())
        seen_cursors: set[str] = set(state.get("seen_cursors") or ())
        queue = list(state.get("queue") or ())
        if not queue:
            return InventoryPage((), None, tuple(dict.fromkeys(issues)))

        rem_total = self.budget.item_limit - int(state["total_discovered"])
        if rem_total <= 0:
            return InventoryPage((), None, tuple(dict.fromkeys([*issues, "listing_cap"])), "listing_cap")
        batch_limit = min(self.batch_size, rem_total)
        task = dict(queue[0])

        with tempfile.TemporaryDirectory(prefix="pgm-ig-hist-") as tmp:
            stage = Path(tmp)
            _, items, paired, dl_count, _, real_cursor, primary_reason = self._run_gallery_historical(
                url=task["url"],
                batch_limit=batch_limit,
                stage=stage,
                issues=issues,
                cursor=task.get("cursor"),
                resolve=True,
                disable_loop=False,
                visited_ids=visited_ids,
            )

            final_reason = primary_reason
            if not items and primary_reason in _HARD_STOP_REASONS:
                raise SourceStop(self._map_stop(primary_reason) or "access_blocked")

            if not items and self.allow_fallbacks and primary_reason in _ELIGIBLE_FALLBACK_REASONS:
                if target_hint:
                    il_stage = (self.resume_dir / task["section"]) if self.resume_dir is not None else (stage / "instaloader")
                    il_stage.mkdir(parents=True, exist_ok=True)
                    try:
                        items, paired, dl_count, final_reason = self._discover_instaloader(
                            url_kind,
                            target_hint,
                            il_stage,
                            issues,
                            historical=True,
                            include_reels=False,
                            reels_only=task["section"] == "reels",
                            use_resume=self.resume_dir is not None,
                            max_attachments=batch_limit,
                            visited_ids=visited_ids,
                        )
                    except SourceStop as exc:
                        final_reason = str(exc)
                if not items and url_kind == "single_post" and final_reason in _ELIGIBLE_FALLBACK_REASONS:
                    yt_stage = stage / "yt-dlp"
                    yt_stage.mkdir(parents=True, exist_ok=True)
                    try:
                        items, paired, dl_count, final_reason = self._discover_ytdlp(
                            yt_stage,
                            issues,
                            target_url=task["url"],
                            allow_repeat=True,
                            max_attachments=batch_limit,
                            visited_ids=visited_ids,
                        )
                    except SourceStop as exc:
                        final_reason = str(exc)

            if not items and final_reason in _HARD_STOP_REASONS:
                raise SourceStop(self._map_stop(final_reason) or "access_blocked")

            consecutive_older, section_cutoff = _evaluate_batch_cutoff(
                paired, self.cutoff_utc, int(state["consecutive_older"]), self.cutoff_consecutive_older,
            )
            state["total_discovered"] = int(state["total_discovered"]) + dl_count
            state["visited_ids"] = sorted(visited_ids)

            if final_reason in _HARD_STOP_REASONS and items:
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), self._map_stop(final_reason))

            valid_next_cursor = real_cursor if real_cursor and real_cursor not in seen_cursors else None
            if state["total_discovered"] >= self.budget.item_limit:
                issues.append("listing_cap")
                if valid_next_cursor and not section_cutoff:
                    task["cursor"] = valid_next_cursor
                    queue[0] = task
                    seen_cursors.add(valid_next_cursor)
                    state["queue"] = queue
                    state["seen_cursors"] = sorted(seen_cursors)
                    state["consecutive_older"] = consecutive_older
                    return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)), "listing_cap")
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), "listing_cap")

            if not section_cutoff and valid_next_cursor:
                task["cursor"] = valid_next_cursor
                queue[0] = task
                seen_cursors.add(valid_next_cursor)
                state["queue"] = queue
                state["seen_cursors"] = sorted(seen_cursors)
                state["consecutive_older"] = consecutive_older
                return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)))

            # Section complete (cutoff reached, exhausted, or capped without real cursor).
            queue.pop(0)
            state["queue"] = queue
            state["consecutive_older"] = 0
            if section_cutoff:
                state["cutoff_sections"] = list(dict.fromkeys([*state.get("cutoff_sections", []), task["section"]]))
            elif dl_count >= batch_limit and not valid_next_cursor:
                state["unsupported_sections"] = list(dict.fromkeys([*state.get("unsupported_sections", []), task["section"]]))

            if queue:
                return InventoryPage(tuple(items), json.dumps(state, sort_keys=True), tuple(dict.fromkeys(issues)))

            if state.get("cutoff_sections") and not state.get("unsupported_sections"):
                return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), "cutoff_reached")
            if state.get("unsupported_sections"):
                return InventoryPage(
                    tuple(items), None, tuple(dict.fromkeys([*issues, "unsupported_pagination"])), "unsupported_pagination",
                )
            if not items and final_reason:
                raise SourceStop(self._map_stop(final_reason) or "source_stopped")
            if state["total_discovered"] == 0:
                raise SourceStop("public_inventory_unavailable")
            return InventoryPage(tuple(items), None, tuple(dict.fromkeys(issues)), self._map_stop(final_reason))

    def page(self, cursor: str | None) -> InventoryPage:
        url_kind, target_hint = self.check_access()
        if self.historical_mode:
            if self.platform == "facebook":
                return self._page_facebook_historical(cursor, url_kind, target_hint)
            return self._page_instagram_historical(cursor, url_kind, target_hint)

        if cursor is not None:
            raise ValueError("Bounded open-source discovery emits a single page without synthetic cursors")
        if self._ran:
            raise SourceStop(self._last_stop or "source_stopped")
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
                        items, _, _, final_reason = self._discover_instaloader(url_kind, target_hint, fb_stage, issues)
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
                        items, _, _, final_reason = self._discover_ytdlp(yt_stage, issues)
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

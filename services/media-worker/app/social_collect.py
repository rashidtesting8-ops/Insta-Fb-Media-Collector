"""Phase 1 social collector skeleton.

Adapted from the legacy `src/engine/social_collect.py` (gallery-dl subprocess,
failure classification, permission gate). Real gallery-dl/Instaloader/yt-dlp
invocation is implemented in Phase 2. No live scraping runs in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SocialResult:
    platform: str
    url: str
    status: str = "not_requested"
    reason: str | None = None
    files_added: int = 0
    note: str = "Phase 1 skeleton; social collection implemented in Phase 2"

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "url": self.url,
            "status": self.status,
            "reason": self.reason,
            "files_added": self.files_added,
            "note": self.note,
        }


def social_stub(platform: str, url: str) -> SocialResult:
    """Return a deterministic, non-scraping placeholder result."""
    return SocialResult(
        platform=platform,
        url=url,
        status="queued" if url else "not_requested",
        note="Phase 1 skeleton; real collection in Phase 2",
    )

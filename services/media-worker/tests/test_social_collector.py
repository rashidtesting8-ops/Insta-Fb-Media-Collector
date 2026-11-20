"""Phase 3 social collection tests — fully offline with mocked tool runners.

The legacy `collect_social` accepts an injectable `runner(cmd, stage)`; the
Phase-3 fallbacks accept the same protocol. No external network access.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from app.manifest import (
    merge_social_into_manifest,
    validate_against_disk,
    write_manifest,
)
from app.social_adapter import (
    run_social_extraction,
    section_from_result,
)
from tests.test_website_collector import _LocalServer, _build_site, _png_bytes

IG_URL = "https://www.instagram.com/somegroomer/"
FB_WATCH_URL = "https://www.facebook.com/watch/?v=12345"
FB_PAGE_URL = "https://www.facebook.com/somegroomer/"


def _mk_runner(behavior, calls):
    def runner(cmd, stage):
        calls.append(list(cmd))
        return behavior(cmd, Path(stage))

    return runner


def _png_bytes_alt() -> bytes:
    """A second, distinct valid PNG so date-boundary tests are not confused
    with sha-dedupe behaviour."""
    pic = Image.new("RGB", (200, 100), (10, 200, 30))
    mem = io.BytesIO()
    pic.save(mem, format="PNG")
    return mem.getvalue()


def _gallery_dl_ok(cmd, stage):
    if "--dump-json" in cmd:
        return 0, json.dumps(
            {
                "filename": "groomed-dog",
                "extension": "jpg",
                "date": "2026-06-01T10:00:00+00:00",
                "post_url": "https://www.instagram.com/p/abc123/",
                "author": "somegroomer",
            }
        )
    (stage / "groomed-dog.jpg").write_bytes(_png_bytes())
    return 0, ""


def _gallery_dl_login_required(cmd, stage):
    return 4, "login required: use --username and --password or --cookies"


def _gallery_dl_rate_limited(cmd, stage):
    return 4, "429 Too Many Requests: rate limit exceeded, please wait"


def _instaloader_ok(cmd, stage):
    stem = "2026-06-01_12-00-00_UTC_abc123"
    (stage / f"{stem}.jpg").write_bytes(_png_bytes())
    (stage / f"{stem}.json").write_text(
        json.dumps({"shortcode": "abc123", "date_utc": "2026-06-01T12:00:00Z"}),
        encoding="utf-8",
    )
    return 0, ""


def _ytdlp_ok(cmd, stage):
    (stage / "vid123_My_Grooming_Video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    (stage / "vid123_My_Grooming_Video.info.json").write_text(
        json.dumps(
            {
                "id": "vid123",
                "upload_date": "20260601",
                "webpage_url": FB_WATCH_URL,
                "uploader": "Some Salon",
            }
        ),
        encoding="utf-8",
    )
    return 0, ""


def test_permission_gate_blocks_collection_entirely(tmp_path):
    calls: list[list[str]] = []
    result = run_social_extraction(
        "job_perm",
        tmp_path / "job_perm",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=False,
        runner=_mk_runner(_gallery_dl_ok, calls),
        enable_fallbacks=False,
    )
    assert result.status == "permission_required"
    assert result.reason == "operator_consent_missing"
    assert result.files_added == 0
    assert calls == []  # nothing was invoked at all


def test_invalid_social_url_is_rejected(tmp_path):
    calls: list[list[str]] = []
    result = run_social_extraction(
        "job_bad_url",
        tmp_path / "job_bad_url",
        "instagram",
        "https://example.com/not-instagram/",
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_ok, calls),
        enable_fallbacks=False,
    )
    assert result.status == "invalid_url"
    assert calls == []


def test_instagram_gallery_dl_success_with_verified_date(tmp_path):
    calls: list[list[str]] = []
    job_dir = tmp_path / "job_ig"
    result = run_social_extraction(
        "job_ig",
        job_dir,
        "instagram",
        IG_URL,
        max_files=6,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_ok, calls),
        enable_fallbacks=False,
    )
    assert result.status == "completed"
    assert result.files_added == 1
    assert result.duplicates == 0
    # exactly which tools executed (req #8): only gallery-dl in this case
    assert [t["tool"] for t in result.tools] == ["gallery-dl"]
    assert result.tools[0]["outcome"].startswith("downloaded_")
    # gallery-dl was invoked with the bounded, documented safety flags
    flat = calls[0]
    assert "--whitelist" in flat and flat[flat.index("--whitelist") + 1] == "instagram"
    assert "--date-after" in flat
    assert "--range" in flat and flat[flat.index("--range") + 1] == "1-6"
    assert "--filesize-max" in flat

    assert len(result.items) == 1
    item = result.items[0]
    assert item["asset_id"].startswith("ig_0001_")
    assert item["platform"] == "instagram"
    assert item["local_path"] == f"instagram/{item['filename']}"
    assert (job_dir / item["local_path"]).is_file()
    assert item["post_date"] is not None and item["date_status"] == "verified"
    assert item["source_url"] == "https://www.instagram.com/p/abc123/"
    assert item["rights_status"] == "unverified"
    assert item["width"] == 320 and item["height"] == 240  # computed from disk
    assert item["detail"]["author"] == "somegroomer"
    # provenance rows retained by the legacy module
    assert (job_dir / "research" / "social-media-manifest.json").is_file()


def test_instagram_login_required_is_restricted(tmp_path):
    calls: list[list[str]] = []
    result = run_social_extraction(
        "job_ig_lr",
        tmp_path / "job_ig_lr",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_login_required, calls),
        enable_fallbacks=False,
    )
    # Access walls are a distinct source status, not a generic failure.
    assert result.status == "restricted"
    assert result.reason == "login_required"
    assert result.files_added == 0
    tool_names = [t["tool"] for t in result.tools]
    assert tool_names == ["gallery-dl"]


def test_rate_limit_status_is_distinct_and_never_triggers_fallback(tmp_path):
    ig_calls: list[list[str]] = []
    result = run_social_extraction(
        "job_rl",
        tmp_path / "job_rl",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_rate_limited, ig_calls),
        instaloader_runner=_mk_runner(_instaloader_ok, []),
        enable_fallbacks=True,
    )
    # rate limits are respected: no second tool is fired at the platform
    assert result.status == "rate_limited"
    assert result.reason == "rate_limited"
    assert result.fallback_attempted is None


def test_instagram_instaloader_fallback_success(tmp_path):
    calls: list[list[str]] = []
    fallback_calls: list[list[str]] = []
    result = run_social_extraction(
        "job_ig_fb",
        tmp_path / "job_ig_fb",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_login_required, calls),
        instaloader_runner=_mk_runner(_instaloader_ok, fallback_calls),
        enable_fallbacks=True,
    )
    assert result.status == "partial_success"  # primary failed, fallback delivered
    assert result.fallback_attempted == "instaloader"
    assert result.files_added == 1
    assert len(fallback_calls) == 1
    assert "--count" in fallback_calls[0]
    assert "--abort-on=429,403" in fallback_calls[0]
    # exactly which tools executed, with per-tool outcomes (req #8)
    assert [t["tool"] for t in result.tools] == ["gallery-dl", "instaloader"]
    assert result.tools[0]["outcome"].startswith("login_required")
    assert result.tools[1]["outcome"].startswith("downloaded_")
    # offline (mocked) mode captures no versions via extra subprocesses
    assert all(t.get("version") is None for t in result.tools)
    item = result.items[0]
    assert item["post_date"] is not None and item["date_status"] == "verified"
    assert item["source_url"] == "https://www.instagram.com/p/abc123/"
    assert item["detail"]["origin"] == "instaloader_fallback"
    assert (tmp_path / "job_ig_fb" / item["local_path"]).is_file()


def test_facebook_ytdlp_fallback_for_single_video(tmp_path):
    calls: list[list[str]] = []
    ytdlp_calls: list[list[str]] = []
    result = run_social_extraction(
        "job_fb_vid",
        tmp_path / "job_fb_vid",
        "facebook",
        FB_WATCH_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_login_required, calls),
        ytdlp_runner=_mk_runner(_ytdlp_ok, ytdlp_calls),
        enable_fallbacks=True,
    )
    assert result.status == "partial_success"
    assert result.fallback_attempted == "yt-dlp"
    assert result.files_added == 1
    assert ytdlp_calls and "--max-downloads" in ytdlp_calls[0]
    item = result.items[0]
    assert item["type"] == "video"
    assert item["platform"] == "facebook"
    assert item["date_status"] == "verified"
    assert item["post_date"].startswith("2026-06-01")
    assert item["detail"]["origin"] == "ytdlp_fallback"
    disk = tmp_path / "job_fb_vid" / item["local_path"]
    assert disk.is_file()


def test_facebook_page_url_does_not_invoke_ytdlp(tmp_path):
    ytdlp_calls: list[list[str]] = []
    result = run_social_extraction(
        "job_fb_page",
        tmp_path / "job_fb_page",
        "facebook",
        FB_PAGE_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_login_required, []),
        ytdlp_runner=_mk_runner(_ytdlp_ok, ytdlp_calls),
        enable_fallbacks=True,
    )
    # a full page is not a supported single-video URL: no yt-dlp attempt, honest restriction
    assert result.status == "restricted"
    assert result.reason == "login_required"
    assert ytdlp_calls == []
    assert result.fallback_attempted is None


def _website_manifest(tmp_path: Path) -> dict:
    from app.collector import run_website_extraction

    web = _build_site(tmp_path)
    job_dir = tmp_path / "job_merge"
    with _LocalServer(web) as server:
        result = run_website_extraction(
            "job_merge",
            job_dir,
            server.url(),
            max_pages=4,
            max_media=10,
            allow_private=True,
        )
    assert result.status == "completed"
    return json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))


def test_unified_manifest_preserves_website_and_records_social(tmp_path):
    website_doc = _website_manifest(tmp_path)
    job_dir = tmp_path / "job_merge"

    ig_result = run_social_extraction(
        "job_merge",
        job_dir,
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_ok, []),
        enable_fallbacks=False,
    )
    assert ig_result.status == "completed"

    restricted_result = run_social_extraction(
        "job_merge",
        job_dir,
        "facebook",
        FB_PAGE_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_login_required, []),
        enable_fallbacks=False,
    )
    assert restricted_result.status == "restricted"

    merged = merge_social_into_manifest(
        website_doc,
        [
            ("instagram", section_from_result(ig_result)),
            ("facebook", section_from_result(restricted_result)),
        ],
        job_id="job_merge",
        lookback_days=365,
        permission_confirmed=True,
    )
    write_manifest(job_dir, merged)

    # The failed social source did NOT destroy the website results.
    website_items = [a for a in merged["assets"] if a["platform"] == "website"]
    assert len(website_items) == len(website_doc["assets"])
    assert merged["sources"]["website"]["status"] == "completed"
    assert merged["sources"]["instagram"]["status"] == "completed"
    assert merged["sources"]["facebook"]["status"] == "restricted"
    assert merged["source_status"] == "partial_success"

    assert merged["schema_version"] == "3.0-unified"
    assert merged["lookback_days"] == 365 and merged["cutoff_date"]
    assert merged["social_permission_confirmed"] is True

    ids = [a["asset_id"] for a in merged["assets"]]
    assert len(ids) == len(set(ids))
    prefixes = {aid.split("_")[0] for aid in ids}
    assert {"web", "ig"} <= prefixes

    # Every item that claims a file really exists on disk with matching sha.
    assert validate_against_disk(job_dir, merged) == []


def test_unverified_dates_stay_unknown_in_social_items(tmp_path):
    def undated(cmd, stage):
        if "--dump-json" in cmd:
            return 0, ""  # no metadata -> no date
        (stage / "nodate.jpg").write_bytes(_png_bytes())
        return 0, ""

    result = run_social_extraction(
        "job_nodate",
        tmp_path / "job_nodate",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(undated, []),
        enable_fallbacks=False,
    )
    assert result.status == "completed"
    item = result.items[0]
    assert item["post_date"] is None
    assert item["date_status"] == "unknown"


def test_ytdlp_fallback_needs_single_item_url_pattern():
    import re

    from app.social_adapter import _FB_SINGLE_URL_RE

    assert _FB_SINGLE_URL_RE.search(FB_WATCH_URL)
    assert _FB_SINGLE_URL_RE.search("https://www.facebook.com/somepage/posts/12345/")
    assert _FB_SINGLE_URL_RE.search("https://fb.watch/abcdef/")
    assert not _FB_SINGLE_URL_RE.search(FB_PAGE_URL)


@pytest.mark.parametrize("platform,url", [
    ("instagram", IG_URL),
    ("facebook", FB_PAGE_URL),
])
def test_social_platforms_run_independently(tmp_path, platform, url):
    """Each social platform can be collected on its own, isolated from the other."""
    result = run_social_extraction(
        f"job_only_{platform}",
        tmp_path / f"job_only_{platform}",
        platform,
        url,
        max_files=3,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_ok, []),
        enable_fallbacks=False,
    )
    if platform == "instagram":
        assert result.status == "completed"
        assert result.items[0]["platform"] == "instagram"
    else:
        # gallery-dl fake succeeds but this runner is for a facebook URL;
        # the legacy module records the row under facebook.
        assert result.status == "completed"
        assert result.items[0]["platform"] == "facebook"


# ---------------------------------------------------------------------------
# Phase 3 acceptance hardening regressions
# ---------------------------------------------------------------------------


def test_access_gate_envelope_maps_to_restricted(tmp_path):
    """Live-observed pattern: gallery-dl emits an access-gate error envelope
    (code 6) with exit 0. It must surface as `restricted`, never as success."""

    def gate(cmd, stage):
        if "--dump-json" in cmd:
            return 0, (
                '[[6, "https://www.instagram.com/x/posts/", '
                '{"category": "instagram", "subcategory": "user"}]]'
            )
        return 0, ""

    result = run_social_extraction(
        "job_gate",
        tmp_path / "job_gate",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(gate, []),
        enable_fallbacks=False,
    )
    assert result.status == "restricted"
    assert result.reason == "gallery_dl_error_6"
    assert result.files_added == 0
    assert any("restricted anonymous access" in n for n in result.notes)


def test_not_found_maps_to_inaccessible_without_fallback(tmp_path):
    """A missing profile is `inaccessible` — no fallback hammering."""

    def not_found(cmd, stage):
        return 0, (
            '[[4, "https://www.instagram.com/ghost/posts/", '
            '{"category": "instagram"}]]'
        )

    ig_calls: list[list[str]] = []
    result = run_social_extraction(
        "job_nf",
        tmp_path / "job_nf",
        "instagram",
        "https://www.instagram.com/ghost/",
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(not_found, []),
        instaloader_runner=_mk_runner(_instaloader_ok, ig_calls),
        enable_fallbacks=True,
    )
    assert result.status == "inaccessible"
    assert result.reason == "gallery_dl_error_4"
    assert ig_calls == []  # fallback decision: not-found never triggers one


def test_partial_social_success_files_plus_gate(tmp_path):
    """Live-observed Facebook pattern: some files download, then the extractor
    hits an access gate. The source must report `partial_success` with the
    gate reason preserved — never plain `completed`."""

    def partial(cmd, stage):
        if "--dump-json" in cmd:
            return 0, (
                json.dumps(
                    {
                        "filename": "goodpic",
                        "extension": "jpg",
                        "date": "2026-06-01T10:00:00+00:00",
                        "post_url": "https://www.facebook.com/p/good1/",
                    }
                )
                + "\n"
                + '[[6, "https://www.facebook.com/x/photos", {"category": "facebook"}]]'
            )
        (stage / "goodpic.jpg").write_bytes(_png_bytes())
        return 0, ""

    result = run_social_extraction(
        "job_partial_fb",
        tmp_path / "job_partial_fb",
        "facebook",
        FB_PAGE_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(partial, []),
        enable_fallbacks=False,
    )
    assert result.status == "partial_success"
    assert result.files_added == 1
    assert result.reason == "gallery_dl_error_6"
    assert result.tools[0]["outcome"] == "gallery_dl_error_6"
    item = result.items[0]
    assert item["local_path"] and item["local_path"].startswith("facebook/")
    assert (tmp_path / "job_partial_fb" / item["local_path"]).is_file()


def test_date_boundary_old_verified_post_filtered(tmp_path):
    """Only VERIFIED dates gate the lookback: an in-range dated post is kept,
    an out-of-range dated post is dropped; undated posts are never filtered
    by invented dates."""

    def dated(cmd, stage):
        if "--dump-json" in cmd:
            return 0, "\n".join(
                [
                    json.dumps(
                        {
                            "filename": "newpost",
                            "extension": "jpg",
                            "date": "2026-06-01T10:00:00+00:00",
                            "post_url": "https://www.instagram.com/p/new1/",
                        }
                    ),
                    json.dumps(
                        {
                            "filename": "oldpost",
                            "extension": "jpg",
                            "date": "2024-01-01T10:00:00+00:00",
                            "post_url": "https://www.instagram.com/p/old1/",
                        }
                    ),
                ]
            )
        (stage / "newpost.jpg").write_bytes(_png_bytes())
        (stage / "oldpost.jpg").write_bytes(_png_bytes_alt())
        return 0, ""

    result = run_social_extraction(
        "job_boundary",
        tmp_path / "job_boundary",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(dated, []),
        enable_fallbacks=False,
    )
    assert result.files_added == 1
    item = result.items[0]
    assert item["post_date"] is not None and item["date_status"] == "verified"
    # the out-of-range verified post never reaches the manifest
    old_sha = hashlib.sha256(_png_bytes_alt()).hexdigest()
    assert all(i["sha256"] != old_sha for i in result.items)
    # and it was never relocated into the job folder
    job_ig_dir = tmp_path / "job_boundary" / "instagram"
    listed = [p.name for p in job_ig_dir.iterdir()] if job_ig_dir.is_dir() else []
    assert len(listed) == 1  # only the in-range asset was relocated


def test_duplicate_fallback_posts_deduped(tmp_path):
    """Identical media delivered twice by a fallback is stored once and the
    duplicate is counted, not silently duplicated."""

    def dup_instaloader(cmd, stage):
        for day, short in ((1, "aaa111"), (2, "bbb222")):
            stem = f"2026-06-{day:02d}_12-00-00_UTC_{short}"
            (stage / f"{stem}.jpg").write_bytes(_png_bytes())
            (stage / f"{stem}.json").write_text(
                json.dumps({"shortcode": short, "date_utc": f"2026-06-{day:02d}T12:00:00Z"}),
                encoding="utf-8",
            )
        return 0, ""

    result = run_social_extraction(
        "job_dup",
        tmp_path / "job_dup",
        "instagram",
        IG_URL,
        max_files=5,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_login_required, []),
        instaloader_runner=_mk_runner(dup_instaloader, []),
        enable_fallbacks=True,
    )
    assert result.status == "partial_success"
    assert result.files_added == 1
    assert result.duplicates == 1
    shas = [i["sha256"] for i in result.items]
    assert len(set(shas)) == len(shas)


def test_coverage_metadata_records_bounded_sample(tmp_path):
    """The manifest must label social collection as a bounded sample — it must
    never read as a claim of complete 365-day coverage (req #1/#9)."""
    result = run_social_extraction(
        "job_cov",
        tmp_path / "job_cov",
        "instagram",
        IG_URL,
        max_files=6,
        lookback_days=365,
        permission_confirmed=True,
        runner=_mk_runner(_gallery_dl_ok, []),
        enable_fallbacks=False,
    )
    assert result.status == "completed"
    cov = result.coverage
    assert cov["discovery_selection"] == "bounded_sample_not_full_coverage"
    assert cov["full_coverage_claimed"] is False
    assert cov["lookback_days"] == 365 and cov["cutoff_date"]
    assert cov["discovery_limit_files"] == 6

    section = section_from_result(result)
    merged = merge_social_into_manifest(
        None,
        [("instagram", section)],
        job_id="job_cov",
        lookback_days=365,
        permission_confirmed=True,
    )
    assert merged["sources"]["instagram"]["coverage"]["full_coverage_claimed"] is False
    lowered = json.dumps(merged).lower()
    assert "complete 365-day" not in lowered
    assert "full coverage" not in lowered

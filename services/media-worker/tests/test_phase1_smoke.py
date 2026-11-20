"""Offline smoke tests. No network, no Docker, no scraping.

Phase 1 website stub test removed in Phase 2: real website extraction is
implemented and covered by tests/test_website_collector.py (in-process HTTP
fixtures). The social stubs remain until social collection (Phase 3).
"""

from app.social_collect import social_stub


def test_social_stub_requires_url():
    result = social_stub("instagram", "")
    assert result.status == "not_requested"
    assert result.files_added == 0


def test_social_stub_queued_when_url_given():
    result = social_stub("instagram", "https://www.instagram.com/examplegroomer/")
    assert result.status == "queued"
    assert result.platform == "instagram"

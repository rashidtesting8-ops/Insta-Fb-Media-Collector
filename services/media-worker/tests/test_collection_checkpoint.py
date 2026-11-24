import hashlib
from datetime import UTC, datetime

import pytest

from app.collection_checkpoint import (
    Attachment, CollectionCheckpoint, Download, InventoryPage, SourceItem, SourceStop,
)
from app.publication import frozen_window, publication_evidence
from app.website_inventory import WebsiteInventory


ORIGIN = "https://fixture.test"
WINDOW = frozen_window(datetime(2024, 3, 10, 7, 30, tzinfo=UTC))


class Feed:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def page(self, cursor):
        self.calls.append(cursor)
        response = self.pages[cursor]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def job(tmp_path):
    job_id = "a" * 32
    root = tmp_path / job_id
    root.mkdir()
    return root, {"job_id": job_id, **WINDOW}


def media(native_id, kind="image", published=None):
    return Attachment(native_id, f"{ORIGIN}/{native_id}", kind, publication_evidence(
        published, source_url=f"{ORIGIN}/{native_id}", field="datePublished", attribution="offline fixture",
    ))


def post(native_id, *attachments, expected=()):
    return SourceItem(native_id, f"{ORIGIN}/{native_id}", attachments, expected)


@pytest.mark.parametrize("source", ["instagram", "facebook"])
def test_paginated_inventory_preserves_child_ids_and_timestamp_provenance(job, source):
    checkpoint = CollectionCheckpoint(*job)
    old = media("old", published="2020-01-01T00:00:00Z")
    recent = media("new", "video", WINDOW["window_end_utc"])
    pages = {
        None: InventoryPage((post("pinned", old),), "page-2"),
        "page-2": InventoryPage((post("carousel-or-album", recent, media("preview", "thumbnail"),
                                      media("undated"), expected=("new", "preview", "missing")),)),
    }
    first = Feed(pages)
    checkpoint.collect(source, ORIGIN, first, page_budget=1)
    assert first.calls == [None]
    assert checkpoint.report(source)["stop_reason"] == "page_budget"
    restarted = CollectionCheckpoint(*job)
    second = Feed(pages)
    restarted.collect(source, ORIGIN, second, page_budget=2)
    assert second.calls == ["page-2"]
    report = restarted.report(source)
    assert {key: report[key] for key in WINDOW} == WINDOW
    assert report["listing_exhausted"] and report["coverage"] == "incomplete"
    assert report["coverage_percentage"] is None
    assets = {row["native_id"]: row for row in report["assets"]}
    assert assets["old"]["date_eligibility"] == "out_of_window"
    assert assets["new"]["date_eligibility"] == "in_window"
    assert assets["new"]["publication_evidence"] == recent.publication_evidence
    assert assets["preview"]["status"] == "thumbnail"
    assert assets["missing"]["status"] == "missing_attachment"
    assert report["unknown_date_review"] == [assets["undated"]["asset_id"]]


@pytest.mark.parametrize("failure", [SourceStop("rate_limited"), SourceStop("access_blocked"), OSError()])
def test_cursor_failure_preserves_checkpoint_for_explicit_retry(job, failure):
    state = CollectionCheckpoint(*job)
    pages = {None: InventoryPage((post("first", media("first-image")),), "next"), "next": failure}
    feed = Feed(pages)
    state.collect("facebook", ORIGIN, feed, page_budget=5)
    assert feed.calls == [None, "next"]
    assert state.report("facebook")["stop_reason"] == (str(failure) or "cursor_failure")
    replacement = Feed({"next": InventoryPage(())})
    CollectionCheckpoint(*job).collect("facebook", ORIGIN, replacement, page_budget=1)
    assert replacement.calls == ["next"]


def test_loop_and_scope_mismatch_fail_closed(job):
    state = CollectionCheckpoint(*job)
    feed = Feed({None: InventoryPage((), "loop"), "loop": InventoryPage((), "loop")})
    state.collect("instagram", ORIGIN, feed, page_budget=7)
    assert feed.calls == [None, "loop"]
    assert state.report("instagram")["stop_reason"] == "pagination_loop"
    with pytest.raises(ValueError, match="source scope"):
        state.collect("instagram", ORIGIN + "/other", feed, page_budget=1)
    with pytest.raises(ValueError, match="original job window"):
        CollectionCheckpoint(job[0], {**job[1], **frozen_window(datetime(2024, 4, 1, tzinfo=UTC))})


def test_download_resume_checksum_and_distinct_identity(job):
    state = CollectionCheckpoint(*job)
    feed = Feed({None: InventoryPage((post("one", media("image-a")), post("two", media("image-b"))))})
    state.collect("website", ORIGIN, feed, page_budget=1)
    one, two = [asset["key"] for asset in state.report("website")["assets"]]
    data = b"synthetic bytes"
    digest = hashlib.sha256(data).hexdigest()

    def crash():
        yield data[:4]
        raise OSError("connection lost")

    assert state.download(one, lambda *_: Download(len(data), crash(), revision="r1", sha256=digest),
                          byte_limit=64) == "download_interrupted"
    assert not (state.root / (one + ".media")).exists()
    calls = []

    def continue_fetch(url, offset, revision):
        calls.append((offset, revision))
        return Download(len(data), [data[offset:]], offset, "r1", digest)

    restarted = CollectionCheckpoint(*job)
    assert restarted.download(one, continue_fetch, byte_limit=64) == "verified"
    assert calls == [(4, "r1")]
    assert restarted.download(one, lambda *_: pytest.fail("verified file fetched again"), byte_limit=64) == "verified"
    assert restarted.download(two, continue_fetch, byte_limit=64) == "verified"
    report = restarted.report("website")
    assert {row["item"] for row in report["assets"]} == {"one", "two"}
    assert {row["digest"] for row in report["assets"]} == {digest}
    assert report["coverage"] == "unknown"  # Listing exhaustion is not independent verification.


def test_checksum_mismatch_and_crash_during_publish(job, monkeypatch):
    state = CollectionCheckpoint(*job)
    state.collect("website", ORIGIN, Feed({None: InventoryPage((post("page", media("pic")),))}), page_budget=1)
    key = state.report("website")["assets"][0]["key"]
    assert state.download(key, lambda *_: Download(3, [b"bad"], sha256="0" * 64),
                          byte_limit=10) == "checksum_mismatch"
    assert not (state.root / (key + ".media")).exists()
    with monkeypatch.context() as patch:
        def fail_rename(*_):
            raise OSError("simulated crash before rename")

        patch.setattr("app.collection_checkpoint.os.replace", fail_rename)
        assert state.download(key, lambda *_: Download(4, [b"good"]), byte_limit=10) == "download_interrupted"
    restarted = CollectionCheckpoint(*job)
    assert restarted.download(key, lambda *_: pytest.fail("verified staged file fetched again"),
                               byte_limit=10) == "verified"
    assert (state.root / (key + ".media")).read_bytes() == b"good"


def test_changed_revision_restarts_and_respects_byte_cap(job):
    state = CollectionCheckpoint(*job)
    state.collect("website", ORIGIN, Feed({None: InventoryPage((post("page", media("pic")),))}), page_budget=1)
    key = state.report("website")["assets"][0]["key"]
    assert state.download(key, lambda *_: Download(4, [b"ab"], revision="old"),
                          byte_limit=4) == "download_interrupted"
    calls = []

    def changed(url, offset, revision):
        calls.append((offset, revision))
        return Download(5, [b"fresh"[offset:]], offset, "new")

    assert state.download(key, changed, byte_limit=4) == "byte_limit_or_invalid_range"
    assert calls == [(2, "old"), (0, None)]
    assert state.report("website")["coverage"] == "incomplete"
    assert state.download(key, changed, byte_limit=5) == "verified"
    assert (state.root / (key + ".media")).read_bytes() == b"fresh"


def test_website_sitemap_archive_pagination_and_policy(job):
    documents = {
        "/": '<a href="/archive">Archive</a>',
        "/sitemap.xml": '<sitemapindex><sitemap><loc>/nested.xml</loc></sitemap></sitemapindex>',
        "/nested.xml": '<urlset><url><loc>/dated</loc></url><url><loc>/blocked</loc></url></urlset>',
        "/archive": '<a rel="next" href="/archive?p=2">Next</a>',
        "/archive?p=2": '<a rel="next" href="/archive">Loop</a><video src="/clip.mp4" poster="/thumb.jpg"></video>',
        "/dated": '<img src="/photo.jpg"><script type="application/ld+json">'
                  '{"@type":"ImageObject","contentUrl":"/photo.jpg","datePublished":"2024-03-10T07:30:00Z"}</script>',
    }
    calls = []

    def fetch(url):
        path = url.removeprefix(ORIGIN)
        calls.append(path)
        return url, documents[path].encode()

    site = WebsiteInventory(ORIGIN + "/", sitemaps=[ORIGIN + "/sitemap.xml"], fetch=fetch,
                            permitted=lambda url: url != ORIGIN + "/blocked", document_limit=4096, entry_limit=8)
    state = CollectionCheckpoint(*job)
    state.collect("website", ORIGIN, site, page_budget=2)
    restarted = CollectionCheckpoint(*job)
    restarted.collect("website", ORIGIN, site, page_budget=10)
    assert len(calls) == len(set(calls)) == 6
    report = restarted.report("website")
    assert set(report["issues"]) == {"pagination_loop", "access_blocked"}
    assert report["coverage"] == "incomplete"
    assert any(item["url"] == ORIGIN + "/blocked" and item["status"] == "access_blocked"
               for item in report["items"])
    assets = {row["kind"]: row for row in report["assets"]}
    assert assets["image"]["date_eligibility"] == "in_window"
    assert assets["video"]["url"] == ORIGIN + "/clip.mp4"
    assert assets["thumbnail"]["status"] == "thumbnail"

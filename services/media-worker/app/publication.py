"""Frozen collection windows and attributable source publication dates."""

from datetime import UTC, datetime, timedelta

WINDOW_FIELDS = ("created_at", "window_start_utc", "window_end_utc")
WINDOW_LENGTH = timedelta(days=365)


def aware_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp if timestamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def frozen_window(created: datetime) -> dict[str, str]:
    if created.utcoffset() is None:
        raise ValueError("Job creation must be timezone-aware")
    end = created.astimezone(UTC)
    return dict(zip(WINDOW_FIELDS, (end.isoformat(), (end - WINDOW_LENGTH).isoformat(), end.isoformat())))


def window_bounds(metadata):
    created, start, end = (aware_timestamp(metadata.get(key)) for key in WINDOW_FIELDS)
    if created is None or start is None or end is None:
        return None
    if created != end or end - start != WINDOW_LENGTH:
        return None
    return start, end


def publication_evidence(value, *, source_url, field, attribution, unix_seconds=False):
    timestamp = aware_timestamp(value)
    if unix_seconds and isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            timestamp = datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OverflowError, OSError):
            pass
    if timestamp is None or not isinstance(source_url, str) or not source_url.startswith(("https://", "http://")):
        return None
    try:
        normalized = timestamp.astimezone(UTC).isoformat()
    except OverflowError:
        return None
    return {
        "source_url": source_url,
        "field": field,
        "attribution": attribution,
        "original_timestamp": value,
        "original_timezone": None if unix_seconds else timestamp.strftime("%z"),
        "timestamp_utc": normalized,
    }


def verified_publication(asset):
    evidence = asset.get("publication_evidence")
    if asset.get("date_status") != "verified" or not isinstance(evidence, dict):
        return None
    if not all(isinstance(evidence.get(key), str) and evidence[key] for key in ("source_url", "field", "attribution")):
        return None
    timestamp = aware_timestamp(evidence.get("timestamp_utc"))
    original = publication_evidence(
        evidence.get("original_timestamp"), source_url=evidence["source_url"],
        field=evidence["field"], attribution=evidence["attribution"],
        unix_seconds=evidence["field"] in ("taken_at_timestamp", "release_timestamp"),
    )
    if original is None or evidence["timestamp_utc"] != original["timestamp_utc"]:
        return None
    return timestamp if timestamp == aware_timestamp(asset.get("post_date")) else None


def date_eligibility(asset, bounds):
    timestamp = verified_publication(asset)
    if timestamp is None:
        return "unknown_date"
    if bounds is None:
        return "window_unverified"
    return "in_window" if bounds[0] <= timestamp <= bounds[1] else "out_of_window"


def record_publication_review(manifest, job):
    manifest.update({key: job[key] for key in WINDOW_FIELDS if key in job})
    bounds = window_bounds(job)
    review = []
    for asset in manifest.get("assets", []):
        state = date_eligibility(asset, bounds)
        asset["date_eligibility"] = state
        if state == "unknown_date":
            asset.update(post_date=None, date_status="unknown_date")
            if asset.get("extraction_status") in ("downloaded", "duplicate"):
                review.append(asset["asset_id"])
    manifest["unknown_date_review"] = review

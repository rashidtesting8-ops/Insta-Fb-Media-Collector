"""Named-admin view of a single immutable collection snapshot."""
import json
import re

from app.publication import date_eligibility, window_bounds


def source_rows(manifest, inventory):
    rows = []
    for name in ("website", "instagram", "facebook"):
        source = {**inventory.get(name, {}), **manifest.get("sources", {}).get(name, {})}
        assets = [
            a for a in manifest["assets"]
            if a.get("platform") == name and a.get("type") != "frame"
        ]
        dates = [date_eligibility(a, window_bounds(manifest)) for a in assets]
        # Export only status codes. Extractor diagnostics may include private URLs.
        codes = [source.get(key) for key in ("status", "stop_reason", "termination_reason", "reason")]
        for issue in source.get("issues") or []:
            codes.append(issue.get("reason") if isinstance(issue, dict) else issue)
        codes = list(dict.fromkeys(c for c in codes if isinstance(c, str) and re.fullmatch(r"[a-z0-9_]{1,80}", c)))
        rows.append(dict(source=name, discovered=len(assets),
                         downloaded=sum(a.get("extraction_status") == "downloaded" for a in assets),
                         unknown_date=dates.count("unknown_date"), out_of_window=dates.count("out_of_window"),
                         missing_children=sum(a.get("extraction_status") == "missing_attachment" for a in assets),
                         coverage="unknown", evidence="Independent inventory unavailable", outcomes=codes))
    return rows


def panel(con, result, actor, submitter, current, start):
    job, rev = result["job_id"], result["revision"]
    begin, end, manifest, subset, inventory = con.execute("""SELECT window_start_utc,window_end_utc,
        manifest,subset,inventory FROM collection_revisions WHERE job=? AND revision=?""", (job, rev)).fetchone()
    history = con.execute("""SELECT revision, actor, action, reason, utc FROM collection_decisions WHERE job=?
        UNION ALL SELECT revision,actor,'start',NULL,utc FROM collection_ai_starts WHERE job=?
        ORDER BY utc,revision""", (job, job)).fetchall()
    eligible = json.loads(subset)
    identified = current and bool(con.execute("SELECT 1 FROM named_accounts WHERE id=?", (submitter,)).fetchone())
    state = result["state"]
    return {**result, "window_start_utc": begin, "window_end_utc": end,
            "sources": source_rows(json.loads(manifest), json.loads(inventory)),
            "eligible_assets": [candidate["asset_id"] for candidate in eligible["candidates"]],
            "inherited_assets": eligible["inherited"],
            "history": [dict(zip(("revision", "actor", "action", "reason", "utc"), row)) for row in history],
            "permissions": {"approve": identified and actor != submitter and state != "approved",
                            "deny": identified and state != "denied", "revoke": identified and state == "approved",
                            "start": identified and state == "approved" and not start}}

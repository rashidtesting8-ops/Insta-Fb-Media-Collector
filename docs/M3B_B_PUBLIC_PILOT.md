# Lily’s Club public-source pilot

The owner designated the following URLs. They identify public sources. Access, live requests, media retention and reuse remain subject to separate approval.

- Website: [https://www.lilys-club.co.uk/](https://www.lilys-club.co.uk/)
- Instagram: [https://www.instagram.com/lilysclubuk/](https://www.instagram.com/lilysclubuk/)
- Facebook: [https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/](https://www.facebook.com/people/Lilys-club-Dog-Grooming/61564648418385/)

No CMS inventory, account credentials, social exports, API permissions or media reuse rights were supplied. The website URL passes the worker’s HTTP(S) input format, and the social URLs match the existing HTTPS host allowlists. The Facebook `/people/…/<id>/` URL is a profile-style URL. Its syntax provides no evidence of publicly enumerable Page history.

## Default-off worker integration

The legacy bounded path remains the default. The new path requires server-side `PUBLIC_COLLECTION_CHECKPOINTS=1` and `PUBLIC_COLLECTION_CONFIG` pointing to an operator-controlled JSON file. The configuration’s `sources` entries must exactly match the submitted website, Instagram and Facebook URLs. Each source requires explicit `page_budget`, `transfer_budget`, `byte_limit`, `request_budget`, `time_budget_seconds` and `media_origins`. The website also takes approved `sitemaps`, `document_limit` and `entry_limit`. No values are configured for this pilot.

The website adapter handles the configured origin, approved sitemap indexes, archive links, next links, galleries exposed in HTML and direct media. It checks DNS and redirect scope, robots rules and the approved media origins before network hops. Initial downloads require declared length and a supported media type. Range continuation requires a strong ETag and valid range; the checkpoint stores a computed SHA-256 and publishes files atomically. SVG and compressed range responses are excluded on this path. An origin that returns a rate limit or challenge is held for the rest of that execution.

Instagram and Facebook accept a local, operator-supplied public snapshot for the exact profile URL and still require the existing social collection acknowledgement. The snapshot can contain gallery-dl URL records or normalized post and attachment records. The worker does not launch the extractor, log in, or create a cursor for a bounded snapshot. Without a supplied snapshot, it records that public inventory is unavailable. Media downloads, when separately authorized, use the same guarded transport. A file hash does not merge post or attachment provenance.

Listing exhaustion supplies no independent coverage evidence. The manifest keeps the M2 window, publication evidence and unknown-date review queue. The worker, AI pipeline and delivery planning enforce the collection hold. No part of M4 is implemented. An explicitly restarted execution can reopen checkpoints for the same job and scope. This milestone adds no automatic requeue or live retry policy.

M3B-C adds a separate recorded-only entry to the same authenticated `POST /jobs` route. It reads an operator-local configuration and never fetches a URL or launches an extractor. This can exercise the pilot URL formats with recordings while live access is unavailable. It does not require an owner export, establish permission for automated collection, or supply independent inventory evidence.

## Proposed live test, pending approval

1. Start with one isolated website discovery job only. Proposed maximum is six HTTP requests total, including robots and redirects, four documents, 256 KiB per document and a 30-second execution budget. Approve the sitemap and archive roots first. Stop on a restriction, challenge, rate limit or cap. Set `transfer_budget` to zero for discovery only. Media downloads require separate approval and confirmed CDN origins.
2. For Instagram and Facebook, confirm a permitted public access method or obtain a permitted offline snapshot. Until then, record the sources as unavailable for pilot coverage. If live public discovery is approved later, run at most one bounded listing per source under owner-approved budgets and stop on the first access restriction. Keep Groq and Drive disabled.
3. Compare findings with an independent authorized inventory if one becomes available. Without it, report observed discovery and gaps with coverage unknown.

## Owner input before live operation

- Approve the exact sources, sitemap and archive roots, gallery or embed scope, allowed origin and redirect list, and any CDN hosts.
- Confirm permitted content types and public-source access method. Supply a trustworthy dated inventory or approve reporting coverage as unknown.
- Approve exact request, page, entry, file, byte and time limits, concurrency, storage and retention. Approve media downloads separately from discovery and provide any reuse rights separately.

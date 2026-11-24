# Phase 10 guarded live E2E pre-flight

The mock QA workflow `b627ee2205a34c7a` and canonical Phase 7 workflow remain separate from the inactive live draft `a10e2e0f3194c2e1`. The live draft has unique disabled webhooks, a disabled guarded bridge, and both disconnected Drive write templates bound to the local test credential. Its intake creates an extraction-only, website-only job. It limits collection to one page and one media item. Browser input carries no live authorization.

## One proposed live test

| Scope | Proposed value |
| --- | --- |
| Candidate | Locally drawn `storage/media-delivery-preflight-20261123/synthetic-pup-bath.jpg`, 240 × 240 and 10,013 bytes. SHA-256 `e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162`. |
| Authorization | The JPEG has prior authorization and evidence for one standalone Drive test only. There is currently no approval for fresh Groq inference and job-scoped E2E delivery. The original genuine zero-selection source remains unverified and is ineligible. |
| Source | Obtain approval for an exact controlled website fixture URL serving the unchanged JPEG. Provision a fixture-only route that respects the collector's network safeguards. Leave normal worker SSRF controls intact. Obtain the new job ID and original manifest asset ID from that extraction before granting inference. |
| Groq | `qwen/qwen3.8-27b`. MODE A sends the approved JPEG bytes once, with a 1,200-token maximum. Text-only MODE B runs only after MODE A passes, with a 900-token maximum. Reasoning disabled, at most two attempts, zero retries, and a $0.25 estimated ceiling checked before sending. Stop on any HTTP, finish reason, schema, reference or cost failure. |
| Drive | Existing owner-only folder `1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF`. Verify identity against the prior text receipt, root and folder owner-only permissions, and list the exact filename and folder before any write. Reuse the previous matching JPEG file `1TafQrXRM72MecjIWs3MWEmC8TCIeEzyB` if its metadata, ACL and downloaded checksum still match. |
| Conditional write | Only if no match exists and explicit separate Drive approval permits it, send one multipart upload and the installed node's metadata update on that same file. No new folders, sharing changes or automatic upload retries. Read back metadata, ACL and SHA-256. Resolve any ambiguous write with reads only; stop if reconciliation is inconclusive. |

## Wiring and release checks

1. Provision a dedicated QA worker and storage root, the `/guarded-job` app factory, and a private n8n adapter session. Wire its job read and AI-status persistence callbacks to the same isolated worker record, and its Drive callback to `deliverGuardedE2eJpeg` in the project n8n credential context. Use a separate adapter header secret and scoped worker credentials. Keep the saved draft inactive as the credential template. Use a separate execution copy in the isolated acceptance instance when the operator authorizes the test. The isolated service endpoints and approval configuration are not provisioned by this pre-flight.
2. Start the loopback frontend against the dedicated QA worker and distinct QA intake route. After approval for collection, submit one authenticated extraction-only job and verify the dashboard, worker record, manifest, original preview and exact approved bytes share that job ID and hash.
3. Configure a private operator grant for that job ID, website URL, exact source URL, manifest asset ID, hash and destination. Keep inference and delivery grants independent. Resume the existing job once from the authenticated QA intake interface after the grant is installed. The worker validates the physical JPEG and its preprocessing state against that grant before loading the provider. An intent file prevents re-running inference for the same job after a partial or failed attempt.
4. Persist curation and the authenticated worker AI status only after both Groq stages validate. Reject mock labels, scoped asset mismatches, and a model selection changed by normalization. Leave Drive untouched when MODE B selects zero assets, the manifest rights are unverified, or the separate Drive approval is missing.
5. For an eligible selection, use the n8n-owned OAuth adapter and a per-job reconciliation ledger. Send only the matching verified receipt to the worker delivery ledger. Compare its job ID, asset ID, upload key, folder, file ID and checksum to the dashboard status and artifact. Keep the draft inactive outside the separately approved test.

The previous privately uploaded JPEG may make this run a reuse-and-reconciliation test with zero new writes. A live test that must prove a fresh upload needs a separately approved, nonmatching fixture or exact destination filename.

## Offline evidence

The ambiguous-write scenario passed on a fresh disposable stack for authenticated job `8a72fee602624f67bcb406353b6ea223`. The same dashboard job reached n8n, worker extraction and mock AI, then displayed the verified worker ledger after fake Drive reconciliation with one simulated write. The prior full rerun passed its other cases but encountered a transient local manifest HTTP 502 on the ambiguous case. No external operation was retried.

Worker tests passed 158/158. Graph and Drive regressions passed 13/13, with four additional installed-node fake-transport scenarios passing against the saved disabled draft. Frontend unit and mock integration tests, build, type checks and `git diff --check` passed.

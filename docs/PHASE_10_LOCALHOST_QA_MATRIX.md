# Phase 10 localhost acceptance matrix

**Decision: PASS for isolated mock end-to-end acceptance. BLOCKED for live localhost acceptance.** A single synthetic job completed each tested mock path through the built dashboard, n8n, worker, and back to the dashboard. The saved genuine Groq and private Drive tests remain separate evidence. No Groq or Google Drive API calls were made during these runs.

Project QA workflow `b627ee2205a34c7a` is inactive and unpublished, with distinct QA webhook paths and both disabled Drive v3 templates bound only to the local test OAuth credential. Test activation occurred only in a disposable n8n database on an internal Docker network. The disposable containers and network were removed.

**Status key:** PASS means the stated scope passed. FAIL means a checked requirement is unmet. BLOCKED means a prerequisite prevents the check. NOT TESTED means the scenario was outside this run.

| Scenario | Status | Evidence and required result |
| --- | --- | --- |
| Project n8n, worker and legacy control | PASS | Project n8n readiness on 5679, worker health and readiness on 8788, and legacy n8n health on 5678 returned HTTP 200. |
| Frontend loopback smoke | PASS | Temporary built frontend on 3000 returned HTTP 200 for login and HTTP 401 for anonymous health. The test process was stopped. |
| Persistent frontend availability on port 3000 | FAIL | No frontend listener existed at baseline or after the temporary smoke. Start the built frontend on loopback for the actual acceptance run. |
| Frontend signed sessions, security and intake route | PASS | Six unit tests and one built-dashboard test with mocked upstreams passed. Signed sessions, expired or invalid signatures, protected requests and routing to n8n were covered. |
| Worker collection, preprocessing and failure states | PASS | All 152 tests passed with external networking disabled. Coverage includes website and social fixtures, successful, failed and partial jobs, deduplication, path and checksum checks, persistence, and mock suppression. |
| Existing workflow routing | PASS | The published localhost MVP still owns the intake and status webhooks and contains no Drive nodes. The original canonical workflow and the project QA copy remain unpublished. |
| Isolated adapter compatibility and upload guards | PASS | Both isolated v3 nodes have the test credential binding and remain disabled with no workflow execution. Four mock scenarios exercised the installed handler with the exact JPEG: repeat after success, lost response with a match, lost response with no visible match, and a shared-folder rejection. |
| OAuth refresh and retry guards | PASS | Nine Node regressions passed, including one in-memory refresh after an initial identity 401, refresh rejection, repeated identity 401, no refresh or replay after a write, checksum mismatch, concurrent-lock rejection, and duplicate prevention. A recovered match is read back before marking it verified. |
| Genuine Groq MODE A and MODE B | PASS | Offline revalidation of the HTTP 200 MODE A checkpoint and HTTP 200 MODE B result. The checkpoint digest and input JPEG SHA-256 matched, both schemas passed, and the model was `qwen/qwen3.8-27b` with non-mock output. MODE B used 1,153 input and 850 output tokens. |
| Original unverified source excluded | PASS | The genuine curation has zero selections, the source manifest remains unverified, and the worker delivery plan has zero pending uploads. The curation and manifest match their preserved hashes. |
| Per-asset rights enforcement in the isolated canonical QA copy | PASS | n8n forwards only pending keys marked verified with no rights review required. The fake adapter reloads the worker plan before reading any bytes. In the mixed case, one verified asset was delivered and the unverified asset was excluded. The no-eligible case issued zero writes. The original canonical graph still uses job-level approval. |
| Prior text and JPEG Drive receipts | PASS | Both local receipts are verified, report owner-only file permissions, and match the local fixtures' SHA-256 values and recorded IDs. The JPEG upload, metadata update and download were HTTP 200. These are saved results from two earlier authorized tests. |
| Drive v3 fields in the isolated canonical QA copy | PASS | The saved copy uses `driveId`, `folderId`, `name` and `inputDataFieldName`. Both native nodes are disabled and disconnected. In the disposable run, the QA delivery step uses an internal fake adapter. The original canonical export still has legacy fields and placeholder credential IDs. |
| Verified receipts reach the worker ledger and dashboard | PASS | n8n verified the response job ID, upload key, unique receipt, owner-only check, checksum and file ID before marking the worker ledger entry uploaded. A mock ambiguous write was recovered by read-only reconciliation and reached the same job's dashboard without a second write. |
| Full integrated mock positive and negative jobs | PASS | Seven scenarios and one duplicate submit passed with the real built frontend, actual n8n 2.48.7, real worker scraping and preprocessing, mock MODE A/B, and fake Drive. See the scenario matrix below. |
| Full integrated live positive job | BLOCKED | The project QA graph has no live adapter connection. A fresh job needs a separately approved source and exact Groq and Drive approvals. Drive delivery requires a genuine eligible selection and per-asset rights evidence. |
| Live duplicate, concurrent retry, process restart, lost ledger | NOT TESTED | Covered with offline cases only. The isolated runner's lock protects one host and one stable ledger path. Cross-host or lost-ledger recovery has no live evidence. |
| Live social coverage and authorized real-media quality | NOT TESTED | Website-only fixture coverage and synthetic assets do not establish performance on authorized real pet-grooming media or live social sources. |
| Build, lint and working-tree checks | PASS | 152 worker tests, six frontend unit tests, one frontend integration test, and 11 graph/Drive regression tests passed. Frontend build and TypeScript passed. Lint emitted three existing warnings. Worker tests emitted existing Pillow deprecation warnings. `git diff --check` passed. |

## Collection V1.1 acceptance baseline

The earlier mock and live component results predate the [Collection V1.1 requirements](PROJECT_SPEC.md#collection-v11--owner-confirmed). Milestones 1 and 2 cover intake validation and date eligibility offline. Live workflow verification and historical coverage are separate acceptance steps.

| Gate | Current status | Required acceptance evidence |
| --- | --- | --- |
| Three mandatory sources | PASS (offline) | 161 worker tests, eight frontend unit tests, one built-dashboard integration test and five isolated n8n graph tests passed. Missing, wrong-platform and malformed URLs are rejected before job creation. The worker verifies that rejected requests leave the historical job list intact and valid retries reuse the job ID. The canonical n8n graph delegates to the worker validator before its create node. |
| Frozen UTC window and publication eligibility (M2) | PASS (offline) | Tests cover both inclusive endpoints, offsets, leap day, DST, invalid or naive timestamps, website and social metadata attribution, duplicate provenance, restart and idempotent retry. Unknown dates enter the manifest review queue. Unknown and out-of-window media and historical jobs without a verified window are blocked from AI payloads, including the guarded QA path. A worker request cannot supply its own window or publication evidence. |
| Historical pagination and attachment coverage | PASS for M3A fixtures. Real-source acceptance BLOCKED | The opt-in checkpoint tests cover nested sitemaps, archive pagination, old pinned posts, separate album or carousel children, video versus thumbnail, cursor failure, rate limits, access denial, caps, interrupted transfers, checksum mismatch and recovery before atomic publication. The original frozen window and distinct source identities survive restart. Exhausted listing remains unknown without independently verified inventory. Real transports, source permissions and coverage reconciliation remain M3B dependencies. |
| Fixture-only worker integration (M3B-A) | PASS (offline) | Two fresh worker processes use the same fixture checkpoint and immutable window. Recorded website robots rules, sitemap and archive pagination, Instagram and Facebook cursors, missing children, changed transfer revisions, checksum validation and separate provenance for equal hashes are exercised together. Reports stay unknown or incomplete. Output is a separate fixture manifest, outside normal intake, AI and delivery. Real source integration remains blocked on M3B-B approvals. |
| Default-off public checkpoint worker (M3B-B) | PASS (offline). Live pilot BLOCKED | Mocked HTTP exercises the three designated Lily’s Club URL formats, guarded website sitemap and archive traversal, robots and redirect policy, bounded public social snapshots, media provenance and range validation. Unverified collection blocks both AI and Drive, including through direct worker entry points. See the [pilot configuration and approval record](M3B_B_PUBLIC_PILOT.md). |
| Recorded extractor handoff (M3B-C) | PASS (offline). Live discovery BLOCKED | Authenticated intake with the default-off recorded mode uses only local artifacts. The regression exercises source and attachment provenance, gallery-dl, Instaloader and yt-dlp shapes, ambiguous dates, recorded stops and continuations, interrupted transfer recovery, idempotent requeue, duplicate bytes and direct AI/Drive denials. Coverage remains unknown with no percentage. |
| Partial-collection approval gate | FAIL | Pending or declined jobs make zero Groq calls. An explicit approval for the specific job and reviewed evidence permits only the approved subset to continue. Preserve source gaps in the dashboard and verify that the approval leaves media rights unchanged. |
| Selection and delivery against the infographic | BLOCKED | Supply the authoritative infographic and version, authorized sources and inventory, and approved test permissions. Verify quality checks and deduplication precede Groq, selection matches the infographic, and rights-unverified assets never reach Drive. |

## Integrated mock results

| Scenario | Status | Worker delivery ledger | Dashboard stage | Fake upload attempts |
| --- | --- | --- | --- | --- |
| Verified synthetic asset | PASS | `completed`, one verified receipt | `completed` | 1 |
| Mixed verified and unverified selections | PASS | `partial_success`, only the verified asset uploaded | `partial_success` | 1 |
| No eligible selections | PASS | `not_started` | `blocked` | 0 |
| Upload unavailable | PASS | `failed`, no verified receipt | `blocked` | 1 |
| OAuth expired, refresh succeeds | PASS | `completed`, one refresh and one verified receipt | `completed` | 1 |
| OAuth refresh rejected | PASS | `failed` | `blocked` | 0 |
| Drive accepts the bytes but the response is lost | PASS | `completed`, existing file recovered and verified | `completed` | 1 |
| Duplicate dashboard submit | PASS | Original ledger and job ID reused | `completed` | 0 additional |

## Evidence anchors

- Worker and n8n design baseline: [Phase 10 report](PHASE_10_REPORT.md) and [canonical workflow](../n8n-media/07-pipeline-orchestration.json).
- Stage-specific offline code and tests: [n8n adapter and mock command](../n8n-media/README.md).
- Saved Groq artifacts in ignored local storage: `storage/groq-acceptance-fixtures/live-acceptance-checkpoint-2e01cb8ce504/mode-a-validated.json` and `storage/groq-acceptance-fixtures/live-acceptance-mode-b-enum-fix-2e01cb8ce504/` containing `curation.json`, `acceptance-result.json` and `attempts.json`. Authorized source SHA-256 is `2e01cb8ce50473e7901d70777ef7f9578feb358d7642d2e6e78df5442c5cf986`.
- Saved Drive records in ignored local storage: `storage/drive-upload-preflight-20261123/test-delivery-ledger.json` and `storage/media-delivery-preflight-20261123/jpeg-delivery-ledger.json` plus its `jpeg-acceptance-result.json`.

| Prior private Drive artifact | Folder ID | File ID | Verified SHA-256 |
| --- | --- | --- | --- |
| Synthetic text | `1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF` | `1lC_OUVRwXDM1NwdG8vqAZykcAHP_W8yA` | `12c8eb2ef38561b1443c97207a71afb56a0a7a2c9c0b779689bb99e4c340ab1c` |
| Synthetic JPEG | `1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF` | `1TafQrXRM72MecjIWs3MWEmC8TCIeEzyB` | `e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162` |

## Minimum path to local acceptance

1. Review and connect the guarded live n8n adapter to a separate acceptance instance of the canonical QA graph. Use isolated worker storage and ports or QA routes distinct from the published Drive-disabled MVP. Verify the actual OAuth binding, worker token scopes, and how trusted asset-level rights evidence is recorded.
2. Obtain explicit approval for one eligible source and the exact Groq and Drive actions. Start the loopback frontend and use a fresh website-only job with bounded collection and preprocessing. Proceed only if the genuine, strictly validated curation selects the specifically approved asset. Preserve the genuine zero-selection job.
3. Verify the private destination and reconcile any match. Upload one approved original only if needed, with zero automatic retries. Check owner-only permissions, downloaded SHA-256, final worker ledger and dashboard status for that same job. Restore the QA workflow to inactive, retain the evidence, and leave the canonical and legacy workflows intact.

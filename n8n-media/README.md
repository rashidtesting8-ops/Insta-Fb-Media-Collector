# n8n media workflows

- `00` verifies worker health, worker authentication and shared storage.
- `05` reads curated job status from the worker.
- `06` delivers selected originals after rights approval.
- `07` validates intake, responds with a job ID and continues orchestration.

Configure and bind instance-specific credentials before publishing. See the
[Phase 9 staging runbook](../docs/PHASE_9_REPORT.md). Never commit real credentials.

## Isolated Drive compatibility check

Generate an unpublished copy from the repository root:

```powershell
node n8n-media/private-drive-delivery.mjs storage/media-delivery-preflight-20261123/07-drive-compatibility-unpublished.json
node --test n8n-media/private-drive-delivery.test.mjs
```

This export disables both webhooks and disconnects both disabled Drive nodes. It keeps the credential placeholders. The generator never imports the copy into n8n.

After importing the isolated copy into project n8n and binding both disabled nodes to the local test credential, run the mock transport pre-flight:

```powershell
node n8n-media/private-drive-jpeg-preflight.mjs <workflow-id> storage/media-delivery-preflight-20261123
```

The command-line entry runs offline only. It checks stored credential references and invokes the installed upload handler with an in-memory fake transport. A separately approved call to `runApprovedJpeg` uses the same handler and bound credential in the project n8n container, verifies the prior text receipt and destination, and uses `jpeg-delivery-ledger.json` in the isolated directory. Keep the same ledger path for reconciliation. The adapter requires explicit approval for the exact JPEG, hash, destination, and workflow. It may refresh the OAuth grant in memory after an initial read-only HTTP 401. No credential contents are exported and no upload retry is performed.

The runner records intent before upload, verifies owner-only permissions, paginates by exact filename and parent, and verifies the downloaded SHA-256. If a request may have completed, an absent remote match blocks a repeat write. Retain the same ledger and never clear a stale lock until the operator confirms no other process is running and reconciles remote state. The lock coordinates a single host and ledger path.

## Canonical Phase 10 mock E2E

Build the frontend locally, then run the fixture harness from the repository root:

```powershell
Push-Location frontend
npm run build
Pop-Location
& ./n8n-media/qa-fixtures/run.ps1
```

The harness uses local n8n and worker images on a disposable internal Docker network. It publishes the QA graph only in the empty fixture n8n database, uses synthetic worker jobs and fake credentials, and exposes no host ports. It checks dashboard intake and the final dashboard status and delivery ledger for the same job ID. Both native Drive templates remain disabled. The QA graph sends only individually verified upload keys to the fixture adapter, which independently rechecks the worker plan and verifies fake Drive metadata, ownership, and original bytes before returning a receipt.

Generate the inactive project review copy with `node n8n-media/canonical-qa-workflow.mjs <new-export-path>`. Its QA-only webhooks and native Drive write templates are disabled. The fixture adapter and mock worker are absent from the running project. A live follow-up requires a separately reviewed adapter connection and explicit authorization for the source, Groq requests and Drive writes.

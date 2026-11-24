# Phase 10 local execution service

The unchanged guarded draft `a10e2e0f3194c2e1` retains disabled webhooks, bridge and Drive nodes. The separate unpublished workflow `ea10e2e001b0c2e1` uses fixture-only header credentials and contains zero native Drive nodes. Its only enabled trigger is the one-off test intake webhook. Production and legacy workflow registrations stay separate.

- Project intake for a controlled test is `POST http://127.0.0.1:5679/webhook-test/phase10-exec-pipeline-intake`. The n8n editor must be listening for the test event. The URL is inactive between controlled tests.
- The fixture frontend runs inside the n8n Docker network namespace. It uses `N8N_URL=http://127.0.0.1:5678` and `N8N_INTAKE_PATH=/webhook-test/phase10-exec-pipeline-intake` with the fixture intake credential. This is the container side of the project's host port 5679, separate from legacy n8n's host port 5678. Normal frontend intake keeps its existing default unless the exact QA path is configured.
- The fixture worker and guarded mock adapter share n8n's container namespace and listen on `127.0.0.1:8877`. The fake Drive adapter listens on `127.0.0.1:8899`, and the deterministic source page is `http://127.0.0.1:8790/guarded/` inside the namespace. Docker publishes none of these ports. The frontend session, n8n header, worker tokens, bridge token and fake Drive token are separate. Only the test service uses the ignored fixture credential file.
- The fixture worker verifies the SHA-256 of the original 240 × 240 synthetic JPEG. Fixture rights and mock AI results exist only in the separate QA job store. The bridge labels its receipt `fixture`; the live generator only accepts `live` receipts. No real source-processing or Drive-delivery authorizations were granted. The [blank operator grant](../n8n-media/qa-fixtures/guarded-approval.example.json) has both approvals set to false.

## Repeat the offline verification

Build the frontend, then run the following from the repository root in PowerShell:

```powershell
node n8n-media/qa-fixtures/provision-project.mjs
& ./n8n-media/qa-fixtures/start-project-execution.ps1
& ./n8n-media/qa-fixtures/verify-project-execution.ps1
```

The verification command runs the same execution graph with a simulated test endpoint in a disposable n8n database and a Docker namespace with networking disabled. It checks unauthenticated request rejection, one authenticated frontend job, exact asset hash and stable upload key, mock-only curation, fake Drive checksum and worker ledger status. The copied workflow may be active only for the duration of this disposable test. The saved project workflow remains unpublished.

To exercise the configured project endpoint with only fake services, use **Listen for test event** on the fixture intake node in the separate project copy, then run `docker exec pgm-phase10-local-drive node /repo/n8n-media/qa-fixtures/project-dashboard.mjs`. The script starts a temporary fixture frontend and stops it after the check. Keep the workflow unpublished.

The separate live plan and eligibility checks are in [Guarded live pre-flight](PHASE_10_GUARDED_LIVE_PREFLIGHT.md). Real execution still requires an approved source URL, a recorded per-job Groq grant, and an independent conditional Drive grant for the genuinely selected asset. The fixture adapter never switches to a real provider.

## Verification result

The networking-disabled run completed authenticated job `e939d96742f7430ba47e58cca4a7dc5e`, with asset `web_0001_e5c5dd3e2d67` and SHA-256 `e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162`. The worker ledger and dashboard both showed `completed` after two mock AI stages and one fake Drive write. The first run exposed a missing MODE B increment in the mock usage counter, which was corrected before the passing rerun.

The project-side fixture worker returned readiness HTTP 200. The worker, guarded adapter and fake Drive rejected unauthenticated requests. The two fixture service ports were unreachable on the container's non-loopback interface. The project editor's test webhook remained unarmed because browser control could not verify the current window URL. The test on the disposable instance therefore confirms the graph and services, while the project-port HTTP submission remains unverified.

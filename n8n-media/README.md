# n8n media workflows

- `00` verifies worker health, worker authentication and shared storage.
- `05` reads curated job status from the worker.
- `06` delivers selected originals after rights approval.
- `07` validates intake, responds with a job ID and continues orchestration.

Configure and bind instance-specific credentials before publishing. See the
[Phase 9 staging runbook](../docs/PHASE_9_REPORT.md). Never commit real credentials.

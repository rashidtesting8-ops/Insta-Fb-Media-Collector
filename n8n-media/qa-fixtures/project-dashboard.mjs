import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout } from "node:timers/promises";
import { intakePath } from "./project-execution.mjs";

const base = "http://127.0.0.1:8880";
const n8n = "http://127.0.0.1:5678";
const worker = "http://127.0.0.1:8877";
const drive = "http://127.0.0.1:8899";
const sha256 = "e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162";
const { PGM_QA_FRONTEND_PASSWORD: password, PGM_QA_DASHBOARD_TOKEN: workerToken,
  PGM_QA_INTAKE_TOKEN: intakeToken, PGM_QA_SESSION_SECRET: sessionSecret, PGM_QA_DRIVE_TOKEN: driveToken } = process.env;
assert.equal(process.env.PGM_QA_LOOPBACK, "1");
assert.ok([password, workerToken, intakeToken, sessionSecret, driveToken].every((value) => value?.length >= 32));

const waitFor = async (check, name, attempts = 80) => {
  for (let attempt = 0; attempt < attempts; attempt++) {
    const result = await check();
    if (result) return result;
    await setTimeout(400);
  }
  throw new Error("Timed out waiting for " + name);
};
const responded = (url) => fetch(url, { signal: AbortSignal.timeout(3000) }).then((response) => response.ok, () => false);
await waitFor(() => responded(worker + "/ready"), "isolated worker");
await waitFor(() => responded(n8n + "/healthz/readiness"), "n8n", 500);
assert.equal((await fetch(worker + "/jobs")).status, 401);
assert.equal((await fetch(drive + "/health")).status, 401);
assert.equal((await fetch(worker + "/guarded-job", {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ job_id: "0".repeat(32) }),
})).status, 401);
const next = spawn(process.execPath, ["/repo/frontend-build/server.js"], {
  cwd: "/repo/frontend-build", stdio: "inherit", env: { ...process.env, PORT: "8880", HOSTNAME: "127.0.0.1",
    DASHBOARD_ORIGIN: base, DASHBOARD_PASSWORD: password, DASHBOARD_SESSION_SECRET: sessionSecret,
    MEDIA_WORKER_DASHBOARD_TOKEN: workerToken, N8N_PIPELINE_TOKEN: intakeToken,
    WORKER_URL: worker, N8N_URL: n8n, N8N_INTAKE_PATH: "/webhook-test/" + intakePath },
});
const nextClosed = once(next, "close");
try {
  await waitFor(() => responded(base + "/api/ready"), "QA frontend");
  assert.equal((await fetch(base + "/api/jobs")).status, 401);
  const headers = { origin: base, "content-type": "application/json" };
  const login = await fetch(base + "/api/auth/login", { method: "POST", headers, body: JSON.stringify({ password }) });
  assert.equal(login.status, 200);
  const cookie = login.headers.get("set-cookie").split(";")[0];
  console.log("Fixture frontend signed in; submitting one job to the registered project n8n test webhook.");
  const intake = await fetch(base + "/api/jobs", {
    method: "POST", headers: { ...headers, cookie }, body: JSON.stringify({
      website_url: "http://127.0.0.1:8790/guarded/", idempotency_key: "phase10-exec-" + Date.now(),
      instagram_url: "https://www.instagram.com/pgm_qa_fixture/",
      facebook_url: "https://www.facebook.com/pgm_qa_fixture/",
    }),
  });
  const accepted = await intake.json();
  assert.equal(intake.status, 202, JSON.stringify(accepted));
  const id = accepted.job_id;
  assert.match(id, /^[a-f0-9]{32}$/);
  const at = async (suffix) => {
    const response = await fetch(base + "/api/jobs/" + id + suffix, { headers: { cookie }, signal: AbortSignal.timeout(3000) });
    assert.equal(response.status, 200, suffix);
    return response.json();
  };
  const ledger = await waitFor(async () => {
    const data = await at("/delivery");
    return data.status === "completed" && data;
  }, "worker delivery ledger");
  const status = await at("/status");
  const manifest = await at("/manifest");
  const curation = await at("/curation");
  assert.equal(status.stage, "completed");
  for (const record of [ledger, status, manifest, curation]) assert.equal(record.job_id, id);
  assert.equal(curation.ai_envelope.mock, true);
  assert.equal(curation.curation.analysis_coverage.mock, true);
  const [asset] = manifest.assets;
  assert.equal(manifest.assets.length, 1);
  assert.equal(asset.sha256, sha256);
  assert.match(asset.rights_evidence, /mock-only/);
  assert.deepEqual(curation.curation.selected_asset_ids, [asset.asset_id]);
  assert.equal(curation.ai_envelope.usage.requests, 2);
  assert.equal(curation.ai_envelope.usage.retries, 0);
  assert.equal(ledger.uploaded.length, 1);
  const [entry] = ledger.uploaded;
  assert.equal(entry.status, "uploaded");
  assert.equal(entry.asset_id, asset.asset_id);
  assert.equal(entry.sha256, sha256);
  assert.equal(entry.upload_key, asset.asset_id + ":" + sha256);
  const trace = await (await fetch(drive + "/trace/" + id, { headers: { "x-qa-drive-token": driveToken } })).json();
  assert.deepEqual(trace.attempts, [entry.upload_key]);
  assert.equal(trace.files, 1);
  console.log(JSON.stringify({ passed: true, jobId: id, assetId: asset.asset_id, sha256,
    dashboardStage: status.stage, ledger: ledger.status, mockInferenceRequests: 2, mockDriveWrites: 1, externalCalls: 0 }));
} finally {
  if (next.exitCode === null) next.kill();
  await nextClosed;
}

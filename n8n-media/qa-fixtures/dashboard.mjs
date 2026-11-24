import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout } from "node:timers/promises";

const base = "http://127.0.0.1:3000";
const n8n = "http://127.0.0.1:5678";
const worker = "http://pgm-qa-worker:8787";
const adapter = "http://pgm-qa-adapter:8799";
const password = "offline-fixture-dashboard-password";

async function waitFor(fn, description, attempts = 120) {
  for (let count = 0; count < attempts; count++) {
    const ready = await fn();
    if (ready) return ready;
    await setTimeout(500);
  }
  throw new Error("Timed out waiting for " + description);
}

for (const url of [n8n + "/healthz/readiness", worker + "/ready", adapter + "/health"]) {
  await waitFor(() => fetch(url).then((response) => response.ok, () => false), url, 480);
}
const next = spawn(process.execPath, ["/repo/frontend/.next/standalone/server.js"], {
  cwd: "/repo/frontend", stdio: "inherit", env: { ...process.env, PORT: "3000", HOSTNAME: "127.0.0.1",
    DASHBOARD_ORIGIN: base, DASHBOARD_PASSWORD: password, DASHBOARD_SESSION_SECRET: "offline-fixture-independent-signing-secret",
    MEDIA_WORKER_DASHBOARD_TOKEN: "fixture-dashboard", N8N_PIPELINE_TOKEN: "fixture-pipeline",
    WORKER_URL: worker, N8N_URL: n8n },
});
const nextClosed = once(next, "close");
try {
  await waitFor(() => fetch(base + "/api/ready").then((response) => response.ok, () => false), "frontend");
  assert.equal((await fetch(base + "/api/jobs")).status, 401);
  const headers = { origin: base, "content-type": "application/json" };
  const login = await fetch(base + "/api/auth/login", { method: "POST", headers, body: JSON.stringify({ password }) });
  assert.equal(login.status, 200);
  const cookie = login.headers.get("set-cookie").split(";")[0];
  const at = async (path) => {
    const response = await fetch(base + path, { headers: { cookie } });
    assert.equal(response.status, 200, path);
    return response.json();
  };
  const trace = async (id) => (await fetch(adapter + "/trace/" + id)).json();
  const submit = async (scenario) => {
    const response = await fetch(base + "/api/jobs", {
      method: "POST", headers: { ...headers, cookie }, body: JSON.stringify({
        website_url: "http://127.0.0.1:8790/" + scenario + "/", idempotency_key: "phase10-qa-" + scenario,
        instagram_url: "https://www.instagram.com/pgm_qa_fixture/",
        facebook_url: "https://www.facebook.com/pgm_qa_fixture/",
        max_pages: 2, max_media: 4, run_ai_curation: true,
      }),
    });
    const body = await response.json();
    assert.equal(response.status, 202, JSON.stringify(body));
    assert.match(body.job_id, /^[a-f0-9]{32}$/);
    return body.job_id;
  };
  const expected = [
    ["success", "completed", "completed", 1, 1, 0, 0],
    ["partial", "partial_success", "partial_success", 1, 1, 0, 0],
    ["no-eligible", "not_started", "blocked", 0, 0, 0, 0],
    ["upload-failure", "failed", "blocked", 1, 0, 0, 0],
    ["oauth-expiry", "completed", "completed", 1, 1, 1, 0],
    ["oauth-rejected", "failed", "blocked", 0, 0, 1, 0],
    ["ambiguous", "completed", "completed", 1, 1, 0, 1],
  ];
  const requested = process.env.PGM_QA_SCENARIO?.trim();
  const scenarios = requested ? expected.filter(([scenario]) => scenario === requested) : expected;
  assert.ok(scenarios.length, "Unknown QA scenario");
  for (const [scenario, ledgerStatus, stage, writes, verified, refreshes, recovered] of scenarios) {
    const id = await submit(scenario);
    const path = "/api/jobs/" + id;
    const ledger = await waitFor(async () => {
      const data = await at(path + "/delivery");
      const status = await at(path + "/status");
      return data.status === ledgerStatus && status.stage === stage && data;
    }, scenario + " delivery=" + ledgerStatus + " and stage=" + stage);
    const manifest = await at(path + "/manifest");
    const curation = await at(path + "/curation");
    assert.ok(manifest.assets.length > 0);
    assert.equal(curation.ai_envelope.mock, true);
    assert.ok(curation.mode_a_items.length && curation.curation.selected_asset_ids.length);
    assert.equal((ledger.uploaded ?? []).filter(({ status }) => status === "uploaded").length, verified);
    const state = await trace(id);
    assert.equal(state?.attempts.length ?? 0, writes);
    assert.equal(state?.files ?? 0, verified);
    assert.equal(state?.refreshes ?? 0, refreshes);
    assert.equal(state?.reconciled ?? 0, recovered);
    for (const receipt of ledger.uploaded ?? []) {
      const asset = manifest.assets.find(({ asset_id }) => asset_id === receipt.asset_id);
      assert.equal(asset.rights_status, "verified");
      assert.equal(receipt.sha256, asset.sha256);
    }
    if (scenario === "partial" || scenario === "no-eligible") {
      const unverified = manifest.assets.filter(({ rights_status }) => rights_status === "unverified");
      assert.ok(unverified.length);
      assert.ok(!unverified.some((asset) => state?.attempts.includes(asset.asset_id + ":" + asset.sha256)));
    }
    if (scenario === "success") {
      assert.equal(await submit(scenario), id);
      await setTimeout(1500);
      assert.deepEqual(await trace(id), state);
      console.log("PASS duplicate intake reused the same job and issued no additional Drive writes");
    }
    console.log("PASS " + scenario + " job " + id + " through frontend, actual n8n, worker, mock AI, and dashboard");
  }
} finally {
  if (next.exitCode === null) next.kill();
  await nextClosed;
}

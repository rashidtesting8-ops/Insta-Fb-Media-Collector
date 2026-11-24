import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout } from "node:timers/promises";
import { createDiagnostics } from "./qa_diagnostics.mjs";

const diagnostics = createDiagnostics("dashboard");
const base = "http://127.0.0.1:3000";
const n8n = "http://127.0.0.1:5678";
const worker = "http://pgm-qa-worker:8787";
const adapter = "http://pgm-qa-adapter:8799";
const password = "offline-fixture-dashboard-password";
const namedAssertionSecret = process.env.PGM_QA_NAMED_ASSERTION_SECRET || "";
assert.ok(namedAssertionSecret.length >= 32, "PGM_QA_NAMED_ASSERTION_SECRET (>= 32 chars) is required");

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
  cwd: "/repo/frontend", stdio: "inherit", env: {
    ...process.env, PORT: "3000", HOSTNAME: "127.0.0.1",
    DASHBOARD_ORIGIN: base, DASHBOARD_PASSWORD: password,
    DASHBOARD_SESSION_SECRET: "offline-fixture-independent-signing-secret",
    NAMED_ASSERTION_SECRET: namedAssertionSecret,
    MEDIA_WORKER_DASHBOARD_TOKEN: "fixture-dashboard",
    N8N_PIPELINE_TOKEN: "fixture-pipeline",
    WORKER_URL: worker, N8N_URL: n8n,
  },
});
const nextClosed = once(next, "close");
try {
  await waitFor(() => fetch(base + "/api/ready").then((response) => response.ok, () => false), "frontend");
  assert.equal((await fetch(base + "/api/jobs")).status, 401);
  const headers = { origin: base, "content-type": "application/json" };
  const login = await fetch(base + "/api/auth/login", { method: "POST", headers, body: JSON.stringify({ password }) });
  assert.equal(login.status, 200);
  const cookie = login.headers.get("set-cookie").split(";")[0];

  const namedPassword = (name) =>
    name === "qa-admin-b"
      ? (process.env.PGM_QA_NAMED_PASSWORD_B || "")
      : (process.env.PGM_QA_NAMED_PASSWORD_A || "");
  const namedLogin = async (name) => {
    const pw = namedPassword(name);
    assert.ok(pw.length >= 8, "Missing ephemeral QA named password for " + name);
    diagnostics.logEvent("named_login_start", { actor: name });
    const response = await fetch(base + "/api/auth/named/login", {
      method: "POST", headers, body: JSON.stringify({ name, password: pw }),
    });
    diagnostics.logEvent("named_login_response", { actor: name, status: response.status });
    assert.equal(response.status, 200, "Named login failed for " + name);
    const setCookie = response.headers.get("set-cookie");
    assert.ok(setCookie, "Named login did not set cookie for " + name);
    return setCookie.split(";")[0];
  };
  const namedMutation = async (path, namedCookie, etag, body) => {
    const requestKey = crypto.randomUUID();
    diagnostics.logEvent("named_mutation", { path, action: body.action || "start", request_key: requestKey });
    const response = await fetch(base + path, {
      method: "POST",
      headers: {
        ...headers,
        cookie: namedCookie,
        "if-match": `"${etag}"`,
        "idempotency-key": requestKey,
      },
      body: JSON.stringify(body),
    });
    diagnostics.logEvent("named_mutation_response", { path, status: response.status });
    return response;
  };
  const getPanel = async (id, namedCookie, actor) => {
    diagnostics.logEvent("approval_panel_get", { job_id: id, actor });
    const response = await fetch(base + "/api/jobs/" + id + "/collection-approval", { headers: { cookie: namedCookie } });
    diagnostics.logEvent("approval_panel_response", { job_id: id, actor, status: response.status });
    assert.equal(response.status, 200, "GET collection-approval failed for " + actor);
    return response.json();
  };
  const approve = async (id, panel, namedCookie) => {
    diagnostics.logEvent("approval_decision", { job_id: id, actor: "qa-admin-b", revision: panel.revision });
    const response = await namedMutation(
      "/api/jobs/" + id + "/collection-approval/decisions",
      namedCookie,
      panel.etag,
      { action: "approve", reason: "QA: collection gaps reviewed" },
    );
    assert.equal(response.status, 200, "Admin B approval failed");
    const nextPanel = await getPanel(id, namedCookie, "qa-admin-b");
    assert.equal(nextPanel.state, "approved");
    assert.equal(nextPanel.start_authorized, false);
    assert.equal(nextPanel.revision, panel.revision);
    assert.equal(nextPanel.manifest_sha256, panel.manifest_sha256);
    return nextPanel;
  };
  const startAi = async (id, panel, namedCookie, actor) => {
    diagnostics.logEvent("ai_start_call", { job_id: id, actor, revision: panel.revision });
    const response = await namedMutation("/api/jobs/" + id + "/ai-start", namedCookie, panel.etag, {});
    assert.equal(response.status, 200, "Start AI failed");
    const nextPanel = await getPanel(id, namedCookie, actor);
    assert.equal(nextPanel.state, "approved");
    assert.equal(nextPanel.start_authorized, true);
    assert.equal(nextPanel.revision, panel.revision);
    assert.equal(nextPanel.manifest_sha256, panel.manifest_sha256);
    return nextPanel;
  };

  const at = async (path) => {
    const response = await fetch(base + path, { headers: { cookie } });
    assert.equal(response.status, 200, path);
    return response.json();
  };
  const trace = async (id) => (await fetch(adapter + "/trace/" + id)).json();
  const submit = async (scenario, namedCookie) => {
    diagnostics.logEvent("intake_start", { scenario, actor: "qa-admin-a" });
    const response = await fetch(base + "/api/jobs", {
      method: "POST",
      headers: { ...headers, cookie: `${cookie}; ${namedCookie}` },
      body: JSON.stringify({
        website_url: "http://127.0.0.1:8790/" + scenario + "/", idempotency_key: "phase10-qa-" + scenario,
        instagram_url: "https://www.instagram.com/pgm_qa_fixture/",
        facebook_url: "https://www.facebook.com/pgm_qa_fixture/",
        max_pages: 2, max_media: 4, run_ai_curation: true,
      }),
    });
    const body = await response.json();
    diagnostics.logEvent("intake_response", { status: response.status, job_id: body.job_id, intake_status: body.status });
    assert.equal(response.status, 202, JSON.stringify(body));
    assert.match(body.job_id, /^[a-f0-9]{32}$/);
    diagnostics.setJobId(body.job_id);
    return body.job_id;
  };

  const cookieA = await namedLogin("qa-admin-a");
  const cookieB = await namedLogin("qa-admin-b");

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
    const id = await submit(scenario, cookieA);
    const path = "/api/jobs/" + id;

    // Wait for extraction + preprocessing completion under M4 collection-approval hold.
    const extractionReady = await waitFor(async () => {
      const data = await at(path + "/status");
      return data.job_status === "extraction_complete" &&
        data.ai_status === "awaiting_collection_approval" &&
        data.stage === "blocked" && data;
    }, scenario + " extraction_complete awaiting_collection_approval");
    diagnostics.recordStage("extraction_complete_awaiting_approval", {
      job_status: extractionReady.job_status, ai_status: extractionReady.ai_status, stage: extractionReady.stage,
    });

    // Verify Admin A submitter attribution and zero pre-approval AI execution.
    const jobRecord = await at(path);
    assert.match(jobRecord.submitter_principal_id ?? "", /^[a-f0-9]{32}$/, "submitter_principal_id must be recorded");
    diagnostics.recordStage("submitter_attribution_verified", { status: jobRecord.status, submitter_recorded: true });
    const preApprovalCuration = await at(path + "/curation");
    assert.equal(preApprovalCuration.status, "awaiting_collection_approval");
    assert.equal(preApprovalCuration.ai_envelope, undefined);

    // Verify Admin A self-approval is rejected (403).
    const panelA = await getPanel(id, cookieA, "qa-admin-a");
    assert.equal(panelA.state, "pending");
    assert.equal(panelA.permissions?.approve, false);
    assert.match(panelA.manifest_sha256 ?? "", /^[a-f0-9]{64}$/);
    const selfApprove = await namedMutation(
      path + "/collection-approval/decisions",
      cookieA,
      panelA.etag,
      { action: "approve", reason: "QA: verify self-approval rejected" },
    );
    assert.equal(selfApprove.status, 403, "Admin A self-approval must be rejected");
    diagnostics.recordStage("self_approval_rejected", { actor: "qa-admin-a", status: selfApprove.status });

    // Distinct Admin B approves current revision; verify AI still held before Start AI.
    const panelB = await getPanel(id, cookieB, "qa-admin-b");
    assert.equal(panelB.state, "pending");
    assert.equal(panelB.permissions?.approve, true);
    assert.equal(panelB.etag, panelA.etag);
    const approved = await approve(id, panelB, cookieB);
    diagnostics.recordStage("approval_approved", {
      actor: "qa-admin-b", state: approved.state, revision: approved.revision, manifest_sha256: approved.manifest_sha256,
    });
    const preStartCuration = await at(path + "/curation");
    assert.equal(preStartCuration.status, "awaiting_collection_approval");
    assert.equal(preStartCuration.ai_envelope, undefined);

    // Separate authorized Start AI request.
    const started = await startAi(id, approved, cookieA, "qa-admin-a");
    diagnostics.recordStage("ai_started", {
      actor: "qa-admin-a", state: started.state, start_authorized: started.start_authorized, revision: started.revision,
    });

    // Wait for n8n poll/resume -> mock AI -> fake Drive -> dashboard final state.
    const curation = await waitFor(async () => {
      const data = await at(path + "/curation");
      return data.ai_envelope?.mock === true && data;
    }, scenario + " mock AI curation after Start AI");
    diagnostics.recordStage("mock_ai_completed", {
      status: curation.status, mock: curation.ai_envelope.mock, selected_count: curation.curation?.selected_asset_ids?.length ?? 0,
    });

    const ledger = await waitFor(async () => {
      const data = await at(path + "/delivery");
      const status = await at(path + "/status");
      return data.status === ledgerStatus && status.stage === stage && data;
    }, scenario + " delivery=" + ledgerStatus + " and stage=" + stage);
    diagnostics.recordStage("delivery_and_dashboard_final", {
      delivery_status: ledger.status, dashboard_stage: stage, uploaded_count: (ledger.uploaded ?? []).length,
    });

    // Confirm manifest SHA-256 and revision remained immutable after approval.
    const finalPanel = await getPanel(id, cookieB, "qa-admin-b");
    assert.equal(finalPanel.state, "approved");
    assert.equal(finalPanel.revision, panelB.revision);
    assert.equal(finalPanel.manifest_sha256, panelB.manifest_sha256);

    const manifest = await at(path + "/manifest");
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
      assert.equal(await submit(scenario, cookieA), id);
      await setTimeout(1500);
      assert.deepEqual(await trace(id), state);
      diagnostics.recordStage("duplicate_intake_reused", { job_id: id, extra_drive_writes: 0 });
      console.log("PASS duplicate intake reused the same job and issued no additional Drive writes");
    }
    console.log("PASS " + scenario + " job " + id + " through frontend, actual n8n, worker, mock AI, and dashboard");
  }
} finally {
  if (next.exitCode === null) next.kill();
  await nextClosed;
}

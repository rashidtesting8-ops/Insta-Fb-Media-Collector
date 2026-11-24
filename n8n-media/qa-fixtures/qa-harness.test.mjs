import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { makeApprovalE2eQa } from "./approval-e2e-workflow.mjs";
import { createDiagnostics, sanitizeValue } from "./qa_diagnostics.mjs";

const canonical = JSON.parse(await readFile(new URL("../07-pipeline-orchestration.json", import.meta.url), "utf8"));
const workflow = makeApprovalE2eQa(canonical);

test("approval-aware QA E2E workflow preserves canonical workflow and Drive isolation", () => {
  assert.equal(canonical.name, "07 - Phase 7 Pipeline Orchestration (status + rights + delivery)");
  assert.equal(workflow.active, false);

  const names = new Set(workflow.nodes.map(({ name }) => name));
  for (const [source, { main }] of Object.entries(workflow.connections)) {
    assert.ok(names.has(source), "Missing source node " + source);
    assert.ok(main.flat().every(({ node }) => names.has(node)), "Missing target from " + source);
  }

  const connected = Object.values(workflow.connections).flatMap(({ main }) => main.flat()).map(({ node }) => node);
  const driveNodes = workflow.nodes.filter(({ type }) => type === "n8n-nodes-base.googleDrive");
  assert.equal(driveNodes.length, 2);
  assert.ok(driveNodes.every(({ name, disabled, retryOnFail }) => disabled && !retryOnFail && !connected.includes(name)));
});

test("approval-aware QA E2E workflow wires M4 poll/resume bridge and avoids broken paired-item refs", () => {
  const node = (name) => workflow.nodes.find((entry) => entry.name === name);
  const poll = node("Poll Collection Approval");
  const resume = node("Resume Mock Analysis");
  assert.equal(poll.parameters.method, "GET");
  assert.equal(resume.parameters.method, "POST");
  assert.equal(poll.retryOnFail, false);
  assert.equal(resume.retryOnFail, false);
  assert.match(poll.parameters.url, /http:\/\/pgm-qa-worker:8787\/qa\/collection\/.*\/poll/);
  assert.match(resume.parameters.url, /http:\/\/pgm-qa-worker:8787\/qa\/collection\/.*\/resume/);
  assert.equal(poll.parameters.genericAuthType, "httpHeaderAuth");
  assert.equal(resume.parameters.genericAuthType, "httpHeaderAuth");

  assert.deepEqual(
    workflow.connections["Terminal?"].main.map((branch) => branch[0].node),
    ["Delivery Already Complete?", "Poll Budget Exceeded?"],
  );
  assert.deepEqual(
    workflow.connections["Delivery Already Complete?"].main.map((branch) => branch[0].node),
    ["Check Existing Delivery", "Needs Collection Approval?"],
  );
  assert.deepEqual(
    workflow.connections["Needs Collection Approval?"].main.map((branch) => branch[0].node),
    ["Poll Collection Approval", "Rights Gate"],
  );
  assert.deepEqual(
    workflow.connections["Authorized Start?"].main.map((branch) => branch[0].node),
    ["Resume Mock Analysis", "Wait For Review?"],
  );
  assert.deepEqual(
    workflow.connections["AI Resume Completed?"].main.map((branch) => branch[0].node),
    ["Check Existing Delivery", "Respond Blocked"],
  );

  for (const name of ["Check Existing Delivery", "Get Delivery Plan", "Respond Blocked", "Persist Final Status"]) {
    assert.doesNotMatch(JSON.stringify(node(name).parameters), /\$\('Map Stage'\)\.item\.json/);
  }
});

test("Evaluate Approval State code node reuses nextStep gate rules strictly", () => {
  const code = workflow.nodes.find(({ name }) => name === "Evaluate Approval State").parameters.jsCode;
  const run = new Function("$input", "$runIndex", "$", code);
  const sessionRef = () => ({ first: () => ({ json: { job_id: "a".repeat(32) } }) });
  for (const [state, start_authorized, claimed, count, expected] of [
    ["pending", false, false, 1, "wait"],
    ["approved", false, false, 2, "wait"],
    ["approved", true, false, 3, "resume"],
    ["approved", true, true, 4, "hold"],
    ["denied", false, false, 1, "hold"],
    ["revoked", false, false, 1, "hold"],
    ["stale", false, false, 1, "hold"],
    ["pending", false, false, 120, "hold"],
  ]) {
    const out = run(
      { first: () => ({ json: { state, start_authorized, claimed, etag: "f".repeat(64) } }) },
      count - 1,
      sessionRef,
    );
    assert.equal(out[0].json.next, expected);
    assert.equal(out[0].json.job_id, "a".repeat(32));
  }
});

test("diagnostics persists sanitized files, redacts credentials/cookies, and surfaces write errors", () => {
  const root = mkdtempSync(join(tmpdir(), "pgm-qa-diag-test-"));
  const prevSecret = process.env.PGM_QA_NAMED_ASSERTION_SECRET;
  const runtimeSecret = "ephemeral-secret-value-1234567890abcdef";
  process.env.PGM_QA_NAMED_ASSERTION_SECRET = runtimeSecret;
  try {
    const diag = createDiagnostics("dashboard", { root, runId: "run-001" });
    diag.setJobId("b".repeat(32));
    diag.recordStage("approval_approved", {
      actor: "qa-admin-b",
      password: "super-secret-password",
      cookie: "__Host-pgm_named=abc123def456",
      note: "leaked " + runtimeSecret,
      header: "Bearer ya29.fake-oauth-token-123456789",
    });

    const jsonlPath = join(root, "run-001", "dashboard.jsonl");
    const jsonPath = join(root, "run-001", "dashboard.json");
    const rawJsonl = readFileSync(jsonlPath, "utf8");
    const summary = JSON.parse(readFileSync(jsonPath, "utf8"));

    assert.equal(summary.job_id, "b".repeat(32));
    assert.equal(summary.transitions.length, 1);
    assert.equal(summary.transitions[0].detail.password, "[REDACTED]");
    assert.equal(summary.transitions[0].detail.cookie, "[REDACTED]");
    assert.equal(summary.transitions[0].detail.note, "[REDACTED]");
    assert.equal(summary.transitions[0].detail.header, "[REDACTED]");
    assert.doesNotMatch(rawJsonl, /super-secret-password|abc123def456|ephemeral-secret-value|ya29\./);

    const scrubbed = sanitizeValue({
      authorization: "Bearer secret",
      safe_cookie_text: "pgm_session=12345.abc.def",
      ok_field: "extraction_complete",
    });
    assert.equal(scrubbed.authorization, "[REDACTED]");
    assert.equal(scrubbed.safe_cookie_text, "[REDACTED]");
    assert.equal(scrubbed.ok_field, "extraction_complete");

    // Verify write failures throw instead of being silently suppressed.
    const blockerFile = join(root, "not-a-directory");
    writeFileSync(blockerFile, "occupied", "utf8");
    const broken = createDiagnostics("dashboard", { root: blockerFile, runId: "run-fail" });
    assert.throws(() => broken.logEvent("should_fail", {}), /QA diagnostics write failed/);
  } finally {
    if (prevSecret === undefined) delete process.env.PGM_QA_NAMED_ASSERTION_SECRET;
    else process.env.PGM_QA_NAMED_ASSERTION_SECRET = prevSecret;
    rmSync(root, { recursive: true, force: true });
  }
});

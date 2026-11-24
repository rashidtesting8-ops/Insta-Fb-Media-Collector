import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import { makeCanonicalQa } from "./canonical-qa-workflow.mjs";
import { makeGuardedLiveQa } from "./guarded-live-qa-workflow.mjs";
import { deliverGuardedE2eJpeg } from "./private-drive-jpeg-preflight.mjs";
import { makeProjectExecution } from "./qa-fixtures/project-execution.mjs";

const canonical = JSON.parse(await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url)));
const qa = makeCanonicalQa(canonical);

test("QA export leaves canonical unchanged and disconnects both Drive write templates", () => {
  assert.equal(canonical.name, "07 - Phase 7 Pipeline Orchestration (status + rights + delivery)");
  assert.equal(qa.active, false);
  const names = new Set(qa.nodes.map(({ name }) => name));
  for (const [source, { main }] of Object.entries(qa.connections)) {
    assert.ok(names.has(source));
    assert.ok(main.flat().every(({ node }) => names.has(node)));
  }
  const connected = Object.values(qa.connections).flatMap(({ main }) => main.flat()).map(({ node }) => node);
  const writes = qa.nodes.filter(({ type }) => type === "n8n-nodes-base.googleDrive");
  assert.equal(writes.length, 2);
  assert.ok(writes.every(({ name, disabled, retryOnFail }) => disabled && !retryOnFail && !connected.includes(name)));
  assert.ok(qa.nodes.filter(({ type }) => type === "n8n-nodes-base.webhook")
    .every(({ disabled, parameters }) => disabled && parameters.path.startsWith("qa-")));
});

test("only verified, matched private receipts become worker ledger successes", () => {
  const node = qa.nodes.find(({ name }) => name === "Verify Delivery Receipts");
  const allowed = { upload_key: "approved", rights_status: "verified", rights_review_required: false, sha256: "expected" };
  const unverified = { ...allowed, upload_key: "unverified", rights_status: "unverified", rights_review_required: true };
  const run = (uploads, job_id = "job") => vm.runInNewContext("(function(){" + node.parameters.jsCode + "})()", {
    $: () => ({ first: () => ({ json: { job_id: "job", pending_uploads: [allowed, unverified] } }) }),
    $input: { first: () => ({ json: { job_id, uploads, drive_folder_id: "private_folder" } }) },
  })[0].json.uploads;
  const valid = { upload_key: allowed.upload_key, status: "verified", owner_only: true, sha256: allowed.sha256,
    drive_file_id: "verified_file" };
  const result = run([valid, { ...valid, upload_key: unverified.upload_key }]);
  assert.equal(result.length, 1);
  assert.equal(result[0].status, "uploaded");
  assert.equal(result[0].drive_file_id, "verified_file");
  for (const invalid of [[{ ...valid, sha256: "wrong" }], [{ ...valid, owner_only: false }], [valid, valid]]) {
    assert.equal(run(invalid)[0].status, "failed");
  }
  assert.equal(run([valid], "other-job")[0].status, "failed");
});

test("live draft cannot reach mock services or forward dashboard controls to the guarded adapter", () => {
  const draft = makeGuardedLiveQa(canonical);
  assert.equal(draft.active, false);
  assert.notEqual(draft.id, qa.id);
  const node = (name) => draft.nodes.find((entry) => entry.name === name);
  const bridge = node("Guarded Job Adapter");
  assert.equal(bridge.disabled, true);
  assert.equal(bridge.retryOnFail, false);
  assert.match(bridge.parameters.jsonBody, /JSON.stringify\(\{ job_id: /);
  assert.doesNotMatch(bridge.parameters.jsonBody, /authorization|approval|folder|url/);
  assert.match(node("Create Worker Job").parameters.jsonBody, /run_ai_curation: false/);
  assert.match(node("Verify Delivery Receipts").parameters.jsCode, /1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF/);
  assert.doesNotMatch(JSON.stringify(draft), /http:\/\/pgm-qa-(worker|adapter)/);
  assert.ok(draft.nodes.filter(({ type }) => ["n8n-nodes-base.webhook", "n8n-nodes-base.googleDrive"].includes(type))
    .every(({ disabled }) => disabled === true));
});

test("the prior standalone JPEG approval cannot authorize E2E Drive access", async () => {
  await assert.rejects(deliverGuardedE2eJpeg({
    approval: { jpegUploadApproved: true, workflowId: "11b162905d85ad49",
      folderId: "1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF" },
  }), /Separate job-scoped Groq and Drive approvals required/);
});

test("project execution copy accepts only authenticated fixture intake and loopback calls", async () => {
  const copy = await makeProjectExecution();
  assert.equal(copy.active, false);
  assert.ok(!copy.nodes.some(({ type }) => type === "n8n-nodes-base.googleDrive"));
  const node = (name) => copy.nodes.find((entry) => entry.name === name);
  assert.equal(node("Pipeline Intake Webhook").parameters.authentication, "headerAuth");
  assert.equal(node("Pipeline Intake Webhook").parameters.path, "phase10-exec-pipeline-intake");
  assert.equal(node("Status Webhook").disabled, true);
  assert.match(node("Validate Intake").parameters.jsonBody, /authorization_confirmed: false/);
  assert.match(node("Plan Ready?").parameters.conditions.conditions[0].leftValue, /mode === 'fixture'/);
  for (const item of copy.nodes.filter(({ type }) => type === "n8n-nodes-base.httpRequest")) {
    assert.match(item.parameters.url, /http:\/\/127\.0\.0\.1:8877\//);
    assert.ok(item.credentials?.httpHeaderAuth?.name.startsWith("Phase 10 Fixture"));
  }
});

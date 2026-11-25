import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

async function loadWorkflow(file) {
  return JSON.parse(await readFile(new URL("./" + file, import.meta.url), "utf8"));
}

test("00-health-probe enforces headerAuth on webhook and remains inactive", async () => {
  const wf = await loadWorkflow("00-health-probe.json");
  assert.equal(wf.active, false);
  const hook = wf.nodes.find((n) => n.type === "n8n-nodes-base.webhook");
  assert.equal(hook.parameters.authentication, "headerAuth");
  assert.equal(hook.credentials?.httpHeaderAuth?.name, "Media Admin Webhook Token");
});

test("05-ai-curation-probe validates 32-hex job_id before calling collector", async () => {
  const wf = await loadWorkflow("05-ai-curation-probe.json");
  assert.equal(wf.active, false);
  const check = wf.nodes.find((n) => n.name === "Valid Job ID?");
  assert.ok(check, "Valid Job ID? node missing");
  assert.match(check.parameters.conditions.conditions[0].leftValue, /\[a-f0-9\]\{32\}/);
  const badResp = wf.nodes.find((n) => n.name === "Respond Invalid Job ID");
  assert.equal(badResp.parameters.options.responseCode, 400);
  assert.equal(wf.connections["Valid Job ID?"].main[0][0].node, "Job Status (incl. ai_curation)");
  assert.equal(wf.connections["Valid Job ID?"].main[1][0].node, "Respond Invalid Job ID");
});

test("06-google-drive-delivery uses valid Drive v3 params and handles multi-asset uploads", async () => {
  const wf = await loadWorkflow("06-google-drive-delivery.json");
  assert.equal(wf.active, false);
  const byName = Object.fromEntries(wf.nodes.map((n) => [n.name, n]));

  // Drive v3 schema assertions (no deprecated v2 parents/fileName/binaryPropertyName)
  const folder = byName["Create Business Drive Folder"];
  const upload = byName["Upload Original to Drive"];
  assert.equal(folder.parameters.driveId.value, "My Drive");
  assert.equal(folder.parameters.folderId.value, "root");
  assert.equal(folder.parameters.parents, undefined);
  assert.equal(folder.retryOnFail, false);
  assert.equal(upload.parameters.driveId.value, "My Drive");
  assert.match(upload.parameters.folderId.value, /drive_folder_id/);
  assert.equal(upload.parameters.inputDataFieldName, "data");
  assert.match(upload.parameters.name, /filename/);
  assert.equal(upload.parameters.fileName, undefined);
  assert.equal(upload.parameters.binaryPropertyName, undefined);
  assert.equal(upload.parameters.parents, undefined);
  assert.equal(upload.retryOnFail, false);
  assert.equal(byName["Read Selected Original (binary)"].onError, "continueRegularOutput");

  // Simulate multi-asset Split Pending Assets + Collect Delivery Receipts
  const plan = {
    job_id: "a".repeat(32),
    drive_folder_id: null,
    drive_folder_link: null,
    pending_uploads: [
      { asset_id: "img1", filename: "img1.jpg", n8n_path: "/data/media/a/img1.jpg", upload_key: "img1:sha1" },
      { asset_id: "img2", filename: "img2.jpg", n8n_path: "/data/media/a/img2.jpg", upload_key: "img2:sha2" },
    ],
  };
  const splitOut = vm.runInNewContext(
    "(function(){" + byName["Split Pending Assets"].parameters.jsCode + "})()",
    {
      $: () => ({ first: () => ({ json: plan }) }),
      $input: { first: () => ({ json: { id: "folder_12345678", webViewLink: "https://drive.google.com/drive/folders/folder_12345678" } }) },
    },
  );
  assert.equal(splitOut.length, 2);
  assert.equal(splitOut[0].json.drive_folder_id, "folder_12345678");
  assert.equal(splitOut[1].pairedItem?.item, 0);

  const collectOut = vm.runInNewContext(
    "(function(){" + byName["Collect Delivery Receipts"].parameters.jsCode + "})()",
    {
      $: (name) => {
        if (name === "Split Pending Assets") {
          return {
            first: () => splitOut[0],
            itemMatching: (i) => splitOut[i],
          };
        }
        return { first: () => ({ json: { authorization_confirmed: true, business_name: "Lilys Club" } }) };
      },
      $input: {
        all: () => [
          { json: { id: "file_aaa11111", webViewLink: "https://drive.google.com/file/d/file_aaa11111/view" } },
          { json: { error: { message: "quota exceeded" } } },
        ],
      },
    },
  );
  assert.equal(collectOut.length, 1);
  const ledger = collectOut[0].json;
  assert.equal(ledger.authorization_confirmed, true);
  assert.equal(ledger.visibility, "private");
  assert.equal(ledger.public_sharing, false);
  assert.equal(ledger.drive_folder_id, "folder_12345678");
  assert.equal(ledger.drive_folder_link, "https://drive.google.com/drive/folders/folder_12345678");
  assert.equal(ledger.uploads.length, 2);
  assert.equal(ledger.uploads[0].upload_key, "img1:sha1");
  assert.equal(ledger.uploads[0].status, "uploaded");
  assert.equal(ledger.uploads[1].upload_key, "img2:sha2");
  assert.equal(ledger.uploads[1].status, "failed");
  assert.match(ledger.uploads[1].error, /quota exceeded/);
});

test("07-pipeline-orchestration fixes Drive v3, pairedItem, post-ack safety, folder failure & status job_id validation", async () => {
  const wf = await loadWorkflow("07-pipeline-orchestration.json");
  const byName = Object.fromEntries(wf.nodes.map((n) => [n.name, n]));

  // Drive v3 fields on both nodes
  const folder = byName["Create Business Drive Folder"];
  const upload = byName["Upload Original to Drive"];
  assert.equal(folder.parameters.driveId.value, "My Drive");
  assert.equal(folder.parameters.folderId.value, "root");
  assert.equal(folder.parameters.parents, undefined);
  assert.equal(upload.parameters.driveId.value, "My Drive");
  assert.match(upload.parameters.folderId.value, /drive_folder_id/);
  assert.equal(upload.parameters.inputDataFieldName, "data");
  assert.match(upload.parameters.name, /filename/);
  assert.equal(upload.parameters.fileName, undefined);
  assert.equal(upload.parameters.binaryPropertyName, undefined);

  // pairedItem in Session and Map Stage
  const sessionOut = vm.runInNewContext(
    "(function(){" + byName["Session"].parameters.jsCode + "})()",
    {
      $: () => ({ first: () => ({ json: { authorization_confirmed: false, business_name: "Test" } }) }),
      $json: { job_id: "b".repeat(32) },
    },
  );
  assert.equal(sessionOut[0].pairedItem?.item, 0);
  const mapOut = vm.runInNewContext(
    "(function(){" + byName["Map Stage"].parameters.jsCode + "})()",
    {
      $: () => ({ first: () => sessionOut[0] }),
      $json: { stage: "collecting", terminal: false },
      $runIndex: 2,
    },
  );
  assert.equal(mapOut[0].pairedItem?.item, 0);
  assert.equal(mapOut[0].json.attempts, 3);

  // Post-ack respondToWebhook nodes tolerate already-sent 202 response
  for (const name of ["Respond Polling Timeout", "Respond Awaiting Rights", "Respond Final Status", "Respond Blocked"]) {
    assert.equal(byName[name].onError, "continueRegularOutput", `${name} must have onError continueRegularOutput`);
  }

  // Folder failure clears delivering stage via Mark Delivery Failed
  assert.equal(wf.connections["Folder Created?"].main[1][0].node, "Mark Delivery Failed");
  assert.equal(wf.connections["Mark Delivery Failed"].main[0][0].node, "Respond Blocked");
  assert.equal(byName["Read Selected Original"].onError, "continueRegularOutput");

  // Status webhook validates job_id and propagates worker statusCode
  assert.equal(wf.connections["Status Webhook"].main[0][0].node, "Valid Status Job ID?");
  assert.equal(wf.connections["Valid Status Job ID?"].main[0][0].node, "Status Job Lookup");
  assert.equal(wf.connections["Valid Status Job ID?"].main[1][0].node, "Respond Invalid Status Job ID");
  assert.match(String(byName["Respond Status"].parameters.options.responseCode), /statusCode/);
});

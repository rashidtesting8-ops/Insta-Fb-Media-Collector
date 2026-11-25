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

test("06-google-drive-delivery uses valid Drive v3 params, creates subfolders, and separates media + report receipts", async () => {
  const wf = await loadWorkflow("06-google-drive-delivery.json");
  assert.equal(wf.active, false);
  const byName = Object.fromEntries(wf.nodes.map((n) => [n.name, n]));

  // Drive v3 schema assertions (no deprecated v2 parents/fileName/binaryPropertyName)
  const folder = byName["Create Business Drive Folder"];
  const subFolder = byName["Create Drive Subfolder"];
  const upload = byName["Upload Original to Drive"];
  assert.equal(folder.parameters.driveId.value, "My Drive");
  assert.equal(folder.parameters.folderId.value, "root");
  assert.equal(folder.parameters.parents, undefined);
  assert.equal(folder.retryOnFail, false);
  assert.equal(subFolder.parameters.driveId.value, "My Drive");
  assert.match(subFolder.parameters.folderId.value, /parent_folder_id/);
  assert.equal(subFolder.retryOnFail, false);
  assert.equal(upload.parameters.driveId.value, "My Drive");
  assert.match(upload.parameters.folderId.value, /target_folder_id/);
  assert.equal(upload.parameters.inputDataFieldName, "data");
  assert.match(upload.parameters.name, /filename/);
  assert.equal(upload.parameters.fileName, undefined);
  assert.equal(upload.parameters.binaryPropertyName, undefined);
  assert.equal(upload.parameters.parents, undefined);
  assert.equal(upload.retryOnFail, false);
  assert.equal(byName["Read Selected Original (binary)"].onError, "continueRegularOutput");

  // Simulate Plan Subfolders -> Split Pending Assets -> Collect Delivery Receipts
  const plan = {
    job_id: "a".repeat(32),
    drive_folder_id: null,
    drive_folder_link: null,
    subfolders: { "Best Images": "existing_img_sub" },
    pending_uploads: [
      { asset_id: "img1", filename: "img1.jpg", mime: "image/jpeg", target_subfolder: "Best Images", n8n_path: "/data/media/a/img1.jpg", upload_key: "img1:sha1" },
      { asset_id: "vid1", filename: "vid1.mp4", mime: "video/mp4", target_subfolder: "Best Videos", n8n_path: "/data/media/a/vid1.mp4", upload_key: "vid1:sha2" },
    ],
    pending_reports: [
      { report_id: "ai_curation_report", filename: "ai-curation-report.json", mime: "application/json", target_subfolder: "Reports", n8n_path: "/data/media/a/delivery_reports/ai-curation-report.json", upload_key: "report:ai_curation_report:sha3" },
    ],
  };

  const plannedSubs = vm.runInNewContext(
    "(function(){" + byName["Plan Subfolders"].parameters.jsCode + "})()",
    {
      $: () => ({ first: () => ({ json: plan }) }),
      $input: { first: () => ({ json: { id: "folder_12345678", webViewLink: "https://drive.google.com/drive/folders/folder_12345678" } }) },
    },
  );
  // Only Best Videos & Reports need creation (Best Images reused!)
  assert.equal(plannedSubs.length, 2);
  assert.deepEqual(Array.from(plannedSubs, (s) => s.json.name), ["Best Videos", "Reports"]);

  const splitOut = vm.runInNewContext(
    "(function(){" + byName["Split Pending Assets"].parameters.jsCode + "})()",
    {
      $: (name) => {
        if (name === "Plan Subfolders") return { first: () => plannedSubs[0], all: () => plannedSubs };
        return { first: () => ({ json: plan }) };
      },
      $input: {
        all: () => [
          { json: { id: "new_vid_sub", name: "Best Videos" } },
          { json: { id: "new_rep_sub", name: "Reports" } },
        ],
      },
    },
  );
  assert.equal(splitOut.length, 3);
  assert.equal(splitOut[0].json.target_folder_id, "existing_img_sub");
  assert.equal(splitOut[1].json.target_folder_id, "new_vid_sub");
  assert.equal(splitOut[2].json.target_folder_id, "new_rep_sub");
  assert.equal(splitOut[2].json.kind, "report");
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
          { json: { id: "file_rep99999", webViewLink: "https://drive.google.com/file/d/file_rep99999/view" } },
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
  assert.equal(ledger.subfolders["Best Images"], "existing_img_sub");
  assert.equal(ledger.subfolders["Best Videos"], "new_vid_sub");
  assert.equal(ledger.subfolders["Reports"], "new_rep_sub");
  assert.equal(ledger.uploads.length, 2);
  assert.equal(ledger.uploads[0].status, "uploaded");
  assert.equal(ledger.uploads[1].status, "failed");
  assert.equal(ledger.report_uploads.length, 1);
  assert.equal(ledger.report_uploads[0].upload_key, "report:ai_curation_report:sha3");
  assert.equal(ledger.report_uploads[0].status, "uploaded");
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

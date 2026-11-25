import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { makeUnpublishedDriveTestCopy } from "./private-drive-delivery.mjs";

export const workflowId = "b627ee2205a34c7a";
export const workflowName = "TEST | Phase 10 Canonical QA E2E";

export function makeCanonicalQa(canonical) {
  const copy = makeUnpublishedDriveTestCopy(canonical);
  copy.id = workflowId;
  copy.name = workflowName;
  const worker = "http://pgm-qa-worker:8787";
  const adapter = "http://pgm-qa-adapter:8799/deliver";
  const remove = new Set(["Authorization Confirmed?", "Respond Awaiting Rights", "Mark Delivering",
    "Mark Delivery Failed", "Existing Drive Folder?", "Folder Created?", "Split Assets", "Read Selected Original",
    "Collect Upload Results"]);
  copy.nodes = copy.nodes.filter(({ name }) => !remove.has(name));
  for (const [name, edges] of Object.entries(copy.connections)) {
    if (remove.has(name)) delete copy.connections[name];
    else edges.main = edges.main.map((branch) => branch.filter(({ node }) => !remove.has(node)));
  }
  const node = (name) => copy.nodes.find((entry) => entry.name === name);
  for (const entry of copy.nodes) {
    if (entry.type === "n8n-nodes-base.httpRequest") {
      entry.parameters.url = entry.parameters.url.replace("http://collector:8787", worker);
      delete entry.credentials;
    } else if (entry.type === "n8n-nodes-base.webhook") {
      entry.parameters.path = "qa-" + entry.parameters.path;
      entry.webhookId = "phase10-qa-" + entry.parameters.path;
      delete entry.credentials;
    }
  }
  node("Wait Before Retry").parameters.amount = 1;
  node("Rights Gate").parameters.conditions.conditions[0].leftValue =
    "={{ $json.ai_status === 'completed' && ['extraction_complete', 'partial_success'].includes($json.job_status) }}";

  const planCheck = node("Plan Ready?");
  planCheck.parameters.conditions.conditions[0].leftValue =
    "={{ $json.status === 'ready' && $json.pending_uploads.some(asset => asset.rights_status === 'verified' && asset.rights_review_required === false) }}";
  node("Respond Blocked").parameters.responseBody =
    "={{ JSON.stringify({ job_id: $('Map Stage').item.json.job_id, stage: 'blocked', detail: 'No rights-approved assets eligible for delivery' }) }}";

  const request = {
    id: "qa-verified-drive-request", name: "Request Verified Delivery", type: "n8n-nodes-base.httpRequest", typeVersion: 4.2,
    position: [3750, -280], retryOnFail: false, onError: "continueRegularOutput",
    parameters: { method: "POST", url: adapter, sendBody: true, specifyBody: "json",
      jsonBody: "={{ JSON.stringify({ job_id: $json.job_id, upload_keys: $json.pending_uploads.filter(asset => asset.rights_status === 'verified' && asset.rights_review_required === false).map(asset => asset.upload_key) }) }}",
      options: { timeout: 20000 } },
  };
  const verify = {
    id: "qa-verify-drive-receipts", name: "Verify Delivery Receipts", type: "n8n-nodes-base.code", typeVersion: 2,
    position: [3980, -280], parameters: { jsCode: [
      "const plan = $('Get Delivery Plan').first().json;",
      "const eligible = plan.pending_uploads.filter(asset => asset.rights_status === 'verified' && asset.rights_review_required === false);",
      "const { job_id, uploads, drive_folder_id } = $input.first().json;",
      "const ownResults = job_id === plan.job_id && Array.isArray(uploads) ? uploads : [];",
      "const folderOk = typeof drive_folder_id === 'string' && /^[A-Za-z0-9_-]{8,200}$/.test(drive_folder_id);",
      "const outcomes = eligible.map(asset => {",
      "  const matches = ownResults.filter(item => item?.upload_key === asset.upload_key);",
      "  const result = matches[0];",
      "  if (matches.length === 1 && folderOk && result.status === 'verified' && result.owner_only === true &&",
      "      result.sha256 === asset.sha256 && /^[A-Za-z0-9_-]{8,200}$/.test(result.drive_file_id ?? '')) {",
      "    return { upload_key: asset.upload_key, drive_file_id: result.drive_file_id, status: 'uploaded' };",
      "  }",
      "  return { upload_key: asset.upload_key, drive_file_id: null, status: 'failed', error: 'verified_drive_receipt_unavailable' };",
      "});",
      "return [{json: { authorization_confirmed: true, drive_folder_id: folderOk && ownResults.length ? drive_folder_id : null, uploads: outcomes }}];",
    ].join("\n") },
  };
  copy.nodes.push(request, verify);
  const wire = (from, ...branches) => {
    copy.connections[from] = { main: branches.map((name) => [{ node: name, type: "main", index: 0 }]) };
  };
  wire("Rights Gate", "Check Existing Delivery", "Persist Final Status");
  wire("Plan Ready?", request.name, "Respond Blocked");
  wire(request.name, verify.name);
  wire(verify.name, "Persist Delivery Ledger");
  return copy;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const canonical = JSON.parse(await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url), "utf8"));
  await writeFile(process.argv[2], JSON.stringify(makeCanonicalQa(canonical), null, 2) + "\n", { flag: "wx" });
}

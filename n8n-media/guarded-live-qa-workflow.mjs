import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { makeCanonicalQa } from "./canonical-qa-workflow.mjs";

export const workflowId = "a10e2e0f3194c2e1";
export const workflowName = "TEST | Phase 10 Guarded Live E2E Draft";

export function makeGuardedLiveQa(canonical) {
  const qa = makeCanonicalQa(canonical);
  qa.id = workflowId;
  qa.name = workflowName;
  const node = (name) => qa.nodes.find((entry) => entry.name === name);
  for (const entry of qa.nodes) {
    if (entry.type === "n8n-nodes-base.webhook") {
      entry.parameters.path = "live-" + entry.parameters.path;
      entry.webhookId = "guarded-e2e-" + entry.parameters.path;
    } else if (entry.type === "n8n-nodes-base.httpRequest") {
      entry.parameters.url = entry.parameters.url.replace("http://pgm-qa-worker:8787", "http://pgm-live-qa-worker:8787");
    }
  }
  node("Create Worker Job").parameters.jsonBody =
    "={{ JSON.stringify({ ...($json.create || {}), run_ai_curation: false, social_permission_confirmed: false, instagram_url: 'https://www.instagram.com/pgm_qa_fixture/', facebook_url: 'https://www.facebook.com/pgm_qa_fixture/', max_pages: 1, max_media: 1, max_selected_images: 1, max_selected_videos: 1 }) }}";
  node("Rights Gate").parameters.conditions.conditions[0].leftValue = "={{ $json.job_status === 'extraction_complete' }}";
  const bridge = node("Request Verified Delivery");
  bridge.name = "Guarded Job Adapter";
  bridge.disabled = true;
  bridge.position = [3270, -400];
  bridge.parameters.url = "http://pgm-live-qa-adapter:8799/guarded-job";
  // Approval, rights, hashes, folder and provider inputs come from the isolated adapter's trusted scope, never intake.
  bridge.parameters.jsonBody = "={{ JSON.stringify({ job_id: $('Session').first().json.job_id }) }}";
  bridge.parameters.authentication = "genericCredentialType";
  bridge.parameters.genericAuthType = "httpHeaderAuth";
  bridge.parameters.options = { timeout: 180000, redirect: { redirect: { followRedirects: false } } };
  node("Plan Ready?").parameters.conditions.conditions[0].leftValue =
    "={{ $json.status === 'ready' && $json.pending_uploads.length === 1 && $json.pending_uploads[0].rights_status === 'verified' && $json.pending_uploads[0].rights_review_required === false && $('Guarded Job Adapter').first().json.job_id === $json.job_id && $('Guarded Job Adapter').first().json.status === 'verified' && $('Guarded Job Adapter').first().json.mode === 'live' }}";
  node("Verify Delivery Receipts").parameters.jsCode = node("Verify Delivery Receipts").parameters.jsCode
    .replace("$input.first().json", "$('Guarded Job Adapter').first().json")
    .replace("typeof drive_folder_id === 'string' && /^[A-Za-z0-9_-]{8,200}$/.test(drive_folder_id)",
      "drive_folder_id === '1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF'");
  delete qa.connections["Request Verified Delivery"];
  const to = (name) => [{ node: name, type: "main", index: 0 }];
  qa.connections["Delivery Already Done?"].main[1] = to(bridge.name);
  qa.connections[bridge.name] = { main: [to("Get Delivery Plan")] };
  qa.connections["Plan Ready?"].main[0] = to("Verify Delivery Receipts");
  return qa;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const canonical = JSON.parse(await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url), "utf8"));
  await writeFile(process.argv[2], JSON.stringify(makeGuardedLiveQa(canonical), null, 2) + "\n", { flag: "wx" });
}

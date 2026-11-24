import { readFile } from "node:fs/promises";
import { makeGuardedLiveQa } from "../guarded-live-qa-workflow.mjs";

export const projectExecutionId = "ea10e2e001b0c2e1";
export const projectExecutionName = "TEST | Phase 10 Local Execution (Fake Only)";
export const intakePath = "phase10-exec-pipeline-intake";
export const fixtureKeys = {
  worker: { id: "phase10ExecWrkr1", name: "Phase 10 Fixture Worker", header: "x-api-key", env: "PGM_QA_WORKER_TOKEN" },
  intake: { id: "phase10ExecInTk1", name: "Phase 10 Fixture Intake", header: "x-pipeline-key", env: "PGM_QA_INTAKE_TOKEN" },
  adapter: { id: "phase10ExecAdpt1", name: "Phase 10 Fixture Adapter", header: "x-qa-adapter-token", env: "PGM_QA_ADAPTER_TOKEN" },
};

export async function makeProjectExecution() {
  const canonical = JSON.parse(await readFile(new URL("../07-pipeline-orchestration.json", import.meta.url)));
  const qa = makeGuardedLiveQa(canonical);
  qa.id = projectExecutionId;
  qa.name = projectExecutionName;
  qa.nodes = qa.nodes.filter((node) => node.type !== "n8n-nodes-base.googleDrive");
  const bind = (node, { id, name }) => { node.credentials = { httpHeaderAuth: { id, name } }; };
  for (const node of qa.nodes) {
    if (node.type === "n8n-nodes-base.webhook") {
      node.parameters.path = node.name === "Pipeline Intake Webhook" ? intakePath : "phase10-exec-pipeline-status";
      node.webhookId = node.parameters.path + "-fixture";
      node.disabled = node.name !== "Pipeline Intake Webhook";
      bind(node, fixtureKeys.intake);
    } else if (node.name === "Guarded Job Adapter") {
      node.disabled = false;
      node.parameters.url = "http://127.0.0.1:8877/guarded-job";
      bind(node, fixtureKeys.adapter);
    } else if (node.type === "n8n-nodes-base.httpRequest") {
      node.parameters.url = node.parameters.url.replace("http://pgm-live-qa-worker:8787", "http://127.0.0.1:8877");
      if (node.parameters.genericAuthType === "httpHeaderAuth") bind(node, fixtureKeys.worker);
    }
  }
  const plan = qa.nodes.find(({ name }) => name === "Plan Ready?");
  plan.parameters.conditions.conditions[0].leftValue =
    plan.parameters.conditions.conditions[0].leftValue.replace("mode === 'live'", "mode === 'fixture'");
  const intake = qa.nodes.find(({ name }) => name === "Validate Intake");
  intake.parameters.jsonBody =
    "={{ JSON.stringify({ ...($json.body || {}), website_url: 'http://127.0.0.1:8790/guarded/', social_permission_confirmed: false, authorization_confirmed: false, job_id: null }) }}";
  return qa;
}

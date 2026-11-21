import { readFileSync } from "node:fs";

const source = new URL("../n8n-media/07-pipeline-orchestration.json", import.meta.url);
const workflow = JSON.parse(readFileSync(source, "utf8"));
workflow.name += " — staging";

// n8n validates all credentials at publish time, including unreachable nodes.
// Both rights outcomes end at the existing no-upload response in staging.
const authorization = workflow.connections["Authorization Confirmed?"].main;
authorization[0] = authorization[1];

const reachable = new Set();
const pending = workflow.nodes.filter((node) => node.type === "n8n-nodes-base.webhook").map((node) => node.name);
while (pending.length) {
  const name = pending.pop();
  if (reachable.has(name)) continue;
  reachable.add(name);
  for (const branch of workflow.connections[name]?.main ?? []) {
    for (const edge of branch) pending.push(edge.node);
  }
}

workflow.nodes = workflow.nodes.filter((node) => reachable.has(node.name));
workflow.connections = Object.fromEntries(Object.entries(workflow.connections).filter(([name]) => reachable.has(name)));
const deliveryResponse = workflow.nodes.find((node) => node.name === "Respond Awaiting Rights");
deliveryResponse.parameters.responseBody =
  "={{ JSON.stringify({ job_id: $('Map Stage').item.json.job_id, stage: 'blocked', detail: 'Drive delivery disabled in staging' }) }}";

if (workflow.nodes.some((node) => node.type === "n8n-nodes-base.googleDrive")) {
  throw new Error("Staging workflow must contain no Google Drive nodes");
}
process.stdout.write(JSON.stringify(workflow, null, 2) + "\n");

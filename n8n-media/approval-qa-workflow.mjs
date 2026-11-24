import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { makeCanonicalQa } from "./canonical-qa-workflow.mjs";

export function nextStep(poll, count) {
  if (poll.claimed || !["pending", "approved"].includes(poll.state)) return "hold";
  if (poll.state === "approved" && poll.start_authorized === true) return "resume";
  return count < 120 ? "wait" : "hold";
}

export function makeApprovalQa(canonical) {
  const copy = makeCanonicalQa(canonical);
  const original = name => structuredClone(copy.nodes.find(n => n.name === name));
  const branch = (name, value, pos) => {
    const n = original("Intake Valid?");
    Object.assign(n, { name, id: name.replace(/\W/g, ""), position: pos });
    n.parameters.conditions.conditions[0].leftValue = "={{ $json.next === '" + value + "' }}";
    return n;
  };
  const worker = "http://pgm-qa-worker:8787/qa/collection/";
  const http = (name, path, pos) => {
    const n = original("Create Worker Job");
    Object.assign(n, { name, id: "approval-" + path, retryOnFail: false, position: pos });
    delete n.onError;
    n.credentials = { httpHeaderAuth: { name: "Phase 10 Fixture Worker" } };
    n.parameters.url = '={{ "' + worker + '" + $("Validate job ID").first().json.job_id + "/' + path + '" }}';
    if (path === "poll") {
      n.parameters.method = "GET";
      for (const k of ["sendBody", "specifyBody", "jsonBody"]) delete n.parameters[k];
    } else n.parameters.jsonBody = "={{ JSON.stringify({ etag: $json.etag }) }}";
    return n;
  };
  copy.id = "a404000000000001";
  copy.name = "TEST | M4 Collection Approval Poll";
  copy.active = false;
  const wait = original("Wait Before Retry");
  wait.parameters = { resume: "timeInterval", amount: 5, unit: "seconds" };
  wait.webhookId = "m4-approval-qa-wait";
  wait.position = [1100, 220];
  copy.nodes = [
    { id: "approval-trigger", name: "QA job", type: "n8n-nodes-base.executeWorkflowTrigger", typeVersion: 1.1,
      position: [0, 0], parameters: { workflowInputs: { values: [{ name: "job_id" }] } } },
    { id: "approval-input", name: "Validate job ID", type: "n8n-nodes-base.code", typeVersion: 2, position: [200, 0],
      parameters: { jsCode: "const id = $input.first().json.job_id; if (typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)) throw Error('Invalid job ID'); return [{json:{job_id:id}}];" } },
    http("Poll worker", "poll", [400, 0]),
    { id: "approval-state", name: "Read worker state", type: "n8n-nodes-base.code", typeVersion: 2, position: [600, 0],
      parameters: { jsCode: nextStep.toString() + "\nconst poll = $input.first().json; return [{json:{etag:poll.etag,state:poll.state,next:nextStep(poll,$runIndex+1)}}];" } },
    branch("Authorized start?", "resume", [800, 0]), http("Resume mock analysis", "resume", [1100, -100]),
    branch("Wait for review?", "wait", [950, 180]), wait,
  ];
  const links = (...branches) => ({ main: branches.map(name => name ? [{ node: name, type: "main", index: 0 }] : []) });
  copy.connections = { "QA job": links("Validate job ID"), "Validate job ID": links("Poll worker"),
    "Poll worker": links("Read worker state"), "Read worker state": links("Authorized start?"),
    "Authorized start?": links("Resume mock analysis", "Wait for review?"),
    "Wait for review?": links("Wait Before Retry", null), "Wait Before Retry": links("Poll worker") };
  return copy;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const canonical = JSON.parse(await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url)));
  await writeFile(process.argv[2], JSON.stringify(makeApprovalQa(canonical), null, 2) + "\n", { flag: "wx" });
}

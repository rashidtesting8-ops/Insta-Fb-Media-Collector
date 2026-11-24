import { makeCanonicalQa, workflowId } from "../canonical-qa-workflow.mjs";
import { nextStep } from "../approval-qa-workflow.mjs";

export const approvalE2eWorkflowId = workflowId;
export const approvalE2eWorkflowName = "TEST | M5 Approval-Aware Canonical QA E2E";

export function makeApprovalE2eQa(canonical) {
  const copy = makeCanonicalQa(canonical);
  copy.id = approvalE2eWorkflowId;
  copy.name = approvalE2eWorkflowName;
  copy.active = false;

  const node = (name) => copy.nodes.find((entry) => entry.name === name);
  const clone = (name) => structuredClone(node(name));

  // Avoid paired-item lineage breaks across approval wait/code loops in n8n 2.x.
  for (const name of ["Check Existing Delivery", "Get Delivery Plan"]) {
    const entry = node(name);
    entry.parameters.url = entry.parameters.url.replaceAll("$('Map Stage').item.json", "$('Session').first().json");
  }
  node("Respond Blocked").parameters.responseBody =
    node("Respond Blocked").parameters.responseBody.replaceAll("$('Map Stage').item.json", "$('Session').first().json");
  node("Persist Final Status").parameters.url =
    node("Persist Final Status").parameters.url.replaceAll("$('Map Stage').item.json", "$('Map Stage').first().json");

  const makeIf = (id, name, leftValue, position) => {
    const entry = clone("Intake Valid?");
    Object.assign(entry, { id, name, position });
    entry.parameters.conditions.conditions[0].leftValue = leftValue;
    return entry;
  };

  const makeWorkerHttp = (id, name, method, pathSuffix, position, jsonBody = null) => {
    const entry = clone("Create Worker Job");
    Object.assign(entry, { id, name, position, retryOnFail: false });
    delete entry.credentials;
    entry.parameters.method = method;
    entry.parameters.url = `={{ 'http://pgm-qa-worker:8787/qa/collection/' + $('Session').first().json.job_id + '/${pathSuffix}' }}`;
    if (method === "GET") {
      delete entry.onError;
      for (const key of ["sendBody", "specifyBody", "jsonBody"]) delete entry.parameters[key];
    } else {
      entry.onError = "continueRegularOutput";
      entry.parameters.sendBody = true;
      entry.parameters.specifyBody = "json";
      entry.parameters.jsonBody = jsonBody;
    }
    return entry;
  };

  const deliveryDone = makeIf(
    "qa-delivery-already-complete",
    "Delivery Already Complete?",
    "={{ $json.delivery_status === 'completed' }}",
    [2320, 120],
  );
  const needsApproval = makeIf(
    "qa-needs-collection-approval",
    "Needs Collection Approval?",
    "={{ ['extraction_complete', 'partial_success'].includes($json.job_status) && $json.ai_status === 'awaiting_collection_approval' }}",
    [2520, 40],
  );
  const pollApproval = makeWorkerHttp(
    "qa-poll-collection-approval",
    "Poll Collection Approval",
    "GET",
    "poll",
    [2740, -140],
  );
  const evalApproval = {
    id: "qa-evaluate-approval-state",
    name: "Evaluate Approval State",
    type: "n8n-nodes-base.code",
    typeVersion: 2,
    position: [2940, -140],
    parameters: {
      jsCode: [
        nextStep.toString(),
        "const poll = $input.first().json;",
        "const job_id = $('Session').first().json.job_id;",
        "return [{ json: { job_id, etag: poll.etag, state: poll.state, start_authorized: poll.start_authorized === true, claimed: poll.claimed === true, next: nextStep(poll, $runIndex + 1) } }];",
      ].join("\n"),
    },
  };
  const authorizedStart = makeIf(
    "qa-authorized-start",
    "Authorized Start?",
    "={{ $json.next === 'resume' }}",
    [3140, -140],
  );
  const waitForReview = makeIf(
    "qa-wait-for-review",
    "Wait For Review?",
    "={{ $json.next === 'wait' }}",
    [3340, 20],
  );
  const waitForApproval = clone("Wait Before Retry");
  Object.assign(waitForApproval, {
    id: "qa-wait-for-approval",
    name: "Wait For Approval",
    position: [3540, 120],
  });
  waitForApproval.parameters = { resume: "timeInterval", amount: 1, unit: "seconds" };

  const resumeAi = makeWorkerHttp(
    "qa-resume-mock-analysis",
    "Resume Mock Analysis",
    "POST",
    "resume",
    [3340, -240],
    "={{ JSON.stringify({ etag: $json.etag }) }}",
  );
  const aiResumed = makeIf(
    "qa-ai-resume-completed",
    "AI Resume Completed?",
    "={{ $json.status === 'completed' && $json.mock === true && $json.job_id === $('Session').first().json.job_id }}",
    [3540, -240],
  );

  copy.nodes.push(
    deliveryDone,
    needsApproval,
    pollApproval,
    evalApproval,
    authorizedStart,
    waitForReview,
    waitForApproval,
    resumeAi,
    aiResumed,
  );

  const wire = (from, ...branches) => {
    copy.connections[from] = {
      main: branches.map((name) => (name ? [{ node: name, type: "main", index: 0 }] : [])),
    };
  };

  wire("Terminal?", deliveryDone.name, "Poll Budget Exceeded?");
  wire(deliveryDone.name, "Check Existing Delivery", needsApproval.name);
  wire(needsApproval.name, pollApproval.name, "Rights Gate");
  wire(pollApproval.name, evalApproval.name);
  wire(evalApproval.name, authorizedStart.name);
  wire(authorizedStart.name, resumeAi.name, waitForReview.name);
  wire(waitForReview.name, waitForApproval.name, "Respond Blocked");
  wire(waitForApproval.name, pollApproval.name);
  wire(resumeAi.name, aiResumed.name);
  wire(aiResumed.name, "Check Existing Delivery", "Respond Blocked");

  return copy;
}

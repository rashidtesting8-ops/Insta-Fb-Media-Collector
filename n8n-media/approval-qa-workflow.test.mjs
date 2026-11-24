import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { makeApprovalQa } from "./approval-qa-workflow.mjs";

const original = await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url), "utf8");
const qa = makeApprovalQa(JSON.parse(original));
test("isolated approval graph can only poll and conditionally resume the fake worker", () => {
  assert.equal(qa.active, false);
  const http = qa.nodes.filter(n => n.type.endsWith(".httpRequest"));
  assert.deepEqual(http.map(n => n.parameters.method), ["GET", "POST"]);
  for (const n of http) {
    assert.match(n.parameters.url, /http:\/\/pgm-qa-worker:8787\/qa\/collection\//);
    assert.equal(n.retryOnFail, false);
  }
  assert.deepEqual(qa.connections["Authorized start?"].main.map(b => b[0].node), ["Resume mock analysis", "Wait for review?"]);
  assert.equal(qa.connections["Wait Before Retry"].main[0][0].node, "Poll worker");
  assert.doesNotMatch(JSON.stringify(qa.nodes), /googleDrive|\/named\/|collection-decisions|api\.groq/i);
});
for (const [state, started, claimed, count, expected] of [
  ["pending", false, false, 1, "wait"], ["approved", false, false, 2, "wait"],
  ["approved", true, false, 3, "resume"], ["approved", true, true, 4, "hold"],
  ["revoked", false, false, 1, "hold"], ["stale", false, false, 1, "hold"],
  ["denied", false, false, 1, "hold"], ["pending", false, false, 120, "hold"],
]) test(`worker state ${state} / start ${started} / claimed ${claimed} / poll ${count}`, () => {
  const step = new Function("$input", "$runIndex", qa.nodes.find(n => n.name === "Read worker state").parameters.jsCode);
  assert.equal(step({ first: () => ({ json: { state, start_authorized: started, claimed } }) }, count - 1)[0].json.next, expected);
});

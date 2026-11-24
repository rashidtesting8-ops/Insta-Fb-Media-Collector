import { readFile, writeFile, mkdtemp } from "node:fs/promises";
import { spawnSync, spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { makeCanonicalQa, workflowId } from "../canonical-qa-workflow.mjs";
import { fixtureKeys, makeProjectExecution } from "./project-execution.mjs";

const dir = await mkdtemp(join(tmpdir(), "pgm-qa-n8n-"));
const canonical = JSON.parse(await readFile(new URL("../07-pipeline-orchestration.json", import.meta.url)));
const loopback = process.env.PGM_QA_LOOPBACK === "1";
const workflow = loopback ? await makeProjectExecution() : makeCanonicalQa(canonical);
const admin = { id: "fixtureWorkerKey", name: "Fixture worker key", type: "httpHeaderAuth",
  data: { name: "x-api-key", value: "fixture-admin" } };
const intake = { id: "fixtureIntakeKey", name: "Fixture intake key", type: "httpHeaderAuth",
  data: { name: "x-pipeline-key", value: "fixture-pipeline" } };
for (const node of loopback ? [] : workflow.nodes) {
  if (node.type === "n8n-nodes-base.webhook") {
    // This alias exists only in the separate throwaway n8n instance.
    node.parameters.path = node.parameters.path.replace(/^qa-/, "");
    node.disabled = false;
    node.credentials = { httpHeaderAuth: { id: intake.id, name: intake.name } };
  } else if (node.type === "n8n-nodes-base.httpRequest" && node.parameters.genericAuthType === "httpHeaderAuth") {
    node.credentials = { httpHeaderAuth: { id: admin.id, name: admin.name } };
  }
}
const credentialsPath = join(dir, "fixture-credentials.json");
const workflowPath = join(dir, "fixture-workflow.json");
const credentials = loopback ? Object.values(fixtureKeys).map(({ id, name, header, env }) => {
  const value = process.env[env];
  if (!/^[a-f0-9]{64}$/.test(value ?? "")) throw new Error("Missing fixture credential");
  return { id, name, type: "httpHeaderAuth", data: { name: header, value } };
}) : [admin, intake];
await writeFile(credentialsPath, JSON.stringify(credentials), { mode: 0o600 });
await writeFile(workflowPath, JSON.stringify(workflow));
try {
  for (const args of [["import:credentials", "--input=" + credentialsPath],
    ["import:workflow", "--input=" + workflowPath], ["publish:workflow", "--id=" + (loopback ? workflow.id : workflowId)]]) {
    const result = spawnSync("n8n", args, { stdio: "inherit" });
    if (result.status !== 0) throw new Error("Disposable n8n fixture setup failed");
  }
} finally {
  const { unlink } = await import("node:fs/promises");
  await unlink(credentialsPath);
}
const n8n = spawn("n8n", ["start"], { stdio: "inherit" });
process.on("SIGTERM", () => n8n.kill("SIGTERM"));
n8n.on("close", (code) => { process.exitCode = code ?? 1; });

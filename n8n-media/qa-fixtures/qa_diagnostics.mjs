import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import { randomUUID } from "node:crypto";

const SENSITIVE_KEY = /password|secret|token|cookie|authorization|assertion|api[_-]?key|session|credential/i;
const SENSITIVE_VALUE = /(__Host-)?pgm_(named|session)=|\bBearer\s+\S+|\bBasic\s+\S+|x-api-key|x-named-assertion|x-named-session|x-pipeline-key|gsk_[A-Za-z0-9]{8,}|ya29\.[A-Za-z0-9._-]{8,}|sk-[A-Za-z0-9]{8,}/i;
const ENV_SECRET_KEYS = [
  "PGM_QA_NAMED_ASSERTION_SECRET", "PGM_QA_NAMED_PASSWORD_A", "PGM_QA_NAMED_PASSWORD_B",
  "PGM_QA_WORKER_TOKEN", "PGM_QA_DASHBOARD_TOKEN", "PGM_QA_ADAPTER_TOKEN", "PGM_QA_DRIVE_TOKEN",
  "NAMED_ASSERTION_SECRET", "DASHBOARD_PASSWORD", "DASHBOARD_SESSION_SECRET",
  "MEDIA_WORKER_TOKEN", "MEDIA_WORKER_DASHBOARD_TOKEN", "N8N_PIPELINE_TOKEN", "AI_API_KEY",
];

function runtimeSecrets(extraSecrets = []) {
  const values = [];
  for (const key of ENV_SECRET_KEYS) {
    const val = process.env[key];
    if (typeof val === "string" && val.length >= 8) values.push(val);
  }
  for (const val of extraSecrets) {
    if (typeof val === "string" && val.length >= 8) values.push(val);
  }
  return values;
}

export function sanitizeValue(input, key = "", secrets = runtimeSecrets()) {
  if (key && SENSITIVE_KEY.test(key)) return "[REDACTED]";
  if (typeof input === "string") {
    if (SENSITIVE_VALUE.test(input)) return "[REDACTED]";
    for (const secret of secrets) {
      if (input.includes(secret)) return "[REDACTED]";
    }
    return input;
  }
  if (Array.isArray(input)) return input.map((item) => sanitizeValue(item, "", secrets));
  if (input && typeof input === "object") {
    const out = {};
    for (const [k, v] of Object.entries(input)) {
      out[k] = sanitizeValue(v, k, secrets);
    }
    return out;
  }
  return input;
}

export function sanitize(input, extraSecrets = []) {
  return JSON.stringify(sanitizeValue(input, "", runtimeSecrets(extraSecrets)), null, 2);
}

function safeName(raw) {
  const clean = String(raw || "qa").replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_-]+/g, "_");
  return clean || "qa";
}

export function createDiagnostics(component = process.env.PGM_QA_DIAGNOSTICS_COMPONENT || basename(process.argv[1] || "qa"), options = {}) {
  const safeComponent = safeName(component);
  const root = options.root || process.env.PGM_QA_DIAGNOSTICS_ROOT || "/qa-diagnostics";
  const runId = options.runId || process.env.PGM_QA_RUN_ID || randomUUID();
  const startedAt = Date.now();
  const events = [];
  const transitions = [];
  let jobId = null;

  const dir = () => join(root, runId);
  const writeRecord = (record) => {
    const targetDir = dir();
    const secrets = runtimeSecrets(options.extraSecrets);
    try {
      mkdirSync(targetDir, { recursive: true });
      appendFileSync(join(targetDir, `${safeComponent}.jsonl`),
        JSON.stringify(sanitizeValue(record, "", secrets)) + "\n", "utf8");
      writeFileSync(join(targetDir, `${safeComponent}.json`),
        JSON.stringify(sanitizeValue({
          component: safeComponent, run_id: runId, started_at: startedAt, job_id: jobId, events, transitions,
        }, "", secrets), null, 2) + "\n", "utf8");
    } catch (error) {
      const message = `QA diagnostics write failed (${targetDir}): ${error?.message || error}`;
      console.error(message);
      throw new Error(message, { cause: error });
    }
  };

  return {
    component: safeComponent,
    root,
    runId,
    startedAt,
    events,
    transitions,
    get jobId() { return jobId; },
    path() { return dir(); },
    logEvent(event, data = {}) {
      const entry = { t: Date.now(), component: safeComponent, job_id: jobId, event, ...data };
      events.push(entry);
      writeRecord({ kind: "event", ...entry });
      return entry;
    },
    setJobId(id) {
      jobId = id;
      return this.logEvent("qa_job_created", { job_id: id });
    },
    recordStage(stage, detail = {}) {
      const entry = { t: Date.now(), component: safeComponent, job_id: jobId, stage, detail };
      transitions.push(entry);
      writeRecord({ kind: "transition", ...entry });
      return entry;
    },
  };
}

export const diagnostics = createDiagnostics();

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:http";
import test from "node:test";
import { setTimeout } from "node:timers/promises";

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

test("built dashboard enforces sessions and routes authorized intake to n8n", { timeout: 20_000 }, async (t) => {
  const requests = [];
  const id = "ab".repeat(16);
  const upstream = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString();
    requests.push({ url: req.url, headers: req.headers, body });
    const intake = req.url === "/webhook/pipeline-intake";
    res.writeHead(intake ? 202 : 200, { "content-type": "application/json" });
    res.end(JSON.stringify(intake ? { job_id: id } : { ok: true, jobs: [] }));
  });
  const upstreamPort = await listen(upstream);
  t.after(() => {
    upstream.closeAllConnections();
    upstream.close();
  });

  const reservation = createServer();
  const port = await listen(reservation);
  await new Promise((resolve) => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const origin = "https://dashboard.example.test";
  const password = "fixture-operator-password";
  const child = spawn(process.execPath, [".next/standalone/server.js"], {
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env, PORT: String(port), HOSTNAME: "127.0.0.1", DASHBOARD_ORIGIN: origin,
      DASHBOARD_PASSWORD: password, DASHBOARD_SESSION_SECRET: "fixture-independent-signing-secret",
      MEDIA_WORKER_DASHBOARD_TOKEN: "fixture-read-only", N8N_PIPELINE_TOKEN: "fixture-pipeline",
      WORKER_URL: `http://127.0.0.1:${upstreamPort}`, N8N_URL: `http://127.0.0.1:${upstreamPort}`,
    },
  });
  let output = "";
  child.stdout.on("data", (chunk) => { output += chunk; });
  child.stderr.on("data", (chunk) => { output += chunk; });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) {
      const closed = once(child, "close");
      child.kill();
      await closed;
    }
  });

  const fetchAt = (path, options) => fetch(base + path, { ...options, redirect: "manual", signal: AbortSignal.timeout(3000) });
  let ready = false;
  for (let i = 0; i < 60 && !ready && child.exitCode === null; i++) {
    ready = await fetchAt("/api/ready").then((res) => res.ok, () => false);
    if (!ready) await setTimeout(100);
  }
  assert.ok(ready, output);
  assert.equal((await fetchAt("/api/jobs")).status, 401);
  const post = (url, body, headers) => fetchAt(url, {
    method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json", origin, ...headers },
  });
  assert.equal((await post("/api/auth/login", { password }, { origin: "https://foreign.example" })).status, 403);
  assert.equal((await post("/api/auth/login", { password: "wrong" })).status, 401);
  const login = await post("/api/auth/login", { password });
  assert.equal(login.status, 200);
  const cookieHeader = login.headers.get("set-cookie");
  assert.match(cookieHeader, /^__Host-pgm_session=/);
  assert.match(cookieHeader, /HttpOnly/);
  assert.match(cookieHeader, /Secure/);
  assert.match(cookieHeader, /SameSite=strict/i);
  assert.ok(!cookieHeader.includes(password));
  const cookie = cookieHeader.split(";")[0];
  const jobs = await fetchAt("/api/jobs", { headers: { cookie } });
  assert.equal(jobs.status, 200);
  assert.equal(jobs.headers.get("access-control-allow-origin"), null);
  assert.equal(requests.find((request) => request.url === "/jobs").headers["x-api-key"], "fixture-read-only");
  assert.equal((await post("/api/jobs", {}, { cookie, origin: "https://foreign.example" })).status, 403);
  const intake = await post("/api/jobs", { website_url: "https://business.test", authorization_confirmed: true }, { cookie });
  assert.deepEqual(await intake.json(), { job_id: id });
  assert.equal(intake.status, 202);
  const forwarded = requests.find((request) => request.url === "/webhook/pipeline-intake");
  assert.equal(forwarded.headers["x-pipeline-key"], "fixture-pipeline");
  assert.deepEqual(JSON.parse(forwarded.body), { website_url: "https://business.test" });
  assert.match((await post("/api/auth/logout", {}, { cookie })).headers.get("set-cookie"), /Max-Age=0/);
});

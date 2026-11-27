import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:http";
import { createHash, createHmac } from "node:crypto";
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
    if (req.url.startsWith("/named/")) {
      const [encoded, mac] = req.headers["x-named-assertion"].split(".");
      const claims = JSON.parse(Buffer.from(encoded, "base64url"));
      assert.equal(mac, createHmac("sha256", "disposable-assertion-key-".repeat(2)).update(encoded).digest("hex"));
      assert.equal(claims.aud, "pgm-worker");
      assert.equal(claims.action, req.url.slice(7));
      assert.equal(claims.body, createHash("sha256").update(body).digest("hex"));
      assert.equal(claims.sub, createHash("sha256").update(req.headers["x-named-session"]).digest("hex"));
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(req.url === "/named/login" ? { session: "x".repeat(43) } :
        req.url === "/named/intake" ? { idempotency_key: "named-" + "a".repeat(64) } : { ok: true }));
      return;
    }
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
      NAMED_ASSERTION_SECRET: "disposable-assertion-key-".repeat(2),
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
  const sourceUrls = {
    website_url: "https://business.test",
    instagram_url: "https://www.instagram.com/example/",
    facebook_url: "https://www.facebook.com/example/",
  };
  assert.equal((await post("/api/jobs", { facebook_url: sourceUrls.facebook_url }, { cookie })).status, 400);
  assert.equal((await post("/api/jobs", { website_url: sourceUrls.website_url, facebook_url: "https://evil.example/page" }, { cookie })).status, 400);
  const intake = await post("/api/jobs", { ...sourceUrls, authorization_confirmed: true }, { cookie });
  assert.deepEqual(await intake.json(), { job_id: id });
  assert.equal(intake.status, 202);
  const forwarded = requests.find((request) => request.url === "/webhook/pipeline-intake");
  assert.equal(forwarded.headers["x-pipeline-key"], "fixture-pipeline");
  assert.deepEqual(JSON.parse(forwarded.body), sourceUrls);
  assert.match((await post("/api/auth/logout", {}, { cookie })).headers.get("set-cookie"), /Max-Age=0/);
  assert.equal((await post("/api/auth/named/create", { role: "admin" }, { cookie })).status, 401);
  const namedLogin = await post("/api/auth/named/login", { name: "synthetic", password });
  assert.deepEqual(await namedLogin.json(), { ok: true });
  const namedHeader = namedLogin.headers.get("set-cookie");
  assert.match(namedHeader, /^__Host-pgm_named=/);
  for (const protection of [/Secure/, /HttpOnly/, /SameSite=strict/i]) assert.match(namedHeader, protection);
  const named = namedHeader.split(";")[0];
  assert.equal((await post("/api/auth/named/create", {}, { cookie: named, origin: "https://foreign.example" })).status, 403);
  assert.equal((await post("/api/jobs", { ...sourceUrls, actor: "spoof" }, { cookie: named })).status, 202);
  const namedIntake = requests.filter(r => r.url === "/webhook/pipeline-intake").at(-1);
  assert.deepEqual(JSON.parse(namedIntake.body), { ...sourceUrls, idempotency_key: "named-" + "a".repeat(64) });
});

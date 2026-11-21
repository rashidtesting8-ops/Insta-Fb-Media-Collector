import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { admitRequest, cookieName, cookieOptions, issueSession, readObject, validSession } from "../src/lib/security.ts";
import { submitIntake } from "../src/lib/intake.ts";

const now = Date.now();

test("signed session fails closed, expires and rejects tampering", () => {
  delete process.env.DASHBOARD_SESSION_SECRET;
  process.env.DASHBOARD_ORIGIN = "https://staging.example.test";
  process.env.DASHBOARD_PASSWORD = "test operator password";
  assert.equal(issueSession(process.env.DASHBOARD_PASSWORD), undefined);
  process.env.DASHBOARD_SESSION_SECRET = "independent test signing key at least 32 bytes";
  assert.equal(issueSession("wrong"), undefined);
  const token = issueSession(process.env.DASHBOARD_PASSWORD, now);
  assert.ok(token);
  assert.ok(!token.includes(process.env.DASHBOARD_PASSWORD));
  assert.ok(validSession(token, now));
  assert.ok(!validSession(token.replace(/^./, token.startsWith("1") ? "2" : "1"), now));
  assert.ok(!validSession(token, now + 8 * 60 * 60 * 1000));
  assert.equal(cookieName(), "__Host-pgm_session");
  assert.deepEqual(cookieOptions(), { httpOnly: true, secure: true, sameSite: "strict", path: "/", maxAge: 28800 });
});

test("login limiter resets after the window", () => {
  for (let i = 0; i < 10; i++) assert.ok(admitRequest("login", now));
  assert.ok(!admitRequest("login", now));
  assert.ok(admitRequest("login", now + 60_000));
});

test("object reader rejects invalid content and streamed overflow", async () => {
  const headers = { "content-type": "application/json" };
  const request = (body) => new Request("http://localhost/", { method: "POST", headers, body });
  assert.deepEqual(await readObject(request('{"ok":true}')), { ok: true });
  await assert.rejects(readObject(request("[]")), { status: 400 });
  await assert.rejects(readObject(request('{"long":true}'), 4), { status: 413 });
});

test("intake goes only to n8n and excludes rights and resume fields", async () => {
  const jobId = "ab".repeat(16);
  const result = await submitIntake(
    { website_url: "https://business.test", authorization_confirmed: true, job_id: jobId },
    "http://n8n:5678", "test-only",
    async (url, init) => {
      assert.equal(url.href, "http://n8n:5678/webhook/pipeline-intake");
      assert.equal(init.headers["X-Pipeline-Key"], "test-only");
      assert.equal(init.redirect, "error");
      assert.deepEqual(JSON.parse(init.body), { website_url: "https://business.test" });
      return Response.json({ job_id: jobId }, { status: 202 });
    },
  );
  assert.deepEqual(result, { status: 202, body: { job_id: jobId } });
  const bad = await submitIntake({}, "http://n8n:5678", "test-only", async () => Response.json({ ok: true }));
  assert.equal(bad.status, 502);
});

test("staging workflow retains intake and status without Drive credentials", () => {
  const staging = JSON.parse(execFileSync(process.execPath, ["../deploy/staging-workflow.mjs"], { encoding: "utf8" }));
  const names = new Set(staging.nodes.map((node) => node.name));
  assert.ok(names.has("Respond Accepted") && names.has("Status Webhook"));
  assert.ok(!staging.nodes.some((node) => node.type.endsWith(".googleDrive") || node.credentials?.googleDriveOAuth2Api));
  for (const branches of Object.values(staging.connections)) {
    for (const branch of branches.main) for (const edge of branch) assert.ok(names.has(edge.node));
  }
});

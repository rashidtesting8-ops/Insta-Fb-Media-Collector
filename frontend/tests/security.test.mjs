import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { admitRequest, cookieName, cookieOptions, issueSession, readObject, validSession } from "../src/lib/security.ts";
import { submitIntake } from "../src/lib/intake.ts";
import { curationSelection, isMockCuration } from "../src/lib/types.ts";

const now = Date.now();

test("dashboard reads nested selection and suppresses boolean mock output", () => {
  const selection = { selected_asset_ids: ["sample"], rights_review_required_ids: ["sample"], analysis_coverage: { mock: false } };
  assert.equal(curationSelection({ curation: selection }), selection);
  assert.equal(curationSelection({ curation: selection, ai_envelope: { mock: true } }), undefined);
  assert.ok(isMockCuration({ curation: { analysis_coverage: { mock: true } } }));
  assert.equal(curationSelection({ status: "provider_unavailable" }), undefined);
});

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

const sourceUrls = {
  website_url: "https://business.test",
  instagram_url: "https://www.instagram.com/example/",
  facebook_url: "https://www.facebook.com/example/",
};

test("intake goes only to n8n and excludes rights and resume fields", async () => {
  const jobId = "ab".repeat(16);
  const result = await submitIntake(
    { ...sourceUrls, authorization_confirmed: true, job_id: jobId },
    "http://n8n:5678", "test-only",
    async (url, init) => {
      assert.equal(url.href, "http://n8n:5678/webhook/pipeline-intake");
      assert.equal(init.headers["X-Pipeline-Key"], "test-only");
      assert.equal(init.redirect, "error");
      assert.deepEqual(JSON.parse(init.body), sourceUrls);
      return Response.json({ job_id: jobId }, { status: 202 });
    },
  );
  assert.deepEqual(result, { status: 202, body: { job_id: jobId } });
  const bad = await submitIntake(sourceUrls, "http://n8n:5678", "test-only", async () => Response.json({ ok: true }));
  assert.equal(bad.status, 502);
});

test("invalid or missing required source URLs never reach n8n while Instagram is optional", async () => {
  for (const [field, wrongPlatform, required] of [
    ["website_url", sourceUrls.facebook_url, true],
    ["instagram_url", sourceUrls.facebook_url, false],
    ["facebook_url", sourceUrls.instagram_url, true],
  ]) {
    const badValues = required
      ? [undefined, null, " ", "invalid", "https://example.test:bad/", wrongPlatform]
      : ["invalid", "https://example.test:bad/", wrongPlatform];
    for (const value of badValues) {
      const result = await submitIntake(
        { ...sourceUrls, [field]: value }, "http://n8n:5678", "test-only",
        () => assert.fail("Invalid job must not reach n8n"),
      );
      assert.equal(result.status, 400);
      assert.ok(result.body.error.startsWith(field + ":"));
    }
  }
  const jobId = "cd".repeat(16);
  const webAndFbOnly = { website_url: sourceUrls.website_url, facebook_url: sourceUrls.facebook_url };
  const ok = await submitIntake(webAndFbOnly, "http://n8n:5678", "test-only", async (_url, init) => {
    assert.deepEqual(JSON.parse(init.body), webAndFbOnly);
    return Response.json({ job_id: jobId }, { status: 202 });
  });
  assert.deepEqual(ok, { status: 202, body: { job_id: jobId } });
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

test("the optional fixture intake path is loopback-only and fails closed", async () => {
  const path = "/webhook-test/phase10-exec-pipeline-intake";
  const neverSend = () => assert.fail("Invalid fixture routing must never send");
  assert.equal((await submitIntake({}, "http://n8n:5678", "test-only", neverSend, path)).status, 503);
  assert.equal((await submitIntake({}, "http://127.0.0.1:5679", "test-only", neverSend, "/webhook-test/wrong")).status, 503);
  const response = await submitIntake(sourceUrls, "http://127.0.0.1:5679", "test-only", async (url) => {
    assert.equal(url.href, "http://127.0.0.1:5679" + path);
    return Response.json({ job_id: "fe".repeat(16) }, { status: 202 });
  }, path);
  assert.equal(response.status, 202);
});

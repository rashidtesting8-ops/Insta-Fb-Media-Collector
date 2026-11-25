import assert from "node:assert/strict";
import test from "node:test";
import {
  handleSetupStatus,
  handleSetupSubmit,
  SETUP_CSRF_HEADER,
  SETUP_CSRF_VALUE,
} from "../src/lib/setup-admin.ts";

const ORIGIN = "http://localhost:3000";
let clock = Date.now() + 1_000_000;

function nextWindow() {
  clock += 120_000;
  return clock;
}

function makeReq(method, body, headers = {}) {
  const merged = new Headers({
    host: "localhost:3000",
    origin: ORIGIN,
    "content-type": "application/json",
    [SETUP_CSRF_HEADER]: SETUP_CSRF_VALUE,
    "sec-fetch-site": "same-origin",
    ...headers,
  });
  return new Request(`${ORIGIN}/api/auth/setup-admin`, {
    method,
    headers: merged,
    body: method === "POST" ? (typeof body === "string" ? body : JSON.stringify(body)) : undefined,
  });
}

test("setup-admin enforces loopback origin, loopback host, and rejects reverse-proxy headers", async () => {
  const prevOrigin = process.env.DASHBOARD_ORIGIN;
  const neverCall = async () => assert.fail("Remote request must never reach collector");
  try {
    // Non-loopback HTTPS staging/prod origin hard-disables browser setup.
    process.env.DASHBOARD_ORIGIN = "https://app.example.test";
    const httpsStatus = await handleSetupStatus(makeReq("GET", null, { host: "app.example.test" }), neverCall);
    assert.equal(httpsStatus.status, 403);
    const httpsSubmit = await handleSetupSubmit(
      makeReq("POST", { setup_token: "t", name: "a", password: "p", confirm_password: "p" }, { origin: "https://app.example.test", host: "app.example.test" }),
      neverCall,
      nextWindow(),
    );
    assert.equal(httpsSubmit.status, 403);

    // Loopback DASHBOARD_ORIGIN still rejects non-loopback Host headers.
    process.env.DASHBOARD_ORIGIN = ORIGIN;
    for (const badHost of ["evil.example:3000", "192.168.1.50:3000", "10.0.0.5:3000", ""]) {
      const r = await handleSetupStatus(makeReq("GET", null, { host: badHost }), neverCall);
      assert.equal(r.status, 403);
    }

    // Reverse-proxy forwarding headers with non-loopback/external peers are always rejected.
    for (const [fwdHeader, fwdVal] of [
      ["x-forwarded-for", "203.0.113.10"],
      ["x-forwarded-for", "203.0.113.10, 127.0.0.1"],
      ["x-real-ip", "203.0.113.10"],
      ["forwarded", "for=203.0.113.10"],
      ["cf-connecting-ip", "203.0.113.10"],
      ["x-forwarded-host", "evil.example:3000"],
    ]) {
      const r = await handleSetupSubmit(
        makeReq("POST", { setup_token: "t", name: "a", password: "p", confirm_password: "p" }, { [fwdHeader]: fwdVal }),
        neverCall,
        nextWindow(),
      );
      assert.equal(r.status, 403);
    }

    // Next.js standalone internal loopback headers are accepted for direct local connections.
    const localStandalone = await handleSetupStatus(
      makeReq("GET", null, { "x-forwarded-for": "::ffff:127.0.0.1", "x-forwarded-host": "localhost:3000" }),
      async () => Response.json({ available: true }),
    );
    assert.equal(localStandalone.status, 200);
    assert.deepEqual(localStandalone.body, { available: true });
  } finally {
    if (prevOrigin === undefined) delete process.env.DASHBOARD_ORIGIN;
    else process.env.DASHBOARD_ORIGIN = prevOrigin;
  }
});

test("setup-admin enforces strict CSRF (Origin, custom header, and Sec-Fetch-Site)", async () => {
  const prevOrigin = process.env.DASHBOARD_ORIGIN;
  process.env.DASHBOARD_ORIGIN = ORIGIN;
  const neverCall = async () => assert.fail("CSRF-invalid request must never reach collector");
  const body = {
    setup_token: "single-use-token-1234567890",
    name: "ahsan-rashid",
    password: "ValidLongPassword!2026",
    confirm_password: "ValidLongPassword!2026",
  };
  try {
    // Bad Origin
    assert.equal(
      (await handleSetupSubmit(makeReq("POST", body, { origin: "https://evil.example" }), neverCall, nextWindow())).status,
      403,
    );
    // Missing custom CSRF header
    assert.equal(
      (await handleSetupSubmit(makeReq("POST", body, { [SETUP_CSRF_HEADER]: "" }), neverCall, nextWindow())).status,
      403,
    );
    // Cross-site Sec-Fetch-Site
    assert.equal(
      (await handleSetupSubmit(makeReq("POST", body, { "sec-fetch-site": "cross-site" }), neverCall, nextWindow())).status,
      403,
    );
  } finally {
    if (prevOrigin === undefined) delete process.env.DASHBOARD_ORIGIN;
    else process.env.DASHBOARD_ORIGIN = prevOrigin;
  }
});

test("setup-admin handles success, validation/token/repeat errors, rate limits, and never discloses plaintext secrets", async () => {
  const prevOrigin = process.env.DASHBOARD_ORIGIN;
  process.env.DASHBOARD_ORIGIN = ORIGIN;
  const logs = [];
  const origInfo = console.info;
  console.info = (...args) => logs.push(args.join(" "));
  const secretToken = "OneTimeSetupToken_AbCdEf0123456789";
  const secretPassword = "SuperSecretBrowserPassword!2026";
  try {
    // Status check
    const avail = await handleSetupStatus(
      makeReq("GET"),
      async (action) => {
        assert.equal(action, "bootstrap-status");
        return Response.json({ available: true });
      },
    );
    assert.equal(avail.status, 200);
    assert.deepEqual(avail.body, { available: true });

    // Missing setup token fails before collector
    const missingToken = await handleSetupSubmit(
      makeReq("POST", { setup_token: "  ", name: "ahsan-rashid", password: secretPassword, confirm_password: secretPassword }),
      async () => assert.fail("Missing token should fail locally"),
      nextWindow(),
    );
    assert.equal(missingToken.status, 403);

    // Successful one-time setup
    const ok = await handleSetupSubmit(
      makeReq("POST", {
        setup_token: secretToken,
        name: "ahsan-rashid",
        password: secretPassword,
        confirm_password: secretPassword,
      }),
      async (action, body) => {
        assert.equal(action, "bootstrap");
        assert.equal(body.name, "ahsan-rashid");
        assert.equal(body.setup_token, secretToken);
        return Response.json({ principal_id: "a".repeat(32) }, { status: 200 });
      },
      nextWindow(),
    );
    assert.equal(ok.status, 201);
    assert.deepEqual(ok.body, { ok: true, principal_id: "a".repeat(32) });
    assert.equal(ok.headers?.["Cache-Control"], "no-store");

    // Upstream error that accidentally echoed a secret is scrubbed to safe fallback
    const scrubbed = await handleSetupSubmit(
      makeReq("POST", {
        setup_token: secretToken,
        name: "ahsan-rashid",
        password: secretPassword,
        confirm_password: "MismatchPassword!2026",
      }),
      async () => Response.json({ detail: `Rejected ${secretPassword}` }, { status: 400 }),
      nextWindow(),
    );
    assert.equal(scrubbed.status, 400);
    assert.ok(!JSON.stringify(scrubbed.body).includes(secretPassword));
    assert.ok(!JSON.stringify(scrubbed.body).includes(secretToken));

    // Repeated bootstrap returns 409
    const repeat = await handleSetupSubmit(
      makeReq("POST", {
        setup_token: secretToken,
        name: "ahsan-rashid",
        password: secretPassword,
        confirm_password: secretPassword,
      }),
      async () => Response.json({ detail: "Bootstrap already consumed" }, { status: 409 }),
      nextWindow(),
    );
    assert.equal(repeat.status, 409);

    // Rate limiter caps setup endpoint at 5 attempts/minute
    const t = nextWindow();
    const body = {
      setup_token: secretToken,
      name: "ahsan-rashid",
      password: secretPassword,
      confirm_password: secretPassword,
    };
    for (let i = 0; i < 5; i++) {
      const r = await handleSetupSubmit(
        makeReq("POST", body),
        async () => Response.json({ detail: "Invalid or expired setup token" }, { status: 403 }),
        t,
      );
      assert.equal(r.status, 403);
    }
    const limited = await handleSetupSubmit(
      makeReq("POST", body),
      async () => assert.fail("Rate-limited call must not reach collector"),
      t,
    );
    assert.equal(limited.status, 429);
    assert.equal(limited.headers?.["Retry-After"], "60");

    // Confirm zero plaintext passwords or setup tokens ever appeared in logs.
    const allLogs = logs.join("\n");
    assert.ok(!allLogs.includes(secretPassword));
    assert.ok(!allLogs.includes(secretToken));
  } finally {
    console.info = origInfo;
    if (prevOrigin === undefined) delete process.env.DASHBOARD_ORIGIN;
    else process.env.DASHBOARD_ORIGIN = prevOrigin;
  }
});

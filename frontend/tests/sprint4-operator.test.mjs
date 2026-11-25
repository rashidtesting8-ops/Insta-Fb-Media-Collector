import assert from "node:assert/strict";
import test from "node:test";
import React from "react";

const { JSDOM } = await import("jsdom");
const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", { url: "http://localhost/" });
globalThis.window = dom.window;
globalThis.self = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.HTMLInputElement = dom.window.HTMLInputElement;
globalThis.HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
globalThis.HTMLButtonElement = dom.window.HTMLButtonElement;
globalThis.Node = dom.window.Node;
globalThis.Event = dom.window.Event;
globalThis.MouseEvent = dom.window.MouseEvent;
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);

const { act, fireEvent, render, waitFor } = await import("@testing-library/react");
const { namedCookieName, namedCookieOptionsFor } = await import("../src/lib/named.ts");
const { default: CollectionReview } = await import("../src/app/jobs/[id]/CollectionReview.tsx");
const { default: JobDetailClient } = await import("../src/app/jobs/[id]/JobDetailClient.tsx");

const ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const ETAG = "c".repeat(64);

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("namedCookieName adapts cleanly for HTTPS vs localhost HTTP", () => {
  const prev = process.env.DASHBOARD_ORIGIN;
  try {
    process.env.DASHBOARD_ORIGIN = "https://dashboard.example.test";
    assert.equal(namedCookieName(), "__Host-pgm_named");
    assert.equal(namedCookieOptionsFor().secure, true);

    process.env.DASHBOARD_ORIGIN = "http://localhost:3000";
    assert.equal(namedCookieName(), "pgm_named");
    assert.equal(namedCookieOptionsFor().secure, false);

    process.env.DASHBOARD_ORIGIN = "http://127.0.0.1:3000";
    assert.equal(namedCookieName(), "pgm_named");
    assert.equal(namedCookieOptionsFor().secure, false);
  } finally {
    if (prev === undefined) delete process.env.DASHBOARD_ORIGIN;
    else process.env.DASHBOARD_ORIGIN = prev;
  }
});

test("CollectionReview renders undated website toggle and async AI execute trigger", async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), opts });
    if (String(url).endsWith("/collection-approval")) {
      return jsonResponse({
        job_id: ID,
        revision: 2,
        etag: ETAG,
        state: "approved",
        start_authorized: true,
        window_start_utc: "2025-11-25T00:00:00+00:00",
        window_end_utc: "2026-11-25T00:00:00+00:00",
        manifest_sha256: "m".repeat(64),
        eligible_subset_sha256: "s".repeat(64),
        permissions: { approve: false, deny: false, revoke: true, start: false },
        eligible_assets: ["fb_001"],
        inherited_assets: {},
        sources: [
          { source: "website", discovered: 10, downloaded: 10, unknown_date: 10, out_of_window: 0, missing_children: 0, coverage: "unknown", evidence: "", outcomes: [] },
          { source: "facebook", discovered: 1, downloaded: 1, unknown_date: 0, out_of_window: 0, missing_children: 0, coverage: "unknown", evidence: "", outcomes: [] },
        ],
        history: [],
      });
    }
    if (String(url).endsWith("/ai-execute")) {
      return jsonResponse({ job_id: ID, status: "queued", async: true }, 202);
    }
    if (String(url).endsWith("/website-policy")) {
      return jsonResponse({ ok: true }, 200);
    }
    return jsonResponse({ error: "not found" }, 404);
  };

  const { container, getByText, getByPlaceholderText, unmount } = render(
    React.createElement(CollectionReview, { id: ID, includeUndatedWebsite: false, aiStatus: null }),
  );
  await waitFor(() => assert.ok(container.textContent.includes("Undated website media")), { timeout: 2000 });
  assert.ok(container.textContent.includes("Execute AI Visual Director V2"));

  // Toggle website policy
  const policyInput = getByPlaceholderText("Reason for website undated-media policy change");
  await act(async () => {
    fireEvent.change(policyInput, { target: { value: "Include evergreen salon gallery photos" } });
  });
  await act(async () => {
    fireEvent.click(getByText("Include undated website media"));
  });
  await waitFor(() => assert.ok(calls.some((c) => c.url.endsWith("/website-policy"))), { timeout: 2000 });
  const policyCall = calls.find((c) => c.url.endsWith("/website-policy"));
  assert.equal(policyCall.opts.headers["If-Match"], `"${ETAG}"`);
  assert.deepEqual(JSON.parse(policyCall.opts.body), {
    include_undated_website: true,
    reason: "Include evergreen salon gallery photos",
  });

  // Check consent checkbox and trigger async AI execute
  const checkbox = container.querySelector('input[type="checkbox"]');
  assert.ok(checkbox, "Consent checkbox missing");
  await act(async () => {
    fireEvent.click(checkbox);
  });
  await act(async () => {
    fireEvent.click(getByText("Run AI Curation (Async)"));
  });
  await waitFor(() => assert.ok(calls.some((c) => c.url.endsWith("/ai-execute"))), { timeout: 2000 });
  const execCall = calls.find((c) => c.url.endsWith("/ai-execute"));
  assert.equal(execCall.opts.headers["If-Match"], `"${ETAG}"`);
  assert.deepEqual(JSON.parse(execCall.opts.body), { confirm_processing_consent: true });
  unmount();
});

test("JobDetailClient renders video player, filters raw frames, shows counts+coverage, rights & Drive trigger", async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    const u = String(url);
    calls.push({ url: u, opts });
    if (u === `/api/jobs/${ID}`) {
      return jsonResponse({
        job_id: ID,
        status: "extraction_complete",
        website_url: "https://www.lilys-club.co.uk/",
        sources: { website: { status: "completed" }, facebook: { status: "completed" } },
        preprocessing: { status: "completed" },
        ai_curation: { status: "completed" },
      });
    }
    if (u === `/api/jobs/${ID}/status`) {
      return jsonResponse({
        job_id: ID,
        stage: "awaiting_rights_approval",
        terminal: true,
        job_status: "extraction_complete",
        detail: "Ready for rights review",
      });
    }
    if (u === `/api/jobs/${ID}/manifest`) {
      return jsonResponse({
        job_id: ID,
        counts: { total: 4 },
        coverage: { website: "complete" },
        assets: [
          {
            asset_id: "vid_001",
            platform: "facebook",
            type: "video",
            mime: "video/mp4",
            extraction_status: "downloaded",
            local_path: "social/vid_001.mp4",
            bytes: 204800,
            published_at_utc: "2026-09-12T10:00:00+00:00",
            frames: [{ local_path: "derived/frames/vid_001_f0.jpg" }],
          },
          {
            asset_id: "vid_001_f0",
            platform: "facebook",
            type: "frame",
            mime: "image/jpeg",
            extraction_status: "extracted_frame",
            local_path: "derived/frames/vid_001_f0.jpg",
          },
          {
            asset_id: "web_001",
            platform: "website",
            type: "image",
            mime: "image/jpeg",
            extraction_status: "downloaded",
            local_path: "website/raw/abc.media",
            bytes: 65536,
            published_at_utc: null,
          },
          {
            asset_id: "fb_meta_only",
            platform: "facebook",
            type: "image",
            extraction_status: "discovered",
            local_path: null,
          },
        ],
      });
    }
    if (u === `/api/jobs/${ID}/curation`) {
      return jsonResponse({
        ai_envelope: { mock: false, prompt_version: "v2", prompt_sha256: "1234567890abcdef" },
        curation: {
          mode: "B",
          selected_asset_ids: ["vid_001", "web_001"],
          rights_review_required_ids: ["vid_001"],
          best_images: [{ asset_id: "web_001", recommended_section: "hero", hero_rank: 1, overall_score: 94 }],
          best_videos: [{ asset_id: "vid_001", recommended_section: "feature", overall_score: 88 }],
          analysis_coverage: { mock: false },
        },
      });
    }
    if (u === `/api/jobs/${ID}/delivery` && (!opts || opts.method !== "POST")) {
      return jsonResponse({ status: "not_started" });
    }
    if (u === `/api/jobs/${ID}/delivery` && opts?.method === "POST") {
      return jsonResponse({
        status: "completed",
        drive_folder_id: "fld_12345678",
        drive_folder_link: "https://drive.google.com/drive/folders/fld_12345678",
      });
    }
    if (u === `/api/jobs/${ID}/rights` && (!opts || opts.method !== "POST")) {
      return jsonResponse({
        job_id: ID,
        revision: 1,
        manifest_sha256: "m".repeat(64),
        state: "approved",
        etag: ETAG,
        verified_asset_ids: [],
        verified_rights: {},
        history: [],
      });
    }
    if (u === `/api/jobs/${ID}/rights` && opts?.method === "POST") {
      return jsonResponse({
        job_id: ID,
        revision: 1,
        manifest_sha256: "m".repeat(64),
        state: "approved",
        etag: ETAG,
        verified_asset_ids: ["vid_001"],
        verified_rights: {
          vid_001: { actor: "admin1", evidence_ref: "client-email-01", reason: "Cleared", utc: "2026-11-25T00:00:00Z", decision: "verified" },
        },
        history: [],
      });
    }
    if (u === `/api/jobs/${ID}/collection-approval`) {
      return jsonResponse({ error: "none" }, 409);
    }
    return jsonResponse({ error: "not found" }, 404);
  };

  const { container, getAllByText, getByPlaceholderText, getByText, unmount } = render(
    React.createElement(JobDetailClient, { id: ID }),
  );
  await waitFor(() => assert.ok(container.textContent.includes("Media gallery & AI curation (3 assets)")), {
    timeout: 2000,
  });

  // Raw frame excluded (3 primary assets, not 4); video rendered as <video>; .media image as <img>; metadata-only has NO <img>
  assert.equal(container.querySelectorAll("video").length, 1);
  assert.equal(container.querySelectorAll("img").length, 1);
  assert.ok(container.textContent.includes("No local binary"));
  assert.ok(container.textContent.includes("Website · Date unverified"));
  assert.ok(container.textContent.includes("Published 2026-09-12"));
  assert.ok(container.textContent.includes("Hero #1"));
  assert.ok(container.textContent.includes('"coverage"'));
  assert.ok(container.textContent.includes('"counts"'));

  // Per-asset rights verification
  await act(async () => {
    fireEvent.change(getByPlaceholderText(/Evidence reference/), { target: { value: "client-email-01" } });
    fireEvent.change(getByPlaceholderText(/Reason \(e\.g\./), { target: { value: "Client confirmed" } });
  });
  await act(async () => {
    fireEvent.click(getAllByText("Verify")[0]);
  });
  await waitFor(() => assert.ok(calls.some((c) => c.url === `/api/jobs/${ID}/rights` && c.opts?.method === "POST")), {
    timeout: 2000,
  });
  const rightsPost = calls.find((c) => c.url === `/api/jobs/${ID}/rights` && c.opts?.method === "POST");
  assert.equal(rightsPost.opts.headers["If-Match"], `"${ETAG}"`);
  assert.equal(JSON.parse(rightsPost.opts.body).decision, "verified");

  // Trigger Google Drive delivery
  const checkboxes = container.querySelectorAll('input[type="checkbox"]');
  const releaseCheckbox = checkboxes[checkboxes.length - 1];
  await act(async () => {
    fireEvent.click(releaseCheckbox);
  });
  await act(async () => {
    fireEvent.click(getByText("Deliver Approved Media to Google Drive"));
  });
  await waitFor(() => assert.ok(calls.some((c) => c.url === `/api/jobs/${ID}/delivery` && c.opts?.method === "POST")), {
    timeout: 2000,
  });
  const deliveryPost = calls.find((c) => c.url === `/api/jobs/${ID}/delivery` && c.opts?.method === "POST");
  assert.equal(JSON.parse(deliveryPost.opts.body).authorization_confirmed, true);
  unmount();
});

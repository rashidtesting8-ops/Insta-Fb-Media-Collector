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
        status: "partial_success",
        collection_mode: "public_checkpoint",
        website_url: "https://www.lilys-club.co.uk/",
        sources: { website: { status: "completed" }, facebook: { status: "partial_success" } },
        preprocessing: { status: "completed" },
        ai_curation: { status: "completed" },
      });
    }
    if (u === `/api/jobs/${ID}/resume` && opts?.method === "POST") {
      return jsonResponse({ job_id: ID, status: "queued", resumed: true }, 202);
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
        ai_envelope: { mock: false, prompt_version: "V2.0", prompt_sha256: "1234567890abcdef" },
        curation: {
          mode: "FINAL_CURATE",
          business_visual_identity: { style_summary: "Warm salon aesthetic", recommended_design_direction: "Editorial" },
          selected_asset_ids: ["vid_001", "web_001"],
          rights_review_required_ids: ["vid_001"],
          hero_selection: [{ rank: 1, asset_id: "web_001", reason: "Clean portrait", desktop_crop: "16:9", mobile_crop: "4:5", focal_point: { x: 0.5, y: 0.4 } }],
          video_recommendations: [{ asset_id: "vid_001", recommended_section: "grooming_transformation", quality_score: 88, video_review_status: "frames_reviewed" }],
          analysis_coverage: { mock: false },
        },
      });
    }
    if (u === `/api/jobs/${ID}/delivery` && (!opts || opts.method !== "POST")) {
      return jsonResponse({
        status: "completed",
        reports_status: "completed",
        drive_folder_id: "fld_12345678",
        drive_folder_link: "https://drive.google.com/drive/folders/fld_12345678",
        subfolders: { "Best Images": "sub_img_1", Reports: "sub_rep_1" },
        uploaded: [{ asset_id: "web_001", filename: "web_001.jpg", target_subfolder: "Best Images", status: "uploaded", drive_file_id: "drv_1" }],
        reports_uploaded: [{ report_id: "ai_curation_report", filename: "ai-curation-report.json", target_subfolder: "Reports", status: "uploaded", drive_file_id: "drv_rep_1" }],
      });
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

  // Raw frame excluded (3 primary assets, not 4); video rendered as <video>; .media image as <img> in gallery; metadata-only has NO <img>
  assert.equal(container.querySelectorAll("video").length, 1);
  assert.equal(container.querySelectorAll("article img").length, 1);
  assert.ok(container.textContent.includes("Top Pick · Hero #1"));
  assert.ok(container.textContent.includes("Copy Drive Link"));
  assert.ok(container.textContent.includes("No local binary"));
  assert.ok(container.textContent.includes("Website · Date unverified"));
  assert.ok(container.textContent.includes("Published 2026-09-12"));
  assert.ok(container.textContent.includes("Hero #1"));
  assert.ok(container.textContent.includes("Warm salon aesthetic"));
  assert.ok(container.textContent.includes("frames_reviewed"));
  assert.ok(container.textContent.includes('"coverage"'));
  assert.ok(container.textContent.includes('"counts"'));
  assert.ok(container.textContent.includes("Best Images/"));
  assert.ok(container.textContent.includes("Reports/ai-curation-report.json"));

  // Resume checkpoint button triggers POST /resume
  await act(async () => {
    fireEvent.click(getByText("Resume Collection (Next Batch)"));
  });
  await waitFor(() => assert.ok(calls.some((c) => c.url === `/api/jobs/${ID}/resume` && c.opts?.method === "POST")), {
    timeout: 2000,
  });

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

test("CollectionReview renders plain-English coverage summary and second-admin submitter guidance", async () => {
  globalThis.fetch = async () =>
    jsonResponse({
      job_id: ID,
      revision: 2,
      etag: ETAG,
      state: "pending",
      start_authorized: false,
      window_start_utc: "2025-11-26T00:00:00+00:00",
      window_end_utc: "2026-11-26T00:00:00+00:00",
      manifest_sha256: "m".repeat(64),
      eligible_subset_sha256: "s".repeat(64),
      permissions: { approve: false, deny: true, revoke: false, start: false },
      eligible_assets: ["fb_upload_01"],
      inherited_assets: {},
      sources: [
        {
          source: "website",
          discovered: 4,
          downloaded: 4,
          unknown_date: 4,
          out_of_window: 0,
          missing_children: 0,
          coverage: "unknown",
          evidence: "independent inventory unavailable",
          outcomes: ["page_budget", "third_party_video_embed"],
        },
        {
          source: "facebook",
          discovered: 5,
          downloaded: 2,
          unknown_date: 0,
          out_of_window: 0,
          missing_children: 0,
          coverage: "unknown",
          evidence: "independent inventory unavailable",
          outcomes: ["robots_disallowed", "owner_media_uploaded"],
        },
      ],
      history: [],
    });

  const { container, unmount } = render(React.createElement(CollectionReview, { id: ID }));
  await waitFor(() => assert.ok(container.textContent.includes("Independent Second Admin Required")), { timeout: 2000 });
  assert.ok(container.textContent.includes("Facebook CDN blocked automated downloads (robots.txt)"));
  assert.ok(container.textContent.includes("Client Facebook media uploaded"));
  assert.ok(container.textContent.includes("External YouTube/Vimeo video embed detected"));
  assert.ok(container.textContent.includes("Scan limit reached"));
  unmount();
});

test("JobDetailClient renders robots.txt blocked Facebook card and 60-second client export guide", async () => {
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u === `/api/jobs/${ID}`) {
      return jsonResponse({
        job_id: ID,
        status: "partial_success",
        collection_mode: "public_checkpoint",
        website_url: "https://www.lilys-club.co.uk/",
      });
    }
    if (u === `/api/jobs/${ID}/status`) {
      return jsonResponse({ job_id: ID, stage: "partial_success", terminal: true, job_status: "partial_success", detail: "Review needed" });
    }
    if (u === `/api/jobs/${ID}/manifest`) {
      return jsonResponse({
        job_id: ID,
        assets: [
          {
            asset_id: "fb_blocked_2",
            platform: "facebook",
            type: "image",
            extraction_status: "pending",
            failure_reason: "robots_disallowed",
            local_path: null,
          },
        ],
      });
    }
    if (u === `/api/jobs/${ID}/curation`) return jsonResponse({ error: "none" }, 404);
    if (u === `/api/jobs/${ID}/delivery`) return jsonResponse({ error: "none" }, 404);
    if (u === `/api/jobs/${ID}/rights`) return jsonResponse({ error: "none" }, 404);
    return jsonResponse({ error: "none" }, 409);
  };

  const { container, unmount } = render(React.createElement(JobDetailClient, { id: ID }));
  await waitFor(() => assert.ok(container.textContent.includes("robots.txt blocked")), { timeout: 2000 });
  assert.ok(container.textContent.includes("Blocked by platform robots.txt policy"));
  assert.ok(container.textContent.includes("60-second export guide"));
  assert.ok(container.textContent.includes("Copy WhatsApp / Email instructions for client"));
  unmount();
});

test("Guided Studio UX supports 1-click website include, Quick Approve, teammate copy link, and 1-click batch AI rights", async () => {
  // 1. CollectionReview: 1-click Include Website Photos & Quick Approve
  const reviewCalls = [];
  globalThis.fetch = async (url, opts) => {
    const u = String(url);
    reviewCalls.push({ url: u, opts });
    if (u.endsWith("/collection-approval")) {
      return jsonResponse({
        job_id: ID,
        revision: 1,
        etag: ETAG,
        state: "pending",
        start_authorized: false,
        window_start_utc: "2025-11-26T00:00:00+00:00",
        window_end_utc: "2026-11-26T00:00:00+00:00",
        manifest_sha256: "m".repeat(64),
        eligible_subset_sha256: "s".repeat(64),
        permissions: { approve: true, deny: true, revoke: false, start: false },
        eligible_assets: ["img_1", "img_2"],
        inherited_assets: {},
        sources: [
          { source: "website", discovered: 10, downloaded: 10, unknown_date: 10, out_of_window: 0, missing_children: 0, coverage: "unknown", evidence: "", outcomes: [] },
        ],
        history: [],
      });
    }
    if (u.endsWith("/website-policy") || u.endsWith("/decisions")) {
      return jsonResponse({ ok: true }, 200);
    }
    return jsonResponse({ error: "not found" }, 404);
  };

  const revRender = render(React.createElement(CollectionReview, { id: ID, includeUndatedWebsite: false }));
  await waitFor(() => assert.ok(revRender.container.textContent.includes("Yes, Include Website Photos (1-Click)")), { timeout: 2000 });
  await act(async () => {
    fireEvent.click(revRender.getByText("Yes, Include Website Photos (1-Click)"));
  });
  await waitFor(() => assert.ok(reviewCalls.some((c) => c.url.endsWith("/website-policy"))), { timeout: 2000 });
  const wpBody = JSON.parse(reviewCalls.find((c) => c.url.endsWith("/website-policy")).opts.body);
  assert.equal(wpBody.include_undated_website, true);
  assert.ok(wpBody.reason.length > 5);

  await act(async () => {
    fireEvent.click(revRender.getByText("Quick Approve (1-Click)"));
  });
  await waitFor(() => assert.ok(reviewCalls.some((c) => c.url.endsWith("/decisions"))), { timeout: 2000 });
  const decBody = JSON.parse(reviewCalls.find((c) => c.url.endsWith("/decisions")).opts.body);
  assert.equal(decBody.action, "approve");
  assert.ok(decBody.reason.length > 5);
  revRender.unmount();

  // 2. JobDetailClient: 1-click batch commercial rights across multiple AI picks (sequential fresh etags)
  const rightsPosts = [];
  let rightsSeq = 0;
  const verifiedSoFar = [];
  globalThis.fetch = async (url, opts) => {
    const u = String(url);
    if (u === `/api/jobs/${ID}`) {
      return jsonResponse({ job_id: ID, status: "partial_success", collection_mode: "public_checkpoint", website_url: "https://www.petmania.co.uk/", ai_curation: { status: "completed" } });
    }
    if (u === `/api/jobs/${ID}/status`) {
      return jsonResponse({ job_id: ID, stage: "awaiting_rights_approval", terminal: true, job_status: "partial_success" });
    }
    if (u === `/api/jobs/${ID}/manifest`) {
      return jsonResponse({
        job_id: ID,
        assets: [
          { asset_id: "a1", platform: "website", type: "image", mime: "image/jpeg", extraction_status: "downloaded", local_path: "collection/a1.media", source_url: "https://www.petmania.co.uk/uploads/groom-1.jpg", bytes: 40000 },
          { asset_id: "a2", platform: "website", type: "image", mime: "image/jpeg", extraction_status: "downloaded", local_path: "collection/a2.media", source_url: "https://www.petmania.co.uk/uploads/groom-2.jpg", bytes: 42000 },
        ],
      });
    }
    if (u === `/api/jobs/${ID}/curation`) {
      return jsonResponse({
        ai_envelope: { mock: false, prompt_version: "V2.0" },
        curation: {
          mode: "FINAL_CURATE",
          business_visual_identity: { style_summary: "Warm salon portraits" },
          selected_asset_ids: ["a1", "a2"],
          hero_selection: [{ rank: 1, asset_id: "a1", reason: "Sharp portrait" }],
          gallery_images: [{ asset_id: "a2", reason: "Happy pup" }],
          analysis_coverage: { mock: false },
        },
      });
    }
    if (u === `/api/jobs/${ID}/rights` && opts?.method === "POST") {
      const body = JSON.parse(opts.body);
      rightsPosts.push({ asset_id: body.asset_id, ifMatch: opts.headers["If-Match"] });
      verifiedSoFar.push(body.asset_id);
      rightsSeq += 1;
      const nextTag = String(rightsSeq).repeat(64);
      return jsonResponse({
        job_id: ID,
        revision: 2,
        manifest_sha256: "m".repeat(64),
        state: "approved",
        etag: nextTag,
        verified_asset_ids: [...verifiedSoFar],
        verified_rights: Object.fromEntries(verifiedSoFar.map((aid) => [aid, { actor: "admin2", evidence_ref: body.evidence_ref, reason: body.reason, utc: "2026-11-27T00:00:00Z", decision: "verified" }])),
        history: [],
      });
    }
    if (u === `/api/jobs/${ID}/rights`) {
      return jsonResponse({ job_id: ID, revision: 2, manifest_sha256: "m".repeat(64), state: "approved", etag: ETAG, verified_asset_ids: [], verified_rights: {}, history: [] });
    }
    if (u === `/api/jobs/${ID}/delivery`) return jsonResponse({ error: "none" }, 404);
    return jsonResponse({ error: "none" }, 409);
  };

  const jobRender = render(React.createElement(JobDetailClient, { id: ID }));
  await waitFor(() => assert.ok(jobRender.container.textContent.includes("Confirm Rights for All 2 AI Picks (1-Click)")), { timeout: 2000 });
  // Human-readable source_url filenames shown instead of raw SHA hashes!
  assert.ok(jobRender.container.textContent.includes("groom-1.jpg"));
  assert.ok(jobRender.container.textContent.includes("groom-2.jpg"));

  await act(async () => {
    fireEvent.click(jobRender.getByText("Confirm Rights for All 2 AI Picks (1-Click)"));
  });
  await waitFor(() => assert.equal(rightsPosts.length, 2), { timeout: 2000 });
  assert.deepEqual(rightsPosts.map((r) => r.asset_id), ["a1", "a2"]);
  assert.equal(rightsPosts[0].ifMatch, `"${ETAG}"`);
  assert.equal(rightsPosts[1].ifMatch, `"${"1".repeat(64)}"`);
  jobRender.unmount();
});

test("JobDetailClient and CollectionReview surface live AI failure reason and 1-click audited AI reconciliation", async () => {
  const reconcileCalls = [];
  let revNum = 2;
  let claimed = true;
  let unresolved = 1;
  let aiStatus = "failed";

  globalThis.fetch = async (url, init) => {
    const u = String(url);
    const method = init?.method || "GET";
    if (u === `/api/jobs/${ID}`) {
      return jsonResponse({
        job_id: ID,
        status: "partial_success",
        website_url: "https://vanityfurr.co.uk/",
        include_undated_website: true,
        ai_curation:
          aiStatus === "failed"
            ? {
                status: "failed",
                model: "gemini-3.1-flash-lite",
                warnings: ["MODE A batch failed after 3 attempt(s): Read timed out. (read timeout=120)"],
                errors: ["Previous attempt requires reconciliation or was already sent"],
              }
            : { status: "reconciled" },
      });
    }
    if (u === `/api/jobs/${ID}/status`) {
      return jsonResponse({
        job_id: ID,
        stage: "partial_success",
        terminal: true,
        job_status: "partial_success",
        detail: "AI curation stopped before completing — review error below to reconcile and retry.",
      });
    }
    if (u === `/api/jobs/${ID}/manifest`) {
      return jsonResponse({
        job_id: ID,
        assets: [{ asset_id: "w1", platform: "website", type: "image", extraction_status: "downloaded", local_path: "website/w1.jpg", source_url: "https://vanityfurr.co.uk/groom.jpg" }],
      });
    }
    if (u === `/api/jobs/${ID}/curation` || u === `/api/jobs/${ID}/delivery`) return jsonResponse({ error: "none" }, 404);
    if (u === `/api/jobs/${ID}/rights`) {
      return jsonResponse({ job_id: ID, revision: revNum, manifest_sha256: "m".repeat(64), state: claimed ? "approved" : "pending", etag: ETAG, verified_asset_ids: [], verified_rights: {}, history: [] });
    }
    if (u === `/api/jobs/${ID}/collection-approval`) {
      return jsonResponse({
        job_id: ID,
        revision: revNum,
        etag: ETAG,
        state: claimed ? "approved" : "pending",
        start_authorized: claimed,
        ai_execution_claimed: claimed,
        unresolved_ai_attempts: unresolved,
        window_start_utc: "2025-11-26T00:00:00Z",
        window_end_utc: "2026-11-26T00:00:00Z",
        manifest_sha256: "m".repeat(64),
        eligible_subset_sha256: "s".repeat(64),
        permissions: { approve: !claimed, deny: true, revoke: claimed, start: false },
        eligible_assets: ["w1"],
        inherited_assets: {},
        sources: [{ source: "website", discovered: 1, downloaded: 1, unknown_date: 1, out_of_window: 0, missing_children: 0, coverage: "unknown", evidence: "x", outcomes: [] }],
        history: [],
      });
    }
    if (u === `/api/jobs/${ID}/ai-reconcile` && method === "POST") {
      const body = JSON.parse(String(init?.body || "{}"));
      reconcileCalls.push({ body, ifMatch: init?.headers?.["If-Match"], key: init?.headers?.["Idempotency-Key"] });
      revNum = 3;
      claimed = false;
      unresolved = 0;
      aiStatus = "reconciled";
      return jsonResponse({ ok: true });
    }
    return jsonResponse({ error: "unexpected" }, 404);
  };

  const rendered = render(React.createElement(JobDetailClient, { id: ID }));
  await waitFor(() => assert.ok(rendered.container.textContent.includes("AI Visual Director stopped before finishing")), { timeout: 2000 });
  const reconcileBtn = await waitFor(
    () => rendered.getByRole("button", { name: "Reconcile & Reset Failed AI Run" }),
    { timeout: 2000 },
  );
  assert.ok(rendered.container.textContent.includes("Read timed out"));
  // Dead Run AI Curation button must be hidden when revision already has a failed claim
  assert.ok(!rendered.container.textContent.includes("Run AI Curation (Async)"));

  await act(async () => {
    fireEvent.click(reconcileBtn);
  });
  await waitFor(() => assert.equal(reconcileCalls.length, 1), { timeout: 2000 });
  assert.equal(reconcileCalls[0].ifMatch, `"${ETAG}"`);
  assert.ok(reconcileCalls[0].key);
  assert.ok(reconcileCalls[0].body.reason.length > 10);
  await waitFor(() => assert.ok(rendered.container.textContent.includes("Quick Approve (1-Click)")), { timeout: 2000 });
  rendered.unmount();
});

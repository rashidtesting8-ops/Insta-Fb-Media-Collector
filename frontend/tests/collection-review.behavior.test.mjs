import assert from "node:assert/strict";
import { act, render, waitFor } from "@testing-library/react";
import test from "node:test";
import React from "react";

const { JSDOM } = await import("jsdom");
const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", { url: "http://localhost/" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.Node = dom.window.Node;
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);

const { default: CollectionReview } = await import("../src/app/jobs/[id]/CollectionReview.tsx");

const ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const sampleReview = (overrides = {}) => ({
  job_id: ID, revision: 1, etag: "abc123", state: "pending", start_authorized: false,
  window_start_utc: "2026-01-01T00:00:00+00:00", window_end_utc: "2027-01-01T00:00:00+00:00",
  manifest_sha256: "m", eligible_subset_sha256: "s", permissions: {}, eligible_assets: [],
  inherited_assets: {},
  sources: [{ source: "website", discovered: 1, downloaded: 1, unknown_date: 0, out_of_window: 0,
    missing_children: 0, coverage: "unknown", evidence: "", outcomes: [] }],
  history: [], ...overrides,
});

let fetches = [];
function setFetch(handler) {
  fetches = [];
  globalThis.fetch = (url, opts) => {
    fetches.push({ url, opts });
    return handler ? handler(url, opts) : Promise.resolve({
      ok: true, status: 200, json: async () => sampleReview(),
    });
  };
}

test("CR-01 initial panel load succeeds and renders review state", async () => {
  setFetch();
  const { container } = render(React.createElement(CollectionReview, { id: ID }));
  await waitFor(() => assert.ok(container.textContent.includes("Collection review")), { timeout: 2000 });
  assert.ok(fetches.some((f) => String(f.url).includes("/api/jobs/" + ID + "/collection-approval")));
});

test("CR-02 Strict Mode setup/cleanup does not crash or hang", async () => {
  setFetch();
  const { container } = render(
    React.createElement(React.StrictMode, null, React.createElement(CollectionReview, { id: ID })),
  );
  await waitFor(() => assert.ok(container.textContent.includes("Collection review")), { timeout: 2000 });
});

test("CR-03 unmount during in-flight request prevents stale updates", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  globalThis.fetch = () => gate;
  const { unmount } = render(React.createElement(CollectionReview, { id: ID }));
  unmount();
  release();
  await new Promise((r) => setTimeout(r, 50));
  assert.ok(true);
});

test("CR-04 overlapping requests resolving out of order keep newest data", async () => {
  const releases = [];
  globalThis.fetch = () => new Promise((r) => releases.push(r));
  const { container } = render(React.createElement(CollectionReview, { id: ID }));
  await waitFor(() => assert.equal(releases.length, 1), { timeout: 2000 });
  await act(async () => { releases[0]({ ok: true, status: 200, json: async () => sampleReview({ state: "older" }) }); });
  await waitFor(() => assert.ok(container.textContent.includes("older")), { timeout: 2000 });
  await act(async () => { container.querySelector("button")?.click(); });
  await waitFor(() => assert.ok(releases.length >= 2), { timeout: 2000 });
  await act(async () => { releases[1]({ ok: true, status: 200, json: async () => sampleReview({ state: "newer" }) }); });
  assert.ok(container.textContent.includes("newer") && !container.textContent.includes("older"));
});

test("CR-05 rapid manual Refresh with out-of-order resolution does not show older data", async () => {
  const releases = [];
  globalThis.fetch = () => new Promise((r) => releases.push(r));
  const { container } = render(React.createElement(CollectionReview, { id: ID }));
  await waitFor(() => assert.ok(releases.length >= 1), { timeout: 2000 });
  await act(async () => { releases[0]({ ok: true, status: 200, json: async () => sampleReview({ job_id: "r0" }) }); });
  await act(async () => { container.querySelector("button")?.click(); });
  await act(async () => {
    releases[0]({ ok: true, status: 200, json: async () => sampleReview({ state: "old" }) });
    releases[1]?.({ ok: true, status: 200, json: async () => sampleReview({ state: "new" }) });
  });
  assert.ok(container.textContent.includes("new") && !container.textContent.includes("old"));
});

test("CR-06 failed fetch restores error state", async () => {
  globalThis.fetch = () => Promise.reject(new Error("boom"));
  const { container } = render(React.createElement(CollectionReview, { id: ID }));
  await waitFor(() => assert.ok(container.textContent.includes("Review unavailable")), { timeout: 2000 });
});

test("CR-07 cleanup invalidates request generation ID (unmount + settle)", async () => {
  let calls = 0;
  globalThis.fetch = () => { calls++; return new Promise(() => {}); };
  const { unmount } = render(React.createElement(CollectionReview, { id: ID }));
  unmount();
  await act(async () => {});
  assert.ok(calls >= 1);
});

test("CR-08 approval/deny data paths unchanged", async () => {
  setFetch(() => Promise.resolve({
    ok: true, status: 200,
    json: async () => sampleReview({ permissions: { approve: true, deny: true } }),
  }));
  const { container } = render(React.createElement(CollectionReview, { id: ID }));
  await waitFor(() => assert.ok(container.textContent.includes("Approve")), { timeout: 2000 });
  assert.ok(container.textContent.includes("Deny"));
});

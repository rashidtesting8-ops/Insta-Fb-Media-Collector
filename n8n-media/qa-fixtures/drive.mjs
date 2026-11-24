import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { timingSafeEqual } from "node:crypto";
import { deliverWithReconciliation } from "../private-drive-jpeg-preflight.mjs";
import adapter from "../private-drive-n8n-adapter.cjs";
import { createDiagnostics } from "./qa_diagnostics.mjs";

const loopback = process.env.PGM_QA_LOOPBACK === "1";
const diagnostics = createDiagnostics("drive", loopback && !process.env.PGM_QA_DIAGNOSTICS_ROOT
  ? { root: join(tmpdir(), "pgm-qa-diagnostics") }
  : {});
const worker = loopback ? "http://127.0.0.1:8877" : "http://pgm-qa-worker:8787";
const workerToken = loopback ? process.env.PGM_QA_WORKER_TOKEN : "fixture-admin";
const expectedToken = Buffer.from(process.env.PGM_QA_DRIVE_TOKEN ?? "");
if (loopback) assert.ok(expectedToken.length >= 32 && workerToken?.length >= 32);
const owner = "offline_owner_id";
const root = "offline_root_id";
const folderMime = "application/vnd.google-apps.folder";
const jobs = new Map();
const ledgers = await mkdtemp(join(tmpdir(), "phase10-drive-fixture-"));

async function getWorker(path) {
  const response = await fetch(worker + path, { headers: { "x-api-key": workerToken }, redirect: "error" });
  assert.ok(response.ok, "Worker returned " + response.status);
  return response;
}

function fakeDrive(state, asset, content) {
  const params = { clientId: "fixture-client", clientSecret: "fixture-secret",
    oauthTokenData: { access_token: "fixture-access", refresh_token: "fixture-refresh" } };
  const fail = (status) => Object.assign(new Error("Fixture Drive " + status), { httpStatus: status });
  return adapter.makeAuthorizedRequest(params, async (url, method, headers, _body, _max, _binary, onStatus) => {
    if (url.origin === "https://oauth2.googleapis.com") {
      state.refreshes++;
      if (state.scenario === "oauth-rejected") { onStatus(400); throw fail(400); }
      onStatus(200);
      return { access_token: "fixture-refreshed" };
    }
    assert.equal(url.origin, "https://www.googleapis.com");
    const resource = url.pathname.replace(/^\/drive\/v3\//, "");
    if (resource === "about" && state.scenario.startsWith("oauth-") && headers.Authorization !== "Bearer fixture-refreshed") {
      onStatus(401);
      throw fail(401);
    }
    if (method === "POST") {
      assert.equal(resource, "/upload/drive/v3/files");
      state.attempts.push(asset.upload_key);
      if (state.scenario === "upload-failure") { onStatus(503); throw fail(503); }
      const id = "offline_" + state.folderId + "_" + asset.sha256.slice(0, 12);
      state.files.set(id, { id, name: asset.filename, mimeType: asset.mime, size: String(content.length),
        parents: [state.folderId], sha256Checksum: asset.sha256, content });
      if (state.scenario === "ambiguous") throw new Error("Fixture transport lost the committed upload response");
      onStatus(200);
      return { id };
    }
    assert.equal(method, "GET");
    onStatus(200);
    if (resource === "about") return { user: { me: true, permissionId: owner } };
    if (resource === "files") {
      const q = url.searchParams.get("q");
      assert.ok(q.includes("'" + state.folderId + "' in parents"));
      return { files: [...state.files.values()].filter((file) => q.endsWith("name = '" + file.name + "'"))
        .map(({ id }) => ({ id })) };
    }
    if (resource.endsWith("/permissions")) return { permissions: [{ id: owner, role: "owner", type: "user" }] };
    const id = resource.slice("files/".length);
    const item = id === "root" ? { id: root, mimeType: folderMime } :
      id === state.folderId ? { id, name: "Offline owner-only folder", mimeType: folderMime, parents: [root] } :
      state.files.get(id);
    assert.ok(item);
    if (url.searchParams.get("alt") === "media") return item.content;
    const { content: _stored, ...metadata } = item;
    return { ...metadata, ownedByMe: true, trashed: false, owners: [{ permissionId: owner }] };
  });
}

async function deliver({ job_id, upload_keys }) {
  assert.match(job_id, /^[a-f0-9]{32}$/);
  assert.ok(Array.isArray(upload_keys) && upload_keys.length && new Set(upload_keys).size === upload_keys.length);
  const job = await (await getWorker("/jobs/" + job_id)).json();
  const plan = await (await getWorker("/jobs/" + job_id + "/delivery-plan")).json();
  assert.equal(plan.status, "ready");
  const eligible = plan.pending_uploads.filter((item) => item.rights_status === "verified" && item.rights_review_required === false);
  assert.deepEqual(upload_keys, eligible.map(({ upload_key }) => upload_key));
  const state = jobs.get(job_id) ?? { folderId: loopback ? "1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF" : "qa_folder_" + job_id,
    scenario: new URL(job.website_url).pathname.split("/")[1],
    attempts: [], files: new Map(), refreshes: 0, reconciled: 0 };
  jobs.set(job_id, state);
  diagnostics.setJobId(job_id);
  diagnostics.recordStage("mock_drive_deliver_start", { job_id, eligible: eligible.map((a) => a.asset_id), folder_id: state.folderId });
  const uploads = [];
  for (const asset of eligible) {
    const original = await getWorker("/jobs/" + job_id + "/file?" + new URLSearchParams({ path: asset.local_path }));
    const bytes = Buffer.from(await original.arrayBuffer());
    const transport = fakeDrive(state, asset, bytes);
    const input = {
      folderId: state.folderId, folderName: "Offline owner-only folder", expectedOwner: owner,
      ledgerPath: join(ledgers, job_id + "-" + asset.sha256 + ".json"),
      asset: { mime: asset.mime, name: asset.filename, bytes: asset.bytes, sha256: asset.sha256, assetId: asset.asset_id },
      bytes, approval: { syntheticTestOnly: true, uploadKey: asset.upload_key, folderId: state.folderId, ownerPermissionId: owner },
      driveGet: (url, query, options) => transport.request({ url, method: "GET", query, maxBytes: options?.maxBytes }),
      uploadOriginal: (item) => {
        assert.equal(item.json.verifiedDriveFolderId, state.folderId);
        assert.equal(item.binary.data.fileName, asset.filename);
        assert.deepEqual(Buffer.from(item.binary.data.data, "base64"), bytes);
        return transport.request({ url: new URL("https://www.googleapis.com/upload/drive/v3/files"), method: "POST" });
      },
    };
    try {
      const { result, recoveredAfterError } = await deliverWithReconciliation(input, transport.httpStatuses);
      if (recoveredAfterError) state.reconciled++;
      uploads.push({ upload_key: asset.upload_key, status: result.status, drive_file_id: result.fileId,
        owner_only: true, sha256: result.sha256 });
    } catch {
      uploads.push({ upload_key: asset.upload_key, status: "failed" });
    }
  }
  diagnostics.recordStage("mock_drive_deliver_result", { job_id, folder_id: state.folderId, uploads });
  return { job_id, uploads, drive_folder_id: state.folderId };
}

createServer(async (request, response) => {
  try {
    if (loopback) {
      const actual = Buffer.from(request.headers["x-qa-drive-token"]?.toString() ?? "");
      if (actual.length !== expectedToken.length || !timingSafeEqual(actual, expectedToken)) {
        response.writeHead(401).end();
        return;
      }
    }
    let result;
    if (request.url === "/health") result = { ok: true };
    else if (request.url.startsWith("/trace/")) {
      const state = jobs.get(request.url.split("/").at(-1));
      result = state ? { attempts: state.attempts, files: state.files.size, refreshes: state.refreshes, reconciled: state.reconciled } : null;
    } else if (request.url === "/deliver" && request.method === "POST") {
      let body = "";
      for await (const chunk of request) {
        body += chunk;
        assert.ok(body.length < 65536);
      }
      result = await deliver(JSON.parse(body));
    } else {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(result));
  } catch (error) {
    console.error("Fixture adapter:", error.message);
    response.writeHead(409, { "content-type": "application/json" }).end(JSON.stringify({ error: "fixture_delivery_failed" }));
  }
}).listen(loopback ? 8899 : 8799, loopback ? "127.0.0.1" : "0.0.0.0");

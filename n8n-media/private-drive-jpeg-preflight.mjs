import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rmdir, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, resolve, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import adapter from "./private-drive-n8n-adapter.cjs";
import { deliverPrivateTestFile } from "./private-drive-delivery.mjs";

const { contract } = adapter;
const adapterSource = await readFile(new URL("./private-drive-n8n-adapter.cjs", import.meta.url), "utf8");
const textReceipt = {
  id: "1lC_OUVRwXDM1NwdG8vqAZykcAHP_W8yA",
  name: "synthetic-drive-upload-test.txt",
  digest: "12c8eb2ef38561b1443c97207a71afb56a0a7a2c9c0b779689bb99e4c340ab1c",
  bytes: 165,
};

// Runs inside the project n8n container. The dry transport never opens a socket.
async function containerSession() {
  const input = require("node:readline").createInterface({ input: process.stdin, crlfDelay: Infinity });
  const messages = input[Symbol.asyncIterator]();
  const { loadProjectBinding, makeAdapter, createLiveAdapter, contract } = module.exports;
  const emit = (message) => process.stdout.write(JSON.stringify(message) + "\n");

  try {
    const initial = await messages.next();
    const options = JSON.parse(initial.value);
    let session;
    let file;
    let posted = 0;
    let patched = 0;
    if (options.mode === "dry-run") {
      const binding = loadProjectBinding(options.workflowId);
      const root = "offline_root_id";
      const owner = "offline_owner_id";
      const content = Buffer.from(options.base64, "base64");
      const priorContent = Buffer.from(options.prior.base64, "base64");
      const metadata = {
        root: { id: root, mimeType: "application/vnd.google-apps.folder" },
        [contract.folderId]: { id: contract.folderId, name: options.folderName, parents: [root],
          mimeType: "application/vnd.google-apps.folder" },
        [options.prior.id]: { id: options.prior.id, name: options.prior.name, parents: [contract.folderId],
          mimeType: "text/plain", size: String(priorContent.length) },
      };
      const request = async ({ url, method, query, body }) => {
        if (method === "POST") {
          posted++;
          if (!body.includes(content)) throw new Error("Binary fixture missing from multipart payload");
          file = { id: "offline_jpeg_id", name: contract.fileName, parents: [contract.folderId],
            mimeType: "image/jpeg", size: String(content.length), sha256Checksum: contract.digest };
          metadata[file.id] = file;
          return { id: file.id };
        }
        if (method === "PATCH") {
          patched++;
          if (options.scenario.startsWith("lost-response")) throw new Error("Simulated metadata response loss");
          return { id: file.id };
        }
        const part = url.pathname.slice("/drive/v3/".length);
        if (part === "about") return { user: { permissionId: owner, me: true } };
        if (part === "files") return { files: file && options.scenario !== "lost-response-no-match" ? [{ id: file.id }] : [] };
        if (part.endsWith("/permissions")) {
          if (options.scenario === "shared-folder" && part.includes(contract.folderId)) {
            return { permissions: [{ id: owner, type: "user", role: "owner" }, { type: "anyone", role: "reader" }] };
          }
          return { permissions: [{ id: owner, type: "user", role: "owner" }] };
        }
        if (query.alt === "media") return part === "files/" + options.prior.id ? priorContent : content;
        const found = metadata[part.slice("files/".length)];
        if (!found) throw new Error("Unknown mock Drive ID");
        return { ...found, owners: [{ permissionId: owner }], ownedByMe: true, trashed: false };
      };
      session = makeAdapter({ ...binding, request });
    } else if (options.mode === "live") {
      session = createLiveAdapter(options.workflowId, options.liveApproval);
    } else {
      throw new Error("Invalid n8n Drive session mode");
    }
    emit({ ok: true });
    for await (const line of messages) {
      const message = JSON.parse(line);
      try {
        let result;
        if (message.command === "read") {
          const parsed = new URL(message.url);
          result = await session.driveGet(parsed, message.query, message.options);
        } else if (message.command === "upload") {
          result = await session.uploadOriginal(message.item);
          // None of the provider's extra user or account metadata leaves n8n.
          result = { id: result.id };
        } else if (message.command === "counts" && options.mode === "dry-run") {
          result = { posted, patched };
        } else if (message.command === "http-statuses" && options.mode === "live") {
          result = session.httpStatuses();
        } else {
          throw new Error("Unsupported Drive session command");
        }
        emit(Buffer.isBuffer(result) ? { ok: true, binary: result.toString("base64") } : { ok: true, result });
      } catch (error) {
        emit({ ok: false, status: Number.isInteger(error.httpStatus) ? error.httpStatus : undefined });
      }
    }
  } catch {
    emit({ ok: false });
    process.exitCode = 1;
  } finally {
    input.close();
  }
}

async function withProjectDriveSession(options, run) {
  const child = spawn("docker", ["exec", "-i", "pgm-media-n8n-1", "node", "-e",
    adapterSource + "\n(" + containerSession.toString() + ")();"], { stdio: ["pipe", "pipe", "ignore"] });
  child.stdin.on("error", () => {});
  child.on("error", () => {});
  const closed = new Promise((done) => child.once("close", done));
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity })[Symbol.asyncIterator]();
  async function exchange(message) {
    child.stdin.write(JSON.stringify(message) + "\n");
    const response = await lines.next();
    if (response.done) throw new Error("Project n8n Drive session closed early");
    const value = JSON.parse(response.value);
    if (!value.ok) throw new Error(value.status ? "Drive HTTP " + value.status : "Project n8n Drive operation failed");
    return value.binary ? Buffer.from(value.binary, "base64") : value.result;
  }
  try {
    await exchange(options);
    return await run({
      driveGet: (url, query, readOptions) => exchange({ command: "read", url, query, options: readOptions }),
      uploadOriginal: (item) => exchange({ command: "upload", item }),
      counts: () => exchange({ command: "counts" }),
      httpStatuses: () => exchange({ command: "http-statuses" }),
    });
  } finally {
    child.stdin.end();
    await closed;
  }
}

async function verifyFixture(directory, liveApproval) {
  const manifest = JSON.parse(await readFile(join(directory, "test-manifest.json"), "utf8"));
  assert.equal(manifest.related_to_groq_curation, false);
  assert.deepEqual(manifest.authorization, {
    local_generation_and_offline_preparation: "explicitly_authorized",
    groq_inference: "not_authorized",
    drive_upload: manifest.drive_test_approval ? "one_isolated_jpeg_upload_only" : "not_authorized",
    commercial_distribution: "not_authorized",
  });
  if (liveApproval) assert.deepEqual(manifest.drive_test_approval, liveApproval);
  const [entry] = manifest.assets;
  assert.equal(manifest.assets.length, 1);
  assert.equal(entry.asset_id, contract.assetId);
  assert.equal(entry.local_path, contract.fileName);
  assert.equal(entry.rights_status, "synthetic_fixture_local_test_only");
  assert.equal(entry.third_party_assets_used, false);
  assert.equal(entry.source, "locally drawn original illustration with Python/Pillow");
  const bytes = await readFile(join(directory, contract.fileName));
  assert.equal(entry.sha256, contract.digest);
  assert.equal(createHash("sha256").update(bytes).digest("hex"), contract.digest);
  assert.equal(entry.bytes, contract.byteCount);
  assert.equal(bytes.length, entry.bytes);
  return { bytes, asset: { assetId: entry.asset_id, name: entry.local_path, mime: entry.mime, bytes: entry.bytes, sha256: entry.sha256 } };
}

async function verifyPriorOwner(driveGet, directory) {
  const receiptPath = resolve(directory, "../drive-upload-preflight-20261123/test-delivery-ledger.json");
  const ledger = JSON.parse(await readFile(receiptPath, "utf8"));
  assert.equal(ledger.status, "verified");
  assert.equal(ledger.drive_folder_id, contract.folderId);
  assert.equal(ledger.test_attempt.drive_file_id, textReceipt.id);
  assert.equal(ledger.test_attempt.observed_sha256, textReceipt.digest);
  const get = (resource, query, options) =>
    driveGet(new URL("https://www.googleapis.com/drive/v3" + resource), query, options);
  const { user } = await get("/about", { fields: "user(me,permissionId)" });
  assert.ok(user?.me === true && /^[A-Za-z0-9_-]{8,200}$/.test(user.permissionId), "Google account identity unavailable");
  const resource = "/files/" + textReceipt.id;
  const prior = await get(resource, { fields: "id,name,parents,mimeType,ownedByMe,owners(permissionId),driveId,trashed,shared,size" });
  assert.ok(prior.id === textReceipt.id && prior.name === textReceipt.name && prior.ownedByMe === true && !prior.driveId &&
    prior.trashed === false && prior.shared !== true && prior.mimeType === "text/plain" && Number(prior.size) === textReceipt.bytes &&
    prior.parents?.length === 1 && prior.parents[0] === contract.folderId &&
    prior.owners?.length === 1 && prior.owners[0].permissionId === user.permissionId, "Prior private receipt does not match this account");
  const acl = await get(resource + "/permissions", { fields: "permissions(id,type,role,deleted),nextPageToken" });
  const [permission] = acl.permissions ?? [];
  assert.ok(!acl.nextPageToken && acl.permissions?.length === 1 && permission.id === user.permissionId &&
    permission.type === "user" && permission.role === "owner" && !permission.deleted, "Prior text receipt is not owner-only");
  const content = await get(resource, { alt: "media" }, { maxBytes: textReceipt.bytes });
  assert.ok(Buffer.isBuffer(content) && content.length === textReceipt.bytes &&
    createHash("sha256").update(content).digest("hex") === textReceipt.digest, "Prior text receipt checksum mismatch");
  return user.permissionId;
}

export async function deliverWithReconciliation(input, httpStatuses) {
  try {
    return { result: await deliverPrivateTestFile(input), recoveredAfterError: false };
  } catch (error) {
    const ledger = await readFile(input.ledgerPath, "utf8").then(JSON.parse, () => null);
    const statuses = await httpStatuses();
    if (!["upload_intent", "received"].includes(ledger?.status) ||
      !statuses.some(({ action }) => action === "jpeg_upload")) throw error;
    try {
      const result = await deliverPrivateTestFile({ ...input, uploadOriginal: () => {
        throw new Error("Reconciliation cannot perform uploads");
      } });
      return { result, recoveredAfterError: true };
    } catch (reconciliationError) {
      reconciliationError.deliveryStage = "read_only_reconciliation";
      throw reconciliationError;
    }
  }
}

// The command-line entry is dry only. The live API requires separate approval.
export async function runApprovedJpeg({ workflowId, directory, folderName, liveApproval }) {
  assert.ok(liveApproval && liveApproval.workflowId === workflowId &&
    liveApproval.folderId === contract.folderId && liveApproval.filename === contract.fileName &&
    liveApproval.sha256 === contract.digest && liveApproval.jpegUploadApproved === true &&
    liveApproval.maximumUploads === 1 && liveApproval.automaticRetries === 0);
  const fixture = await verifyFixture(directory, liveApproval);
  const ledgerPath = join(directory, "jpeg-delivery-ledger.json");
  await assert.rejects(readFile(ledgerPath), { code: "ENOENT" }, "An existing ledger requires a separate reconciliation");
  return withProjectDriveSession({ mode: "live", workflowId, liveApproval }, async (transport) => {
    let stage = "verify_prior_account";
    try {
      const ownerPermissionId = await verifyPriorOwner(transport.driveGet, directory);
      const input = { ...transport, ...fixture, ledgerPath, folderId: contract.folderId, folderName,
        expectedOwner: ownerPermissionId, approval: { syntheticTestOnly: true,
          uploadKey: contract.assetId + ":" + contract.digest, folderId: contract.folderId, ownerPermissionId } };
      stage = "verify_destination_and_deliver";
      const outcome = await deliverWithReconciliation(input, transport.httpStatuses);
      return { passed: true, ...outcome, httpStatuses: await transport.httpStatuses() };
    } catch (error) {
      return { passed: false, stage: error.deliveryStage ?? stage, reason: error.message,
        httpStatuses: await transport.httpStatuses() };
    }
  });
}

// Use only as the protected worker bridge's Drive callback. This path cannot
// reuse the earlier isolated upload grant or write the earlier receipt.
export async function deliverGuardedE2eJpeg({ jobDirectory, fixtureDirectory, approval, uploadKey, asset, bytes }) {
  const jobId = approval?.job_id;
  assert.ok(approval?.groq_approved === true && approval?.drive_approved === true &&
    approval.folder_id === contract.folderId && /^[a-f0-9]{32}$/.test(jobId ?? "") &&
    basename(resolve(jobDirectory)) === jobId, "Separate job-scoped Groq and Drive approvals required");
  assert.ok(approval.sha256 === contract.digest && asset.sha256 === contract.digest &&
    asset.asset_id === approval.asset_id && asset.source_url === approval.source_url && asset.rights_status === "verified" &&
    uploadKey === asset.asset_id + ":" + contract.digest && asset.mime === "image/jpeg" &&
    Buffer.isBuffer(bytes) && bytes.length === contract.byteCount &&
    createHash("sha256").update(bytes).digest("hex") === contract.digest, "E2E JPEG or rights scope mismatch");
  const stored = await readFile(join(jobDirectory, "curation.json"));
  const curation = JSON.parse(stored);
  const proof = JSON.parse(await readFile(join(jobDirectory, "guarded-groq-verified.json"), "utf8"));
  assert.ok(curation.job_id === jobId && curation.status === "completed" &&
    curation.ai_envelope?.mock === false && curation.ai_envelope.provider === "openai_compatible" &&
    curation.curation?.job_id === jobId && curation.curation.analysis_coverage?.mock === false &&
    curation.ai_envelope.model === "qwen/qwen3.8-27b" &&
    curation.curation.selected_asset_ids.length === 1 && curation.curation.selected_asset_ids[0] === asset.asset_id &&
    curation.curation.rights_review_required_ids.length === 0 &&
    proof.job_id === jobId && proof.asset_id === asset.asset_id && proof.sha256 === contract.digest && proof.model === curation.ai_envelope.model &&
    proof.curation_sha256 === createHash("sha256").update(stored).digest("hex"),
  "Drive adapter requires matching verified non-mock curation and asset proof");
  assert.deepEqual(proof.attempts.map(({ mode, http, schema }) => [mode, http, schema]),
    [["A", 200, "PASS"], ["B", 200, "PASS"]]);
  const workflowId = contract.guardedWorkflowId;
  const liveApproval = { jpegUploadApproved: true, workflowId, jobId, scope: "phase10-guarded-e2e",
    sha256: contract.digest, filename: contract.fileName, folderId: contract.folderId };
  return withProjectDriveSession({ mode: "live", workflowId, liveApproval }, async (transport) => {
    const expectedOwner = await verifyPriorOwner(transport.driveGet, fixtureDirectory);
    const { result } = await deliverWithReconciliation({
      ...transport, bytes, expectedOwner, folderId: contract.folderId, folderName: "PGM Local Drive Acceptance - 2026-11-23",
      ledgerPath: join(jobDirectory, "guarded-drive-ledger.json"),
      asset: { assetId: asset.asset_id, mime: asset.mime, name: contract.fileName, bytes: bytes.length, sha256: contract.digest },
      approval: { syntheticTestOnly: true, uploadKey, folderId: contract.folderId, ownerPermissionId: expectedOwner },
    }, transport.httpStatuses);
    return { job_id: jobId, folder_id: result.folderId, upload_key: result.uploadKey, sha256: result.sha256,
      owner_only: true, status: result.status, drive_file_id: result.fileId, reused: result.reused };
  });
}

async function dryRun(workflowId, directory) {
  const fixture = await verifyFixture(directory);
  const prior = await readFile(resolve(directory, "../drive-upload-preflight-20261123", textReceipt.name));
  const tmp = await mkdtemp(join(tmpdir(), "pgm-jpeg-preflight-"));
  const expectedOwner = "offline_owner_id";
  const folderName = "PGM Local Drive Acceptance - 2026-11-23";
  const scenarios = ["success", "lost-response", "lost-response-no-match", "shared-folder"];
  try {
    for (const scenario of scenarios) {
      const config = { mode: "dry-run", workflowId, scenario, folderName, base64: fixture.bytes.toString("base64"),
        prior: { id: textReceipt.id, name: textReceipt.name, base64: prior.toString("base64") } };
      await withProjectDriveSession(config, async (transport) => {
        assert.equal(await verifyPriorOwner(transport.driveGet, directory), expectedOwner);
        const ledgerPath = join(tmp, scenario + ".json");
        const input = { ...transport, ...fixture, ledgerPath, folderName, expectedOwner,
          folderId: contract.folderId, approval: { syntheticTestOnly: true, uploadKey: contract.assetId + ":" + contract.digest,
            folderId: contract.folderId, ownerPermissionId: expectedOwner } };
        const deliver = () => deliverWithReconciliation(input, async () =>
          (await transport.counts()).posted ? [{ action: "jpeg_upload" }] : []);
        if (scenario === "shared-folder") {
          await assert.rejects(deliver, /not owner-only/);
          await assert.rejects(readFile(ledgerPath), { code: "ENOENT" });
          assert.deepEqual(await transport.counts(), { posted: 0, patched: 0 });
          return;
        }
        if (scenario === "lost-response-no-match") {
          await assert.rejects(deliver, /unresolved/);
          assert.equal(JSON.parse(await readFile(ledgerPath)).status, "upload_intent");
          assert.deepEqual(await transport.counts(), { posted: 1, patched: 1 });
          return;
        }
        const first = await deliver();
        assert.equal(first.recoveredAfterError, scenario === "lost-response");
        assert.equal(first.result.reused, scenario === "lost-response");
        assert.equal((await deliver()).result.reused, true);
        const receipt = JSON.parse(await readFile(ledgerPath));
        assert.equal(receipt.sha256, contract.digest);
        assert.equal(receipt.fileId, "offline_jpeg_id");
        assert.equal(receipt.status, "verified");
        assert.deepEqual(await transport.counts(), { posted: 1, patched: 1 });
      });
      console.log("Offline " + scenario + " passed");
    }
  } finally {
    for (const scenario of scenarios) {
      try { await unlink(join(tmp, scenario + ".json")); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    await rmdir(tmp);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const [workflowId, directory] = process.argv.slice(2);
  assert.match(workflowId ?? "", /^[a-f0-9]{16}$/);
  assert.ok(directory && process.argv.length === 4, "Pass the workflow ID and fixture directory for an offline dry-run");
  await dryRun(workflowId, directory);
}

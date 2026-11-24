import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { deliverPrivateTestFile, makeUnpublishedDriveTestCopy } from "./private-drive-delivery.mjs";
import adapter from "./private-drive-n8n-adapter.cjs";

const owner = "owner_permission_id";
const root = "private_root_id";
const folder = "private_folder_id";
const file = "private_file_id";
const bytes = Buffer.from([0xff, 0xd8, 0x42, 0xff, 0xd9]);
const digest = createHash("sha256").update(bytes).digest("hex");
const asset = { mime: "image/jpeg", assetId: "synthetic_test", name: "fixture.jpg", bytes: bytes.length, sha256: digest };
const folderName = "Private acceptance";
const approval = { syntheticTestOnly: true, uploadKey: asset.assetId + ":" + digest, folderId: folder, ownerPermissionId: owner };

async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), "private-drive-test-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const ledgerPath = join(dir, "ledger.json");
  const state = { listed: [], writes: 0, downloads: 0, collaborator: false, wrongHash: false, ambiguous: false, nextPage: false };
  const folderMime = "application/vnd.google-apps.folder";
  const metadata = {
    root: { id: root, mimeType: folderMime },
    [folder]: { id: folder, mimeType: folderMime, name: folderName, parents: [root] },
    [file]: { id: file, mimeType: asset.mime, name: asset.name, parents: [folder], size: String(bytes.length) },
  };
  const input = {
    ledgerPath, folderId: folder, folderName, expectedOwner: owner, asset, bytes, approval,
    async driveGet(url, params, options) {
      assert.equal(url.origin, "https://www.googleapis.com");
      const resource = url.pathname.replace("/drive/v3/", "");
      if (resource === "about") return { user: { me: true, permissionId: owner } };
      if (resource === "files") {
        assert.match(params.q, /'private_folder_id' in parents .* name = 'fixture.jpg'/);
        if (state.nextPage && !params.pageToken) return { files: [], nextPageToken: "second" };
        return { files: state.listed.map((id) => ({ id })) };
      }
      if (resource.endsWith("/permissions")) {
        const permissions = [{ id: owner, role: "owner", type: "user" }];
        if (state.collaborator && resource === "files/" + folder + "/permissions") {
          if (params.pageToken) return { permissions: [{ id: "someone_else", role: "writer", type: "user" }] };
          return { permissions, nextPageToken: "extra" };
        }
        return { permissions };
      }
      if (params.alt === "media") {
        state.downloads++;
        assert.equal(options.maxBytes, bytes.length);
        return state.wrongHash ? Buffer.from([0xff, 0xd8, 0x41, 0xff, 0xd9]) : bytes;
      }
      return { ...metadata[resource.slice("files/".length)], ownedByMe: true, trashed: false, owners: [{ permissionId: owner }] };
    },
    async uploadOriginal(item) {
      state.writes++;
      assert.equal(item.json.verifiedDriveFolderId, folder);
      assert.deepEqual(Buffer.from(item.binary.data.data, "base64"), bytes);
      const intent = JSON.parse(await readFile(ledgerPath, "utf8"));
      assert.equal(intent.status, "upload_intent");
      state.listed = [file];
      if (state.ambiguous) throw new Error("lost response after Drive accepted the file");
      return { id: file };
    },
  };
  return { state, input, ledgerPath };
}

test("isolated export fixes v3 fields, disables triggers and isolates Drive nodes", async () => {
  const source = JSON.parse(await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url)));
  const original = structuredClone(source);
  const copy = makeUnpublishedDriveTestCopy(source);
  const [create, upload] = copy.nodes.filter(({ type }) => type === "n8n-nodes-base.googleDrive");
  assert.deepEqual(source, original);
  assert.equal(copy.active, false);
  assert.ok(copy.nodes.filter(({ type }) => type === "n8n-nodes-base.webhook").every(({ disabled }) => disabled));
  assert.equal(create.parameters.folderId.value, "root");
  assert.equal(upload.parameters.inputDataFieldName, "data");
  assert.match(upload.parameters.folderId.value, /verifiedDriveFolderId/);
  for (const node of [create, upload]) {
    assert.equal(node.parameters.driveId.value, "My Drive");
    assert.equal(node.disabled, true);
    assert.equal(node.retryOnFail, false);
    assert.equal(node.credentials.googleDriveOAuth2Api.id, "GOOGLE_DRIVE_CREDENTIAL_ID");
    const links = Object.values(copy.connections).flatMap(({ main }) => main.flat());
    assert.ok(!links.some(({ node: target }) => target === node.name) && !copy.connections[node.name]);
  }
});

test("fresh upload sends one binary and recheck uses the receipt without another write", async (t) => {
  const { state, input } = await fixture(t);
  const first = await deliverPrivateTestFile(input);
  assert.equal(first.reused, false);
  assert.equal(first.status, "verified");
  const repeat = await deliverPrivateTestFile(input);
  assert.equal(repeat.reused, true);
  assert.equal(repeat.fileId, first.fileId);
  assert.equal(state.writes, 1);
  assert.equal(state.downloads, 2);
});

test("lost upload response recovers an exact match from a later Drive result page", async (t) => {
  const { state, input, ledgerPath } = await fixture(t);
  state.ambiguous = true;
  await assert.rejects(deliverPrivateTestFile(input), /lost response/);
  assert.equal(JSON.parse(await readFile(ledgerPath)).status, "upload_intent");
  state.nextPage = true;
  const recovered = await deliverPrivateTestFile(input);
  assert.equal(recovered.reused, true);
  assert.equal(recovered.fileId, file);
  assert.equal(state.writes, 1);
});

test("an ambiguous write with no visible candidate blocks a second upload", async (t) => {
  const { state, input } = await fixture(t);
  state.ambiguous = true;
  await assert.rejects(deliverPrivateTestFile(input), /lost response/);
  state.listed = [];
  await assert.rejects(deliverPrivateTestFile(input), /unresolved/);
  assert.equal(state.writes, 1);
});

test("refuses sharing, duplicate candidates, and corrupted downloads before any upload", async (t) => {
  const { state, input } = await fixture(t);
  state.collaborator = true;
  await assert.rejects(deliverPrivateTestFile(input), /not owner-only/);
  state.collaborator = false;
  state.listed = [file, "second_file_id"];
  await assert.rejects(deliverPrivateTestFile(input), /Multiple Drive matches/);
  state.listed = [file];
  state.wrongHash = true;
  await assert.rejects(deliverPrivateTestFile(input), /download checksum/);
  assert.equal(state.writes, 0);
});

test("bad authorization, tampered bytes and an existing lock never write", async (t) => {
  const { state, input, ledgerPath } = await fixture(t);
  await assert.rejects(deliverPrivateTestFile({ ...input, approval: null }), /Explicit approval/);
  await assert.rejects(deliverPrivateTestFile({ ...input, bytes: Buffer.alloc(5) }), /manifest/);
  await writeFile(ledgerPath + ".lock", "");
  await assert.rejects(deliverPrivateTestFile(input), { code: "EEXIST" });
  assert.equal(state.writes, 0);
});

const fakeGrant = {
  clientId: "fixture-client",
  clientSecret: "fixture-secret",
  oauthTokenData: { access_token: "fixture-old-access", refresh_token: "fixture-refresh" },
};
const about = () => ({ url: new URL("https://www.googleapis.com/drive/v3/about"), method: "GET" });

function oauthFixture(steps) {
  const requests = [];
  const transport = adapter.makeAuthorizedRequest(fakeGrant, async (url, method, headers, body, _max, _binary, onStatus) => {
    requests.push({ origin: url.origin, method, headers, body });
    const response = steps.shift();
    assert.ok(response, "Unexpected HTTP retry");
    onStatus(response.status);
    if (response.status >= 400) throw Object.assign(new Error("Fixture HTTP error"), { httpStatus: response.status });
    return response.body;
  });
  return { ...transport, requests };
}

test("OAuth recovers one initial identity 401 in memory and records sanitized statuses", async () => {
  const client = oauthFixture([
    { status: 401 }, { status: 200, body: { access_token: "fixture-new-access" } },
    { status: 200, body: { user: { me: true } } },
  ]);
  assert.deepEqual(await client.request(about()), { user: { me: true } });
  assert.equal(client.requests[1].origin, "https://oauth2.googleapis.com");
  assert.equal(new URLSearchParams(client.requests[1].body).get("grant_type"), "refresh_token");
  assert.equal(client.requests[1].headers.Authorization, undefined);
  assert.equal(client.requests[2].headers.Authorization, "Bearer fixture-new-access");
  assert.deepEqual(client.httpStatuses().map(({ action, status }) => [action, status]),
    [["account_identity", 401], ["oauth_refresh", 200], ["account_identity", 200]]);
  assert.doesNotMatch(JSON.stringify(client.httpStatuses()), /fixture-/);
});

test("OAuth refresh failure and a second identity 401 both stop", async () => {
  const rejected = oauthFixture([{ status: 401 }, { status: 400 }]);
  await assert.rejects(rejected.request(about()), { httpStatus: 400 });
  assert.equal(rejected.requests.length, 2);
  const expired = oauthFixture([{ status: 401 }, { status: 200, body: { access_token: "fixture-new-access" } }, { status: 401 }]);
  await assert.rejects(expired.request(about()), { httpStatus: 401 });
  assert.equal(expired.requests.length, 3);
});

test("OAuth never refreshes or replays a write after a 401", async () => {
  const client = oauthFixture([{ status: 401 }, { status: 401 }]);
  await assert.rejects(client.request({
    url: new URL("https://www.googleapis.com/upload/drive/v3/files"), method: "POST",
  }), { httpStatus: 401 });
  await assert.rejects(client.request(about()), { httpStatus: 401 });
  assert.equal(client.requests.length, 2);
  assert.ok(client.requests.every(({ origin }) => origin === "https://www.googleapis.com"));
});

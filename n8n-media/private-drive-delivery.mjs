import { createHash, randomUUID } from "node:crypto";
import { open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const folderMime = "application/vnd.google-apps.folder";
const fileFields = "id,name,mimeType,parents,driveId,ownedByMe,owners(permissionId),shared,trashed,size,sha256Checksum";
const driveId = /^[A-Za-z0-9_-]{8,200}$/;
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

export function makeUnpublishedDriveTestCopy(canonical) {
  const copy = structuredClone(canonical);
  copy.name = "TEST | Phase 7 Canonical Drive Integration";
  copy.active = false;
  const folder = copy.nodes.find((node) => node.name === "Create Business Drive Folder");
  const upload = copy.nodes.find((node) => node.name === "Upload Original to Drive");
  const selector = (value) => ({ __rl: true, mode: "id", value });
  folder.parameters = {
    authentication: "oAuth2", resource: "folder", operation: "create",
    name: folder.parameters.name, driveId: selector("My Drive"), folderId: selector("root"),
    options: { simplifyOutput: false },
  };
  upload.parameters = {
    authentication: "oAuth2", resource: "file", operation: "upload",
    name: "={{ $binary.data.fileName }}", driveId: selector("My Drive"),
    folderId: selector("={{ $json.verifiedDriveFolderId }}"),
    inputDataFieldName: "data", options: { simplifyOutput: false },
  };
  const isolated = new Set([folder.name, upload.name]);
  for (const node of copy.nodes) {
    if (node.type === "n8n-nodes-base.webhook" || isolated.has(node.name)) {
      node.disabled = true;
      node.retryOnFail = false;
      delete node.onError;
    }
  }
  // Drive writes in this copy must go through the guarded single-file runner.
  for (const [source, edges] of Object.entries(copy.connections)) {
    if (isolated.has(source)) delete copy.connections[source];
    else edges.main = edges.main.map((branch) => branch.filter((edge) => !isolated.has(edge.node)));
  }
  return copy;
}

function check(condition, reason) {
  if (!condition) throw new Error(reason);
}

async function readPages(read, resource, params, resultKey) {
  const items = [];
  let pageToken;
  for (let page = 0; page < 25; page++) {
    const result = await read(resource, { ...params, pageSize: 100, pageToken });
    check(result && result.incompleteSearch !== true && (
      result[resultKey] === undefined || Array.isArray(result[resultKey])
    ), "Drive returned an incomplete or invalid listing");
    items.push(...(result[resultKey] ?? []));
    if (!result.nextPageToken) return items;
    pageToken = result.nextPageToken;
  }
  throw new Error("Drive pagination limit reached");
}

async function verifyOwnerOnly(read, file, owner, expectedParent) {
  check(driveId.test(file?.id ?? "") && file.ownedByMe === true && !file.driveId &&
    file.shared !== true && file.trashed === false && file.owners?.length === 1 &&
    file.owners[0].permissionId === owner &&
    (expectedParent === undefined || (file.parents?.length === 1 && file.parents[0] === expectedParent)),
  "Drive item ownership or parent could not be verified");
  const permissions = await readPages(read, "/files/" + file.id + "/permissions", {
    fields: "nextPageToken,permissions(id,type,role,deleted)",
  }, "permissions");
  const [permission] = permissions;
  check(permissions.length === 1 && permission.type === "user" && permission.role === "owner" &&
    permission.id === owner && !permission.deleted, "Drive item is not owner-only");
}

async function saveReceipt(filename, receipt) {
  const temp = filename + "." + randomUUID() + ".tmp";
  const handle = await open(temp, "wx", 0o600);
  try {
    try {
      await handle.writeFile(JSON.stringify(receipt, null, 2) + "\n");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temp, filename);
  } catch (error) {
    await unlink(temp).catch(() => {});
    throw error;
  }
}

// The n8n adapter must use the same OAuth credential for all GETs and the single
// Upload Original to Drive invocation. GETs must reject non-2xx, enforce the
// supplied byte limit, and never follow redirects with a credential attached.
export async function deliverPrivateTestFile({ ledgerPath, expectedOwner, folderId, folderName,
  asset, bytes, approval, driveGet, uploadOriginal }) {
  check(driveId.test(expectedOwner ?? "") && driveId.test(folderId ?? "") && typeof folderName === "string" &&
    folderName.trim().length > 0, "Approved Drive account and folder are required");
  check(asset?.mime === "image/jpeg" && typeof asset.assetId === "string" && asset.assetId.length > 0 &&
    typeof asset.name === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]*\.jpe?g$/i.test(asset.name) &&
    Buffer.isBuffer(bytes) && bytes.length >= 4 && bytes.length <= 100_000 &&
    bytes[0] === 0xff && bytes[1] === 0xd8 && bytes.at(-2) === 0xff && bytes.at(-1) === 0xd9 &&
    bytes.length === asset.bytes && sha256(bytes) === asset.sha256,
  "Synthetic JPEG does not match its manifest");
  const uploadKey = asset.assetId + ":" + asset.sha256;
  check(approval?.syntheticTestOnly === true && approval.uploadKey === uploadKey &&
    approval.folderId === folderId && approval.ownerPermissionId === expectedOwner,
  "Explicit approval for this test asset and destination is required");

  const lock = ledgerPath + ".lock";
  const handle = await open(lock, "wx", 0o600);
  try {
    let previous;
    try {
      previous = JSON.parse(await readFile(ledgerPath, "utf8"));
      check(previous.uploadKey === uploadKey && previous.owner === expectedOwner && previous.folderId === folderId &&
        previous.filename === asset.name && previous.sha256 === asset.sha256 &&
        ["upload_intent", "received", "verified"].includes(previous.status) &&
        (previous.status === "upload_intent" || driveId.test(previous.fileId ?? "")),
      "Ledger receipt does not match the authorized upload");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }

    const read = (resource, params, options) =>
      driveGet(new URL("https://www.googleapis.com/drive/v3" + resource), params, options);
    const getFile = (id) => read("/files/" + id, { fields: fileFields });
    const { user } = await read("/about", { fields: "user(me,permissionId)" });
    check(user?.me === true && user.permissionId === expectedOwner, "Drive account identity changed");
    const root = await getFile("root");
    check(root?.mimeType === folderMime, "My Drive root could not be verified");
    await verifyOwnerOnly(read, root, expectedOwner);
    const folder = await getFile(folderId);
    check(folder?.id === folderId && folder?.name === folderName && folder.mimeType === folderMime,
      "Drive folder does not match the authorized destination");
    await verifyOwnerOnly(read, folder, expectedOwner, root.id);

    const candidates = await readPages(read, "/files", {
      q: "'" + folderId + "' in parents and trashed = false and name = '" + asset.name + "'",
      corpora: "user", spaces: "drive", fields: "nextPageToken,incompleteSearch,files(id)",
    }, "files");
    check(candidates.length <= 1, "Multiple Drive matches require manual reconciliation");
    const candidateId = candidates[0]?.id;
    check(!candidates.length || driveId.test(candidateId ?? ""), "Drive returned an invalid candidate ID");
    check(!previous?.fileId || !candidateId || candidateId === previous.fileId,
      "Drive listing conflicts with the recorded file ID");
    const receipt = { uploadKey, owner: expectedOwner, folderId, filename: asset.name, sha256: asset.sha256 };
    let fileId = previous?.fileId || candidateId;
    const reused = Boolean(fileId);
    if (!fileId) {
      check(!previous, "Prior write outcome is unresolved; no second upload is allowed");
      await saveReceipt(ledgerPath, { ...receipt, status: "upload_intent" });
      const result = await uploadOriginal({
        json: { verifiedDriveFolderId: folderId },
        binary: { data: { data: bytes.toString("base64"), mimeType: asset.mime, fileName: asset.name } },
      });
      check(driveId.test(result?.id ?? ""), "Upload returned no verifiable file ID");
      fileId = result.id;
      await saveReceipt(ledgerPath, { ...receipt, fileId, status: "received" });
    }

    const file = await getFile(fileId);
    check(file?.id === fileId && file?.name === asset.name && file.mimeType === asset.mime &&
      Number(file.size) === bytes.length && (!file.sha256Checksum || file.sha256Checksum === asset.sha256),
    "Drive file metadata does not match the authorized asset");
    await verifyOwnerOnly(read, file, expectedOwner, folderId);
    const downloaded = await read("/files/" + fileId, { alt: "media" }, { maxBytes: bytes.length });
    check(Buffer.isBuffer(downloaded) && downloaded.length === bytes.length && sha256(downloaded) === asset.sha256,
      "Drive download checksum does not match the authorized asset");
    const verified = { ...receipt, fileId, status: "verified" };
    await saveReceipt(ledgerPath, verified);
    return { ...verified, reused };
  } finally {
    await handle.close();
    await unlink(lock);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const destination = process.argv[2];
  check(destination, "Provide the isolated workflow export path");
  const canonical = JSON.parse(await readFile(new URL("./07-pipeline-orchestration.json", import.meta.url), "utf8"));
  await writeFile(destination, JSON.stringify(makeUnpublishedDriveTestCopy(canonical), null, 2) + "\n", { flag: "wx" });
}

const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const { createHash } = require("node:crypto");

const credentialName = "Google Drive \u2013 Local Test";
const workflowName = "TEST | Phase 7 Canonical Drive Integration";
const guardedWorkflowId = "a10e2e0f3194c2e1";
const guardedWorkflowName = "TEST | Phase 10 Guarded Live E2E Draft";
const folderId = "1hsSZM63PQBO5jmi_-hM80lPGpr_LD7BF";
const fileName = "synthetic-pup-bath.jpg";
const digest = "e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162";
const byteCount = 10013;
const assetId = "local_synthetic_media_e5c5dd3e2d67";
const idPattern = /^[A-Za-z0-9_-]{8,200}$/;
const n8n = "/usr/local/lib/node_modules/n8n";

function check(ok, message) {
  if (!ok) throw new Error(message);
}

function validateBinding(workflow, reference, definitions, expectedName) {
  check(workflow.name === expectedName && !workflow.active && workflow.activeVersionId === null,
    "Expected an unpublished test workflow");
  check(reference?.name === credentialName && reference.type === "googleDriveOAuth2Api",
    "Expected the local test OAuth credential reference");
  const nodes = workflow.nodes.filter(({ type }) => type === "n8n-nodes-base.googleDrive");
  const incoming = Object.values(workflow.connections).flatMap(({ main }) => main.flat());
  check(nodes.length === 2 && nodes.every((node) => node.typeVersion === 3 && node.disabled === true &&
    node.retryOnFail === false && node.parameters.authentication === "oAuth2" &&
    node.credentials?.googleDriveOAuth2Api?.id === reference.id &&
    node.credentials.googleDriveOAuth2Api.name === credentialName &&
    !workflow.connections[node.name] && !incoming.some((edge) => edge.node === node.name)),
  "Drive nodes are not both isolated and bound to the test credential");
  check(workflow.nodes.filter(({ type }) => type === "n8n-nodes-base.webhook").every(({ disabled }) => disabled === true),
    "Test workflow has an enabled webhook");
  const folder = nodes.find((node) => node.name === "Create Business Drive Folder");
  const upload = nodes.find((node) => node.name === "Upload Original to Drive");
  check(folder?.parameters.resource === "folder" && folder.parameters.operation === "create" &&
    folder.parameters.folderId.value === definitions.root && upload?.parameters.resource === "file" &&
    upload.parameters.operation === "upload" && upload.parameters.inputDataFieldName === "data" &&
    upload.parameters.name === "={{ $binary.data.fileName }}" &&
    upload.parameters.folderId.value === "={{ $json.verifiedDriveFolderId }}" &&
    nodes.every((node) => node.parameters.driveId.value === definitions.myDrive), "Unexpected Drive v3 parameter values");
  for (const [node, fields] of [[folder, definitions.createFields], [upload, definitions.uploadFields]]) {
    const operationKeys = Object.keys(node.parameters).filter((key) => !["resource", "operation", "authentication"].includes(key));
    check(operationKeys.every((key) => fields.includes(key)), "An installed Drive node parameter is missing");
  }
  return upload;
}

function loadProjectBinding(workflowId) {
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync("/home/node/.n8n/database.sqlite", { readOnly: true });
  try {
    const row = db.prepare("select name,active,activeVersionId,nodes,connections from workflow_entity where id=?").get(workflowId);
    check(row, "Test workflow not found in project n8n");
    const refs = db.prepare("select id,name,type,length(data)>0 as stored from credentials_entity where name=? and type=?")
      .all(credentialName, "googleDriveOAuth2Api");
    check(refs.length === 1 && refs[0].stored, "Saved test credential unavailable");
    const packages = path.join(n8n, "node_modules/.pnpm");
    const dist = path.join(packages, fs.readdirSync(packages).find((name) => name.startsWith("n8n-nodes-base@")),
      "node_modules/n8n-nodes-base/dist/nodes/Google/Drive/v2");
    const folderAction = require(dist + "/actions/folder/create.operation.js");
    const uploadAction = require(dist + "/actions/file/upload.operation.js");
    const defaults = require(dist + "/helpers/interfaces.js");
    const workflow = { ...row, nodes: JSON.parse(row.nodes), connections: JSON.parse(row.connections) };
    const definitions = { root: defaults.RLC_FOLDER_DEFAULT, myDrive: defaults.RLC_DRIVE_DEFAULT,
      createFields: folderAction.description.map(({ name }) => name), uploadFields: uploadAction.description.map(({ name }) => name) };
    const uploadNode = validateBinding(workflow, refs[0], definitions,
      workflowId === guardedWorkflowId ? guardedWorkflowName : workflowName);
    return { uploadNode, executeUpload: uploadAction.execute, credentialId: refs[0].id };
  } finally {
    db.close();
  }
}

function makeAdapter({ uploadNode, executeUpload, request }) {
  let posted = false;
  let patched = false;
  let uploadedId;
  let started = false;
  const driveGet = (url, query, options) => {
    check(url.origin === "https://www.googleapis.com" && !url.search && !url.hash &&
      /^\/drive\/v3\/(about|files(?:\/[A-Za-z0-9_-]+(?:\/permissions)?)?)$/.test(url.pathname),
    "Unsupported Drive read");
    return request({ url, method: "GET", query, maxBytes: options?.maxBytes });
  };

  async function uploadOriginal(item) {
    check(!started, "This session has already attempted an upload");
    const binary = item.binary?.data;
    check(binary && !binary.id && item.json?.verifiedDriveFolderId === folderId &&
      binary.mimeType === "image/jpeg" && binary.fileName === fileName && typeof binary.data === "string" &&
      binary.data.length === Math.ceil(byteCount / 3) * 4, "Unexpected JPEG binary or destination");
    const bytes = Buffer.from(binary.data, "base64");
    check(bytes.length === byteCount && createHash("sha256").update(bytes).digest("hex") === digest, "JPEG digest mismatch");
    started = true;

    const context = {
      getNode: () => uploadNode,
      getNodeParameter(key, _index, fallback, options) {
        let value = key.split(".").reduce((params, field) => params?.[field], uploadNode.parameters) ?? fallback;
        if (key === "folderId") value = { value: folderId };
        if (key === "name") value = fileName;
        return options?.extractValue ? value?.value : value;
      },
      helpers: {
        assertBinaryData(index, key) {
          check(index === 0 && key === "data", "Unexpected binary input index or field");
          return binary;
        },
        returnJsonArray: (data) => [{ json: data }],
        constructExecutionMetaData: (data) => data,
        async httpRequestWithAuthentication(type, options) {
          const url = new URL(options.url);
          check(type === "googleDriveOAuth2Api" && url.origin === "https://www.googleapis.com" && !url.search && !url.hash,
            "Unexpected upload endpoint or credentials");
          if (options.method === "POST") {
            check(!posted && url.pathname === "/upload/drive/v3/files" && options.qs?.uploadType === "multipart" &&
              Buffer.isBuffer(options.body) && options.body.length < 32_768, "Unexpected multipart upload");
            const multipart = options.body.toString("latin1");
            const metadata = Buffer.from(JSON.stringify({ name: fileName, parents: [folderId] }));
            check(multipart.includes(metadata.toString("latin1")) && options.body.includes(bytes), "Multipart payload mismatch");
            posted = true;
            const response = await request({ url, method: "POST", query: options.qs, headers: options.headers, body: options.body });
            check(idPattern.test(response?.id ?? ""), "Upload response lacks a valid file ID");
            uploadedId = response.id;
            return response;
          }
          check(options.method === "PATCH" && uploadedId && !patched &&
            url.pathname === "/drive/v3/files/" + uploadedId && options.qs?.addParents === folderId &&
            options.body?.name === fileName && options.body.mimeType === "image/jpeg" &&
            Object.keys(options.body).every((key) => ["name", "mimeType", "originalFilename"].includes(key)),
          "Unexpected upload metadata update");
          patched = true;
          return request({ url, method: "PATCH", query: options.qs, headers: { "Content-Type": "application/json" },
            body: JSON.stringify(options.body) });
        },
      },
    };
    const [result, ...extra] = await executeUpload.call(context, 0);
    check(!extra.length && posted && patched && result?.json?.id === uploadedId, "Native upload result did not match the receipt");
    return result.json;
  }
  return { driveGet, uploadOriginal };
}

function jsonRequest(url, method, headers, body, maxBytes = 1_048_576, binary = false, onStatus = () => {}) {
  return new Promise((resolve, reject) => {
    const request = https.request(url, { method, headers, agent: false }, (response) => {
      const status = response.statusCode;
      onStatus(status);
      if (status < 200 || status >= 300) {
        response.destroy();
        reject(Object.assign(new Error("Google HTTP " + status), { httpStatus: status }));
        return;
      }
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > maxBytes) response.destroy(new Error("Google response exceeded the configured limit"));
        else chunks.push(chunk);
      });
      response.on("error", reject);
      response.on("end", () => {
        const content = Buffer.concat(chunks);
        try { resolve(binary ? content : JSON.parse(content.toString("utf8"))); }
        catch { reject(new Error("Google response was not valid JSON")); }
      });
    });
    request.setTimeout(12_000, () => request.destroy(new Error("Google request timed out")));
    request.on("error", reject);
    request.end(body);
  });
}

function makeAuthorizedRequest(oauth, requestJson = jsonRequest) {
  let accessToken = oauth.oauthTokenData?.access_token;
  let refreshAttempted = false;
  let writeAttempted = false;
  const httpStatuses = [];
  check(typeof accessToken === "string" && accessToken.length > 0, "Saved OAuth access grant unavailable");
  const request = async ({ url, method, query, headers, body, maxBytes }) => {
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
    const action = method === "POST" ? "jpeg_upload" : method === "PATCH" ? "jpeg_metadata_update" :
      query?.alt === "media" ? "download" : url.pathname === "/drive/v3/about" ? "account_identity" :
      url.pathname.endsWith("/permissions") ? "permissions" : url.pathname === "/drive/v3/files" ? "file_search" : "file_metadata";
    const send = () => {
      const entry = { action, method, status: null };
      httpStatuses.push(entry);
      return requestJson(url, method, { ...headers, Authorization: "Bearer " + accessToken }, body,
        maxBytes, query?.alt === "media", (status) => { entry.status = status; });
    };
    if (method !== "GET") writeAttempted = true;
    try { return await send(); } catch (error) {
      if (error.httpStatus !== 401 || method !== "GET" || url.pathname !== "/drive/v3/about" || writeAttempted || refreshAttempted) throw error;
      refreshAttempted = true;
      check(oauth.oauthTokenData?.refresh_token && oauth.clientId && oauth.clientSecret, "OAuth refresh grant unavailable");
      const form = new URLSearchParams({ grant_type: "refresh_token", refresh_token: oauth.oauthTokenData.refresh_token,
        client_id: oauth.clientId, client_secret: oauth.clientSecret }).toString();
      const entry = { action: "oauth_refresh", method: "POST", status: null };
      httpStatuses.push(entry);
      const refreshed = await requestJson(new URL("https://oauth2.googleapis.com/token"), "POST",
        { "Content-Type": "application/x-www-form-urlencoded", "Content-Length": Buffer.byteLength(form) },
        form, undefined, false, (status) => { entry.status = status; });
      check(typeof refreshed.access_token === "string" && refreshed.access_token.length > 0, "OAuth refresh did not return an access grant");
      accessToken = refreshed.access_token;
      return send();
    }
  };
  return { request, httpStatuses: () => httpStatuses };
}

function createLiveAdapter(workflowId, approval) {
  check(approval?.jpegUploadApproved === true && approval.workflowId === workflowId && approval.sha256 === digest &&
    approval.folderId === folderId && approval.filename === fileName, "Explicit live approval for this JPEG and destination is required");
  if (workflowId === guardedWorkflowId) {
    check(approval.scope === "phase10-guarded-e2e" && /^[a-f0-9]{32}$/.test(approval.jobId ?? ""),
      "Live E2E Drive approval must name the scoped job");
  }
  const binding = loadProjectBinding(workflowId);
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync("/home/node/.n8n/database.sqlite", { readOnly: true });
  let encrypted;
  try { encrypted = db.prepare("select data from credentials_entity where id=?").get(binding.credentialId)?.data; }
  finally { db.close(); }
  check(encrypted && Buffer.from(encrypted, "base64").subarray(0, 8).toString() === "Salted__", "Unexpected saved credential format");
  const config = JSON.parse(fs.readFileSync("/home/node/.n8n/config", "utf8"));
  const { CipherAes256CBC } = require(n8n + "/node_modules/n8n-core/dist/encryption/aes-256-cbc.js");
  const oauth = JSON.parse(new CipherAes256CBC().decrypt(encrypted, process.env.N8N_ENCRYPTION_KEY || config.encryptionKey));
  const { request, httpStatuses } = makeAuthorizedRequest(oauth);
  return { ...makeAdapter({ ...binding, request }), httpStatuses };
}

module.exports = { loadProjectBinding, makeAdapter, createLiveAdapter, makeAuthorizedRequest,
  contract: Object.freeze({ folderId, fileName, digest, byteCount, assetId, workflowName, guardedWorkflowId }) };

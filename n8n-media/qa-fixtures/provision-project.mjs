import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fixtureKeys, intakePath, makeProjectExecution, projectExecutionId } from "./project-execution.mjs";

const directory = "storage/phase10-local-execution";
const settings = join(directory, "fixture.env");
const keys = [...Object.values(fixtureKeys).map(({ env }) => env), "PGM_QA_DASHBOARD_TOKEN",
  "PGM_QA_DRIVE_TOKEN", "PGM_QA_FRONTEND_PASSWORD", "PGM_QA_SESSION_SECRET"];
await mkdir(directory, { recursive: true });
let env;
try {
  env = Object.fromEntries((await readFile(settings, "utf8")).trim().split(/\r?\n/).map((row) => row.split("=")));
} catch (error) {
  if (error.code !== "ENOENT") throw error;
  env = Object.fromEntries(keys.map((name) => [name, randomBytes(32).toString("hex")]));
  await writeFile(settings, Object.entries(env).map(([name, value]) => name + "=" + value).join("\n") + "\n",
    { flag: "wx", mode: 0o600 });
}
assert.ok(keys.every((name) => /^[a-f0-9]{64}$/.test(env[name] ?? "")));
const credentials = Object.values(fixtureKeys).map(({ id, name, header, env: key }) => ({
  id, name, type: "httpHeaderAuth", data: { name: header, value: env[key] },
}));
const workflow = await makeProjectExecution();
const importLocally = String.raw`
const assert=require("node:assert/strict"),fs=require("node:fs"),{spawnSync}=require("node:child_process");
const {DatabaseSync}=require("node:sqlite"),{createHash}=require("node:crypto");
const {workflow,credentials}=JSON.parse(fs.readFileSync(0,"utf8"));
const open=()=>new DatabaseSync("/home/node/.n8n/database.sqlite",{readOnly:true});
function snapshot(db){
 const rows=db.prepare("select id,name,active,activeVersionId,nodes,connections from workflow_entity where id<>? order by id").all(workflow.id);
 const existing=db.prepare("select id,name,type,updatedAt from credentials_entity order by id").all().filter(r=>!credentials.some(c=>c.id===r.id));
 return createHash("sha256").update(JSON.stringify([rows,existing])).digest("hex");
}
let stage="validate";
try{
 let db=open();
 const before=snapshot(db);
 const existing=db.prepare("select name,active,activeVersionId from workflow_entity where id=?").get(workflow.id);
 const refs=credentials.map(c=>db.prepare("select name,type from credentials_entity where id=?").get(c.id));
 assert.ok(refs.every((r,i)=>!r||(r.name===credentials[i].name&&r.type==="httpHeaderAuth")));
 if(existing)assert.ok(existing.name===workflow.name&&!existing.active&&existing.activeVersionId===null);
 db.close();
 const data=[["credentials",credentials.filter((_,i)=>!refs[i])],["workflow",existing?[]:[workflow]]];
 for(const [kind,items] of data){
   if(!items.length)continue;
   stage="import-"+kind;
   const dir=fs.mkdtempSync("/tmp/pgm-qa-exec-"), input=dir+"/fixture.json";
   try {
     fs.writeFileSync(input,JSON.stringify(items),{mode:0o600});
     const result=spawnSync("n8n",["import:"+kind,"--input="+input],{encoding:"utf8",timeout:90000});
     assert.equal(result.status,0);
   } finally { if(fs.existsSync(input))fs.unlinkSync(input); fs.rmdirSync(dir); }
 }
 stage="verify";
 db=open();
 const row=db.prepare("select name,active,activeVersionId,nodes,connections from workflow_entity where id=?").get(workflow.id);
 assert.ok(row&&row.name===workflow.name&&!row.active&&row.activeVersionId===null);
 const nodes=JSON.parse(row.nodes);
 assert.ok(!nodes.some(n=>n.type==="n8n-nodes-base.googleDrive"));
 for(const node of nodes){
  const expected=workflow.nodes.find(n=>n.name===node.name);
  assert.equal(node.credentials?.httpHeaderAuth?.id,expected.credentials?.httpHeaderAuth?.id);
  assert.deepEqual(node.parameters,expected.parameters);
 }
 assert.deepEqual(JSON.parse(row.connections),workflow.connections);
 assert.equal(snapshot(db),before);
 db.close();
 console.log(JSON.stringify({ok:true,id:workflow.id,unpublished:true,realDriveNodes:0,existingRecordsUnchanged:true}));
}catch{console.log(JSON.stringify({ok:false,stage}));process.exitCode=1;}
`;
const result = spawnSync("docker", ["exec", "-i", "pgm-media-n8n-1", "node", "-e", importLocally], {
  input: JSON.stringify({ workflow, credentials }), encoding: "utf8", timeout: 210000,
});
const state = JSON.parse(result.stdout.trim());
console.log(JSON.stringify({ ...state, intake: "http://127.0.0.1:5679/webhook-test/" + intakePath }));
assert.equal(result.status, 0);
assert.equal(state.id, projectExecutionId);

"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const fields = {}, sent = [], timers = [], noted = [];
  const state = { source: "source", classes: [{ name: "destination", shareable: false }],
    nodes: [{ name: "node1", ready: true }, { name: "node2", ready: false }], preparations: [] };
  const ctx = { console, Uint8Array, crypto: { getRandomValues: a => a.fill(1) }, STATE: { data: {} },
    $: key => fields[key], esc: v => String(v).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    modal: (_title, html) => { fields["#selfDataFlow"] = { innerHTML: html }; },
    deployCapacityHtml: c => `<p>${(c.warnings || []).map(ctx.esc).join(", ")}</p>`, noteOperation: o => noted.push(o),
    setTimeout: f => timers.push(f),
    api: async (path, options) => {
      const body = options ? JSON.parse(options.body) : undefined;
      sent.push({ path, body });
      if (!options) return structuredClone(state);
      if (path === "/api/self/data/prepare/preview") return { storage_class: "destination", size: "3Gi", capacity_token: "signed", capacity: { blocked: false, warnings: ["Storage is not reserved"] } };
      if (path === "/api/self/data/prepare") {
        state.preparations = [{ id: "job", operation: body.operation, destination: "target", status: "running", node: "node1", progress: 15, message: "Creating volume", prepared: false }];
        return { operation: { id: "job" }, destination: "target" };
      }
      if (path === "/api/self/data/move/preview") return { capacity_token: "signed-move", downtime: "Homestead will stop while copying.", stages: [{ label: "Copy and verify", detail: "Only after source stops", capacity: {} }] };
      throw new Error("Unexpected mutation " + path);
    },
  };
  ctx.window = ctx; vm.createContext(ctx);ctx.jsArg=s=>JSON.stringify(String(s??""));ctx.jsq=s=>(ctx.esc||String)(ctx.jsArg(s)); vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-settings.js", "utf8"), ctx);
  const form = () => Object.assign(fields, { "#selfDataClass": { value: "destination" }, "#selfDataHost": { value: "node1" },
    "#selfDataReview": { innerHTML: "" }, "#selfDataActions": { innerHTML: "" }, "#selfDataConsent": { checked: false }, "#selfDataPrepare": { disabled: true } });
  return { ctx, fields, sent, state, timers, noted, form };
}

test("settings separates online preparation from downtime without legacy mutation", async () => {
  const t = setup(); await t.ctx.replicasMoveData(); t.form();
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Two steps: make the new volume/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /node2 · unavailable/);
  await t.ctx.selfDataPrepareReview(); await t.ctx.selfDataPrepare();
  assert.equal(t.sent.length, 2, "unchecked preparation cannot create anything");
  t.fields["#selfDataConsent"].checked = true;
  await t.ctx.selfDataPrepare(); await t.ctx.selfDataPrepare();
  assert.equal(t.sent.filter(s => s.path === "/api/self/data/prepare" && s.body).length, 1);
  const body = t.sent.find(s => s.path === "/api/self/data/prepare" && s.body).body;
  assert.equal(body.capacity_token, "signed"); assert.equal(body.confirm_capacity, true);
  assert.match(body.operation, /^[a-f0-9]{24}$/);
  assert.equal(t.noted.length, 1); assert.equal(t.timers.length, 1);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Creating volume/);
  assert.ok(t.sent.every(s => s.path !== "/api/self/data/move"));
});

test("changing preparation input invalidates approval, including a late preview", async () => {
  const t = setup(); await t.ctx.replicasMoveData(); t.form();
  await t.ctx.selfDataPrepareReview(); t.fields["#selfDataConsent"].checked = true;
  t.fields["#selfDataClass"].value = "changed";
  assert.equal(t.ctx.selfDataReady(), false);
  let respond; t.ctx.api = () => new Promise(r => respond = r);
  const waiting = t.ctx.selfDataPrepareReview(); t.ctx.selfDataInvalidate();
  respond({ capacity_token: "late", capacity: {} }); await waiting;
  assert.equal(t.ctx.selfDataReady(), false);
  assert.equal(t.fields["#selfDataReview"].innerHTML, "");
});

test("lost submission cannot be retried through its consumed confirmation", async () => {
  const t = setup(); await t.ctx.replicasMoveData(); t.form(); await t.ctx.selfDataPrepareReview();
  t.fields["#selfDataConsent"].checked = true;
  let writes = 0; t.ctx.api = async () => { writes++; throw new Error("Connection lost"); };
  await t.ctx.selfDataPrepare(); await t.ctx.selfDataPrepare();
  assert.equal(writes, 1); assert.match(t.fields["#selfDataFlow"].innerHTML, /Check saved jobs/);
});

test("reopens completed preparation from jobs and obtains read-only final review", async () => {
  const t = setup();
  t.state.preparations = [{ id: "job", operation: "a".repeat(24), destination: "target", node: "node1", status: "succeeded", prepared: true, progress: 100 }];
  await t.ctx.replicasMoveData("job");
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Review the move/); assert.equal(t.timers.length, 0);
  Object.assign(t.fields, { "#selfDataFinal": { innerHTML: "" }, "#selfDataWorker": { value: "node1" }, "#selfDataCopy": { value: "node1" } });
  await t.ctx.selfDataFinalReview();
  assert.match(t.fields["#selfDataFinal"].innerHTML, /goes offline/);
  assert.match(t.fields["#selfDataFinal"].innerHTML, /Take Homestead offline and move its data/);
  assert.deepEqual(t.sent.at(-1).body, { operation: "a".repeat(24), destination: "target", worker_node: "node1", copy_node: "node1" });
  t.ctx.selfDataFinalInvalidate(); assert.equal(t.fields["#selfDataFinal"].innerHTML, "");
});

test("a volume prepared in this dialog can go on to the move without reopening it", async () => {
  const t = setup(); await t.ctx.replicasMoveData(); t.form(); await t.ctx.selfDataPrepareReview();
  t.fields["#selfDataConsent"].checked = true; await t.ctx.selfDataPrepare();
  t.state.preparations[0] = { ...t.state.preparations[0], status: "succeeded", prepared: true, progress: 100 };
  await t.timers[0]();
  Object.assign(t.fields, { "#selfDataFinal": { innerHTML: "" }, "#selfDataWorker": { value: "node1" }, "#selfDataCopy": { value: "node1" },
    "#selfDataMoveConsent": { checked: true }, "#selfDataMoveStart": { disabled: true } });
  await t.ctx.selfDataFinalReview();
  assert.equal(t.ctx.selfDataMoveReady(), true);
  assert.equal(t.fields["#selfDataMoveStart"].disabled, false);
});

test("closed dialogs do not resume polling or overwrite another dialog", async () => {
  const t = setup(); await t.ctx.replicasMoveData(); t.form(); await t.ctx.selfDataPrepareReview();
  t.fields["#selfDataConsent"].checked = true; await t.ctx.selfDataPrepare();
  const count = t.sent.length; t.ctx.selfDataClose(); // Closing hides, but does not remove, the DOM.
  await t.timers[0](); assert.equal(t.sent.length, count);
});

test("unsafe or incomplete preparation review never enables create", async () => {
  for (const response of [{ capacity: {} }, { capacity_token: "token", capacity: { blocked: true } }]) {
    const t = setup(); await t.ctx.replicasMoveData(); t.form();
    t.ctx.api = async () => response; await t.ctx.selfDataPrepareReview();
    t.fields["#selfDataConsent"].checked = true;
    assert.equal(t.ctx.selfDataReady(), false);
  }
});

test("final move requires consent and matching hosts, and sends its signed review once", async () => {
  const t = setup();
  t.state.preparations = [{ id: "job", operation: "a".repeat(24), destination: "target", node: "node1", status: "succeeded", prepared: true }];
  await t.ctx.replicasMoveData("job");
  Object.assign(t.fields, { "#selfDataFinal": { innerHTML: "" }, "#selfDataWorker": { value: "node1" }, "#selfDataCopy": { value: "node1" },
    "#selfDataMoveConsent": { checked: false }, "#selfDataMoveStart": { disabled: true } });
  await t.ctx.selfDataFinalReview();
  assert.equal(t.ctx.selfDataMoveReady(), false);
  t.fields["#selfDataMoveConsent"].checked = true;
  t.fields["#selfDataCopy"].value = "node2";
  assert.equal(t.ctx.selfDataMoveReady(), false);
  t.fields["#selfDataCopy"].value = "node1";
  let calls = 0, redirect;
  t.ctx.api = async (path, options) => {
    calls++; const b = JSON.parse(options.body);
    assert.equal(path, "/api/self/data/move"); assert.equal(b.capacity_token, "signed-move");
    assert.equal(b.confirm_move, true); assert.equal(b.confirm_capacity, true);
    return { operation: { id: "move" }, handoff: "a".repeat(24) };
  };
  t.ctx.location = { assign: path => redirect = path };
  await t.ctx.selfDataMoveStart(); await t.ctx.selfDataMoveStart();
  assert.equal(calls, 1); assert.equal(redirect, "/api/self/data/handoff/" + "a".repeat(24) + "/view");
});

test("lost final move response consumes consent and never blindly retries", async () => {
  const t = setup();
  t.state.preparations = [{ id: "job", operation: "a".repeat(24), destination: "target", node: "node1", status: "succeeded", prepared: true }];
  await t.ctx.replicasMoveData("job");
  Object.assign(t.fields, { "#selfDataFinal": { innerHTML: "" }, "#selfDataWorker": { value: "node1" }, "#selfDataCopy": { value: "node1" }, "#selfDataMoveConsent": { checked: true } });
  await t.ctx.selfDataFinalReview();
  let calls = 0;
  t.ctx.api = async () => { calls++; throw Error("Connection lost"); };
  await t.ctx.selfDataMoveStart(); await t.ctx.selfDataMoveStart();
  assert.equal(calls, 1); assert.match(t.fields["#selfDataFinal"].innerHTML, /may already have started/);
});


test("completed historical preparations offer archive and identify actual blocking jobs", async () => {
  const t = setup();
  t.state.preparations = [{id:"old", destination:"old-volume", status:"succeeded", prepared:false, archivable:true, message:"Prepared for an earlier source volume"}];
  t.state.blocking_jobs = [{id:"recovery",title:"Recover <backup>",status:"failed",message:"Inspect helper",href:"/protect"}];
  await t.ctx.replicasMoveData(); t.form();
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Archive record/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /View record/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Recover &lt;backup>/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Open job/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /id="selfDataCheck"[^>]*disabled/);
  const count = t.sent.length;
  await t.ctx.selfDataPrepareReview();
  assert.equal(t.sent.length, count, "a blocker prevents programmatic preparation too");
});

function archiveApi(t, submit) {
  const original = t.ctx.api;
  t.ctx.api = async (path, options) => {
    if (!path.includes("/archive")) return original(path, options);
    const body = JSON.parse(options.body); t.sent.push({path,body});
    if (path.endsWith("/preview")) return {id:body.id,source:"source",destination:"target",capacity_token:"archive-token",detail:"Both volumes are retained"};
    if (submit) return submit();
    return {ok:true};
  };
}

test("archiving requires its own consent and sends the exact signed record once", async () => {
  const t = setup(); archiveApi(t);
  t.ctx.STATE.data.operations = [{id:"job"},{id:"other"}];
  t.ctx.refreshOperations = () => {throw Error("Archive must not advance unrelated jobs");};
  await t.ctx.selfDataArchiveReview("job");
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Both volumes are retained/);
  Object.assign(t.fields, {"#selfDataArchiveConsent":{checked:false},"#selfDataArchiveGo":{disabled:true}});
  await t.ctx.selfDataArchiveStart(); assert.equal(t.sent.length, 1);
  t.fields["#selfDataArchiveConsent"].checked = true;
  assert.equal(t.ctx.selfDataArchiveReady(), true);
  await t.ctx.selfDataArchiveStart(); await t.ctx.selfDataArchiveStart();
  const writes = t.sent.filter(s => s.path === "/api/self/data/prepare/archive");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body, {id:"job",capacity_token:"archive-token",confirm_archive:true});
  assert.equal(t.ctx.STATE.data.operations.length, 1);
  assert.equal(t.ctx.STATE.data.operations[0].id, "other");
});

test("lost archive submission keeps volumes and consumes its confirmation", async () => {
  const t = setup(); archiveApi(t, () => {throw new Error("Connection lost");});
  await t.ctx.selfDataArchiveReview("job");
  t.fields["#selfDataArchiveConsent"] = {checked:true};
  await t.ctx.selfDataArchiveStart(); await t.ctx.selfDataArchiveStart();
  assert.equal(t.sent.filter(s => s.path === "/api/self/data/prepare/archive").length, 1);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Check saved jobs/);
  assert.equal(t.ctx.selfDataArchiveReady(), false);
});

test("closing or reopening move data invalidates a late archive preview", async () => {
  for (const close of [t => t.ctx.selfDataClose(), t => t.ctx.replicasMoveData()]) {
    const t = setup(); let respond; const original = t.ctx.api;
    t.ctx.api = (path, options) => path.includes("/archive") ? new Promise(r => respond=r) : original(path, options);
    const pending = t.ctx.selfDataArchiveReview("job");
    await close(t);
    respond({id:"job",destination:"target",capacity_token:"late"}); await pending;
    assert.equal(t.ctx.selfDataArchiveReady(), false);
    assert.doesNotMatch(t.fields["#selfDataFlow"]?.innerHTML || "", /selfDataArchiveConsent/);
  }
});


test("a retained k3s blocker opens its own recovery review without relying on the jobs cache", async () => {
  const t = setup(), opened = [];
  const job = {id:"old-batch",title:"k3s cluster k3s-demo",kind:"k3s-cluster",status:"failed",
    message:"Guest verification timed out",href:"/vms?find=k3s-demo",recovery:true,mutation_recovery:true};
  t.state.blocking_jobs = [job];
  t.ctx.openOperation = (...args) => opened.push(args);
  await t.ctx.replicasMoveData();
  assert.match(t.fields["#selfDataFlow"].innerHTML, /selfDataOpenJob/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Review batch outcome/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /requires recovery/);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /id="selfDataCheck"[^>]*disabled/);
  t.ctx.selfDataOpenJob("old-batch");
  assert.equal(opened.length, 1);
  assert.equal(opened[0][0], job.href);
  assert.equal(opened[0][1], job.id);
  assert.equal(opened[0][2].mutation_recovery, true);
  assert.equal(opened[0][2].kind, "k3s-cluster");
  const count = t.sent.length;
  await t.ctx.selfDataPrepareReview();
  t.ctx.selfDataOpenJob("old-batch");
  assert.equal(t.sent.length, count, "opening recovery invalidates the preparation dialog and approval");
  assert.equal(opened.length, 1);
});

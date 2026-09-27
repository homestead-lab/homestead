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
  ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-settings.js", "utf8"), ctx);
  const form = () => Object.assign(fields, { "#selfDataClass": { value: "destination" }, "#selfDataHost": { value: "node1" },
    "#selfDataReview": { innerHTML: "" }, "#selfDataActions": { innerHTML: "" }, "#selfDataConsent": { checked: false }, "#selfDataPrepare": { disabled: true } });
  return { ctx, fields, sent, state, timers, noted, form };
}

test("settings separates online preparation from downtime without legacy mutation", async () => {
  const t = setup(); await t.ctx.replicasMoveData(); t.form();
  assert.match(t.fields["#selfDataFlow"].innerHTML, /First prepare a new volume/);
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
  assert.match(t.fields["#selfDataFlow"].innerHTML, /Review downtime/); assert.equal(t.timers.length, 0);
  Object.assign(t.fields, { "#selfDataFinal": { innerHTML: "" }, "#selfDataWorker": { value: "node1" }, "#selfDataCopy": { value: "node1" } });
  await t.ctx.selfDataFinalReview();
  assert.match(t.fields["#selfDataFinal"].innerHTML, /Planned downtime/);
  assert.match(t.fields["#selfDataFinal"].innerHTML, /I accept the downtime/);
  assert.deepEqual(t.sent.at(-1).body, { operation: "a".repeat(24), destination: "target", worker_node: "node1", copy_node: "node1" });
  t.ctx.selfDataFinalInvalidate(); assert.equal(t.fields["#selfDataFinal"].innerHTML, "");
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

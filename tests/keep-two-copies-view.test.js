"use strict";
// #731 item 3: once the cluster can keep two copies of Homestead's data, the
// Redundancy card says so and Keep two copies makes longhorn-r2 if needed,
// then opens the existing move with it picked. Nothing moves on the click.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const SENTENCE = "Homestead's data has one copy, and this cluster can now keep two";

function setup({ role = "admin", copies = { state: "can-keep-two", r2: { exists: false, usable: true, why: "" } }, answer } = {}) {
  const fields = {}, sent = [], toasts = [], modals = [];
  const replicas = { desired: 1, max: 3, spread_nodes: 1, pods: [{ name: "homestead-0", node: "node-a", ready: true, leader: true, this: true }],
    data: { pvc: "homestead-data", storage_class: "longhorn", shareable: true, reason: "", candidates: [], classes: [{ name: "longhorn-r2", shareable: true }] },
    data_copies: copies };
  const ctx = { console, Uint8Array, crypto: { getRandomValues: a => a.fill(1) }, STATE: { data: {} },
    $: key => fields[key], can: r => r === "viewer" || role === "admin",
    esc: v => String(v).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    modal: (title, html) => { modals.push(title); fields["#selfDataFlow"] = { innerHTML: html }; },
    toast: (message, kind) => toasts.push({ message, kind }),
    api: async (path, options) => {
      sent.push({ path, method: options?.method || "GET" });
      if (path === "/api/self/replicas") return structuredClone(replicas);
      if (path === "/api/self/data/two-copies") return answer || { ok: true, created: true, storage_class: "longhorn-r2", message: "longhorn-r2 created" };
      if (path === "/api/self/data/prepare") return { source: "homestead-data", classes: [{ name: "longhorn", shareable: true }, { name: "longhorn-r2", shareable: true }],
        nodes: [{ name: "node-a", ready: true }, { name: "node-b", ready: true }], preparations: [], blocking_jobs: [] };
      throw new Error("Unexpected call " + path);
    },
  };
  ctx.window = ctx; vm.createContext(ctx); ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-settings.js", "utf8"), ctx);
  fields["#replicaCard"] = { innerHTML: "" };
  fields["#rep_class"] = { value: "longhorn" };
  return { ctx, fields, sent, toasts, modals };
}

test("the card offers Keep two copies to an admin only while the cluster can keep two", async () => {
  const t = setup(); await t.ctx.replicasPaint();
  assert.ok(t.ctx.STATE.data.replicaHtml.includes(SENTENCE));
  assert.match(t.ctx.STATE.data.replicaHtml, /replicasKeepTwo\(\)/);
  assert.match(t.ctx.STATE.data.replicaHtml, />Keep two copies</);
  const viewer = setup({ role: "viewer" }); await viewer.ctx.replicasPaint();
  assert.ok(viewer.ctx.STATE.data.replicaHtml.includes(SENTENCE), "a viewer is told");
  assert.doesNotMatch(viewer.ctx.STATE.data.replicaHtml, /replicasKeepTwo/, "but offered nothing");
  for (const state of ["n/a", "single-host", "rising", "ok"]) {
    const other = setup({ copies: { state } }); await other.ctx.replicasPaint();
    assert.ok(!other.ctx.STATE.data.replicaHtml.includes(SENTENCE), state);
    assert.doesNotMatch(other.ctx.STATE.data.replicaHtml, /replicasKeepTwo/, state);
  }
});

test("an existing longhorn-r2 that would not keep two copies is named, and nothing is offered", async () => {
  const why = "longhorn-r2 is migratable: its volumes can be mounted on only one node at a time";
  const t = setup({ copies: { state: "can-keep-two", r2: { exists: true, usable: false, why } } }); await t.ctx.replicasPaint();
  assert.ok(t.ctx.STATE.data.replicaHtml.includes(SENTENCE));
  assert.ok(t.ctx.STATE.data.replicaHtml.includes(t.ctx.esc(why)));
  assert.doesNotMatch(t.ctx.STATE.data.replicaHtml, /replicasKeepTwo/);
});

test("Keep two copies posts once, then opens the move with longhorn-r2 picked", async () => {
  const t = setup();
  await t.ctx.replicasKeepTwo();
  assert.deepEqual(t.sent.filter(s => s.method === "POST").map(s => s.path), ["/api/self/data/two-copies"]);
  assert.deepEqual(t.modals, ["Move Homestead data"]);
  assert.match(t.fields["#selfDataFlow"].innerHTML, /<option value="longhorn-r2"[^>]*selected/, "longhorn-r2 wins over the card's own pick");
  assert.ok(t.sent.every(s => !/\/api\/self\/data\/(prepare|move)/.test(s.path) || s.method === "GET"), "nothing prepared or moved");
});

test("a refusal is shown and opens nothing", async () => {
  const t = setup({ answer: { ok: false, storage_class: "longhorn-r2", created: false, message: "Nothing to do: Homestead's data already has two or more copies" } });
  await t.ctx.replicasKeepTwo();
  assert.deepEqual(t.modals, []);
  assert.equal(t.toasts.length, 1);
  assert.match(t.toasts[0].message, /already has two or more copies/);
});

test("#620: copies that could not be read are said on the card, with the error", async () => {
  const t = setup({ copies: { state: "unknown", error: "Longhorn's nodes could not be read" } }); await t.ctx.replicasPaint();
  assert.ok(t.ctx.STATE.data.replicaHtml.includes("The copies of Homestead&#39;s data could not be read. Longhorn&#39;s nodes could not be read")
    || t.ctx.STATE.data.replicaHtml.includes("The copies of Homestead's data could not be read. Longhorn's nodes could not be read"));
  assert.doesNotMatch(t.ctx.STATE.data.replicaHtml, /replicasKeepTwo/);
});

test("on a class that already keeps two copies, the offer raises in place: no move, no dialog", async () => {
  const t = setup({ copies: { state: "can-keep-two", action: "raise", storage_class: "longhorn-r2", r2: { exists: true, usable: true, why: "" } },
    answer: { ok: true, raised: true, created: false, storage_class: "longhorn-r2", message: "Homestead's data now keeps two copies" } });
  await t.ctx.replicasPaint();
  assert.match(t.ctx.STATE.data.replicaHtml, /without moving the data or stopping Homestead/);
  assert.doesNotMatch(t.ctx.STATE.data.replicaHtml, /opens the move/);
  await t.ctx.replicasKeepTwo();
  assert.deepEqual(t.modals, [], "no move dialog");
  assert.deepEqual(t.toasts.map(x => x.kind), ["ok"]);
  assert.ok(t.sent.filter(s => s.path === "/api/self/replicas").length >= 2, "the card is read again");
});

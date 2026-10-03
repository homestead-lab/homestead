"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const updateState = require("../web/js/update-state.js");

/* The rollout queue, driven for items on this cluster and on a linked one. */
function setup({ restarts = 0 } = {}) {
  const sent = [], fields = { "#imageCapacityApprove": { checked: false }, "#imageCapacityApply": {},
    "#modal": { classList: { contains: () => false } } };
  let away = restarts;
  const ctx = { console, URLSearchParams, Map, Date, Promise, encodeURIComponent,
    document: { addEventListener() {} }, STATE: { data: {} }, HomesteadUpdateState: updateState,
    $: id => fields[id], $$: () => [], esc: s => String(s).replaceAll("<", "&lt;"),
    toast: () => {}, setTimeout: cb => cb(),
    modal: (_title, body) => {
      delete fields["#imageReviewLoading"]; delete fields["#imageQueue"];
      if (body.includes('id="imageReviewLoading"')) fields["#imageReviewLoading"] = {};
      if (body.includes('id="imageQueue"')) fields["#imageQueue"] = { innerHTML: "" };
      fields["#mbody"] = { innerHTML: body };
    },
    api: async (path, options) => {
      const cluster = options?.headers?.["X-Homestead-Cluster"] || "";
      const body = options?.body ? JSON.parse(options.body) : null;
      sent.push({ path: path.split("?")[0], cluster, body, query: path.split("?")[1] || "" });
      if (path.endsWith("/preview")) return { capacity: { blocked: false, warnings: [], candidates: [] },
        capacity_token: `signed-${cluster || "here"}-${body.name}`, images: [{ container: "c", before: "old", after: "new", rollback: "old" }] };
      if (path.includes("/progress")) {
        // Homestead replacing itself: its API is away for a while.
        if (cluster && path.includes("name=homestead") && away > 0) { away -= 1; throw new Error("Bad Gateway"); }
        return { uid: "uid", generation: 2, phase: "ready", ready: 1, desired: 1 };
      }
      return { uid: "uid", generation: 2, phase: "starting", ready: 0, desired: 1 };
    } };
  ctx.window = ctx;
  vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), ctx);
  return { ctx, fields, sent };
}

const ITEMS = [
  { ns: "lab", name: "homestead", part: "self", cluster: "b2c0de", clusterName: "Shed" },
  { ns: "lab", name: "homestead-nfs", part: "nfs" },
  { ns: "lab", name: "homestead", part: "self" },
];

test("a linked cluster's rollout is reviewed, applied and followed there, and this Homestead goes last", async () => {
  const t = setup();
  await t.ctx.reviewImageActions(ITEMS);
  assert.match(t.fields["#mbody"].innerHTML, /Shed · lab/);
  t.fields["#imageCapacityApprove"].checked = true;
  await t.ctx.imageReviewedApply();
  const applies = t.sent.filter(s => s.path.endsWith("/apply"));
  assert.deepEqual(applies.map(s => [s.cluster, s.body.name]),
    [["", "homestead-nfs"], ["b2c0de", "homestead"], ["", "homestead"]]);
  assert.equal(applies[1].body.capacity_token, "signed-b2c0de-homestead", "the token that cluster issued");
  for (const s of applies) assert.equal(s.body.cluster, undefined, "the cluster is a header, not part of the body");
  assert.ok(t.sent.some(s => s.path.endsWith("/progress") && s.cluster === "b2c0de"));
  assert.match(t.fields["#imageQueue"].innerHTML, /3\/3 rollouts finished/, "the two homestead rollouts are kept apart");
});

test("a Homestead restarting mid-rollout is waited for, not counted as a failure", async () => {
  const t = setup({ restarts: 3 });
  await t.ctx.reviewImageActions([ITEMS[0]]);
  t.fields["#imageCapacityApprove"].checked = true;
  await t.ctx.imageReviewedApply();
  assert.match(t.fields["#imageQueue"].innerHTML, /1\/1 rollouts finished/);
  assert.doesNotMatch(t.fields["#imageQueue"].innerHTML, /needs attention/);
});

test("a request naming its cluster goes there, whatever the page is pointed at", () => {
  const ctx = { console, STATE: { view: "workloads", data: {} }, document: { addEventListener() {} },
    $: () => ({ classList: { contains: () => false } }), toast: () => {}, esc: String, jsq: String, jsArg: String,
    localStorage: { getItem: () => null, setItem() {} }, location: { pathname: "/", search: "" } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/fleet.js", "utf8"), ctx);
  ctx.FLEET.view = { self: "a1", linked: true, members: [] };
  ctx.FLEET.target = "c3";
  const named = ctx.fleetRoute("/api/image-updates/apply", { method: "POST", headers: { "X-Homestead-Cluster": "b2" } });
  assert.equal(named.opts.headers["X-Homestead-Cluster"], "b2");
  const unnamed = ctx.fleetRoute("/api/image-updates/apply", { method: "POST", headers: {} });
  assert.equal(unnamed.opts.headers["X-Homestead-Cluster"], "c3");
});

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
  assert.match(t.fields["#imageQueue"].innerHTML, /3 apps · 3 updated/, "the two homestead rollouts are kept apart");
});

test("a Homestead restarting mid-rollout is waited for, not counted as a failure", async () => {
  const t = setup({ restarts: 3 });
  await t.ctx.reviewImageActions([ITEMS[0]]);
  t.fields["#imageCapacityApprove"].checked = true;
  await t.ctx.imageReviewedApply();
  assert.match(t.fields["#imageQueue"].innerHTML, /1 app · 1 updated/);
  assert.doesNotMatch(t.fields["#imageQueue"].innerHTML, /needs attention/);
});

test("a queue says one thing, passes over an app already up to date, and can skip a failure (#295)", async () => {
  const t = setup();
  const apps = [{ ns: "lab", name: "uptime-kuma" }, { ns: "lab", name: "loki" }, { ns: "lab", name: "pot-provider" }];
  await t.ctx.reviewImageActions(apps);
  t.fields["#imageCapacityApprove"].checked = true;
  const api = t.ctx.api;
  t.ctx.api = async (path, options) => {
    const body = options?.body ? JSON.parse(options.body) : null;
    if (path.endsWith("/apply") && body.name === "uptime-kuma") throw new Error("no image update is currently available");
    if (path.endsWith("/apply") && body.name === "loki") return { uid: "uid", generation: 2, phase: "starting", ready: 0, desired: 1, operation: { id: "job-loki" } };
    if (path.includes("/progress") && path.includes("name=loki")) return { uid: "uid", generation: 2, phase: "failed", ready: 0, desired: 1 };
    return api(path, options);
  };
  await t.ctx.imageReviewedApply();
  let html = t.fields["#imageQueue"].innerHTML;
  assert.match(html, /3 apps · 1 already up to date · 1 failed · 1 not started/, "the header and the bar agree");
  assert.match(html, /1 of 3 done/);
  assert.match(html, /Rollout failed\. No automatic retry: check its job before trying it again\./, "sentences, not a run-on");
  assert.match(html, /imageQueueJob\(&quot;job-loki&quot;\)|imageQueueJob\("job-loki"\)/, "its job is one click away");
  assert.match(html, /Skip and continue with 1 more/);
  assert.match(html, /The app not started keeps its current image; review it again to update/, "what closing does");
  await t.ctx.imageQueueSkip();
  html = t.fields["#imageQueue"].innerHTML;
  assert.match(html, /3 apps · 1 updated · 1 already up to date · 1 failed/);
  assert.doesNotMatch(html, /Skip and continue/, "nothing left to skip to");
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

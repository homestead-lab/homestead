"use strict";
// Data protection's "Consistent snapshots" section (web/js/fs-freeze.js):
// Longhorn's filesystem freeze around snapshots, for the cluster and per volume.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const src = fs.readFileSync("web/js/fs-freeze.js", "utf8");

const VOLUMES = [
  { name: "pvc-db", claim: "lab/db", mode: "ignored", freezes: "no", why: "Follows the cluster, which is off", when_on: "yes", engine: "v1", access: "rwo", volume_mode: "Filesystem" },
  { name: "pvc-vm", claim: "lab/vm-disk", mode: "ignored", freezes: "no", why: "Follows the cluster, which is off", when_on: "sync", engine: "v1", access: "rwo", volume_mode: "Block" },
  { name: "pvc-v2", claim: "lab/fast", mode: "ignored", freezes: "no", why: "A V2 volume: Longhorn never freezes these", when_on: "no", engine: "v2", access: "rwo", volume_mode: "Filesystem" },
  { name: "pvc-off", claim: "lab/cache", mode: "disabled", freezes: "no", why: "Set to never freeze", when_on: "yes", engine: "v1", access: "rwo", volume_mode: "Filesystem" },
];
const STATUS = { supported: true, version: "v1.13.0", enabled: false, value: '{"v1":"false"}', unknown_value: false,
  from_chart: false, changed: null, old_kernels: [], volumes: VOLUMES, counts: { total: 4, freezing: 0 } };

function load(answer = STATUS) {
  const host = { innerHTML: "" }, body = { innerHTML: "" }, calls = [], toasts = [], titles = [];
  const ctx = {
    STATE: { data: {} }, calls, toasts, titles, setTimeout: () => 0,
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    jsArg: s => JSON.stringify(String(s)), icon: () => "", fmtAt: at => `at ${at}`,
    api: async (path, init) => { calls.push([path, init && JSON.parse(init.body)]); if (init) return { ok: true, detail: "done" }; if (answer instanceof Error) throw answer; return answer; },
    toast: (text, kind) => toasts.push([text, kind]), closeModal() {},
    modal: (title, html) => { titles.push(title); body.innerHTML = html; },
  };
  ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  ctx.document = { getElementById: id => (id === "fs-freeze" ? host : null) };
  ctx.$ = () => null;
  ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  return { ctx, host, body };
}

test("Longhorn without the freeze says why and offers no switch", async () => {
  const { ctx, host } = load({ supported: false, version: "v1.6.2", volumes: [],
    why: "Longhorn v1.6.2 cannot freeze filesystems for snapshots: Longhorn 1.7 added it." });
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /Consistent snapshots/);
  assert.match(host.innerHTML, /Longhorn v1\.6\.2 cannot freeze filesystems for snapshots: Longhorn 1\.7 added it\./);
  assert.doesNotMatch(host.innerHTML, /id="fsf_on"/);
});

test("a read that failed says so, and is not shown as off", async () => {
  const { ctx, host } = load(new Error("forbidden"));
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /could not be read/);
  assert.match(host.innerHTML, /forbidden/);
  assert.doesNotMatch(host.innerHTML, /id="fsf_on"/);
});

test("off: the switch is off and the page says snapshots are crash-consistent", async () => {
  const { ctx, host } = load();
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /<input type="checkbox" id="fsf_on"\s+onchange="fsFreezeReview\(this\.checked\)">/);
  assert.match(host.innerHTML, /crash-consistent/);
  assert.match(host.innerHTML, /data-need="admin"/);
});

test("on: it counts the volumes that freeze and says who turned it on", async () => {
  const { ctx, host } = load({ ...STATUS, enabled: true, changed: { who: "alice", at: 1700000000 }, counts: { total: 4, freezing: 2 } });
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /id="fsf_on" checked/);
  assert.match(host.innerHTML, /2 of 4 volumes/);
  assert.match(host.innerHTML, /Turned on by alice/);
});

test("each volume can follow the cluster, always freeze or never, and says why", async () => {
  const { ctx, host } = load();
  await ctx.loadFsFreeze();
  for (const label of ["Follow the cluster", "Always freeze", "Never freeze"]) assert.match(host.innerHTML, new RegExp(label));
  assert.match(host.innerHTML, /onchange="fsFreezeVolume\(&quot;pvc-db&quot;,this\.value\)"/);
  assert.match(host.innerHTML, /<option value="disabled" selected>Never freeze<\/option>/);
  assert.match(host.innerHTML, /A V2 volume: Longhorn never freezes these/);
  // A V2 volume cannot be set to always.
  assert.match(between(host.innerHTML, "lab/fast", "</tr>"), /<option value="enabled" disabled>/);
});

test("an unknown value, old kernels and the chart are said", async () => {
  const { ctx, host } = load({ ...STATUS, unknown_value: true, value: "maybe", from_chart: true,
    old_kernels: [{ host: "node-1", kernel: "5.15.0-91-generic" }] });
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /maybe/);
  assert.match(host.innerHTML, /node-1/);
  assert.match(host.innerHTML, /5\.15\.0-91-generic/);
  assert.match(host.innerHTML, /chart/);
});

test("turning it on is reviewed first, counting what changes", async () => {
  const { ctx, body } = load();
  await ctx.loadFsFreeze();
  ctx.fsFreezeReview(true);
  assert.deepEqual(ctx.titles, ["Freeze filesystems for snapshots"]);
  assert.match(body.innerHTML, /^\s*<p class="ui-lead">/);
  // pvc-db freezes; pvc-vm syncs instead; pvc-v2 never; pvc-off stays off.
  assert.match(body.innerHTML, /Frozen for each snapshot<\/dt><dd[^>]*>1</);
  assert.match(body.innerHTML, /Synced instead<\/dt><dd[^>]*>1</);
  assert.match(body.innerHTML, /Not frozen<\/dt><dd[^>]*>2</);
  assert.match(body.innerHTML, /fsFreezeSave\(true\)/);
  assert.doesNotMatch(body.innerHTML, /id="fsf_go"[^>]*disabled/);
  assert.equal(ctx.calls.filter(c => c[1]).length, 0, "nothing is written before the review is confirmed");
});

test("with an old kernel on a Longhorn host, turning it on needs an extra tick", async () => {
  const { ctx, body } = load({ ...STATUS, old_kernels: [{ host: "node-1", kernel: "5.15.0-91-generic" }] });
  await ctx.loadFsFreeze();
  ctx.fsFreezeReview(true);
  assert.match(body.innerHTML, /node-1/);
  assert.match(body.innerHTML, /id="fsf_old"/);
  assert.match(body.innerHTML, /id="fsf_go"[^>]*disabled/);
});

test("turning it off is reviewed too, and names the volumes that stay frozen", async () => {
  const volumes = VOLUMES.map(v => (v.name === "pvc-off" ? { ...v, mode: "enabled", freezes: "yes" } : v));
  const { ctx, body } = load({ ...STATUS, enabled: true, volumes });
  await ctx.loadFsFreeze();
  ctx.fsFreezeReview(false);
  assert.deepEqual(ctx.titles, ["Stop freezing filesystems"]);
  assert.match(body.innerHTML, /fsFreezeSave\(false\)/);
  assert.match(body.innerHTML, /lab\/cache/);
});

test("saving sends the choice and reloads", async () => {
  const { ctx } = load();
  await ctx.loadFsFreeze();
  await ctx.fsFreezeSave(true);
  await ctx.fsFreezeVolume("pvc-db", "enabled");
  const writes = ctx.calls.filter(c => c[1]);
  assert.deepEqual(writes, [["/api/longhorn/fs-freeze", { enabled: true }],
    ["/api/longhorn/fs-freeze/volume", { volume: "pvc-db", mode: "enabled" }]]);
  assert.deepEqual(ctx.toasts.map(t => t[1]), ["ok", "ok"]);
});

test("with an old kernel, Always freeze on one volume is asked first, and a no writes nothing", async () => {
  const { ctx } = load({ ...STATUS, old_kernels: [{ host: "node-1", kernel: "5.15.0-91-generic" }] });
  const asked = [];
  ctx.ask = async (message, options) => { asked.push([message, options]); return false; };
  await ctx.loadFsFreeze();
  await ctx.fsFreezeVolume("pvc-db", "enabled");
  assert.equal(asked.length, 1);
  assert.match(asked[0][0], /node-1/);
  assert.equal(asked[0][1].danger, true);
  assert.deepEqual(ctx.calls.filter(c => c[1]), []);
  ctx.ask = async () => true;
  await ctx.fsFreezeVolume("pvc-db", "enabled");
  await ctx.fsFreezeVolume("pvc-db", "disabled");
  assert.equal(ctx.calls.filter(c => c[1]).length, 2);
});

test("without old kernels, a volume's choice is saved at once", async () => {
  const { ctx } = load();
  ctx.ask = async () => assert.fail("not asked");
  await ctx.loadFsFreeze();
  await ctx.fsFreezeVolume("pvc-db", "enabled");
  assert.equal(ctx.calls.filter(c => c[1]).length, 1);
});

test("a value that is neither on nor off is shown as unknown, not off", async () => {
  const { ctx, host } = load({ ...STATUS, unknown_value: true, value: "maybe" });
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /<b>Unknown<\/b>/);
  assert.doesNotMatch(host.innerHTML, /<b>Off<\/b>/);
});

test("a change made outside Homestead is said, and who set each override", async () => {
  const volumes = VOLUMES.map(v => (v.name === "pvc-off" ? { ...v, changed: { who: "bob", at: 7 } }
    : v.name === "pvc-db" ? { ...v, mode: "enabled", freezes: "yes", changed: { outside: true } } : v));
  const { ctx, host } = load({ ...STATUS, enabled: true, changed: { outside: true }, volumes, counts: { total: 4, freezing: 1 } });
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /Turned on outside Homestead/);
  assert.match(between(host.innerHTML, "lab/cache", "</tr>"), /Set by bob/);
  assert.match(between(host.innerHTML, "lab/db", "</tr>"), /Set outside Homestead/);
});

test("one volume reads as one volume", async () => {
  const { ctx, host } = load({ ...STATUS, enabled: true, volumes: VOLUMES.slice(0, 1), counts: { total: 1, freezing: 1 } });
  await ctx.loadFsFreeze();
  assert.match(host.innerHTML, /1 of 1 volume is frozen/);
});

test("names from the cluster are escaped", async () => {
  const evil = '<img src=x onerror="alert(1)">';
  const { ctx, host } = load({ ...STATUS, volumes: [{ ...VOLUMES[0], claim: evil, name: evil, why: evil }],
    old_kernels: [{ host: evil, kernel: evil }] });
  await ctx.loadFsFreeze();
  assert.doesNotMatch(host.innerHTML, /<img/);
});

test("a viewer sees each volume's choice, unchangeable", async () => {
  const { ctx, host } = load();
  ctx.can = role => role !== "admin";
  await ctx.loadFsFreeze();
  assert.match(between(host.innerHTML, "lab/db", "</tr>"), /<select[^>]* disabled/);
  assert.doesNotMatch(between(host.innerHTML, "lab/db", "</tr>"), /<select[^>]*data-need/);
});

test("the demo's POSTs change what its GET answers", () => {
  const source = fs.readFileSync("web/js/demo.js", "utf8");
  const answer = between(source, '"/api/longhorn/fs-freeze": (url, init) => {', '"/api/workloads/rebalance/review"');
  const ctx = { window: {}, demoState: {}, JSON, Date, Math, String };
  vm.createContext(ctx);
  const routes = vm.runInContext(`({${answer}})`, ctx);
  assert.equal(routes["/api/longhorn/fs-freeze"]("/api/longhorn/fs-freeze").enabled, false);
  routes["/api/longhorn/fs-freeze"]("/api/longhorn/fs-freeze", { method: "POST", body: JSON.stringify({ enabled: true }) });
  routes["/api/longhorn/fs-freeze/volume"]("", { body: JSON.stringify({ volume: "pvc-demo-paperless", mode: "disabled" }) });
  const after = routes["/api/longhorn/fs-freeze"]("/api/longhorn/fs-freeze");
  assert.equal(after.enabled, true);
  assert.equal(after.volumes.find(v => v.name === "pvc-demo-paperless").freezes, "no");
  assert.equal(after.volumes.find(v => v.name === "pvc-demo-nextcloud").freezes, "yes");
});

test("the review says an odd value is replaced", async () => {
  const { ctx, body } = load({ ...STATUS, unknown_value: true, value: "maybe" });
  await ctx.loadFsFreeze();
  ctx.fsFreezeReview(true);
  assert.match(body.innerHTML, /holds &quot;maybe&quot;, which this replaces/);
});

test("a volume that is not attached says it freezes once attached, not that it freezes", async () => {
  const later = { name: "pvc-idle", claim: "lab/idle", mode: "ignored", freezes: "later", attached: false, when_on: "yes", engine: "v1", access: "rwo",
    volume_mode: "Filesystem", why: "Not attached: Longhorn only syncs it until an app uses it, then freezes it" };
  const { ctx, host } = load({ ...STATUS, enabled: true, volumes: [...VOLUMES, later], counts: { total: 5, freezing: 0 } });
  await ctx.loadFsFreeze();
  const row = between(host.innerHTML, "lab/idle", "</tr>");
  assert.match(row, /freezes once attached/);
  assert.doesNotMatch(row, />freezes</);
  assert.match(row, /Not attached: Longhorn only syncs it/);
});

test("the review counts a volume that is not attached apart from those frozen now", async () => {
  const idle = { name: "pvc-idle", claim: "lab/idle", mode: "ignored", freezes: "no", attached: false, when_on: "yes", engine: "v1", access: "rwo", volume_mode: "Filesystem", why: "" };
  const { ctx, body } = load({ ...STATUS, volumes: [VOLUMES[0], idle] });
  await ctx.loadFsFreeze();
  ctx.fsFreezeReview(true);
  assert.match(body.innerHTML, /Frozen for each snapshot<\/dt><dd[^>]*>1</);
  assert.match(body.innerHTML, /Frozen once attached<\/dt><dd[^>]*>1</);
});

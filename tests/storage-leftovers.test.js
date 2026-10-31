"use strict";
// #932: Cluster Health names what storage leaves behind. Released volumes
// (claim deleted, class keeps the data) hold their space until someone deletes
// them; a host's stale iSCSI session to an instance-manager that is gone keeps
// a volume from attaching there. Each row opens a list: the Released volumes
// with the existing reviewed delete for each, or the host's sessions with the
// steps that clear them.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const H = require("../web/js/insights.js");

const GiB = 1024 ** 3;
const scratch = (pv, claim, hosts, extra = {}) => ({ pv, namespace: "vm-imports", claim, class: "longhorn-migratable", policy: "Retain",
  size: "40Gi", size_bytes: 40 * GiB, released_at: "2026-10-16T02:39:48Z", driver: "driver.longhorn.io", longhorn: pv,
  hosts, allocated_bytes: 40 * GiB * Math.max(1, (hosts || []).length), old_copy: false, ...extra });
const RELEASED = [scratch("pvc-a", "disk-1-scratch", ["node-1", "node-2"]), scratch("pvc-b", "disk-2-scratch", ["node-2"])];
const SESSION = { sid: 67, target: "iqn.2019-10.io.longhorn:pvc-c", volume: "pvc-c", namespace: "lab", claim: "notes-data",
  state: "FAILED", address: "192.0.2.40", port: 3260, disks: ["sdj"] };
const facts = (volumes = RELEASED, hosts = [], extra = {}) => ({ released: { state: "ok", volumes }, iscsi: { state: "ok", hosts }, ...extra });
const base = leftovers => ({ overview: { health_issues: [] }, cluster: {}, protection: { volumes: [] }, nodes: [], self: {}, settings: {}, leftovers });

test("Released volumes are one row: how many, what they hold, and on which hosts", () => {
  const row = H.advice(base(facts())).find(r => r.id === "released-volumes");
  assert.equal(row.severity, "low");
  assert.equal(row.title, "2 Released volumes hold 120 GiB");
  assert.match(row.detail, /node-1, node-2/);
  assert.match(row.detail, /Retain/);
  assert.equal(row.route, "released-volumes");
});

test("one Released volume is said in the singular", () => {
  const row = H.advice(base(facts([RELEASED[1]]))).find(r => r.id === "released-volumes");
  assert.equal(row.title, "1 Released volume holds 40 GiB");
});

test("no Released volume is no row, and an unread list is said, not hidden", () => {
  assert.equal(H.advice(base(facts([]))).filter(r => r.id === "released-volumes").length, 0);
  const unknown = H.advice(base({ released: { state: "unknown", error: "forbidden", volumes: [] }, iscsi: { state: "n/a", hosts: [] } }))
    .find(r => r.id === "released-volumes");
  assert.equal(unknown.title, "Could not read Released volumes");
  assert.equal(unknown.detail, "forbidden");
  assert.ok(H.advice({ ...base(), leftovers: null }).some(r => r.id === "unknown:leftovers"));
});

test("a host's stale iSCSI sessions are a row on that host, saying what fails and what to do", () => {
  const row = H.advice(base(facts([], [{ node: "node-2", sessions: [SESSION] }]))).find(r => r.id === "iscsi:node-2");
  assert.equal(row.severity, "medium");
  assert.equal(row.title, "node-2 keeps a stale iSCSI session");
  assert.match(row.detail, /192\.0\.2\.40/);
  assert.match(row.detail, /lab\/notes-data/);
  assert.match(row.detail, /log (it|them) out|restart node-2/i);
  assert.equal(row.route, "iscsi:node-2");
});

test("unread instance-managers are said; no probe reporting sessions is no row", () => {
  const unknown = H.advice(base({ released: { state: "ok", volumes: [] }, iscsi: { state: "unknown", error: "forbidden", hosts: [] } }));
  assert.ok(unknown.some(r => r.id === "iscsi" && r.title === "Could not check hosts' iSCSI sessions"));
  assert.equal(H.advice(base({ released: { state: "ok", volumes: [] }, iscsi: { state: "n/a", hosts: [] } })).filter(r => r.id.startsWith("iscsi")).length, 0);
});

function setup(answer) {
  const sent = [], modals = [], calls = [];
  const ctx = { console, STATE: { data: {} }, modals, calls,
    esc: v => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    icon: () => "", $: () => null, fmtAgo: s => `${Math.round(s / 86400)}d ago`,
    modal: (title, html) => { modals.push({ title, html }); },
    volumeDelete: x => calls.push(["volumeDelete", x]), reclassRemoveOld: async pv => calls.push(["reclassRemoveOld", pv]),
    api: async (path, options) => {
      sent.push({ path, method: options?.method || "GET" });
      if (path !== "/api/storage/leftovers") throw new Error("Unexpected call " + path);
      return structuredClone(answer);
    } };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/storage-leftovers.js", "utf8"), ctx);
  return { ctx, sent, modals, calls };
}

test("the list names each Released volume with its claim, class, size, hosts and age, and changes nothing", async () => {
  const t = setup(facts());
  await t.ctx.releasedVolumesReview();
  const shown = t.modals.at(-1);
  assert.equal(shown.title, "Released volumes");
  for (const text of ["vm-imports/disk-1-scratch", "pvc-a", "longhorn-migratable", "40Gi", "node-1, node-2", "120 GiB"]) assert.ok(shown.html.includes(text), text);
  assert.deepEqual(t.sent.map(s => s.method), ["GET"]);
});

test("each Longhorn volume opens the reviewed delete; an old copy its removal; another driver has no action", async () => {
  const t = setup(facts([RELEASED[0], scratch("pvc-old", "photos", ["node-1"], { old_copy: true }),
    scratch("pv-nfs", "share", [], { driver: "nfs.csi.k8s.io", longhorn: "", hosts: [] })]));
  await t.ctx.releasedVolumesReview();
  const html = t.modals.at(-1).html;
  assert.equal((html.match(/data-need="admin"/g) || []).length, 2, "one admin action per volume Homestead can delete");
  t.ctx.releasedVolumeDelete(0);
  t.ctx.releasedVolumeDelete(1);
  // Asked for by its PV: the review is of that volume, never of a claim found by name.
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls)), [["volumeDelete", { name: "pvc-a", pvc_name: "disk-1-scratch", namespace: "vm-imports", pv: "pvc-a" }],
    ["reclassRemoveOld", "pvc-old"]]);
  assert.match(html, /storage's own tools/);
});

test("a volume whose claim name is in use again opens the review of that PV, and says the name is taken", async () => {
  const t = setup(facts([scratch("pvc-a", "photos", ["node-1"], { claim_reused: true })]));
  await t.ctx.releasedVolumesReview();
  const html = t.modals.at(-1).html;
  assert.equal((html.match(/data-need="admin"/g) || []).length, 1);
  assert.match(html, /claim name is in use again/);
  t.ctx.releasedVolumeDelete(0);
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls)), [["volumeDelete", { name: "pvc-a", pvc_name: "photos", namespace: "vm-imports", pv: "pvc-a" }]]);
});

// The per-app row: an app whose claim names old Released volumes still hold.
const app = (claims, ns = "vm-imports") => ({ ns, name: "notes", claims });

test("an app shows its old Released volumes, with the space they hold, only when it has some", async () => {
  const t = setup(facts([RELEASED[0], scratch("pvc-c", "disk-1-scratch", ["node-1"], { claim_reused: true })]));
  await t.ctx.releasedVolumesLoad();
  assert.equal(t.ctx.appReleasedRow(app(["notes-data"])), "", "no Released volume of its claims: no row");
  assert.equal(t.ctx.appReleasedRow(app(["disk-1-scratch"], "lab")), "", "the same claim name in another namespace is not this app's");
  const row = t.ctx.appReleasedRow(app(["disk-1-scratch", "other"]));
  assert.match(row, /2 old volumes/);
  assert.match(row, /120 GiB/);
  assert.match(row, /releasedVolumesReview\(/);
  const empty = setup(facts([]));
  await empty.ctx.releasedVolumesLoad();
  assert.equal(empty.ctx.appReleasedRow(app(["disk-1-scratch"])), "");
});

test("one old volume opens its reviewed delete directly; several open their list, filtered to the app", async () => {
  const t = setup(facts([RELEASED[0], scratch("pvc-c", "disk-1-scratch", ["node-1"], { claim_reused: true }), RELEASED[1]]));
  await t.ctx.releasedVolumesLoad();
  const one = t.ctx.appReleasedRow(app(["disk-2-scratch"]));
  assert.match(one, /1 old volume /);
  assert.match(one, /releasedVolumeDeleteFor\(/);
  assert.match(one, /data-need="admin"/, "only an admin is offered the delete");
  t.ctx.releasedVolumeDeleteFor("vm-imports", "pvc-b");
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls.at(-1))), ["volumeDelete", { name: "pvc-b", pvc_name: "disk-2-scratch", namespace: "vm-imports", pv: "pvc-b" }]);
  await t.ctx.releasedVolumesReview({ namespace: "vm-imports", claims: ["disk-1-scratch"] });
  const html = t.modals.at(-1).html;
  assert.match(html, /pvc-a/);
  assert.doesNotMatch(html, /disk-2-scratch/);
});

test("an unread list gives no app row, rather than a wrong one", async () => {
  const t = setup({ released: { state: "unknown", error: "forbidden", volumes: [] }, iscsi: { state: "n/a", hosts: [] } });
  await t.ctx.releasedVolumesLoad();
  assert.equal(t.ctx.appReleasedRow(app(["disk-1-scratch"])), "");
});

test("after an old copy is removed, the list is read again", async () => {
  const t = setup(facts([scratch("pvc-old", "photos", ["node-1"], { old_copy: true })]));
  await t.ctx.releasedVolumesReview();
  await t.ctx.releasedVolumeDelete(0);
  assert.equal(t.sent.length, 2);
});

test("the host's sessions are listed with the steps that clear them", async () => {
  const t = setup(facts([], [{ node: "node-2", sessions: [SESSION] }]));
  await t.ctx.staleSessionsReview("node-2");
  const shown = t.modals.at(-1);
  assert.equal(shown.title, "Stale iSCSI sessions · node-2");
  for (const text of ["lab/notes-data", "192.0.2.40:3260", "sdj",
    "sudo iscsiadm -m session -r 67 -u",
    "sudo iscsiadm -m node -T iqn.2019-10.io.longhorn:pvc-c -p 192.0.2.40:3260 -o delete"]) assert.ok(shown.html.includes(text), text);
  assert.match(shown.html, /[Rr]estart/);
  assert.doesNotMatch(shown.html, /data-need/, "Homestead runs nothing on the host");
});

test("the disks removed are the session's own as the host sees them then, and only ones nothing uses", async () => {
  // A disk name from the probe can belong to a live volume by the time the
  // steps run, once the session is logged out: the command finds the disks
  // from the session itself, and skips any with a holder, a mount or a user.
  const t = setup(facts([], [{ node: "node-2", sessions: [SESSION] }]));
  await t.ctx.staleSessionsReview("node-2");
  const html = t.modals.at(-1).html.replaceAll("&quot;", '"').replaceAll("&gt;", ">").replaceAll("&amp;", "&");
  assert.ok(html.includes("for b in /sys/class/iscsi_session/session67/device/target*/*/block/*"), html);
  assert.doesNotMatch(html, /\/sys\/block\/sdj\/device\/delete/);
  for (const guard of ['ls /sys/block/$d/holders', "findmnt -S /dev/$d", "fuser -s /dev/$d", "fuser -s /dev/longhorn/pvc-c"])
    assert.ok(html.includes(guard), guard);
});

test("a host whose sessions have cleared says so", async () => {
  const t = setup(facts([], []));
  await t.ctx.staleSessionsReview("node-2");
  assert.match(t.modals.at(-1).html, /No stale iSCSI sessions on node-2/);
});

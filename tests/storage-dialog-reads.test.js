"use strict";
// The Volumes page no longer reads the storage classes, the V2 engine and
// Longhorn's capacity on every refresh only for dialogs to use (#1211). Each
// dialog that needs one reads it when it opens: Change storage class reads the
// classes, and Add a disk (and setting up a disk, and free space on the system
// drive) read whether the V2 engine is on. Once per opening, with the dialog
// open (so a linked cluster's row asks its own cluster), and a failed read is
// said, never shown as no class or as V2 being off (#620).
const test = require("node:test"), assert = require("node:assert/strict");
const vm = require("node:vm");
const src = require("./helpers/storage-source")();

const CLASSES = [
  { name: "longhorn", default: true, replicas: 3 }, { name: "longhorn-r2", replicas: 2 },
  { name: "fast", replicas: 2 }, { name: "harvester-images", internal: true },
];
const VOLUME = { name: "pvc-1", pvc_name: "frigate-config", namespace: "lab", storage_class: "longhorn-r2" };

function world(answers) {
  const log = [], toasts = [];
  // One dialog at a time, as core-modal.js keeps it: its title, whether it is closed, and the
  // elements of its body (an element exists while its id is in the body; a new dialog starts afresh).
  const body = { innerHTML: "" }, dialog = { title: "", closed: true, els: {} };
  const shown = (title, html) => { log.push(["dialog", title, html]); Object.assign(dialog, { title, closed: false, els: {} }); body.innerHTML = html; };
  const element = id => dialog.els[id] ||= { id, value: "", checked: false, disabled: false, hidden: false, innerHTML: "", textContent: "",
    className: "", dataset: {}, classList: { toggle() {}, add() {}, contains: () => false }, set outerHTML(html) { log.push(["replaced", id, html]); } };
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON, Date, CSS: { escape: String },
    STATE: { data: {}, q: "", view: "storage" }, NAV_TOKEN: 1,
    document: { querySelector: () => null, getElementById: () => null, addEventListener() {} },
    platformLacks: () => false, paint: () => {}, moreMenu: () => "", meter: () => "", fmtAgo: String,
    clusterAttr: () => "", clusterTag: () => "", tip: () => "", icon: () => "", typedConfirmMatches: () => false,
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    toast: (text, tone) => toasts.push([text, tone]), modal: shown, childModal: shown,
    closeModal: () => { dialog.closed = true; }, modalBack() {}, applyRole() {},
    $: sel => sel === "#mbody" ? body : sel === "#mtitle" ? { textContent: dialog.title }
      : sel === "#modal" ? { classList: { contains: name => name === "hidden" && dialog.closed } }
      : sel.startsWith("#") && body.innerHTML.includes(`id="${sel.slice(1)}"`) ? element(sel.slice(1)) : null,
    $$: () => [],
    api: async (path, opts) => {
      log.push(["read", path]);
      const answer = typeof answers[path] === "function" ? await answers[path]() : answers[path];
      if (answer instanceof Error) throw answer;
      return JSON.parse(JSON.stringify(answer ?? null));
    } };
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  // What happens after each dialog is drawn is tested elsewhere.
  ctx.volumeReclassPlan = (ns, claim) => log.push(["plan", ns, claim]);
  ctx.diskOsSpaceChanged = () => {};
  const reads = () => log.filter(([kind]) => kind === "read").map(([, path]) => path);
  const last = () => body.innerHTML;
  const visible = html => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
  return { ctx, log, toasts, reads, last, visible, body };
}
// An answer that waits until the test lets it go.
const held = value => { let release; const promise = new Promise(resolve => { release = () => resolve(value); }); return { answer: () => promise, release }; };

test("Change storage class reads the classes when it opens, with the dialog already open", async () => {
  const w = world({ "/api/storage/classes": CLASSES });
  await w.ctx.volumeReclass(VOLUME);
  assert.deepEqual(w.reads(), ["/api/storage/classes"]);
  const first = w.log.findIndex(([kind]) => kind === "dialog"), read = w.log.findIndex(([kind]) => kind === "read");
  assert.ok(first >= 0 && first < read, "the dialog is open before the read, so fleetRoute sends a linked cluster's row to that cluster");
  assert.match(w.log[first][2], /Reading the storage classes/);
  const options = [...w.last().matchAll(/<option value="([^"]+)"/g)].map(m => m[1]);
  assert.deepEqual(options, ["longhorn", "fast"], "the other classes, not the volume's own or an internal one");
  assert.match(w.last(), /<option value="longhorn" selected>/, "the cluster's default is chosen first");
  assert.deepEqual(w.log.at(-1), ["plan", "lab", "frigate-config"]);
  assert.deepEqual(w.toasts, []);
});

test("Change storage class reads them again on every opening, once each time", async () => {
  const w = world({ "/api/storage/classes": CLASSES });
  await w.ctx.volumeReclass(VOLUME);
  await w.ctx.volumeReclass(VOLUME);
  assert.deepEqual(w.reads(), ["/api/storage/classes", "/api/storage/classes"]);
});

test("unread classes are said with Try again, not 'no other storage class'", async () => {
  const w = world({ "/api/storage/classes": new Error("timed out") });
  await w.ctx.volumeReclass(VOLUME);
  assert.match(w.visible(w.last()), /The storage classes could not be read timed out/);
  assert.match(w.last(), /Try again/);
  assert.doesNotMatch(w.visible(w.last()), /no other storage class/);
  assert.deepEqual(w.toasts, []);
  assert.equal(w.log.some(([kind]) => kind === "plan"), false, "nothing is reviewed against classes that were not read");
});

test("only the volume's own class: says there is no other class to move it to", async () => {
  const w = world({ "/api/storage/classes": [{ name: "longhorn-r2" }, { name: "harvester-images", internal: true }] });
  await w.ctx.volumeReclass(VOLUME);
  assert.match(w.visible(w.last()), /There is no other storage class to move it to/);
  assert.equal(w.log.some(([kind]) => kind === "plan"), false);
});

const V2_ON = { enabled: true, ready_nodes: 2, total_nodes: 3, nodes: [] };

test("Add a disk reads whether the V2 engine is on when it opens, once", async () => {
  const w = world({ "/api/storage/v2": V2_ON });
  await w.ctx.diskAdd("node-a");
  assert.deepEqual(w.reads(), ["/api/storage/v2"]);
  assert.match(w.last(), /<option value="v2">V2 \(SPDK\)/);
  assert.doesNotMatch(w.visible(w.last()), /V2 engine is off/);
  await w.ctx.diskAdd("node-a");
  assert.deepEqual(w.reads(), ["/api/storage/v2", "/api/storage/v2"], "once per opening");
});

test("Add a disk with the V2 engine off offers V1 only and says why", async () => {
  const w = world({ "/api/storage/v2": { enabled: false, nodes: [] } });
  await w.ctx.diskAdd("node-a");
  assert.doesNotMatch(w.last(), /<option value="v2">/);
  assert.match(w.visible(w.last()), /The V2 engine is off/);
});

test("Add a disk: an unread V2 engine is said, not shown as off", async () => {
  const w = world({ "/api/storage/v2": new Error("timed out") });
  await w.ctx.diskAdd("node-a");
  assert.doesNotMatch(w.last(), /<option value="v2">/);
  assert.doesNotMatch(w.visible(w.last()), /The V2 engine is off/);
  assert.match(w.visible(w.last()), /Homestead could not read whether the V2 engine is on, so only V1 is offered \(timed out\)/);
  assert.match(w.last(), /Add disk/, "a V1 disk can still be added");
});

test("setting up a disk reads the V2 engine with the disk's check, once", async () => {
  const facts = { state: "blank", size_gb: 500, choices: ["format"], mount_point: "/var/mnt/sdb", partitions: [], mounts: [], multipath: [] };
  const on = world({ "/api/storage/v2": V2_ON, "/api/disks/inspect": facts });
  await on.ctx.diskSetup("node-a", "/dev/sdb");
  assert.deepEqual(on.reads().sort(), ["/api/disks/inspect", "/api/storage/v2"]);
  assert.match(on.last(), /id="ds_engine"/);
  const unread = world({ "/api/storage/v2": new Error("timed out"), "/api/disks/inspect": facts });
  await unread.ctx.diskSetup("node-a", "/dev/sdb");
  assert.doesNotMatch(unread.last(), /id="ds_engine"/);
  assert.match(unread.visible(unread.last()), /could not read whether the V2 engine is on[^]*timed out/);
});

test("free space on the system drive reads the V2 engine with its check, once", async () => {
  const space = { vg: "vg0", usable_gb: 40, reserve_gb: 10, size_gb: 100, root_free_gb: 20, taken: {}, regions: [] };
  const on = world({ "/api/storage/v2": V2_ON, "/api/disks/os-space": space });
  await on.ctx.diskOsSpace("node-a");
  assert.deepEqual(on.reads().sort(), ["/api/disks/os-space", "/api/storage/v2"]);
  assert.match(on.last(), /id="os_engine"/);
  const unread = world({ "/api/storage/v2": new Error("timed out"), "/api/disks/os-space": space });
  await unread.ctx.diskOsSpace("node-a");
  assert.doesNotMatch(unread.last(), /id="os_engine"/);
  assert.match(unread.visible(unread.last()), /could not read whether the V2 engine is on[^]*timed out/);
});

// A late answer never paints over a dialog opened after it (closed, then another opened).
for (const [label, path, open, answer] of [
  ["Change storage class", "/api/storage/classes", ctx => ctx.volumeReclass(VOLUME), CLASSES],
  ["Add a disk", "/api/storage/v2", ctx => ctx.diskAdd("node-a"), V2_ON],
  ["setting up a disk", "/api/storage/v2", ctx => ctx.diskSetup("node-a", "/dev/sdb"), V2_ON],
  ["free space on the system drive", "/api/storage/v2", ctx => ctx.diskOsSpace("node-a"), V2_ON],
]) {
  test(`${label}: an answer that arrives after another dialog opened is not drawn`, async () => {
    const late = held(answer);
    const w = world({ [path]: late.answer, "/api/disks/inspect": { state: "blank", size_gb: 500, choices: ["format"], mount_point: "/var/mnt/sdb", partitions: [], mounts: [], multipath: [] },
      "/api/disks/os-space": { vg: "vg0", usable_gb: 40, reserve_gb: 10, size_gb: 100, root_free_gb: 20, taken: {}, regions: [] } });
    const opening = open(w.ctx);
    await new Promise(resolve => setImmediate(resolve));
    w.ctx.closeModal();
    w.ctx.modal("Delete volume · other", "<p>another dialog</p>");
    late.release();
    await opening;
    assert.equal(w.last(), "<p>another dialog</p>");
    assert.equal(w.log.some(([kind]) => kind === "plan"), false);
  });
}

test("New storage class opened with no classes known reads them, once, and shows Homestead's class", async () => {
  const classes = [{ name: "longhorn", homestead_default: true, homestead_reason: "install" }];
  const w = world({ "/api/storage/classes": classes, "/api/disks": { nodes: {} } });
  await w.ctx.storageClassCreate();
  assert.equal(w.reads().filter(path => path === "/api/storage/classes").length, 1);
  const first = w.log.find(([kind]) => kind === "dialog")[2];
  assert.match(w.visible(first), /Reading which class Homestead puts new volumes on/);
  const line = w.log.find(([kind, id]) => kind === "replaced" && id === "sc_ours");
  assert.match(w.visible(line[2]), /Homestead puts new volumes on longhorn \(the class it was installed with\)/);
});

test("New storage class with the classes already read does not read them again", async () => {
  const w = world({ "/api/disks": { nodes: {} } });
  w.ctx.STATE.data.storageClasses = [{ name: "longhorn", homestead_default: true, homestead_reason: "install" }];
  await w.ctx.storageClassCreate();
  assert.deepEqual(w.reads().filter(path => path === "/api/storage/classes"), []);
  assert.match(w.visible(w.last()), /Homestead puts new volumes on longhorn/);
});

test("New storage class: unread classes are said, not left out as if Homestead had no class", async () => {
  const w = world({ "/api/storage/classes": new Error("timed out"), "/api/disks": { nodes: {} } });
  await w.ctx.storageClassCreate();
  const line = w.log.find(([kind, id]) => kind === "replaced" && id === "sc_ours");
  assert.match(w.visible(line[2]), /Homestead could not read which class it puts new volumes on \(timed out\)/);
});

test("New storage class: a late answer is not drawn into another dialog", async () => {
  const late = held([{ name: "longhorn", homestead_default: true, homestead_reason: "install" }]);
  const w = world({ "/api/storage/classes": late.answer, "/api/disks": { nodes: {} } });
  const opening = w.ctx.storageClassCreate();
  await new Promise(resolve => setImmediate(resolve));
  w.ctx.modal("Delete volume · other", '<p id="sc_ours">another dialog</p>');
  late.release();
  await opening;
  assert.equal(w.log.some(([kind, id]) => kind === "replaced" && id === "sc_ours"), false);
});

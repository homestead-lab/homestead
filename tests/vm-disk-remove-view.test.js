"use strict";
// Import > Disk images offers Remove (#1217): the row, and the review that says what goes.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const uiCtx = { window: {}, esc };
vm.createContext(uiCtx);
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), uiCtx);
const UI = uiCtx.window.UI;

const OPEN = { namespace: "lab", name: "guest-disk", uid: "uid-1", phase: "ImportInProgress", finished: false, capacity: "20Gi",
  storage_class: "fast", blocking: [], confirm: "guest-disk", goes: ["DataVolume guest-disk (20Gi) and the data on it", "Its claim guest-disk"],
  note: "This removes the disk, its claim and what CDI made for it, along with the data copied so far. The source isn't touched." };
const DONE = { ...OPEN, phase: "Succeeded", finished: true,
  note: "This disk is finished: a VM can still be made from it with Create VM. Removing it deletes it for good. The source isn't touched." };

function load({ plan = OPEN, disks = [], send = () => ({ ok: true, message: "Removing disk guest-disk", operation: { id: "op" } }) } = {}) {
  const elements = {}, requests = [], toasts = [], actions = [], modals = [], painted = [];
  const element = id => (elements[id] = elements[id] || { id, innerHTML: "", disabled: false, checked: false, textContent: "" });
  const ctx = { STATE: { data: {}, view: "vmimport" }, esc, jsq: JSON.stringify, tip: () => "", menuButton: () => "", serviceRow: () => "",
    platformLacks: () => false, SOURCE_KINDS: {}, uvmId: String, uvmLoad: () => {}, operationTone: () => "", operationActive: () => false,
    actionBar: rows => { actions.push(...rows); return ""; }, paint: html => painted.push(html), resetPaint: () => {},
    closeModal: () => { modals.push("closed"); }, modalBack: () => {}, toast: (text, tone) => toasts.push([text, tone]),
    modal: (title, body) => { modals.push(title); element("mbody").innerHTML = body; element("vmDiskRemoveLoading"); },
    typedConfirmMatches: () => ctx.typed === true, startOperationChecks: () => { ctx.followed = true; },
    document: { getElementById: id => elements[id] || null },
    $: selector => (selector.startsWith("#") ? elements[selector.slice(1)] || (selector === "#mbody" ? element("mbody") : null) : null),
    api: async (path, init) => {
      if (path === "/api/vm-disks") return disks;
      if (["/api/sources"].includes(path)) return [];
      if (path === "/api/vm-disks/remove-plan") { requests.push([path, JSON.parse(init.body)]); return plan; }
      if (path === "/api/vm-disks/remove") { requests.push([path, JSON.parse(init.body)]); return send(); }
      return null;
    } };
  ctx.window = ctx;
  ctx.UI = UI;
  uiCtx.document = ctx.document; // the real reviewFlow looks its button up there
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  return { ctx, element, elements, requests, toasts, actions, modals, painted };
}

const rowOf = (name, extra) => ({ namespace: "lab", name, phase: "ImportInProgress", capacity: "20Gi", in_use: false, used_by: [], ...extra });

test("the row offers Remove only for a disk no VM uses", async () => {
  const { ctx, actions } = load({ disks: [rowOf("free"), rowOf("done", { phase: "Succeeded" }), rowOf("taken", { phase: "Succeeded", in_use: true, used_by: ["web"] })] });
  await ctx.viewVmImport();
  const rows = actions.filter(row => /vmDiskRemove|vmNew/.test(row.run));
  assert.deepEqual(Array.from(rows, row => row.run), [
    'vmDiskRemove("lab","free")', 'vmNew("done","lab")', 'vmDiskRemove("lab","done")']);
});

test("a viewer doesn't see it: the button needs admin, and is marked as the dangerous one", async () => {
  const { ctx, actions } = load({ disks: [rowOf("free")] });
  await ctx.viewVmImport();
  const remove = actions.find(row => row.label === "Remove");
  assert.equal(remove.need, "admin");
  assert.equal(remove.danger, true);
});

test("Create VM stays the first button on a finished disk", async () => {
  const { ctx, actions } = load({ disks: [rowOf("done", { phase: "Succeeded" })] });
  await ctx.viewVmImport();
  assert.deepEqual(Array.from(actions.filter(row => /vmDiskRemove|vmNew/.test(row.run)), row => row.label), ["Create VM", "Remove"]);
});

test("the review names each thing that goes", async () => {
  const { ctx, elements, modals, requests } = load();
  await ctx.vmDiskRemove("lab", "guest-disk");
  assert.deepEqual(requests, [["/api/vm-disks/remove-plan", { namespace: "lab", name: "guest-disk" }]]);
  assert.equal(modals[0], "Remove disk · guest-disk");
  for (const line of OPEN.goes) assert.ok(elements.mbody.innerHTML.includes(line), line);
});

test("an unfinished and a finished disk read differently", async () => {
  const open = load();
  await open.ctx.vmDiskRemove("lab", "guest-disk");
  assert.match(open.elements.mbody.innerHTML, /data copied so far/);
  assert.doesNotMatch(open.elements.mbody.innerHTML, /Create VM/);
  const done = load({ plan: DONE });
  await done.ctx.vmDiskRemove("lab", "guest-disk");
  assert.match(done.elements.mbody.innerHTML, /a VM can still be made from it with Create VM/);
  assert.doesNotMatch(done.elements.mbody.innerHTML, /data copied so far/);
});

test("the button stays off until the name is typed", async () => {
  const { ctx, element, requests } = load();
  await ctx.vmDiskRemove("lab", "guest-disk");
  const go = element("vmDiskRemoveGo");
  assert.match(ctx.$("#mbody").innerHTML, /id="vmDiskRemoveGo"[^>]* disabled/);
  ctx.vmDiskRemoveReady();
  assert.equal(go.disabled, true);
  await ctx.vmDiskRemoveSend();
  assert.equal(requests.length, 1, "nothing sent");
  ctx.typed = true;
  ctx.vmDiskRemoveReady();
  assert.equal(go.disabled, false);
});

test("sending carries the reviewed disk and the typed name, once, and follows the job", async () => {
  const { ctx, element, requests, modals } = load();
  await ctx.vmDiskRemove("lab", "guest-disk");
  element("vmDiskRemoveGo");
  element("vmDiskRemoveName").value = "guest-disk";
  ctx.typed = true;
  await ctx.vmDiskRemoveSend();
  assert.deepEqual(requests[1], ["/api/vm-disks/remove", { namespace: "lab", name: "guest-disk", uid: "uid-1", confirmation: "guest-disk" }]);
  assert.ok(modals.includes("closed"));
  assert.equal(ctx.followed, true);
  await ctx.vmDiskRemoveSend();
  assert.equal(requests.length, 2, "a review is used up");
});

test("a blocked disk has no send button, and says why, one per line", async () => {
  const { ctx, elements } = load({ plan: { ...OPEN, blocking: ["web uses it", "Pod <reader> uses it"] } });
  await ctx.vmDiskRemove("lab", "guest-disk");
  const html = elements.mbody.innerHTML;
  assert.match(html, /This disk can&#39;t be removed/);
  assert.match(html, /web uses it/);
  assert.match(html, /Pod &lt;reader&gt; uses it/, "escaped");
  assert.doesNotMatch(html, /vmDiskRemoveGo/);
  assert.doesNotMatch(html, /vmDiskRemoveName/);
  assert.doesNotMatch(html, /data copied so far|Create VM/, "no talk of what removing does when it can't be done");
  await ctx.vmDiskRemoveSend();
});

test("a stale review (409 or 400) leaves no write behind", async () => {
  for (const status of [409, 400]) {
    const { ctx, element, elements, requests, modals } = load({ send: () => {
      throw Object.assign(new Error("The disk changed after this review; review it again"), { status }); } });
    await ctx.vmDiskRemove("lab", "guest-disk");
    element("vmDiskRemoveGo");
    element("vmDiskRemoveName").value = "guest-disk";
    ctx.typed = true;
    await ctx.vmDiskRemoveSend();
    assert.match(elements.mbody.innerHTML, /review it again/);
    assert.doesNotMatch(elements.mbody.innerHTML, /id="vmDiskRemoveGo"/, "no way to resend it");
    assert.equal(requests.filter(([path]) => path === "/api/vm-disks/remove").length, 1, "not repeated");
    assert.ok(!modals.includes("closed"), "the reason stays on screen");
    await ctx.vmDiskRemoveSend();
    assert.equal(requests.filter(([path]) => path === "/api/vm-disks/remove").length, 1, "the review is used up");
  }
});

test("a review that fails to load names the problem and offers no send", async () => {
  const { ctx, elements } = load();
  ctx.api = async () => { throw new Error("Couldn't read the VMs"); };
  await ctx.vmDiskRemove("lab", "guest-disk");
  assert.match(elements.mbody.innerHTML, /Couldn&#39;t read the VMs/);
  assert.doesNotMatch(elements.mbody.innerHTML, /vmDiskRemoveGo/);
});

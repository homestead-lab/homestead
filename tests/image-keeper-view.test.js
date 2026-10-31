"use strict";
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

/* #937: the Image cache page's Retention column says why an image is kept in
   the server's words, and links to the app, scheduled job or VM that keeps it
   where the page has one. */
function load() {
  const opened = [];
  const ui = { window: {}, esc: v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]) };
  vm.createContext(ui);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ui);
  const ctx = { esc: ui.esc, jsq: v => `'${v}'`, UI: ui.window.UI, STATE: {}, $: () => ({}), closeModal: () => {},
    go: (page, options) => opened.push([page, options]) };
  ctx.window = ctx;
  vm.createContext(ctx);
  const src = fs.readFileSync("web/js/views-lifecycle.js", "utf8");
  vm.runInContext(between(src, "const IMAGE_REASONS = {", "async function viewImages()"), ctx);
  return { ctx, opened };
}
const row = (kind, label, workload, extra = {}) => ({ reason: "stopped", kind, label, detail: "Because.", namespace: "lab", workload, container: "c", ...extra });

test("a Job, a template, a snapshot and an import show their own words and are named, not linked", () => {
  const { ctx } = load();
  for (const [kind, label] of [["Job", "Job waiting"], ["VirtualMachineTemplateVersion", "VM template"],
    ["VirtualMachineSnapshotContent", "VM snapshot"], ["DataVolume", "disk import"]]) {
    const html = ctx.imageKeeper(row(kind, label, "thing"));
    assert.ok(html.includes(label), label);
    assert.doesNotMatch(html, /<button/);
    assert.match(html, /<span class="dim xs">thing<\/span>/);
  }
});

test("an app, a scheduled job and a VM link to the page that lists them", () => {
  const { ctx, opened } = load();
  for (const [kind, page] of [["Deployment", "workloads"], ["DaemonSet", "workloads"], ["VirtualMachine", "vms"]]) {
    const html = ctx.imageKeeper(row(kind, "x", "thing"));
    assert.match(html, new RegExp(`<button type="button" class="linkish xs" aria-label="Open thing" onclick="imageKeeperOpen\\('${page}','thing'\\)"`));
  }
  ctx.imageKeeperOpen("vms", "lab-vm");
  assert.deepEqual(JSON.parse(JSON.stringify(opened)), [["vms", { keepSearch: true }]]);
  assert.equal(ctx.STATE.q, "lab-vm");
});

test("the tooltip carries where it is and the server's sentence", () => {
  const { ctx } = load();
  assert.match(ctx.imageKeeper(row("Job", "Job paused", "migrate", { detail: "A suspended Job starts from it when it is resumed." })),
    /lab · migrate · c - A suspended Job starts from it when it is resumed\./);
});

test("a server that sends no label still shows the coarse reason", () => {
  const { ctx } = load();
  assert.ok(ctx.imageKeeper({ reason: "rollback", namespace: "lab", workload: "app", container: "app" }).includes("rollback"));
});

test("what no page finds by name is not linked: scheduled jobs, pools and unreadable records", () => {
  const { ctx } = load();
  for (const r of [row("CronJob", "scheduled job", "nightly"), row("VirtualMachinePool", "VM pool at zero", "pool"),
    row("VirtualMachineInstanceReplicaSet", "x", "set", { reason: "unreadable" })]) assert.doesNotMatch(ctx.imageKeeper(r), /<button/);
});

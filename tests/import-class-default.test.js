"use strict";

/* The import pickers start on the class Homestead puts new volumes on (#1125):
   the one default-storage-class rule, as the server gives it, not a fixed
   longhorn-r2 and not the class the cluster marks as default. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function context(data, api) {
  const shown = {};
  const ctx = { console, URLSearchParams, Map, Date, Promise, encodeURIComponent, Set,
    document: { addEventListener() {} }, STATE: { data },
    $: () => null, $$: () => [], esc: v => String(v ?? ""), toast() {}, icon: () => "", tip: () => "",
    modal: (title, html) => { shown.title = title; shown.html = html; },
    childModal: (title, html) => { shown.title = title; shown.html = html; }, defaultNamespace: () => "lab",
    api: api || (async () => { throw new Error("no API in this test"); }) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  return { ctx, shown };
}

const selected = (html, id) => {
  const select = html.match(new RegExp(`<select id="${id}"[^>]*>([\\s\\S]*?)</select>`));
  assert.ok(select, `the dialog has #${id}`);
  return [...select[1].matchAll(/<option value="([^"]*)" selected>/g)].map(m => m[1]);
};

/* ---------------- Import VM disk (Import page) ---------------- */
function diskImport(classes) {
  const t = context({ importNamespaces: ["lab"], importStorageClasses: classes });
  vm.runInContext(require("./helpers/lifecycle-source")(), t.ctx);
  t.ctx.vmDiskImport();
  return t.shown.html;
}

test("Import VM disk starts on the default class the server lists first, not on longhorn-r2", () => {
  // /api/storageclasses lists the one default first: here the install class longhorn.
  assert.deepEqual(selected(diskImport(["longhorn", "fast", "longhorn-r2"]), "vd_sc"), ["longhorn"]);
});

test("Import VM disk with no class list offers Homestead's default, not a literal longhorn-r2", () => {
  const html = diskImport(undefined);
  assert.deepEqual(selected(html, "vd_sc"), [""]);
  assert.doesNotMatch(html, /longhorn-r2/);
});

/* ---------------- Unraid / Proxmox VM import (Virtual machines › Import) ---------------- */
async function vmImport(options) {
  const t = context({ importNamespaces: ["lab"], importStorageClasses: ["fast", "longhorn"], srcs: [{ name: "unraid", kind: "unraid" }],
    uvms: { unraid: { vms: [{ id: "v1", name: "desktop", slug: "desktop", cores: 2, memory: "4Gi", firmware: "bios",
      disks: [{ index: 0, path: "/mnt/user/domains/desktop/vdisk1.img", size_gb: 30, size: 30 * 2 ** 30, used: 2 ** 30,
        format: "raw", bus: "virtio", unraid_bus: "virtio", virtual: 30 }],
      nic: { mac: "", nic_model: "virtio" }, dropped: [], notes: [] }] } } },
  async path => { if (path === "/api/vm/create-options") return options; throw new Error(`unexpected ${path}`); });
  t.ctx.bytesText = bytes => `${bytes} B`;
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), t.ctx);
  t.ctx.uvmPicture = () => {}; t.ctx.uvmRoom = () => {};
  await t.ctx.uvmImport("unraid", "v1");
  return t.shown.html;
}

test("the VM import starts on the class Homestead uses (create-options' default_class), not the cluster's marked default", async () => {
  const html = await vmImport({ cdi: true, network_details: [], storage_classes: ["longhorn", "fast"],
    storage_class_facts: { fast: { default: true } }, default_class: "longhorn" });
  assert.deepEqual(selected(html, "uvm_sc"), ["longhorn"]);
});

"use strict";
// The setup guide's storage step has its own Make default: it says which class
// Homestead puts new volumes on, in the same words as the storage classes card
// (#1125), since an install class wins over the cluster's default.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function guide() {
  const ctx = { URLSearchParams, location: { search: "", protocol: "https:", hostname: "cluster.example.com", host: "cluster.example.com" },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    STATE: { data: {}, platform: {} }, $: () => null, $$: () => [], go: () => {}, matchMedia: () => ({ matches: false }), navigator: {},
    document: { addEventListener() {}, querySelectorAll: () => [], querySelector: () => null, getElementById: () => null },
    icon: () => "", HomesteadRouter: { urlFor: () => "/setup" }, api: async () => null };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.esc = s => String(s ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
  ctx.jsq = s => ctx.esc(JSON.stringify(s));
  ctx.jsArg = s => JSON.stringify(String(s ?? ""));
  ctx.tip = () => "";
  // As index.html loads them: the storage views before the guide.
  vm.runInContext(fs.readFileSync("web/js/views-storage.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/welcome.js", "utf8"), ctx);
  return ctx;
}
const text = html => html.replace(/<[^>]+>/g, "").replace(/\s+/g, " ");
const recipe = existing => ({ id: "two", title: "Two copies", name: "longhorn-2", replicas: 2, disk_tags: [], hosts: 3,
  recommended: true, existing });
const row = (name, extra = {}) => ({ name, provisioner: "driver.longhorn.io", default: false, internal: false, ...extra });

test("beside the guide's Make default: the install class stays Homestead's", () => {
  const ctx = guide();
  const classes = [row("longhorn-2"), row("longhorn", { default: true, homestead_default: true, homestead_reason: "install" })];
  const html = ctx.setupStorageSuggestionsHtml([recipe(classes[0])], classes);
  assert.match(text(html), /Make default/);
  assert.match(text(html), /Homestead puts new volumes on longhorn \(its install class\); making another class the default here does not change that\./);
});

test("the guide and the storage classes card use the same words", () => {
  const ctx = guide();
  for (const reason of ["install", "default", "fallback"]) {
    const classes = [row("longhorn", { default: reason === "default", homestead_default: true, homestead_reason: reason })];
    const line = text(ctx.homesteadClassLine(classes));
    assert.ok(line.startsWith("Homestead puts new volumes on longhorn"), line);
    assert.ok(text(ctx.setupStorageSuggestionsHtml([recipe(null)], classes)).includes(line), reason);
    assert.ok(text(ctx.storageClassCard(classes)).includes(line), reason);
  }
});

test("without the field the guide says nothing about it", () => {
  const ctx = guide();
  assert.doesNotMatch(text(ctx.setupStorageSuggestionsHtml([recipe(null)], [row("longhorn", { default: true })])), /Homestead puts new volumes/);
});

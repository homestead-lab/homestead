"use strict";
// The storage classes card says which class Homestead puts new volumes on, and
// why, beside Make default (#1125): an install class wins over the cluster's
// default, so marking another class default does not move Homestead's.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const src = require("./helpers/storage-source")();

function card(rows) {
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON, CSS: { escape: String },
    STATE: { data: {}, q: "", view: "settings" }, document: { querySelector: () => null, getElementById: () => null },
    platformLacks: () => false, moreMenu: () => "", meter: () => "", fmtAgo: String, clusterAttr: () => "",
    clusterTag: () => "", tip: () => "", icon: () => "",
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), api: async () => null };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  return ctx.storageClassCard(rows).replace(/<[^>]+>/g, "").replace(/\s+/g, " ");
}
const row = (name, extra = {}) => ({ name, provisioner: "driver.longhorn.io", default: false, internal: false, in_use: 0, ...extra });

test("an install class that is here: Make default does not change Homestead's class", () => {
  const text = card([row("fast", { default: true }), row("longhorn", { homestead_default: true, homestead_reason: "install" })]);
  assert.match(text, /Homestead puts new volumes on longhorn \(its install class\); making another class the default here does not change that\./);
});

test("no install class here: Homestead follows the cluster's default", () => {
  const text = card([row("fast", { default: true, homestead_default: true, homestead_reason: "default" }), row("longhorn")]);
  assert.match(text, /Homestead puts new volumes on fast, the cluster's default: making another class the default here moves new volumes there too\./);
});

test("nothing marked default: Longhorn's usual class, and Make default would change it", () => {
  const text = card([row("longhorn", { homestead_default: true, homestead_reason: "fallback" })]);
  assert.match(text, /Homestead puts new volumes on longhorn \(no class is marked default\); making a class the default here moves new volumes there\./);
});

test("an answer without the field says nothing rather than guess", () => {
  assert.doesNotMatch(card([row("longhorn", { default: true })]), /Homestead puts new volumes/);
});

test("Make default's tip says what the cluster's default is for, not that Homestead follows it", () => {
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON, CSS: { escape: String },
    STATE: { data: {}, q: "", view: "settings" }, document: { querySelector: () => null, getElementById: () => null },
    platformLacks: () => false, moreMenu: () => "", meter: () => "", fmtAgo: String, clusterAttr: () => "",
    clusterTag: () => "", tip: () => "", icon: () => "",
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), api: async () => null };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  const html = ctx.storageClassCard([row("fast"), row("longhorn", { default: true, homestead_default: true, homestead_reason: "default" })]);
  assert.match(html, /title="The cluster&#39;s default, for claims that name no class"|title="The cluster's default, for claims that name no class"/);
  assert.doesNotMatch(html, /Use this class when nothing else is chosen/);
});

test("the new class dialog's default switch says the same as the card", async () => {
  const shown = [];
  const el = () => ({ value: "", checked: false, disabled: false, hidden: false, innerHTML: "", textContent: "", classList: { toggle() {} } });
  const button = el();
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON, CSS: { escape: String },
    STATE: { data: { storageClasses: [row("longhorn", { homestead_default: true, homestead_reason: "install" })] }, q: "", view: "settings" },
    document: { querySelector: () => null, getElementById: () => null },
    platformLacks: () => false, moreMenu: () => "", meter: () => "", fmtAgo: String, clusterAttr: () => "",
    clusterTag: () => "", tip: () => "", icon: () => "", modal: (title, html) => shown.push(html),
    $: sel => sel === "#sc_go" ? button : el(), loadDisks: async () => ({ nodes: {} }),
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), api: async () => null };
  ctx.jsq = s => ctx.esc(JSON.stringify(s)); ctx.window = ctx;
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  await ctx.storageClassCreate().catch(() => {});
  const text = shown[0].replace(/<[^>]+>/g, "").replace(/\s+/g, " ");
  assert.match(text, /Make this the cluster's default class/);
  assert.match(text, /Homestead puts new volumes on longhorn \(its install class\); making another class the default here does not change that\./);
});

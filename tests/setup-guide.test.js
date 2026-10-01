"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function fixture(demo = false) {
  const saved = new Map();
  const store = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) };
  const classes = new Set(["hidden"]);
  const button = { classList: { remove: name => classes.delete(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name) } };
  const ctx = { URLSearchParams, location: { search: demo ? "?demo=1" : "", protocol: "https:", hostname: "cluster.example.com", host: "cluster.example.com" },
    localStorage: store, sessionStorage: store, STATE: { data: {} }, $: () => button,
    go: () => {}, esc: String, matchMedia: () => ({ matches: false }), navigator: {},
    UI: { chip: label => label }, HomesteadRouter: { urlFor: () => "/setup" } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/welcome.js", "utf8"), ctx);
  return { ctx, button, classes, saved };
}

test("real clusters keep the guide shortcut after completion without a demo pulse", () => {
  const { ctx, classes, button } = fixture();
  ctx.setupOffer();
  assert.equal(classes.has("hidden"), false);
  assert.equal(classes.has("pulse"), false);
  assert.equal(typeof button.onclick, "function");
});

test("demo offer stays contained and stops pulsing when opened", () => {
  const { ctx, classes, button } = fixture(true);
  ctx.setupOffer();
  assert.equal(classes.has("pulse"), true);
  button.onclick();
  ctx.setupOffer();
  assert.equal(classes.has("pulse"), false);
});

test("dashboard shortcut remains after completion and hiding only removes the shortcut", () => {
  const { ctx } = fixture();
  ctx.localStorage.setItem("homestead.setup.appearance", "1");
  ctx.matchMedia = () => ({ matches: true });
  const state = { admin: false, skips: [], hidden: false, steps: { notifications: { done: true } } };
  assert.match(ctx.setupDashLine(state), /Setup reviewed · Reopen guide/);
  assert.equal(ctx.setupDashLine({ ...state, hidden: true }), "");
  ctx.setupOffer();
});

test("a skipped check remains unchecked and a later observed success replaces skip", () => {
  const { ctx } = fixture();
  assert.equal(ctx.setupStatus("disks", { disks: { done: false } }, ["disks"]), "skipped");
  assert.equal(ctx.setupStatus("disks", { disks: { done: true } }, ["disks"]), "done");
});

test("confirmation on this device can be undone without manufacturing cluster health", () => {
  const { ctx } = fixture();
  ctx.localStorage.setItem("homestead.setup.appearance", "1");
  assert.equal(ctx.setupFacts({ steps: { health: { done: false } } }).appearance.done, true);
  ctx.localStorage.setItem("homestead.setup.appearance", "0");
  const facts = ctx.setupFacts({ steps: { health: { done: false } } });
  assert.equal(facts.appearance.done, false);
  assert.equal(facts.health.done, false);
});

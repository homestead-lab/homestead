"use strict";
// A browser that blocks site storage (Safari or Firefox set to block all
// cookies and site data, some embedded webviews) throws on every localStorage
// call; a saved value can also be damaged. Homestead must still load and sign
// in, with its defaults, and simply not remember settings (#618).
const test = require("node:test"), assert = require("node:assert/strict");
const between = require("./helpers/between");
const fs = require("node:fs"), vm = require("node:vm");

const blocked = () => {
  const refuse = () => { throw Object.assign(new Error("The operation is insecure."), { name: "SecurityError" }); };
  return { getItem: refuse, setItem: refuse, removeItem: refuse };
};
const holding = value => ({ getItem: () => value, setItem() {}, removeItem() {} });

/* Anything the page's code reaches for exists and does nothing. */
function anything() {
  const fn = function () { return anything(); };
  return new Proxy(fn, {
    get: (target, key) => key === Symbol.toPrimitive ? () => "" : key in target ? target[key] : (target[key] = anything()),
    apply: () => anything(),
    construct: () => anything(),
  });
}

function loadCore(localStorage) {
  const ctx = { console, localStorage, document: anything(), navigator: anything(), location: anything(),
    matchMedia: () => ({ matches: false, addEventListener() {} }), fetch: async () => anything(),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, addEventListener() {}, URL, URLSearchParams, Map, Set, Promise };
  ctx.window = ctx; ctx.self = ctx;
  vm.createContext(ctx);
  // Each part as its own script in one context, in index.html's order, as the
  // browser runs them (#911): a name used before its part loads fails here.
  const CORE = require("./helpers/core-source");
  for (const file of CORE.files()) vm.runInContext(CORE.text(file), ctx, { filename: file });
  return ctx;
}

for (const [label, storage] of [["blocked storage", blocked], ["damaged saved settings", () => holding("{not json")]]) {
  test(`core.js and its parts load to the end with ${label}, using the default settings`, () => {
    const ctx = loadCore(storage());
    assert.deepEqual({ ...vm.runInContext("SET", ctx) },
      { theme: "dark", bg: "soft", blur: 26, motion: "on", refresh: 15 });
    // Declared in core-paint.js and the last part: every part ran to its end.
    assert.equal(typeof vm.runInContext("PAGE_SNAPSHOT", ctx), "object");
    assert.equal(typeof vm.runInContext("logoReady", ctx), "function");
  });
}

test("saved settings are still read when storage works", () => {
  const ctx = loadCore(holding('{"theme":"light","refresh":30}'));
  assert.equal(vm.runInContext("SET.theme", ctx), "light");
  assert.equal(vm.runInContext("SET.refresh", ctx), 30);
});

test("the image update check after sign-in still works with blocked storage", async () => {
  const toasts = [];
  const ctx = { localStorage: blocked(), STATE: { data: {} }, toasts,
    api: async () => ({ updates: 2, errors: 0, workloads: [] }),
    HomesteadUpdateState: { isStale: () => false }, updateKey: (ns, name) => `${ns}/${name}`,
    paintUpdateBadge() {}, toast: (text, kind) => toasts.push([text, kind]) };
  ctx.window = ctx;
  vm.createContext(ctx);
  const source = fs.readFileSync("web/js/views-workloads.js", "utf8");
  vm.runInContext(between(source, "async function loadImageUpdates(", "window.loadImageUpdates = loadImageUpdates;"), ctx);
  const report = await ctx.loadImageUpdates();
  assert.equal(report?.updates, 2);
  await ctx.loadImageUpdates(); // the next check, a quarter of an hour on, says nothing new
  assert.deepEqual(toasts, [["2 container image updates available", "ok"]]);
});

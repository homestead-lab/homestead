"use strict";
// views-workloads.js is split into classic scripts by page (#911). Like core.js's parts they share one
// global scope, so the order of their tags matters, and each is a contiguous piece of the old file.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const WL = require("./helpers/workloads-source");

const WEB = path.join(__dirname, "..", "web");
// The order the pieces had in one file.
const PARTS = ["views-workloads.js", "views-workloads-groups.js", "views-workloads-list.js", "views-workloads-balance.js",
  "views-workloads-actions.js", "views-workloads-updates.js", "views-workloads-console.js", "views-workloads-deploy.js",
  "views-workloads-vip.js", "views-workloads-store.js"];
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const tags = [...html.matchAll(/<script src="\/js\/([^"?]+)\?v=/g)].map(m => m[1]);
const read = file => fs.readFileSync(path.join(WEB, "js", file), "utf8");

/* Anything the page's code reaches for exists and does nothing. */
function anything() {
  const fn = function () { return anything(); };
  return new Proxy(fn, {
    get: (target, key) => key === Symbol.toPrimitive ? () => "" : key in target ? target[key] : (target[key] = anything()),
    apply: () => anything(), construct: () => anything(),
  });
}
function page() {
  const ctx = { console, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, document: anything(),
    navigator: anything(), location: anything(), matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: async () => anything(), setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, addEventListener() {}, MutationObserver: anything(), IntersectionObserver: anything(), ResizeObserver: anything(), URL, URLSearchParams, Map, Set, Promise };
  ctx.window = ctx; ctx.self = ctx;
  return vm.createContext(ctx);
}

test("the pieces load one after another in the head, deferred, in the order the old file had", () => {
  const at = tags.indexOf(PARTS[0]);
  assert.deepEqual(tags.filter((_, i) => i >= at && i < at + PARTS.length), PARTS);
  assert.deepEqual(WL.files(), PARTS, "the tests read the pieces in the same order");
  for (const file of PARTS) assert.match(html, new RegExp(`<script src="/js/${file.replace(".", "\\.")}\\?v=[^"]*" defer></script>`), `${file} is deferred`);
});

test("every piece starts with one header line and ends with the Node-only export, and nothing else was added", () => {
  for (const file of PARTS) {
    const text = read(file);
    assert.match(text, /^\/\* [^\n]*\(#911\)[^\n]*\*\/\n/, `${file} header`);
    assert.match(text, /\n\/\/ Node only \(tests\): [^\n]*\nif \(typeof module === "object" && module\.exports\) module\.exports = \{\n(?:  [^\n]*\n)+\};\n$/, `${file} export`);
  }
  assert.ok(WL().length > 100000 && WL().startsWith("/* Containers, Deploy, App Store */"), "the old file is put back together from its pieces");
});

test("each piece's Node export names the top-level names it declares, and no two share one", () => {
  const owner = {};
  for (const file of PARTS) {
    const exported = Object.keys(exportsOf(file));
    const declared = [...WL.text(file).matchAll(/^(?:async function|function|const|let|var|class)\s+([A-Za-z_$][\w$]*)/gm)].map(m => m[1]);
    assert.deepEqual(exported, declared, file);
    for (const name of exported) {
      assert.equal(owner[name], undefined, `${name} is declared in ${owner[name]} and ${file}`);
      owner[name] = file;
    }
  }
  assert.equal(owner.viewWorkloads, "views-workloads.js");
  assert.equal(owner.viewDeploy, "views-workloads-deploy.js");
  assert.equal(owner.viewStore, "views-workloads-store.js");
});

// The scripts index.html loads before the first piece, as the browser ran them.
function before() {
  const ctx = page();
  for (const file of tags.filter((_, i) => i < tags.indexOf(PARTS[0]))) vm.runInContext(read(file), ctx, { filename: file });
  return ctx;
}
// Each piece run as its own script in one context, as the browser does, with the page's other globals faked.
function exportsOf(file) {
  const ctx = before();
  ctx.module = { exports: {} };
  // The names an earlier piece declares, which this one may use while it loads.
  for (const earlier of PARTS.filter((_, i) => i < PARTS.indexOf(file))) vm.runInContext(read(earlier).replace(/\nif \(typeof module[\s\S]*$/, "\n"), ctx, { filename: earlier });
  vm.runInContext(read(file), ctx, { filename: file });
  return ctx.module.exports;
}

test("the pieces load as separate scripts in order, and each name is in the one shared scope", () => {
  const ctx = before();
  for (const file of PARTS) assert.doesNotThrow(() => vm.runInContext(read(file), ctx, { filename: file }), file);
  for (const name of ["viewWorkloads", "renderWorkloads", "workloadGroupPrefs", "startReview", "balanceReview"])
    assert.equal(typeof vm.runInContext(name, ctx), "function", name);
  assert.throws(() => vm.runInContext(read("views-workloads-groups.js"), ctx), /already been declared/, "a const given twice would fail the same way");
});

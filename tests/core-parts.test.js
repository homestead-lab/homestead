"use strict";
// core.js is split into classic scripts on its old section headers (#911). They
// share one global scope, so the order of their tags matters: a const used before
// its file loads is a ReferenceError, and the same const in two files a SyntaxError.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const CORE = require("./helpers/core-source");

const WEB = path.join(__dirname, "..", "web");
// The order the sections had inside core.js.
const PARTS = ["core.js", "core-format.js", "core-api.js", "core-hardware.js", "core-modal.js",
  "core-tooltips-sort.js", "core-paint.js", "core-charts.js", "core-privileges.js"];
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const tags = [...html.matchAll(/<script src="\/js\/([^"?]+)\?v=/g)].map(m => m[1]);

/* Anything the page's code reaches for exists and does nothing. */
function anything() {
  const fn = function () { return anything(); };
  return new Proxy(fn, {
    get: (target, key) => key === Symbol.toPrimitive ? () => "" : key in target ? target[key] : (target[key] = anything()),
    apply: () => anything(),
    construct: () => anything(),
  });
}
function page() {
  const ctx = { console, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, document: anything(),
    navigator: anything(), location: anything(), matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: async () => anything(), setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, addEventListener() {}, URL, URLSearchParams, Map, Set, Promise };
  ctx.window = ctx; ctx.self = ctx;
  return vm.createContext(ctx);
}

test("every web/js file has exactly one script tag in index.html", () => {
  // demo.js's tag is pinned on its own (homestead_static.DEMO_SCRIPT, test_static_files).
  const files = fs.readdirSync(path.join(WEB, "js")).filter(f => f.endsWith(".js") && f !== "demo.js").sort();
  const counts = Object.fromEntries(files.map(f => [f, tags.filter(t => t === f).length]));
  assert.deepEqual(Object.entries(counts).filter(([, n]) => n !== 1), [], "a file with no tag, or two");
  assert.deepEqual(tags.filter(t => !fs.existsSync(path.join(WEB, "js", t))), [], "a tag for a file that is gone");
});

test("core.js's parts load right after it, in the order they had in one file", () => {
  const at = tags.indexOf("core.js");
  assert.deepEqual(tags.filter((_, i) => i >= at && i < at + PARTS.length), PARTS);
  assert.deepEqual(CORE.files(), PARTS, "the tests read the parts in the same order");
});

test("everything up to the last part loads as separate scripts, as the browser runs them", () => {
  // The scripts before core.js too: none may use a core name while it loads.
  const ctx = page();
  const last = tags.indexOf(PARTS.at(-1));
  for (const file of tags.filter((_, i) => i <= last)) {
    assert.doesNotThrow(() => vm.runInContext(fs.readFileSync(path.join(WEB, "js", file), "utf8"), ctx, { filename: file }), file);
  }
  assert.equal(typeof vm.runInContext("logoReady", ctx), "function");
});

test("in the wrong order a part fails to load, so the check above can catch it", () => {
  const ctx = page();
  assert.throws(() => vm.runInContext(CORE.text("core-modal.js"), ctx), /\$ is not defined|ReferenceError/);
  vm.runInContext(CORE.text("core.js"), ctx);
  assert.throws(() => vm.runInContext(CORE.text("core.js"), ctx), /already been declared/, "a const given twice");
});

test("each part's Node export names the top-level names it declares, and no two parts share one", () => {
  const owner = {};
  for (const file of PARTS) {
    const ctx = page();
    for (const earlier of PARTS.filter((_, i) => i < PARTS.indexOf(file))) vm.runInContext(CORE.text(earlier), ctx);
    ctx.module = { exports: {} };
    vm.runInContext(CORE.text(file), ctx);
    const exported = Object.keys(ctx.module.exports);
    const declared = [...CORE.text(file).matchAll(/^(?:async function|function|const|let|var|class)\s+([A-Za-z_$][\w$]*)/gm)].map(m => m[1]);
    assert.deepEqual(exported, declared, file);
    for (const name of exported) {
      assert.equal(owner[name], undefined, `${name} is declared in ${owner[name]} and ${file}`);
      owner[name] = file;
    }
  }
  assert.equal(owner.HOMESTEAD_VERSION, "core.js", "bump_version.py rewrites the version in core.js");
  assert.equal(owner.api, "core-api.js");
});

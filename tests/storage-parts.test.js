"use strict";
// views-storage.js is split by page into classic scripts (#911). They share one
// global scope with the rest of the page, so their tags keep the old file's order,
// and everything up to the last of them must load as separate scripts.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const STORAGE = require("./helpers/storage-source");

const WEB = path.join(__dirname, "..", "web");
// The order the sections had in views-storage.js.
const PARTS = ["flow", "volumes", "shares", "events", "allocation", "disks", "copies", "reclass"].map(p => `views-storage-${p}.js`);
const tags = [...fs.readFileSync(path.join(WEB, "index.html"), "utf8").matchAll(/<script src="\/js\/([^"?]+)\?v=/g)].map(m => m[1]);
const text = name => fs.readFileSync(path.join(WEB, "js", name), "utf8");

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
  // Browser classes the earlier view files reach for while they load.
  for (const name of ["MutationObserver", "ResizeObserver", "IntersectionObserver", "BroadcastChannel", "CustomEvent",
    "Event", "EventSource", "WebSocket", "HTMLElement", "Image", "Blob", "FormData", "AbortController"]) ctx[name] = anything();
  ctx.window = ctx; ctx.self = ctx;
  return vm.createContext(ctx);
}

test("the parts load together, in the old file's order, before app.js", () => {
  assert.equal(fs.existsSync(path.join(WEB, "js", "views-storage.js")), false, "nothing is left in the old file");
  const at = tags.indexOf(PARTS[0]);
  assert.deepEqual(tags.filter((_, i) => i >= at && i < at + PARTS.length), PARTS);
  assert.deepEqual(STORAGE.names(), PARTS, "the tests read the parts in the same order");
  assert.ok(tags.indexOf(PARTS.at(-1)) < tags.indexOf("app.js"), "app.js's VIEWS names their view functions");
});

test("every tag up to the last part loads as its own script, as the browser runs them", () => {
  const ctx = page(), last = tags.indexOf(PARTS.at(-1));
  for (const file of tags.filter((_, i) => i <= last)) {
    assert.doesNotThrow(() => vm.runInContext(text(file), ctx, { filename: file }), file);
  }
  for (const name of ["viewFlow", "viewStorage", "viewShares", "eventText", "lhCapacityCard", "diskTags", "volumeCopyMove", "volumeReclass"]) {
    assert.equal(typeof vm.runInContext(`window.${name} || (typeof ${name} === "function" ? ${name} : undefined)`, ctx), "function", name);
  }
});

test("each part's Node export names its top-level declarations, and no two parts share one", () => {
  const owner = {};
  for (const file of PARTS) {
    const ctx = page();
    ctx.module = { exports: {} };
    ctx.UI = anything();   // a part that builds a UI.reviewFlow while it loads (#909)
    vm.runInContext(text(file), ctx);
    const declared = [...text(file).matchAll(/^(?:async function|function|const|let|var|class)\s+([A-Za-z_$][\w$]*)/gm)].map(m => m[1]);
    assert.deepEqual(Object.keys(ctx.module.exports), declared, file);
    for (const name of declared) {
      assert.equal(owner[name], undefined, `${name} is declared in ${owner[name]} and ${file}`);
      owner[name] = file;
    }
  }
});

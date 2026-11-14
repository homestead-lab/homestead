"use strict";
// Keyed re-render and app icons that stay put (#906 section 5): rows with data-mkey keep their node,
// focus and state when a list reorders, grows or shrinks; a paint of the same data does no DOM work;
// an icon that failed shows the app's initials and is not fetched again every tick.
// Uses tests/helpers/mini-dom.js, not a browser: the browser groups run the real thing.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const { el, mount, DOC } = require("./helpers/mini-dom");

const paintSrc = require("./helpers/core-source")();
const coreSrc = paintSrc;
const iconSrc = () => between(coreSrc, "const ICON_", "/* ---------------- settings", {}).replace(/function loadAppIcons[\s\S]*$/, "") ;

function paintWorld(extra = {}) {
  const ctx = { console, Map, JSON, Math, Date, Array, Object, document: DOC, updateSparkPath() {}, ...extra };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(between(paintSrc, "const mkeyOf", "function resetPaint()"), ctx);
  return ctx;
}
const row = (key, label, extra = {}) => el("li", { "data-mkey": key, ...extra }, el("button", {}, label));
const list = (...rows) => el("ul", {}, rows);
const keys = ul => ul.childNodes.map(n => n.getAttribute("data-mkey"));

test("a keyed row keeps its node and its focus when the list reorders", () => {
  const { morph } = paintWorld();
  const ul = mount(list(row("a", "A"), row("b", "B"), row("c", "C")));
  const [a, b, c] = ul.childNodes, buttonB = b.childNodes[0];
  buttonB.focus();
  morph(ul, list(row("c", "C2"), row("a", "A"), row("b", "B")));
  assert.deepEqual(keys(ul), ["c", "a", "b"]);
  assert.equal(ul.childNodes[0], c); assert.equal(ul.childNodes[1], a); assert.equal(ul.childNodes[2], b);
  assert.equal(c.childNodes[0].childNodes[0].nodeValue, "C2", "and it was brought up to date in place");
  assert.equal(DOC.activeElement, buttonB, "focus survived the move");
});

test("an inserted or removed keyed row leaves the others as they were", () => {
  const { morph } = paintWorld();
  const ul = mount(list(row("a", "A"), row("b", "B"), row("c", "C")));
  const [a, , c] = ul.childNodes;
  morph(ul, list(row("new", "N"), row("a", "A"), row("c", "C")));
  assert.deepEqual(keys(ul), ["new", "a", "c"]);
  assert.equal(ul.childNodes[1], a); assert.equal(ul.childNodes[2], c);
  morph(ul, list(row("c", "C")));
  assert.deepEqual(keys(ul), ["c"]); assert.equal(ul.childNodes[0], c);
});

test("rows without a key are paired by position, as before", () => {
  const { morph } = paintWorld();
  const plain = (...names) => el("ul", {}, names.map(n => el("li", {}, el("b", {}, n))));
  const ul = mount(plain("a", "b", "c"));
  const [first, second] = ul.childNodes;
  morph(ul, plain("c", "a"));
  assert.equal(ul.childNodes.length, 2);
  assert.equal(ul.childNodes[0], first); assert.equal(ul.childNodes[1], second);
  assert.equal(first.childNodes[0].childNodes[0].nodeValue, "c", "the first row now shows what was third: position decides");
});

test("keyed rows beside an unkeyed header keep the header where it is", () => {
  const { morph } = paintWorld();
  const make = (...rows) => el("div", {}, el("h2", {}, "Title"), ...rows);
  const box = mount(make(row("a", "A"), row("b", "B")));
  const [h, a, b] = box.childNodes;
  morph(box, make(row("b", "B"), row("a", "A")));
  assert.equal(box.childNodes[0], h); assert.equal(box.childNodes[1], b); assert.equal(box.childNodes[2], a);
});

function paintOnly(html, hostExtra = {}) {
  const calls = { created: 0, morphed: 0, icons: 0, role: 0 };
  const host = { dataset: { painted: "1" }, classList: { remove() {}, add() {} }, innerHTML: "", querySelectorAll: () => [], ...hostExtra };
  const ctx = paintWorld({ location: { search: "" }, STATE: { view: "nodes" }, V: () => host, pageTabs: () => "", filterBar: () => "", ROLE: "admin",
    enhanceActions() {}, loadAppIcons() { calls.icons++; }, dropOrphanTooltip() {}, applyRole() { calls.role++; },
    document: { createElement: () => { calls.created++; return { set innerHTML(v) {} }; } } });
  vm.runInContext(between(paintSrc, "const PAGE_SNAPSHOT =", "function pageSkeleton(view)"), ctx);
  vm.runInContext(between(paintSrc, "function paint(html)", "/* A cheap signature") + between(paintSrc, "/* A cheap signature", "/* data-mkey"), ctx);
  ctx.morph = () => { calls.morphed++; };
  return { ctx, host, calls, paint: h => vm.runInContext(`paint(${JSON.stringify(h)})`, ctx) };
}

test("painting the same data again does no DOM work", () => {
  const t = paintOnly();
  t.paint("<p>one</p>"); // first paint of this page after a placeholder: a normal paint
  const first = { ...t.calls };
  t.paint("<p>one</p>"); t.paint("<p>one</p>");
  assert.deepEqual(t.calls, first, "no element built, nothing morphed, no icons or roles re-applied");
  t.paint("<p>two</p>");
  assert.equal(t.calls.morphed, first.morphed + 1, "changed data is painted");
  t.paint("<p>two</p>");
  assert.equal(t.calls.morphed, first.morphed + 1);
});

test("the same markup is painted again when the viewer's role changes, and after the page is reset or replaced", () => {
  const t = paintOnly();
  t.paint("<p>one</p>"); const before = t.calls.morphed;
  t.ctx.ROLE = "viewer"; t.paint("<p>one</p>");
  assert.equal(t.calls.morphed, before + 1);
  vm.runInContext("pagePlaceholder('nodes')", t.ctx); // a skeleton or snapshot replaces the page
  t.paint("<p>one</p>");
  assert.equal(t.calls.morphed, before + 2, "a placeholder forgets the last paint");
});

test("a page whose failed /api/icons/ icon is due a retry is painted again, so the retry can come", () => {
  const icon = "/api/icons/" + "c".repeat(64) + ".png", html = `<span class="appav"><img data-icon-src="${icon}"></span>`;
  const t = paintOnly("", { querySelectorAll: sel => sel === ".appav" ? [{ querySelector: () => null }] : [] });
  t.ctx.ICON_FAILED = new Map([[icon, Date.now()]]); t.ctx.ICON_RETRY_MS = 600000;
  t.paint(html); const before = t.calls.morphed;
  t.paint(html);
  assert.equal(t.calls.morphed, before, "not yet due: nothing to paint, the initials stay");
  t.ctx.ICON_FAILED.set(icon, Date.now() - 600000);
  t.paint(html);
  assert.equal(t.calls.morphed, before + 1, "due: painted, so loadAppIcons asks again");
});

// paint() with the real morph and a stand-in page watcher. The stand-in records only what a test tells it
// changed (record()), and hands its records to the callback a microtask later unless paint() took them
// first, as a browser's MutationObserver does. The markup paint() is given names a tree to build.
function paintMorphWorld(trees, extra = {}) {
  const observers = [];
  class Watcher {
    constructor(callback) { this.callback = callback; this.queue = []; observers.push(this); }
    observe() {}
    takeRecords() { const out = this.queue; this.queue = []; return out; }
    record(entry) { this.queue.push(entry); queueMicrotask(() => { if (this.queue.length) this.callback(this.takeRecords()); }); }
  }
  const host = mount(el("div", { "data-painted": "1" }, trees.first()));
  Object.defineProperty(host, "classList", { value: { remove() {}, add() {}, contains: () => false } });
  const createElement = () => {
    const box = el("div");
    Object.defineProperty(box, "innerHTML", { set(html) { box.appendChild(trees[html]()); } });
    return box;
  };
  const ctx = paintWorld({ location: { search: "" }, STATE: { view: "nodes" }, V: () => host, pageTabs: () => "", filterBar: () => "", ROLE: "admin",
    MutationObserver: Watcher, enhanceActions() {}, loadAppIcons() {}, dropOrphanTooltip() {}, applyRole() {},
    document: Object.assign(Object.create(DOC), { createElement }), ...extra });
  vm.runInContext(between(paintSrc, "const PAGE_SNAPSHOT =", "function pageSkeleton(view)"), ctx);
  vm.runInContext(between(paintSrc, "function paint(html)", "/* A cheap signature") + between(paintSrc, "/* A cheap signature", "/* data-mkey"), ctx);
  return { host, watcher: () => observers[0], paint: html => vm.runInContext(`paint(${JSON.stringify(html)})`, ctx) };
}

test("a change a focus handler makes while paint() gives focus back is seen: the next paint of the same data undoes it", async () => {
  const w = paintMorphWorld({ first: () => list(row("a", "A"), row("b", "B"), row("c", "C")), bac: () => list(row("b", "B"), row("a", "A"), row("c", "C")) });
  const ul = w.host.childNodes[0], buttonB = ul.childNodes[1].childNodes[0];
  buttonB.focus();   // the paint below moves b, so a browser blurs it and morph gives its focus back
  let given = 0;
  buttonB.focus = function () {
    DOC.activeElement = this; given++;
    ul.appendChild(row("stray", "S")); w.watcher().record({ type: "childList" });   // the handler changes the page
  };
  w.paint("bac");
  await Promise.resolve();
  assert.equal(given, 1, "focus was given back once");
  assert.equal(DOC.activeElement, buttonB, "the reader keeps their place");
  w.paint("bac");
  assert.deepEqual(keys(ul), ["b", "a", "c"], "the next paint of the same data morphs and takes the handler's row away");
});

const abc = () => list(row("a", "A"), row("b", "B"), row("c", "C")), bac = () => list(row("b", "B"), row("a", "A"), row("c", "C"));

test("focus a moved row lost comes back even when paint() fails after the move", () => {
  const w = paintMorphWorld({ first: abc, bac }, { loadAppIcons() { throw new Error("icons broke"); } });
  const buttonB = w.host.childNodes[0].childNodes[1].childNodes[0];
  buttonB.focus();
  assert.throws(() => w.paint("bac"), /icons broke/);
  assert.equal(DOC.activeElement, buttonB, "the reader keeps their place on a failed paint");
});

test("paint() does not give focus back to a row its own work took off the page", () => {
  let ul;
  const w = paintMorphWorld({ first: abc, bac }, { loadAppIcons() { ul.childNodes.find(n => n.getAttribute("data-mkey") === "b").remove(); } });
  ul = w.host.childNodes[0];
  const buttonB = ul.childNodes[1].childNodes[0];
  buttonB.focus();
  w.paint("bac");
  assert.notEqual(DOC.activeElement, buttonB, "a row taken off the page is not focused");
});

test("paint() does not take back focus that moved elsewhere during the paint", () => {
  let other;
  const w = paintMorphWorld({ first: abc, bac }, { loadAppIcons() { other.focus(); } });
  const ul = w.host.childNodes[0], buttonB = ul.childNodes[1].childNodes[0];
  other = ul.childNodes[2].childNodes[0];
  buttonB.focus();
  w.paint("bac");
  assert.equal(DOC.activeElement, other, "focus taken during the paint stays where it went");
});

// Only iconRetryDue is tested here (node has no MutationObserver, so no page watcher). In a browser a
// broken external icon still repaints the page: its error handler removes the <img> outside paint().
test("iconRetryDue: an app showing initials for an icon from elsewhere does not by itself force a repaint", () => {
  const t = paintOnly("", { querySelectorAll: sel => sel === ".appav" ? [{ querySelector: () => null }] : [] });
  t.ctx.ICON_FAILED = new Map(); t.ctx.ICON_RETRY_MS = 600000;
  const html = '<span class="appav"><img src="https://example.com/gone.png"></span>';
  t.paint(html); const before = t.calls.morphed;
  t.paint(html); t.paint(html);
  assert.equal(t.calls.morphed, before, "an external icon that failed is the browser's to retry, not a reason to repaint");
});

/* App icons */
function iconWorld() {
  const clock = { now: 1_000_000 }, fetches = [], failing = new Set();
  const ctx = { console, Map, Promise, Date: { now: () => clock.now }, esc: String, document: DOC,
    fetch: url => { fetches.push(url); return failing.has(url) ? Promise.resolve({ ok: false, status: 404 }) : Promise.resolve({ ok: true, blob: async () => ({ url }) }); },
    URL: { createObjectURL: blob => `blob:${blob.url}` } };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(between(coreSrc, "const ICON_", "function loadAppIcons", {}).replace(/const appAvatar[\s\S]*$/, "") + "\n" + between(coreSrc, "function loadAppIcons", "/* ---------------- settings"), ctx);
  const avatar = url => el("span", { class: "av appav" }, el("span", { class: "avfallback" }, "FR"), el("img", { "data-icon-src": url, alt: "" }));
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  return { ctx, clock, fetches, failing, avatar, settle, load: root => ctx.loadAppIcons(root) };
}
const URL_A = "/api/icons/" + "a".repeat(64) + ".png";

test("an icon that failed shows the initials and is not fetched again on the next ticks", async () => {
  const w = iconWorld(), { morph, syncAttrs } = paintWorld();
  w.failing.add(URL_A);
  const page = mount(el("div", {}, w.avatar(URL_A)));
  w.load(page); await w.settle();
  assert.equal(w.fetches.length, 1);
  const av = page.childNodes[0];
  assert.equal(av.querySelectorAll("img").length, 0, "the image is gone");
  assert.equal(av.childNodes[0].hidden, false, "the initials show");
  for (let tick = 0; tick < 3; tick++) {          // each tick's markup brings the img back
    morph(page, el("div", {}, w.avatar(URL_A))); w.load(page); await w.settle();
  }
  assert.equal(w.fetches.length, 1, "three more ticks, no new request");
  assert.equal(page.childNodes[0].querySelectorAll("img").length, 0);
  assert.equal(page.childNodes[0].childNodes[0].hidden, false);
});

test("a failed icon is tried again after ten minutes, and again later if it fails again", async () => {
  const w = iconWorld(), { morph } = paintWorld();
  w.failing.add(URL_A);
  const page = mount(el("div", {}, w.avatar(URL_A)));
  w.load(page); await w.settle();
  const tick = async () => { morph(page, el("div", {}, w.avatar(URL_A))); w.load(page); await w.settle(); };
  w.clock.now += 9 * 60 * 1000 + 59000; await tick();
  assert.equal(w.fetches.length, 1, "just under ten minutes: not yet");
  w.clock.now += 2000; w.failing.delete(URL_A); await tick();
  assert.equal(w.fetches.length, 2, "after ten minutes it is fetched again");
  const img = page.childNodes[0].querySelectorAll("img")[0];
  assert.equal(img.src, "blob:" + URL_A, "and shown now it answers");
  img.dispatch("load");   // the browser's load event once the picture decodes
  assert.equal(page.childNodes[0].childNodes[0].hidden, true);
});

test("an icon that loaded keeps its picture across repaints without being set again", async () => {
  const w = iconWorld(), { morph } = paintWorld();
  const page = mount(el("div", {}, w.avatar(URL_A)));
  w.load(page); await w.settle();
  const img = page.childNodes[0].querySelectorAll("img")[0];
  assert.equal(img.src, "blob:" + URL_A);
  page.childNodes[0].childNodes[0].hidden = true;
  let sets = 0; const real = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(img), "src");
  Object.defineProperty(img, "src", { get() { return real.get.call(img); }, set(v) { sets++; real.set.call(img, v); } });
  for (let tick = 0; tick < 3; tick++) { morph(page, el("div", {}, w.avatar(URL_A))); w.load(page); await w.settle(); }
  assert.equal(page.childNodes[0].querySelectorAll("img")[0], img, "the same image node");
  assert.equal(sets, 0, "its source was not set again");
  assert.equal(img.src, "blob:" + URL_A);
  assert.equal(page.childNodes[0].childNodes[0].hidden, true, "the initials stay hidden behind it");
  assert.equal(w.fetches.length, 1);
});

test("an icon whose address changes is fetched for the new address", async () => {
  const w = iconWorld(), { morph } = paintWorld();
  const page = mount(el("div", {}, w.avatar(URL_A)));
  w.load(page); await w.settle();
  const URL_B = "/api/icons/" + "b".repeat(64) + ".png";
  morph(page, el("div", {}, w.avatar(URL_B))); w.load(page); await w.settle();
  assert.equal(w.fetches.at(-1), URL_B);
  assert.equal(page.childNodes[0].querySelectorAll("img")[0].src, "blob:" + URL_B);
});

/* A part morph leaves alone on purpose (an open "…" menu, a field being typed in) is caught up by the
   next paint, even when that paint's data is the same (#906 review I37-1). */
function paintReal(trees) {
  const host = mount(el("div", { "data-painted": "1" }));
  Object.defineProperty(host, "classList", { value: { contains: () => false, remove() {}, add() {} } });
  const doc = Object.assign(DOC, { createElement: () => { const box = el("div", {}); Object.defineProperty(box, "innerHTML", { set(v) { box.childNodes = []; for (const kid of [...trees[v]().childNodes]) box.appendChild(kid); } }); return box; } });
  const ctx = paintWorld({ location: { search: "" }, STATE: { view: "workloads" }, V: () => host, pageTabs: () => "", filterBar: () => "", ROLE: "admin",
    enhanceActions() {}, loadAppIcons() {}, dropOrphanTooltip() {}, applyRole() {}, document: doc });
  vm.runInContext(between(paintSrc, "const PAGE_SNAPSHOT =", "function pageSkeleton(view)"), ctx);
  vm.runInContext(between(paintSrc, "function paint(html)", "/* data-mkey"), ctx);
  return { host, paint: h => vm.runInContext(`paint(${JSON.stringify(h)})`, ctx) };
}
const menuPage = label => () => el("div", {}, el("details", { class: "actionmenu", open: "" }, el("summary", {}, "…"),
  el("div", { class: "actionmenu-pop" }, el("button", {}, label))));
const menuLabel = details => details.querySelector("button").childNodes[0].nodeValue;

test("an action menu open during a change shows the new items once it closes, though the data does not change again", () => {
  const t = paintReal({ running: menuPage("Stop"), stopped: menuPage("Start") });
  t.host.appendChild(menuPage("Stop")().childNodes[0]);
  t.paint("running");
  const details = t.host.childNodes[0], pop = details.childNodes[1];
  const shell = el("details", {}, pop); details.shell = shell;          // ui.js lift(): the open menu moves to the page
  t.paint("stopped");                                                     // the app stops while its menu is open
  assert.equal(menuLabel(shell), "Stop", "an open menu is left alone");
  details.shell = null; details.appendChild(pop);                        // ui.js lower(): it closes and goes back
  t.paint("stopped"); t.paint("stopped");
  assert.equal(menuLabel(details), "Start", "the next paint brings it up to date");
});

test("a field focused during a change takes the new attributes once it is left, though the data does not change again", () => {
  const page = hint => () => el("div", {}, el("input", { placeholder: hint }));
  const t = paintReal({ before: page("old"), after: page("new") });
  t.host.appendChild(page("old")().childNodes[0]);
  t.paint("before");
  const input = t.host.childNodes[0];
  input.focus();
  t.paint("after");
  assert.equal(input.getAttribute("placeholder"), "old", "a field being typed in is left alone");
  DOC.activeElement = null;
  t.paint("after"); t.paint("after");
  assert.equal(input.getAttribute("placeholder"), "new");
  assert.equal(t.host.childNodes[0], input);
});

test("a keyless new row never reuses a row whose key appeared twice", () => {
  const { morph } = paintWorld();
  const ul = mount(list(row("a", "A1"), row("a", "A2")));
  const [first, second] = ul.childNodes;
  morph(ul, list(row("a", "A1"), el("li", {}, el("button", {}, "plain"))));
  assert.equal(ul.childNodes[0], first);
  assert.notEqual(ul.childNodes[1], second, "the second keyed row is not turned into the plain one");
  assert.equal(second.parentNode, null);
  assert.equal(ul.childNodes[1].getAttribute("data-mkey"), null);
});

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

const paintSrc = fs.readFileSync("web/js/core.js", "utf8");
const coreSrc = fs.readFileSync("web/js/core.js", "utf8");
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

test("a page showing an app's initials because its icon failed is still painted, so the retry can come", () => {
  const t = paintOnly("", { querySelectorAll: sel => sel === ".appav" ? [{ querySelector: () => null }] : [] });
  t.paint("<p>one</p>"); const before = t.calls.morphed;
  t.paint("<p>one</p>");
  assert.equal(t.calls.morphed, before + 1);
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

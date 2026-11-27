"use strict";
// One bar (Nov 2026): on a computer a page's tabs and its main actions sit in
// the top bar beside its title, and the list's own controls stay on the page.
const test = require("node:test");
const assert = require("node:assert/strict");
// The core script expects a browser: a window, and the globals other core parts declare.
global.window = global.window || { location: { search: "" } };
const P = require("../web/js/core-paint.js");

const header = actions => `<div class="phead"><div><h2>Containers</h2><p>6 apps</p></div>${actions}</div><div class="card">rows</div>`;
const actions = `<div class="row page-actions"><span id="scanprogress" class="dim xs scanprogress"></span><button class="pill warn pillbtn">3 updates</button>`
  + `<div class="seg iconseg"><button><svg><use href="#i-dash"/></svg></button></div>`
  + `<details class="actionmenu headmenu"><summary class="btn">⋯</summary><div class="actionmenu-pop"><button>Edit</button></div></details>`
  + `<button class="btn pri">＋ Deploy</button></div>`;

test("the header's own action row is found whole, and only inside the header", () => {
  const html = header(actions);
  const [start, end] = P.pageActionsSpan(html);
  assert.equal(html.slice(start, end), actions);
  assert.equal(P.pageActionsSpan(`<div class="card"><div class="row page-actions"><b>x</b></div></div>`), null, "a row outside a page header is left alone");
});

test("the buttons and the ⋯ menu go up; the count pill, the view switch and the progress stay with the list", () => {
  const { lifted, kept } = P.splitPageActions(actions);
  assert.match(lifted, /^<div class="row page-actions"><details class="actionmenu headmenu">[\s\S]*<button class="btn pri">＋ Deploy<\/button><\/div>$/);
  assert.doesNotMatch(lifted, /pill|iconseg|scanprogress/);
  assert.match(kept, /^<div class="row page-actions page-controls"><span id="scanprogress"[^>]*><\/span><button class="pill warn pillbtn">3 updates<\/button><div class="seg iconseg">/);
});

test("a header that is a search goes up whole, its field and its button together", () => {
  const store = `<div class="row page-actions store-search"><input id="s_q" class="search"><button class="btn pri">Search</button></div>`;
  const { lifted, kept } = P.splitPageActions(store);
  assert.equal(lifted, store, "the App Store's search is in the top bar (Nov 2026 mockup)");
  assert.equal(kept, "");
});

test("nested markup and void elements are read as one element each", () => {
  const parts = P.topLevelElements(`<button><svg><use href="#i-x"/></svg>Go</button><input id="a"><details><summary>⋯</summary><div><button>A</button></div></details>`);
  assert.deepEqual(parts.map(p => p.slice(0, 9)), ["<button><", "<input id", "<details>"]);
});

test("lifting moves the actions into the bar, keeps the rest of the page, and redraws the bar only when it changes", () => {
  const bar = { dataset: {}, innerHTML: "", hidden: true, writes: 0 };
  Object.defineProperty(bar, "html", { get() { return this.innerHTML; } });
  global.document = { getElementById: id => id === "pagebar" ? bar : null };
  global.enhanceActions = () => { bar.writes++; };
  const tabs = `<nav class="seg pagetabs"><a class="on">Running</a></nav>`;
  const page = P.liftPageBar(header(actions), tabs);
  assert.match(bar.innerHTML, /^<nav class="seg pagetabs">[\s\S]*＋ Deploy/);
  assert.equal(bar.hidden, false);
  assert.match(page, /<div class="phead"><div><h2>Containers<\/h2><p>6 apps<\/p><\/div><div class="row page-actions page-controls">/);
  assert.doesNotMatch(page, /Deploy/);
  P.liftPageBar(header(actions), tabs);
  assert.equal(bar.writes, 1, "the same bar is not drawn again, so an open menu in it stays open");
  P.liftPageBar(`<div class="card">no header</div>`, "");
  assert.equal(bar.hidden, true, "a page with nothing to lift hides the bar");
  delete global.document; delete global.enhanceActions;
});

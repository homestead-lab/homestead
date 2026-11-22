"use strict";
// A phone's top bar after the Nov 2026 mockup (design.md "One bar"): the
// title is a picker of the family's pages under the family's name, and ＋
// does the page's main button.
const test = require("node:test"), assert = require("node:assert/strict"), vm = require("node:vm");
const between = require("./helpers/between");
const paintSrc = require("./helpers/core-source")();

function heading(text) {
  // Just enough of an <h1>: textContent replaces what innerHTML set, as in a browser.
  const h1 = { dataset: {}, html: "", text,
    get textContent() { return this.html ? this.html.replace(/<option[^>]*>[^<]*<\/option>/g, "").replace(/<[^>]+>/g, "") : this.text; },
    set textContent(v) { this.text = v; this.html = ""; },
    get innerHTML() { return this.html || this.text; },
    set innerHTML(v) { this.html = v; },
    querySelector(sel) { return sel === ".title-pick" && this.html.includes("title-pick") ? {} : null; } };
  return h1;
}

function world({ h1 = heading("Containers"), main = null } = {}) {
  const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const plus = { hidden: true, dataset: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
  const ctx = { esc, console,
    document: { getElementById: id => ({ title: h1, phonePrimary: plus }[id] || null) },
    V: () => ({ querySelector: () => main }),
    HomesteadRouter: {
      ROUTES: { workloads: { label: "Containers" }, images: { label: "Image Cache" }, storage: { label: "Volumes" } },
      navView: v => (v === "images" ? "workloads" : v),
      tabsFor: v => (["workloads", "images"].includes(v) ? [["workloads", "Running"], ["images", "Image Cache"]] : []),
    } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(between(paintSrc, "function phoneTitle(", "function syncPhoneBar()"), ctx);
  return { ctx, h1, plus };
}

test("on a phone, a page with tabs is titled by its family over a picker of its pages", () => {
  const { ctx, h1 } = world();
  assert.equal(ctx.phoneTitle("workloads", true), true);
  assert.match(h1.html, /^<span class="title-family">Containers<\/span><span class="title-pick"><select aria-label="Containers pages" onchange="go\(this.value\)"><option value="workloads" selected>Running<\/option><option value="images">Image Cache<\/option><\/select>/);
  // Another page of the family: the family's name stays, the picker follows.
  h1.textContent = "Image Cache";
  assert.equal(ctx.phoneTitle("images", true), true);
  assert.match(h1.html, /title-family">Containers<[\s\S]*value="images" selected/);
});

test("a page without tabs, or a wider window, keeps the plain title", () => {
  const { ctx, h1 } = world({ h1: heading("Volumes") });
  assert.equal(ctx.phoneTitle("storage", true), false);
  assert.equal(h1.innerHTML, "Volumes");
  const wide = world();
  wide.ctx.phoneTitle("workloads", true);
  assert.equal(wide.ctx.phoneTitle("workloads", false), false);
  assert.equal(wide.h1.innerHTML, "Containers", "the picker gives the title back");
});

test("＋ takes the page's main button's words and who may use it, and clicks it", () => {
  let clicked = 0;
  const main = { textContent: "＋ Create volume", dataset: { need: "operator" }, disabled: false, getAttribute: () => null, click: () => { clicked += 1; } };
  const { ctx, plus } = world({ main });
  assert.equal(ctx.phonePrimary(true), true);
  assert.equal(plus.hidden, false);
  assert.equal(plus.attrs["aria-label"], "＋ Create volume", "announced as the button it stands for");
  assert.equal(plus.title, "Create volume");
  assert.equal(plus.dataset.need, "operator");
  plus.onclick();
  assert.equal(clicked, 1);
  assert.equal(ctx.phonePrimary(false), false, "a wider window shows the page's own button");
  assert.equal(plus.hidden, true);
  assert.equal(world().ctx.phonePrimary(true), false, "a page with no main button has no ＋");
});

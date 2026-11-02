"use strict";
// Everything that does something can be reached with Tab and used with
// Enter or Space, and every icon-only button has a name (#487).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const read = f => fs.readFileSync(f, "utf8");
const html = read("web/index.html");

test("the top bar's controls are buttons with names", () => {
  for (const id of ["hamburger", "searchbtn", "opensettings", "whoami"])
    assert.match(html, new RegExp(`<button[^>]*\\bid="${id}"[^>]*\\baria-label="[^"]+"|<button[^>]*\\baria-label="[^"]+"[^>]*\\bid="${id}"`), id);
  // The dialog's ✕ (#mclose) becomes a button with the dialog shell work (#486).
  assert.doesNotMatch(html.replace(/<div class="iconbtn" id="mclose">/, ""), /<div class="iconbtn/);
});

test("port links and movable tags are buttons", () => {
  assert.doesNotMatch(require("./helpers/workloads-source")(), /<span class="plink[^"]*"[^>]*onclick=/);
  assert.doesNotMatch(read("web/js/views-overview.js"), /<span class="tag[^"]*movable[^"]*"[^>]*onclick=/);
});

test("a VM card's ⋯ menu is the shared action bar, named for its VM", () => {
  const src = read("web/js/views-vms.js");
  const body = between(src, "function vmActions(", "\n}\n");
  assert.match(body, /actionBar\(/);
  assert.doesNotMatch(body, /<details class="actionmenu">/);
});

test("the console's Screen and Serial switch marks the selected one, and its status is announced", () => {
  const src = read("web/js/vm-console.js");
  assert.match(src, /aria-selected/);
  assert.match(src, /aria-live="polite"/);
});

test("a click-only element gets a tab stop and a role, and an icon-only button gets a name", () => {
  const c = { esc: v => String(v ?? ""), document: {} };
  c.window = c; vm.createContext(c);
  vm.runInContext(read("web/js/ui.js"), c);
  const el = (tag, attrs = {}, text = "") => ({ tagName: tag.toUpperCase(), attrs: { ...attrs }, textContent: text,
    hasAttribute(k) { return k in this.attrs; }, getAttribute(k) { return this.attrs[k] ?? null; }, setAttribute(k, v) { this.attrs[k] = String(v); } });
  const row = el("tr", { onclick: "helmRelease('lab','x')" });
  c.keyboardReach(row);
  assert.equal(row.attrs.tabindex, "0");
  const span = el("span", { onclick: "go('x')" }, "Open");
  c.keyboardReach(span);
  assert.equal(span.attrs.tabindex, "0"); assert.equal(span.attrs.role, "button");
  const link = el("a", { onclick: "go('x')" }, "Edit");
  c.keyboardReach(link);
  assert.equal(link.attrs.role, "button"); assert.equal(link.attrs.tabindex, "0");
  const icon = el("button", { "data-tip": "Snapshots and backups of this volume" }, "");
  c.keyboardReach(icon);
  assert.equal(icon.attrs["aria-label"], "Snapshots and backups of this volume");
  const named = el("button", {}, "Save");
  c.keyboardReach(named);
  assert.equal(named.attrs["aria-label"], undefined);
  const real = el("a", { href: "/x", onclick: "go('x')" }, "x");
  c.keyboardReach(real);
  assert.equal(real.attrs.tabindex, undefined, "a real link is already reachable");
});

test("click-only markup only shrinks", () => {
  const files = fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js").map(f => read(`web/js/${f}`)).join("\n");
  const count = re => (files.match(re) || []).length;
  // What is left is reached through keyboardReach(); new code uses <button>.
  assert.ok(count(/<(tr|span|div)\b[^>]*\bonclick=/g) <= 12, `${count(/<(tr|span|div)\b[^>]*\bonclick=/g)} click-only tr/span/div`);
  assert.ok(count(/<a\b(?![^>]*\bhref=)[^>]*\bonclick=/g) <= 34, `${count(/<a\b(?![^>]*\bhref=)[^>]*\bonclick=/g)} links without href`);
});

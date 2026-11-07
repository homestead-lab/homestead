"use strict";
// The dialog shell is announced as a dialog, its ✕ is a button, and toasts
// are announced - errors assertively, and they stay until dismissed (#486).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const html = fs.readFileSync("web/index.html", "utf8"), core = require("./helpers/core-source")();

test("the dialog shell is a labelled modal dialog with a Close button", () => {
  assert.match(html, /<div class="modalbox" role="dialog" aria-modal="true" aria-labelledby="mtitle msubject">/);
  assert.match(html, /<button type="button" class="iconbtn" id="mclose" aria-label="Close">/);
  assert.doesNotMatch(html, /<div class="iconbtn" id="mclose">/);
});

test("the toast area is announced", () => {
  assert.match(html, /<div id="toast" role="status" aria-live="polite"><\/div>/);
});

function toasts() {
  const timers = [], box = { children: [], appendChild(node) { this.children.push(node); node.parent = this; },
    contains(node) { return node === this || this.children.some(n => n === node || n.children.includes(node)); } };
  const listeners = {};
  const c = { setTimeout: (fn, ms) => timers.push({ fn, ms }), $: id => id === "#toast" ? box : null,
    document: { listeners, addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
      removeEventListener(t, fn) { listeners[t] = (listeners[t] || []).filter(f => f !== fn); }, createElement: tag => ({ tag, className: "", textContent: "", attrs: {}, dataset: {}, children: [], listeners: {},
      setAttribute(k, v) { this.attrs[k] = v; }, appendChild(n) { this.children.push(n); }, addEventListener(t, fn) { this.listeners[t] = fn; },
      remove() { box.children = box.children.filter(n => n !== this); } }), documentElement: { style: { setProperty() {} } } } };
  c.window = c; vm.createContext(c);
  vm.runInContext(between(core, "function toast(", "if (window.ResizeObserver && $(\"#jobTray\"))"), c);
  const act = (type, target) => (listeners[type] || []).slice().forEach(fn => fn({ type, target }));
  return { c, box, timers, act };
}

test("a success toast is polite and goes on its own; an error is an alert that stays until dismissed", () => {
  const { c, box, timers } = toasts();
  c.toast("Saved", "ok");
  assert.equal(box.children[0].attrs.role, "status");
  assert.equal(timers.length, 1);
  c.toast("Could not save: timed out", "bad");
  const error = box.children.at(-1);
  assert.equal(error.attrs.role, "alert");
  assert.equal(timers.length, 1, "no timer removes an error");
  const close = error.children.find(n => n.tag === "button");
  assert.equal(close.attrs["aria-label"], "Dismiss");
  close.listeners.click();
  assert.ok(!box.children.includes(error));
});

test("a newer error replaces an older one, and other toasts leave it showing", () => {
  const { c, box } = toasts();
  const shown = () => box.children.map(n => n.textContent || n.children.find(x => x.tag === "span")?.textContent);
  c.toast("Could not save", "bad");
  c.toast("3 container image updates available");
  assert.deepEqual(shown(), ["Could not save", "3 container image updates available"]);
  c.toast("Could not delete", "bad");
  assert.deepEqual(shown(), ["3 container image updates available", "Could not delete"]);
});

test("a staying error leaves at the person's next action, unless that action is on the toast", () => {
  const { c, box, act } = toasts();
  c.toast("Clear Isolated VM before adding a network card", "bad");
  const error = box.children.at(-1);
  act("keydown", error.children.find(n => n.tag === "button"));
  assert.ok(box.children.includes(error), "a key on its own × leaves it to the ×");
  act("pointerdown", { tag: "button" });
  assert.ok(!box.children.includes(error), "a click elsewhere clears it");
  c.toast("Could not save", "bad");
  act("keydown", { tag: "input" });
  assert.equal(box.children.length, 0, "a key press clears it too");
});

test("a toast never takes a click meant for what is under it; only its × does", () => {
  const css = fs.readFileSync("web/style.css", "utf8");
  assert.match(css, /#toast\{[^}]*pointer-events:none/);
  assert.match(css, /\.tst-close\{[^}]*pointer-events:auto/);
});

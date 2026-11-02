"use strict";
// Escape and the X ask before they throw away what a person typed (#459).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const core = require("./helpers/core-source")();
const ui = fs.readFileSync("web/js/ui.js", "utf8");

function element() {
  const classes = new Set();
  return {
    textContent: "", innerHTML: "", dataset: {}, style: { setProperty() {} }, scrollTop: 0, children: [], listeners: {},
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, on) => (on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c) },
    get firstChild() { return this.children[0] || null; },
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    querySelector() { return null; }, querySelectorAll() { return []; }, focus() {},
  };
}
function field(value = "", type = "text") {
  return { type, value, checked: false, isConnected: true, matches: s => /input/.test(s) };
}

function dialogs() {
  const nodes = { "#modal": element(), "#mbody": element(), "#mtitle": element(), ".modalbox": element(), "#mback": null };
  nodes["#modal"].classList.add("hidden");
  const c = {
    document: { addEventListener() {},
      createDocumentFragment: () => ({ appendChild(node) { nodes["#mbody"].children = nodes["#mbody"].children.filter(n => n !== node); } }) },
    location: { pathname: "/", search: "" }, history: { state: null, replaceState() {} },
    STATE: {}, esc: String, enhanceActions() {}, asked: [], answer: true,
  };
  c.window = c;
  c.$ = s => nodes[s];
  vm.createContext(c);
  vm.runInContext(between(core, "function modal(", "/* ---------------- shared, viewport-safe tooltips"), c);
  c.ask = async (message, options = {}) => { c.asked.push({ message, ...options }); return c.answer; };
  c.isOpen = () => !nodes["#modal"].classList.contains("hidden");
  c.title = () => nodes["#mtitle"].textContent;
  // A person's own edit: the browser marks it trusted, and notes the field on focus first.
  const send = (type, target, isTrusted = true) => (nodes["#mbody"].listeners[type] || []).forEach(fn => fn({ type, target, isTrusted }));
  c.type = (input, value) => { send("focusin", input); input.value = value; send("input", input); };
  c.send = send;
  return c;
}

test("typing in a dialog's field and pressing Escape asks before the changes go", async () => {
  const c = dialogs(), name = field("frigate");
  c.modal("Edit · frigate", "<form></form>");
  c.type(name, "frigate-2");
  c.answer = false;
  await c.dismissModal();
  assert.equal(c.asked.length, 1);
  assert.equal(c.asked[0].message, "Discard your changes to Edit · frigate?");
  assert.equal(c.asked[0].ok, "Discard changes");
  assert.equal(c.asked[0].danger, true, "discarding typed work is red");
  assert.equal(c.asked[0].cancel, "Keep editing");
  assert.ok(c.isOpen(), "Keep editing leaves the dialog open");
  c.answer = true;
  await c.dismissModal();
  assert.ok(!c.isOpen(), "Discard closes it");
});

test("a dialog with nothing changed closes at once", async () => {
  const c = dialogs(), name = field("frigate");
  c.modal("Edit · frigate", "<form></form>");
  c.send("focusin", name);
  await c.dismissModal();
  assert.equal(c.asked.length, 0);
  assert.ok(!c.isOpen());
});

test("a field put back as it was is not a change", async () => {
  const c = dialogs(), name = field("frigate");
  c.modal("Edit · frigate", "<form></form>");
  c.type(name, "frigat");
  c.type(name, "frigate");
  await c.dismissModal();
  assert.equal(c.asked.length, 0);
});

test("fields filled in by code after the dialog opens are not typed changes", async () => {
  const c = dialogs(), name = field("");
  c.modal("New virtual machine", "<form></form>");
  name.value = "vm-1";                     // a dialog's own code fills in a default
  c.send("input", name, false);            // and tells its listeners, untrusted
  await c.dismissModal();
  assert.equal(c.asked.length, 0);
  assert.ok(!c.isOpen());
});

test("a person's edit to a field code filled in still asks", async () => {
  const c = dialogs(), name = field("");
  c.modal("New virtual machine", "<form></form>");
  name.value = "vm-1";
  c.type(name, "vm-2");
  c.answer = false;
  await c.dismissModal();
  assert.equal(c.asked.length, 1);
});

test("ticking a box with no focus first still counts as a change", async () => {
  const c = dialogs(), box = field("on", "checkbox");
  c.modal("New schedule", "<form></form>");
  box.checked = true;
  c.send("change", box);
  c.answer = false;
  await c.dismissModal();
  assert.equal(c.asked.length, 1);
});

test("closeModal after a save never asks, and the next dialog starts clean", async () => {
  const c = dialogs(), name = field("frigate");
  c.modal("Edit · frigate", "<form></form>");
  c.type(name, "frigate-2");
  c.closeModal();
  assert.ok(!c.isOpen());
  assert.equal(c.asked.length, 0);
  c.modal("Edit · other", "<form></form>");
  await c.dismissModal();
  assert.equal(c.asked.length, 0);
});

test("a dialog's own guard still takes precedence", async () => {
  const c = dialogs(), name = field("a");
  c.modal("Edit file", "<form></form>");
  c.type(name, "b");
  c.__modalGuard = () => "example.txt has unsaved changes.";
  c.answer = false;
  await c.dismissModal();
  assert.deepEqual(c.asked.map(a => a.message), ["example.txt has unsaved changes."]);
  c.__modalGuard = () => "";
  await c.dismissModal();
  assert.equal(c.asked.length, 1, "its answer of nothing at stake is final");
  assert.ok(!c.isOpen());
});

test("stacked dialogs each keep their own changes", async () => {
  const c = dialogs(), name = field("host-a"), size = field("10");
  c.modal("Edit · host-a", "<form></form>");
  c.type(name, "host-b");
  c.childModal("Add drive", "<form></form>");
  await c.dismissModal();
  assert.equal(c.asked.length, 0, "the untouched drive dialog goes back at once");
  assert.equal(c.title(), "Edit · host-a");
  c.childModal("Add drive", "<form></form>");
  c.type(size, "20");
  c.answer = false;
  await c.dismissModal();
  assert.equal(c.asked.at(-1).message, "Discard your changes to Add drive?");
  c.answer = true;
  await c.dismissModal();
  assert.equal(c.title(), "Edit · host-a");
  c.answer = false;
  await c.dismissModal();
  assert.equal(c.asked.at(-1).message, "Discard your changes to Edit · host-a?");
  assert.ok(c.isOpen());
});

test("Escape with a row's menu open closes only the menu", () => {
  const listeners = [], menu = { open: true };
  const c = {
    window: { addEventListener() {} }, setInterval() {},
    document: {
      addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
      querySelectorAll: s => s === "details.actionmenu[open]" ? [menu].filter(m => m.open) : [],
    },
  };
  vm.createContext(c);
  vm.runInContext(ui, c);
  let stopped = false;
  const press = () => listeners.filter(l => l.type === "keydown" && l.capture)
    .forEach(l => l.fn({ key: "Escape", stopPropagation() { stopped = true; }, preventDefault() {} , target: {} }));
  press();
  assert.equal(menu.open, false);
  assert.ok(stopped, "the dialog's own Escape never sees it");
  stopped = false;
  press();
  assert.ok(!stopped, "with no menu open, Escape reaches the dialog");
});

"use strict";

/* The file manager's editor saves through the file manager's own pane. It used
   to share the old single-pane browser's save, which posted to /api/files/write
   with that browser's empty volume. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

async function open() {
  const calls = [], keys = [], changes = [];
  const host = { innerHTML: "" }, state = { textContent: "" };
  const editor = {
    addCommand(key, fn) { keys.push({ key, fn }); },
    onDidChangeModelContent(fn) { changes.push(fn); },
    getValue: () => "edited", getModel: () => null, dispose() {},
  };
  const monaco = { KeyMod: { CtrlCmd: 2048 }, KeyCode: { KeyS: 49 }, editor: { create: () => editor } };
  const ctx = { console, Date, Promise, setTimeout, clearTimeout, setInterval, clearInterval, Set, Map, URLSearchParams, JSON,
    document: { addEventListener() {}, removeEventListener() {}, documentElement: { dataset: {} } },
    STATE: { data: {} }, monaco,
    $: key => key === "#fm_editor" ? host : key === "#fm_editstate" ? state : { innerHTML: "" },
    $$: () => [], esc: String, icon: () => "", modal() {}, closeModal() {}, toast() {},
    fmtClock: () => "now", bytesText: n => String(n), UI: { actions: x => x },
    ask: async () => false,
    api: async (path, options = {}) => {
      calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
      if (path.startsWith("/api/files/read")) return { content: "hello", size: 5, revision: "r1" };
      return { revision: "r2", message: "saved" };
    } };
  ctx.window = ctx; vm.createContext(ctx);
  for (const file of [...require("./helpers/storage-source").files(), "web/js/filemanager.js"]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  vm.runInContext(`FM.panes = [{ namespace: "apps", pvc: "data", path: "conf" }, { namespace: "apps", pvc: "data", path: "" }];`, ctx);
  await ctx.fmEdit(0, "app.conf");
  const view = vm.runInContext("({ FM })", ctx);
  return { calls, keys, changes, view, state };
}

test("Ctrl+S in the file manager's editor saves the open file to its own volume", async () => {
  const { calls, keys } = await open();
  assert.equal(keys.length, 1, "Ctrl+S is registered");
  keys[0].fn();
  await new Promise(resolve => setTimeout(resolve, 0));
  const writes = calls.filter(call => call.path === "/api/files/write");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].body.namespace, "apps");
  assert.equal(writes[0].body.pvc, "data");
  assert.equal(writes[0].body.path, "conf/app.conf");
  assert.equal(writes[0].body.content, "edited");
});

test("typing in the file manager's editor marks its own edit dirty", async () => {
  const { changes, view, state } = await open();
  assert.equal(view.FM.editing.dirty, false);
  changes.forEach(fn => fn());
  assert.equal(view.FM.editing.dirty, true);
  assert.equal(state.textContent, "unsaved changes");
});

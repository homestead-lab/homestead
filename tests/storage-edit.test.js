"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function editor(api) {
  const elements = { "#modal": { classList: { contains: () => false } } };
  const context = { window: {}, console, api, encodeURIComponent, tip: () => "", esc: String,
    jsq: JSON.stringify, $: selector => elements[selector], toast: () => {}, closeModal: () => {},
    resetPaint: () => {}, viewStorage: () => {}, modal: (title, html) => {
      context.html = html;
      elements["#ve_loading"] = html.includes('id="ve_loading"') ? {} : null;
    } };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("web/js/views-storage.js", "utf8"), context);
  context.viewStorage = () => {};
  return { context, elements };
}
const volume = { namespace: "lab", pvc_name: "example-data", size_gb: 20, replicas: 2 };

test("unsupported expansion disables size and replica-only saves omit it", async () => {
  let body;
  const { context, elements } = editor(async (path, init) => {
    if (init) { body = JSON.parse(init.body); return {}; }
    assert.match(path, /\/api\/volumes\/edit-options\?ns=lab&name=example-data/);
    return { can_expand: false, requested_gb: 5, reason: "Expansion is disabled.", storage_class: "example-storage" };
  });
  await context.window.volumeEdit(volume);
  assert.match(context.html, /id="ve_size"[^>]*value="5" disabled/);
  assert.match(context.html, /Expansion is disabled/);
  elements["#ve_size"] = { value: 5, disabled: true };
  elements["#ve_reps"] = { value: 3 };
  await context.window.volumeEditNow("lab", "example-data");
  assert.deepEqual(body, { namespace: "lab", name: "example-data", replicas: 3 });
});

test("supported expansion uses the claim request and submits new size", async () => {
  let body;
  const { context, elements } = editor(async (path, init) => {
    if (init) { body = JSON.parse(init.body); return {}; }
    return { can_expand: true, requested_gb: 5, storage_class: "example-storage" };
  });
  await context.window.volumeEdit(volume);
  assert.match(context.html, /id="ve_size"[^>]*min="5" value="5" >/);
  elements["#ve_size"] = { value: 10, disabled: false };
  elements["#ve_reps"] = { value: 2 };
  await context.window.volumeEditNow("lab", "example-data");
  assert.equal(body.size_gb, 10);
});

test("failed check offers no save action", async () => {
  const { context } = editor(async () => { throw new Error("Unavailable"); });
  await context.window.volumeEdit(volume);
  assert.match(context.html, /Could not check this volume/);
  assert.doesNotMatch(context.html, /volumeEditNow|id="ve_size"/);
});

test("late responses do not overwrite another dialog", async () => {
  let resolve;
  const { context, elements } = editor(() => new Promise(r => { resolve = r; }));
  const pending = context.window.volumeEdit(volume);
  elements["#ve_loading"] = null;
  context.html = "Another dialog";
  resolve({ can_expand: true, requested_gb: 5 });
  await pending;
  assert.equal(context.html, "Another dialog");
});

test("eligible missing classes offer an admin review before creation", async () => {
  const calls = [];
  const { context } = editor(async (path, init) => {
    calls.push({ path, init });
    return { can_expand: false, repair_class: true, requested_gb: 5, reason: "Missing class.", storage_class: "example-restore" };
  });
  await context.window.volumeEdit(volume);
  assert.match(context.html, /data-need="admin"[^>]*volumeClassRepairReview/);
  context.window.volumeClassRepairReview("lab", "example-data", "example-restore");
  assert.match(context.html, /No backup is restored, and volume sizes and data stay unchanged/);
  assert.equal(calls.length, 1);
  const button = { disabled: false };
  await context.window.volumeClassRepairNow("lab", "example-data", "example-restore", button);
  assert.equal(calls[1].path, "/api/volumes/repair-class");
  assert.deepEqual(JSON.parse(calls[1].init.body), { namespace: "lab", name: "example-data", confirm: "example-restore" });
});

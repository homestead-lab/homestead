"use strict";
// A configuration restore that stops part way shows each part's result in the
// dialog, instead of one toast and a closed dialog that says nothing changed.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(reply, chosen) {
  const seen = { modals: [], toasts: [], closed: 0, reloads: 0, timers: [] };
  const go = { disabled: false, textContent: "", dataset: { verb: "Restore" } };
  const c = {
    console, document: { addEventListener() {} }, STATE: { view: "other" },
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    tip: t => t,
    $: sel => (sel === "#cfgGo" ? go : null),
    $$: () => chosen.map(part => ({ dataset: { part } })),
    api: async () => reply,
    ask: async () => true,
    modal: (title, html) => seen.modals.push({ title, html }),
    childModal: (title, html) => seen.modals.push({ title, html }),
    closeModal: () => { seen.closed++; },
    toast: (text, tone) => seen.toasts.push([text, tone]),
    resetPaint: () => {},
    setTimeout: fn => { seen.timers.push(fn); },
  };
  c.window = c;
  c.window.location = { reload: () => { seen.reloads++; } };
  vm.createContext(c);
  for (const f of ["web/js/ui.js", "web/js/config-backup.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  c.__configRestore = { doc: { format: "homestead-config-backup" }, pass: "correct horse" };
  return { c, seen };
}

const STOPPED = {
  ok: false, restored: ["settings"], skipped: [],
  detail: "The restore stopped at IP addresses, which may be partly restored. Restored before it: Settings. Not started: Portal.",
  parts: [
    { id: "settings", label: "Settings", state: "restored", detail: "" },
    { id: "ipam", label: "IP addresses", state: "failed",
      detail: "Writing this part failed, so it may be partly restored. Restore it again from the same backup. Error from homestead-unifi: conflict" },
    { id: "portal", label: "Portal", state: "not_started", detail: "" },
    { id: "vmstore", label: "VM image store", state: "skipped", detail: "" },
  ],
};

test("a restore that stops part way lists every part and keeps the dialog open", async () => {
  const { c, seen } = load(STOPPED, ["settings", "ipam", "portal"]);
  await c.configRestoreGo();
  assert.equal(seen.closed, 0, "the dialog stays open");
  assert.deepEqual(seen.toasts, [], "no success toast for a restore that failed");
  const shown = seen.modals.at(-1).html;
  for (const words of ["Settings: Restored", "IP addresses: Failed", "homestead-unifi: conflict", "Portal: Not started",
    "VM image store: Nothing in the backup", "The restore stopped part way"]) assert.match(shown, new RegExp(words), words);
  assert.match(shown, /ui-checklist/);
  assert.equal(seen.reloads, 0);
});

test("restored users still lead to signing in again after the list is shown", async () => {
  const reply = { ...STOPPED, restored: ["users"], parts: [{ id: "users", label: "Users and roles", state: "restored", detail: "" },
    { id: "ipam", label: "IP addresses", state: "failed", detail: "Writing this part failed. Error: x" }] };
  const { c, seen } = load(reply, ["users", "ipam"]);
  await c.configRestoreGo();
  const shown = seen.modals.at(-1).html;
  assert.match(shown, /Failed/, "the list is shown first");
  assert.equal(seen.reloads, 0, "not before the person has seen it");
  assert.match(shown, /configRestoreSignIn\(\)/);
  assert.match(shown, /Everyone must sign in again/);
  c.configRestoreSignIn();
  assert.equal(seen.reloads, 1);
});

test("a users write that failed may still have landed, so it also leads to signing in", async () => {
  const reply = { ...STOPPED, restored: [], parts: [{ id: "users", label: "Users and roles", state: "failed", detail: "Error: timed out" },
    { id: "ipam", label: "IP addresses", state: "not_started", detail: "" }] };
  const { c, seen } = load(reply, ["users", "ipam"]);
  await c.configRestoreGo();
  const shown = seen.modals.at(-1).html;
  assert.match(shown, /Everyone may need to sign in again/);
  assert.match(shown, /configRestoreSignIn\(\)/);
  assert.doesNotMatch(shown, /closeModal\(\)/);
});

test("every part written but the reload after it failed: an error, not a stop", async () => {
  const reply = { ok: false, restored: ["ipam"], skipped: [], parts: [{ id: "ipam", label: "IP addresses", state: "restored", detail: "" }],
    detail: "Restored IP addresses. Homestead could not reload the restored parts." };
  const { c, seen } = load(reply, ["ipam"]);
  await c.configRestoreGo();
  const shown = seen.modals.at(-1).html;
  assert.match(shown, /The restore finished with an error/);
  assert.doesNotMatch(shown, /stopped part way/);
  assert.equal(seen.closed, 0);
});

test("a restore that finished closes the dialog as before", async () => {
  const { c, seen } = load({ ok: true, restored: ["ipam"], skipped: [], detail: "restored IP addresses",
    parts: [{ id: "ipam", label: "IP addresses", state: "restored", detail: "" }] }, ["ipam"]);
  await c.configRestoreGo();
  assert.equal(seen.closed, 1);
  assert.deepEqual(seen.toasts, [["restored IP addresses", "ok"]]);
});

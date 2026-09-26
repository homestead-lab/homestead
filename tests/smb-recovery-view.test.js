"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

async function render(server) {
  let html = "";
  const context = {
    window: {}, console, STATE: { data: {} }, can: () => true, icon: () => "", nfsRecovery: () => "",
    esc: value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    paint: value => { html = value; },
    api: async path => path === "/api/shares" ? [{ name: "photos", pvc: "single" }, { name: "media", pvc: "copies" }]
      : path === "/api/shares/server" ? server : {},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("web/js/views-storage.js", "utf8"), context);
  await context.viewShares();
  return html;
}

test("partial SMB service names offline shares without reporting mapping drift", async () => {
  const html = await render({ installed: true, enabled: true, in_sync: true, partial: true,
    served_shares: ["media"], offline_shares: [{ name: "photos", pvc: "single", reason: "Host <offline>" }],
    recovery_pending: { single: { action: "restore" } } });
  assert.match(html, /Partial service/);
  assert.match(html, /SMB offline · storage unavailable/);
  assert.match(html, /Host &lt;offline&gt;/);
  assert.match(html, /waiting to restore/);
  assert.doesNotMatch(html, /out of sync/);
  assert.doesNotMatch(html, /onclick="repairSamba/);
});

test("failed restore offers retry and explains the working subset rollback", async () => {
  const html = await render({ installed: true, in_sync: true,
    recovery_failures: { single: "Check mounts <first>" }, recovery_warning: "API <unavailable>" });
  assert.match(html, /Repair \/ retry recovery/);
  assert.match(html, /Automatic restore paused/);
  assert.match(html, /working subset was restored/);
  assert.match(html, /API &lt;unavailable&gt;/);
  assert.doesNotMatch(html, /<first>/);
});

"use strict";
// A section saves once (#511): a setting waits for the save bar and is a
// .toggle; something that acts at once is a service row with an Enable or
// Disable button, so nothing that acts at once looks like a field.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");
const read = f => fs.readFileSync(f, "utf8");

test("AI agents are allowed or refused with a button, not a switch", () => {
  const src = read("web/js/views-agents.js");
  assert.doesNotMatch(src, /id="mcp_on"/);
  assert.match(src, /serviceRow\("AI agents"/);
  assert.match(src, /label: on \? "Disable" : "Enable", run: `mcpSet\(\$\{!on\}\)`/);
});

test("allowing or refusing AI agents asks first, since it changes who can reach the cluster", () => {
  const src = read("web/js/views-agents.js");
  assert.match(between(src, "window.mcpSet", "\n};\n"), /await ask\(/);
});

test("the SMB, NFS and host console add-ons switch with Enable and Disable buttons", () => {
  const src = read("web/js/platform.js");
  for (const fn of ["sambaToggle(this)", "nfsToggle(this)", "hostConsoleSet(this)"]) assert.ok(!src.includes(fn), fn);
  for (const fn of ["sambaSet", "nfsSet", "hostConsoleSet"]) assert.match(src, new RegExp(`run: \`${fn}\\(\\$\\{!`), fn);
  assert.doesNotMatch(src, /<label class="switch"><input type="checkbox"[^>]*> \$\{[a-z]+\.enabled \? "On" : "Off"\}/);
});

test("Settings' own on/off settings are toggles", () => {
  assert.match(read("web/js/views-settings.js"), /<label class="toggle"><input id="set_smart_notify"/);
  assert.match(require("./helpers/storage-source")(), /<label class="toggle"><input type="checkbox" id="lh_offline"/);
});

test("VM Edit holds only fields that go through Review; Logo and Monitoring stay in the VM's ⋯", () => {
  const src = read("web/js/views-vms.js");
  assert.doesNotMatch(src, /Saved on its own, at once/);
  assert.doesNotMatch(src, /vmLogo\([^)]*\(\) => vmEdit/);
  assert.doesNotMatch(src, /vmMonitoring\([^)]*\(\) => vmEdit/);
});

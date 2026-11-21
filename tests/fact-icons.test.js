"use strict";
// Facts carry an icon for what they are (Nov 2026), so a panel of values is
// scanned by shape: an address, a size, memory, a host. UI.facts picks it from
// the label; a page's .about-grid gets the same icons before it is painted.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function load() {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { console, esc, jsq: s => esc(JSON.stringify(String(s))), jsArg: s => JSON.stringify(String(s ?? "")), icon: name => `<i data-icon="${name}"></i>`,
    document: { getElementById: () => null }, STATE: { data: {} }, location: { search: "" }, URLSearchParams, JSON, Date, Math, setTimeout: () => 0, clearTimeout() {} };
  c.window = c;
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  return c.UI;
}

test("the labels a details panel uses get the icon for what they are", () => {
  const UI = load();
  const expect = {
    Address: "network", Size: "box", Memory: "memory", "Disk activity": "disk", "Host failure": "alert",
    Host: "node", CPU: "chip", "Guest OS": "vm", "Live migration": "move", "Run strategy": "power", Kernel: "layers",
    Access: "link", Image: "layers", Volumes: "disk", Uptime: "pulse", State: "pulse", "Last snapshot": "snapshot",
    "Last backup": "shield", Created: "clock", Hardware: "chip", "Windows path": "link", Temperature: "thermo",
    "Applied by": "user", Apps: "box", Network: "network", Ports: "port", "Web check": "pulse", Checked: "clock",
  };
  for (const [label, icon] of Object.entries(expect)) assert.equal(UI.factIcon(label), icon, label);
});

test("a fact names its own icon when the label cannot say", () => {
  const UI = load();
  assert.equal(UI.factIcon("Whatever", "plug"), "plug");
  assert.match(UI.facts([["Whatever", "x", "plug"]]), /<dt><svg class="ui-fact-icon" aria-hidden="true"><use href="#i-plug"\/><\/svg>Whatever<\/dt>/);
  assert.equal(UI.factIcon("Something unlisted"), "info");
});

test("every fact in a panel has an icon, in the same place", () => {
  const UI = load();
  const html = UI.facts([["Address", "192.0.2.60"], ["Memory", "4 GiB"], ["Something unlisted", "x"]]);
  assert.equal((html.match(/<dt><svg class="ui-fact-icon"/g) || []).length, 3);
});

test("a page's about-grid items get the same icons, once, and nothing else changes", () => {
  const UI = load();
  const page = `<p><span>Not a fact</span></p><div class="about-grid"><div><span>Host</span><b>node1</b></div><div class="wl-detail-image"><span>Image</span><b>x</b><div><span>inner</span></div></div></div><div><span>Outside</span><b>y</b></div>`;
  const out = UI.withFactIcons(page);
  assert.match(out, /<div><span><svg class="ui-fact-icon" aria-hidden="true"><use href="#i-node"\/><\/svg>Host<\/span>/);
  assert.match(out, /<div class="wl-detail-image"><span><svg class="ui-fact-icon" aria-hidden="true"><use href="#i-layers"\/><\/svg>Image<\/span>/);
  assert.match(out, /<p><span>Not a fact<\/span><\/p>/);
  assert.match(out, /<div><span>Outside<\/span>/, "an item outside the grid is left alone");
  assert.equal(UI.withFactIcons(out), out, "painting twice adds no second icon");
  assert.equal(UI.withFactIcons("<p>no grid</p>"), "<p>no grid</p>");
});

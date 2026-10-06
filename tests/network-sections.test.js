"use strict";

/* Networking in sections: every section the list offers can be opened by
   name (?tab=), has a drawn icon, and every attention item names its section. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const network = fs.readFileSync("web/js/views-network.js", "utf8");
const ipam = fs.readFileSync("web/js/views-ipam.js", "utf8");
const sprite = fs.readFileSync("web/index.html", "utf8");

const ctx = {};
vm.createContext(ctx);
vm.runInContext(network.slice(network.indexOf("const NETWORK_SECTIONS = ["), network.indexOf("const networkSectionRow")) + "\nthis.SECTIONS = NETWORK_SECTIONS;", ctx);
vm.runInContext(ipam.slice(ipam.indexOf("const NETWORK_TABS = ["), ipam.indexOf("\n", ipam.indexOf("const NETWORK_TABS = ["))) + "\nthis.TABS = NETWORK_TABS;", ctx);

test("each section is a ?tab= the page opens", () => {
  assert.deepEqual([...ctx.SECTIONS.map(row => row[0])].sort(), [...ctx.TABS].sort());
});

test("each section has an icon drawn in the sprite, and the groups read in order", () => {
  for (const [key, , , icon] of ctx.SECTIONS) assert.match(sprite, new RegExp(`<symbol id="i-${icon}"`), `${key}: i-${icon}`);
  assert.deepEqual([...new Set(ctx.SECTIONS.map(row => row[2]))], ["", "Apps", "Addresses", "Network", "Protection"]);
});

test("every attention item says which section holds it", () => {
  const helpers = network.slice(network.indexOf("function networkAttention(data)"), network.indexOf("function networkAttentionCounts"));
  const run = { CSS: { escape: s => String(s) } };
  vm.createContext(run);
  vm.runInContext(helpers + "\nthis.networkAttention = networkAttention;", run);
  const items = run.networkAttention({
    addresses: { addresses: [{ ip: "192.0.2.250", state: "unrouted" }] },
    conflicts: [{ ip: "192.0.2.242", protocol: "TCP", port: 80, owners: [{ namespace: "lab", service: "a" }, { namespace: "lab", service: "b" }] }],
    services: [{ namespace: "lab", name: "c", health: "unavailable" }],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.section))), ["addresses", "services", "services"]);
});

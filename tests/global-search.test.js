"use strict";

/* The top bar's search: what it finds, in what order, and where each
   result goes. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const Search = require("../web/js/search.js");

const data = {
  workloads: [{ name: "home-assistant", ns: "home", nodes: ["node-a"], images: ["ghcr.io/home-assistant/home-assistant:stable"] },
    { name: "plex", ns: "media", nodes: ["node-b"], images: ["plexinc/pms-docker"] }],
  nodes: [{ name: "node-a", status: "Ready", workloads: ["home-assistant"], pods_wl: 4 },
    { name: "homelab-gpu", status: "NotReady", workloads: [], pods_wl: 0 }],
  volumes: [{ name: "pvc-1", pvc_name: "plex-config", namespace: "media", size_gb: 10, state: "attached" }],
  vms: [{ name: "win11", ns: "default", status: "Running", node: "node-b" }],
  shares: [{ name: "media", size_gb: 250 }],
  apps: [{ name: "Home Assistant", repo: "community", summary: "Home automation" }],
  pages: [{ view: "network", label: "Networking", section: "System" }, { view: "protect", label: "Data Protection", section: "Storage" },
    { view: "network", tab: "firewall", label: "Firewall", section: "Networking" }],
  settings: [["updates", "Updates", "Releases, platform, hosts, containers", ["updates"], "Cluster", "update"]],
};
const names = q => Search.hits(q, data).map(hit => `${hit.kind}:${hit.name}`);

test("a VM is found by its name and opens the VM", () => {
  const [hit] = Search.hits("win11", data);
  assert.equal(hit.kind, "VM");
  assert.deepEqual(hit.open, { type: "vm", ns: "default", name: "win11" });
});

test("pages and Settings sections are found as places to go", () => {
  assert.equal(names("network")[0], "Page:Networking");
  const [firewall] = Search.hits("firewall", data);
  assert.deepEqual(firewall.open, { type: "page", view: "network", tab: "firewall" });
  assert.equal(firewall.sub, "Networking › Firewall");
  const [updates] = Search.hits("updates", data);
  assert.equal(updates.kind, "Settings");
  assert.deepEqual(updates.open, { type: "settings", tab: "updates" });
  assert.deepEqual(Search.hits("protect", data)[0].open, { type: "page", view: "protect" });
});

test("shares and App Store apps are found", () => {
  assert.ok(names("media").includes("Share:media"));
  const app = Search.hits("home assistant", data).find(hit => hit.kind === "App Store");
  assert.ok(app, names("home assistant").join(", "));
  assert.equal(app.open.type, "store");
  // The App Store searched on the server, which matches more than the name.
  assert.ok(Search.hits("automation", { apps: [{ name: "Frigate", repo: "community", summary: "NVR", tags: ["automation"] }] }).length);
});

test("an exact name comes first, then a name that starts with it, then other matches", () => {
  const found = names("home-assistant");
  assert.equal(found[0], "Container:home-assistant");
  assert.ok(found.indexOf("Host:node-a") > found.indexOf("Container:home-assistant"), "a host that runs it comes after the app");
  assert.deepEqual(names("home").slice(0, 2), ["Container:home-assistant", "Host:homelab-gpu"]);
});

test("each result opens its own item", () => {
  assert.deepEqual(Search.hits("node-a", data)[0].open, { type: "node", name: "node-a" });
  assert.deepEqual(Search.hits("plex", data)[0].open, { type: "workload", name: "plex" });
  assert.deepEqual(Search.hits("plex-config", data)[0].open, { type: "volume", name: "plex-config" });
});

test("an empty search finds nothing", () => {
  assert.deepEqual(Search.hits("   ", data), []);
});

test("the search box is a combobox over a listbox, moved through with the arrow keys", () => {
  const html = fs.readFileSync("web/index.html", "utf8"), app = fs.readFileSync("web/js/app.js", "utf8");
  assert.match(html, /<input id="globalSearch"[^>]*role="combobox"[^>]*aria-controls="searchResults"/);
  assert.match(html, /<script src="\/js\/search\.js\?v=[^"]+" defer><\/script>/);
  assert.match(html, /<div id="searchResults"[^>]*role="listbox"/);
  assert.match(app, /ArrowDown/);
  assert.match(app, /aria-activedescendant/);
});

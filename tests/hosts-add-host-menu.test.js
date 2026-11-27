"use strict";
// The Hosts page's ⋯ menu can add a host, as the Cluster page's button does (#833).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

async function hostsHeader() {
  let painted = "";
  const c = { console, Set, Map, Promise, URLSearchParams, location: { search: "" }, document: { addEventListener() {} },
    STATE: { data: {}, platform: { distribution: "k3s", harvester: false } },
    $: () => null, $$: () => [], toast() {}, icon: name => `<i data-icon="${name}"></i>`,
    esc: v => String(v ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])),
    api: async path => path === "/api/nodes" ? [{ name: "node1" }] : { nodes: {} } };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js", "web/js/views-hosts.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  Object.assign(c, { loadHardwareFeatures: async () => {}, noteNodeUptime() {}, viewLayout: () => "cards", layoutSwitch: () => "",
    nodeCard: () => "", hostCard: () => "", nodeComparison: () => "", paint: html => { painted = html; } });
  await c.viewNodes();
  return painted.split('<details class="actionmenu headmenu">')[1].split("</details>")[0];
}

test("the Hosts page's ⋯ menu adds a host through the same guide as the Cluster page", async () => {
  const menu = await hostsHeader();
  const entry = [...menu.matchAll(/<button type="button"([^>]*)\n?\s*onclick="([^"]*)">([^<]*)<\/button>/g)].find(m => m[3] === "Add a host…");
  assert.ok(entry, "an Add a host… entry");
  assert.match(entry[2], /clusterOnboarding\(\)$/);
  // applyRole hides anything whose data-need the role does not reach.
  const RANK = { viewer: 0, operator: 1, admin: 2 }, need = entry[1].match(/data-need="(\w+)"/)?.[1];
  assert.deepEqual(["viewer", "operator", "admin"].filter(role => RANK[role] >= RANK[need]), ["admin"]);
});

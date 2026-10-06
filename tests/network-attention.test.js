"use strict";

/* Networking's Attention: one count of the items it lists - each with why
   and where - not a figure of one kind beside a line of another (#305). */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("web/js/views-network.js", "utf8");
const helpers = source.slice(source.indexOf("function networkAttention(data)"), source.indexOf("window.networkShow ="));
const ctx = { CSS: { escape: s => String(s) } };
vm.createContext(ctx);
vm.runInContext(helpers + "\nthis.networkAttention = networkAttention; this.networkAttentionCounts = networkAttentionCounts;", ctx);

const data = {
  addresses: { addresses: [{ ip: "192.0.2.250", state: "unrouted", reason: "k3s-3 answers, lab/n8n does not carry it" },
                           { ip: "192.0.2.251", state: "ok" }] },
  conflicts: [{ ip: "192.0.2.242", protocol: "TCP", port: 3100, owners: [{ namespace: "lab", service: "loki" }, { namespace: "monitoring", service: "loki" }] }],
  services: [{ namespace: "lab", name: "loki", health: "conflict" }, { namespace: "lab", name: "n8n", health: "unavailable", reason: "No ready endpoints" },
             { namespace: "lab", name: "plex", health: "healthy" }, { namespace: "kube-system", name: "traefik", health: "pending", system: true },
             // Its app is stopped on purpose: nothing to answer with, nothing to fix.
             { namespace: "lab", name: "rustdesk", health: "stopped", reason: "Its app is stopped" }],
};

test("each item says what, why and where; a conflicted Service is counted once, as its conflict", () => {
  const items = ctx.networkAttention(data);
  assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.kind))), ["address", "conflict", "service"]);
  assert.equal(items[1].what, "192.0.2.242:3100/TCP · lab/loki and monitoring/loki");
  assert.equal(items[1].where, 'tr[data-svc="lab/loki"]');
  assert.equal(items[2].why, "No ready endpoints");
});

test("one count, broken down by kind", () => {
  assert.equal(ctx.networkAttentionCounts(ctx.networkAttention(data)), "1 service unhealthy · 1 listener conflict · 1 address not reachable");
  assert.equal(ctx.networkAttentionCounts([]), "nothing needs attention");
});

test("the Overview counts what it lists, and every Services row can be found", () => {
  // The Overview lists every item, each opening its section; its line in the list counts them.
  assert.match(source, /const due = networkAttentionAll\(\)\.filter\(item => key === "overview" \|\| item\.section === key\)\.length;/);
  assert.match(source, /<tr data-svc="\$\{esc\(row\.namespace \+ "\/" \+ row\.name\)\}">/);
});

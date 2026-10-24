"use strict";
// A host's maintenance shown apart from its outages, on its card and its page (#535).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
function setup(uptime) {
  let html = "";
  const c = { console, Set, Map, Promise, document: { addEventListener() {} }, STATE: { data: { nodeUptime: { nodes: { node1: uptime } } } },
    $: () => ({ set innerHTML(v) { html = v; }, get innerHTML() { return html; } }), $$: () => [],
    esc: v => String(v ?? "").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), toast() {},
    remoteRow: () => null, fmtDay: t => `day${t}`, fmtAt: t => `at${t}`, fmtUp: s => `${s}s`, fmtAgo: s => `${s}s ago`,
    api: async () => ({ nodes: { node1: uptime } }) };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, html: () => html };
}
const day = (n, extra = {}) => ({ day: n, up: 100, maintenance: false, ...extra });
function uptime({ maintenance_s = 360, raw = 99.86, outside = 100, reason = "OS update", ongoing = false } = {}) {
  return {
    windows: { "24h": 100, "7d": 100, "30d": raw, "90d": raw },
    windows_excluding_maintenance: { "24h": 100, "7d": 100, "30d": outside, "90d": outside },
    maintenance_s: { "24h": 0, "7d": maintenance_s, "30d": maintenance_s, "90d": maintenance_s },
    days: [...Array.from({ length: 89 }, (_, i) => day(i)), day(89, { maintenance: maintenance_s > 0 })],
    outages: maintenance_s ? [{ start: 1000, end: 1360, down_s: 360, exact: true, ongoing, maintenance: reason }] : [],
    reboots: [{ t: 1100, planned: true }, { t: 5000, planned: false }],
    maintenance: maintenance_s ? [{ start: 1000, end: ongoing ? null : 1360, reason, job: "op-1", ongoing }] : [],
    since: 0,
  };
}

test("the card shows the share outside maintenance, says maintenance happened, and gives the raw share in its tip", () => {
  const t = setup(uptime());
  const card = t.c.nodeUptimeStrip({ name: "node1" });
  assert.match(card, />100%</);
  assert.match(card, /6m maintenance/);
  assert.match(card, /99\.86%/);
});

test("with no maintenance the card reads as before", () => {
  const t = setup(uptime({ maintenance_s: 0, raw: 100 }));
  const card = t.c.nodeUptimeStrip({ name: "node1" });
  assert.match(card, />100%</);
  assert.doesNotMatch(card, /maintenance/);
});

test("the page shows both shares, maintenance days and the log's own words", async () => {
  const t = setup(uptime());
  await t.c.nodeUptimePaint({ name: "node1", status: "Ready" });
  const page = t.html();
  assert.match(page, /outside maintenance/);
  assert.match(page, /class="uptime-maint"/);
  assert.match(page, /maintenance: OS update/);
  assert.match(page, /6m/);
  assert.match(page, /unexpected reboot/);
  assert.match(page, /rebooted \(planned\)/);
  assert.doesNotMatch(page, /was down/, "an outage inside maintenance is not shown as one");
});

test("maintenance still going says since when", async () => {
  const t = setup(uptime({ ongoing: true }));
  await t.c.nodeUptimePaint({ name: "node1", status: "NotReady" });
  assert.match(t.html(), /maintenance: OS update, since at1000/);
});

test("a reason is escaped", async () => {
  const t = setup(uptime({ reason: "<b>" }));
  await t.c.nodeUptimePaint({ name: "node1", status: "Ready" });
  assert.match(t.html(), /maintenance: &lt;b>/);
  assert.doesNotMatch(t.html(), /<b>,/);
});

test("a maintenance day with a real outage is painted by the outage, not hidden by the stripes", async () => {
  const u = uptime();
  u.days[89] = { day: 89, up: 70, maintenance: true, up_excluding_maintenance: 72 };
  const t = setup(u);
  await t.c.nodeUptimePaint({ name: "node1", status: "Ready" });
  assert.doesNotMatch(t.html(), /class="uptime-maint"/);
  assert.match(t.html(), /class="uptime-bad"/);
  assert.match(t.c.nodeUptimeStrip({ name: "node1" }), /class="uptime-bad"/);
});

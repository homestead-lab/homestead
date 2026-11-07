"use strict";
// Move several containers (#366): tick them, review them together, start one job.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const WL = [{ ns: "lab", name: "ha", nodes: ["k2"] }, { ns: "lab", name: "tiny", nodes: ["k2"] }, { ns: "media", name: "plex", nodes: ["k1"] }];
const PLAN = { rows: [{ id: "lab/ha", from: "k2", to: "k1", why: "", cpu_m: 500, mem_mb: 1024, reserves: true },
                      { id: "media/plex", from: "k1", to: "k1", why: "it already runs on k1", cpu_m: 0, mem_mb: 0, reserves: false }],
  moves: [{ id: "lab/ha", from: "k2", to: "k1" }], blocked: false, review_token: "1.abc",
  hosts: [{ name: "k1", eligible: true, metrics_available: true, used_gb: 2, capacity_gb: 16, projected_gb: 3, projected_percent: 19, over: false }] };

function setup({ plan = PLAN, reply = null } = {}) {
  const fields = {}, sent = [], notices = [];
  let html = "", pending = [];
  const field = id => fields[id] || (fields[id] = { innerHTML: "", value: "", checked: false });
  const c = { console, URLSearchParams, Map, Set, Promise, setTimeout: f => f(), clearTimeout() {}, document: { addEventListener() {} },
    STATE: { data: { wl: WL, nodes: [{ name: "k1", schedulable: true }, { name: "k2", schedulable: true }, { name: "k3", schedulable: false }] } },
    $: s => s === "#mbody" ? { get innerHTML() { return html; }, set innerHTML(v) { html = v; } } : field(s), $$: () => [],
    esc: v => String(v ?? "").replaceAll("<", "&lt;"), modal: (_, body) => { html = body; }, childModal: (_, body) => { html = body; },
    toast: m => notices.push(m), closeModal() {}, refresh() {}, platformShown: () => true,
    api: async (path, options) => {
      const body = options?.body ? JSON.parse(options.body) : null;
      sent.push({ path, body });
      if (path === "/api/workloads/rebalance/review") return typeof plan === "function" ? plan(body) : plan;
      if (reply) throw reply;
      return { operation: { id: "op1" } };
    } };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", ...require("./helpers/workloads-source").paths()]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, fields, sent, notices, html: () => html, field };
}

test("ticks survive a filter, and Tick all shown ticks only what is shown", async () => {
  const t = setup();
  t.c.moveSeveral();
  t.c.mvTick("lab/ha", true);
  t.field("#mv_q").value = "plex"; t.c.mvFilter();
  t.c.mvTickShown(true);
  assert.deepEqual([...t.c.MV.picked].sort(), ["lab/ha", "media/plex"]);
  assert.match(t.field("#mv_count").innerHTML, /2 ticked/);
});

test("the review sends exactly the ticked containers to one host", async () => {
  const t = setup();
  t.c.moveSeveral(["lab/ha", "media/plex"], "k1");
  await t.c.mvPlan();
  const review = t.sent.find(s => s.path === "/api/workloads/rebalance/review");
  assert.deepEqual(review.body.picks, [{ id: "lab/ha", to: "k1" }, { id: "media/plex", to: "k1" }]);
  assert.match(t.html(), /Moves 1 container to/);
  assert.match(t.html(), /already runs on k1/);
  assert.match(t.html(), /Move 1/);
});

test("a blocked row cannot be ticked, and an override changes one row's host", async () => {
  const t = setup();
  t.c.moveSeveral(["lab/ha", "media/plex"], "k1");
  await t.c.mvPlan();
  assert.match(t.html(), /value="media\/plex"[^>]*disabled/);
  t.c.mvOverride("lab/ha", "k3");
  await t.c.mvPlan();
  const last = t.sent.filter(s => s.path === "/api/workloads/rebalance/review").pop();
  assert.deepEqual(last.body.picks[0], { id: "lab/ha", to: "k3" });
});

test("Move starts one job with the review's token, only after the acknowledgement", async () => {
  const t = setup();
  t.c.moveSeveral(["lab/ha"], "k1");
  await t.c.mvPlan();
  await t.c.mvGo();
  assert.equal(t.sent.filter(s => s.path === "/api/workloads/rebalance").length, 0);
  t.field("#mvAck").checked = true;
  await t.c.mvGo();
  const go = t.sent.find(s => s.path === "/api/workloads/rebalance");
  assert.deepEqual(go.body, { picks: [{ id: "lab/ha", to: "k1" }], review_token: "1.abc", restart: true });
});

test("a late review does not replace a newer one", async () => {
  let release;
  const slow = new Promise(r => { release = r; });
  let calls = 0;
  const t = setup({ plan: () => (++calls === 1 ? slow : { ...PLAN, rows: [{ ...PLAN.rows[0], to: "k3" }], moves: [{ id: "lab/ha", from: "k2", to: "k3" }] }) });
  t.c.moveSeveral(["lab/ha"], "k1");
  const first = t.c.mvPlan();
  t.c.mvOverride("lab/ha", "k3");
  await t.c.mvPlan();
  release(PLAN);
  await first;
  assert.match(t.html(), /k2 → k3/);
});

test("with no hosts loaded yet, the review loads them and offers them", async () => {
  const t = setup();
  delete t.c.STATE.data.nodes;
  const api = t.c.api;
  t.c.api = async (path, options) => path === "/api/nodes" ? [{ name: "k1", schedulable: true }, { name: "k3", schedulable: true }] : api(path, options);
  await t.c.moveSeveral(["lab/ha"], "");
  await t.c.mvPlan();
  const review = t.sent.filter(s => s.path === "/api/workloads/rebalance/review").pop();
  assert.equal(review.body.picks[0].to, "k1");
  assert.match(t.html(), /<option value="k3"/);
});

test("the hosts table shows what is reserved, CPU too, and says when some reserve nothing", async () => {
  const plan = { ...PLAN, rows: [{ ...PLAN.rows[0], reserves: false }],
    hosts: [{ name: "k1", used_gb: 2, capacity_gb: 16, projected_gb: 3, projected_percent: 19, cpu_before_m: 500, cpu_after_m: 1000, cpu_total_m: 4000, over: false }] };
  const t = setup({ plan });
  t.c.moveSeveral(["lab/ha"], "k1");
  await t.c.mvPlan();
  assert.match(t.html(), /Reserved/);
  assert.match(t.html(), /1000m of 4000m CPU/);
  assert.match(t.html(), /reserve nothing/);
  assert.doesNotMatch(t.html(), /Memory after it starts/);
});

test("when nothing can move, the review does not say it moves some", async () => {
  const t = setup({ plan: { ...PLAN, moves: [], blocked: true, rows: [PLAN.rows[1]], hosts: [] } });
  t.c.moveSeveral(["media/plex"], "k1");
  await t.c.mvPlan();
  assert.doesNotMatch(t.html(), /Moves 0/);
  assert.match(t.html(), /None of these can move/);
});

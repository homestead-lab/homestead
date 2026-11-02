"use strict";
// Make an app host a server (#364 step 5): offered on k3s app hosts, reviewed first.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const PLAN = { node: "app-1", blockers: [], warnings: ["2 → 3 members: losing any one keeps the cluster up"],
  facts: { version: "v1.37.1+k3s1", server: "192.0.2.5", settings: ["--cluster-cidr", "10.42.0.0/16"], host_settings: ["--node-ip", "192.0.2.11"],
    members: ["server-1", "server-2"], replicas: 4 }, review_token: "review" };

function setup({ plan = PLAN, platform = { distribution: "k3s", harvester: false }, roles = ["worker"] } = {}) {
  let html = "";
  const sent = [], notices = [], ack = { checked: false };
  const c = { console, URLSearchParams, Map, Promise, document: { addEventListener() {} },
    STATE: { platform, data: { nodes: [{ name: "app-1", roles }, { name: "server-1", roles: ["control-plane", "etcd", "master"] }] } },
    $: key => key === "#promoteAck" ? ack : {}, esc: s => String(s ?? "").replaceAll("<", "&lt;"), childModal: (_, body) => { html = body; }, modal: (_, body) => { html = body; },
    toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      if (path === "/api/quorum") return { members: ["server-1", "server-2"], ready: ["server-1", "server-2"], total: 2, quorum_needs: 2, can_lose: 0 };
      if (path.startsWith("/api/node/impact")) return { workloads: [], stranded: [] };
      if (path.startsWith("/api/node/promote/plan")) return plan;
      sent.push({ path, body: JSON.parse(options.body) });
      return { ok: true, operation: { id: "op1" }, detail: "Making app-1 a server" };
    } };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const f of ["web/js/ui.js", ...require("./helpers/workloads-source").paths(), ...require("./helpers/lifecycle-source").files()]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, sent, notices, ack, html: () => html };
}

test("a k3s app host is offered Make server", async () => {
  const t = setup();
  await t.c.nodeActions("app-1");
  assert.match(t.html(), /nodePromoteReview\("app-1"\)[^]*Make server…/);
});

test("a server, an RKE2 host or a Harvester host is not offered it", async () => {
  for (const opts of [{ roles: ["control-plane", "etcd", "master"] }, { platform: { distribution: "rke2" } },
                      { platform: { distribution: "rke2", harvester: true } }]) {
    const t = setup(opts);
    await t.c.nodeActions("app-1");
    assert.doesNotMatch(t.html(), /nodePromoteReview/);
  }
});

test("the review lists warnings, settings and stages, and starts with the reviewed token", async () => {
  const t = setup();
  await t.c.nodePromoteReview("app-1");
  assert.match(t.html(), /2 → 3 members/);
  assert.match(t.html(), /<details class="ui-more"[^]*Settings[^]*--cluster-cidr/, "raw flags sit in Details");
  assert.match(t.html(), /Drain[^]*Reinstall[^]*etcd/);
  assert.doesNotMatch(t.html(), /disabled/);
  await t.c.nodePromoteStart("app-1");
  assert.deepEqual(t.sent, [], "nothing is sent before the outage is acknowledged");
  t.ack.checked = true;
  await t.c.nodePromoteStart("app-1");
  assert.deepEqual(t.sent, [{ path: "/api/node/promote", body: { node: "app-1", review_token: "review" } }]);
});

test("blockers disable the button", async () => {
  const t = setup({ plan: { ...PLAN, blockers: ["Homestead runs on app-1: move it first"] } });
  await t.c.nodePromoteReview("app-1");
  assert.match(t.html(), /Homestead runs on app-1/);
  assert.match(t.html(), /disabled/);
});

"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup(review) {
  let html = "", fail = false;
  const sent = [], notices = [], fields = { "#editCapacityConfirm": { checked: false }, "#editGo": {} };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {} },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      if (path === "/api/edit/preview") return review;
      if (path.startsWith("/api/node/power/plan")) return review;
      sent.push({ path, body: JSON.parse(options.body) });
      if (fail) throw new Error("Review expired");
      return { ok: true, name: "demo" };
    } };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js", "utf8"), c);
  return { c, fields, sent, notices, html: () => html, fail: () => { fail = true; } };
}
const review = { capacity_token: "token", capacity: { blocked: false, requires_confirmation: true, candidates: [],
  warnings: ["unknown <memory>"], additional: 1 } };

test("Edit requires acknowledgement and submits only the frozen reviewed edit", async () => {
  const t = setup(review), body = { ns: "lab", name: "demo", containers: [{ name: "one", memory: "1Gi" }], node: "host1" };
  await t.c.window.editReview(body);
  assert.match(t.html(), /unknown &lt;memory&gt;/);
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 0);
  body.containers[0].memory = "20Gi";
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].path, "/api/edit");
  assert.equal(t.sent[0].body.containers[0].memory, "1Gi");
  assert.equal(t.sent[0].body.node, "host1");
  assert.equal(t.sent[0].body.capacity_token, "token");
});

test("Edit fails closed for missing preview and hard blockers", async () => {
  for (const response of [{}, { ...review, capacity: { ...review.capacity, blocked: true } }]) {
    const t = setup(response);
    await t.c.window.editReview({ ns: "lab", name: "demo" });
    t.fields["#editCapacityConfirm"].checked = true;
    await t.c.window.confirmEdit();
    assert.equal(t.sent.length, 0);
  }
});

test("A refused edit requires another preview, not a replay", async () => {
  const t = setup(review);
  await t.c.window.editReview({ ns: "lab", name: "demo" });
  t.fail();
  t.fields["#editCapacityConfirm"].checked = true;
  await t.c.window.confirmEdit();
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(t.fields["#editGo"].textContent, "Review again");
});

test("Host review names budgets and local data with escaped content", async () => {
  const t = setup({ ready: false, blockers: ["Budget prevents eviction"], pods: 1, vms: [], workloads: [], volumes: [],
    maintenance: { budgets: [{ pod: "lab/<app>", budget: "lab/pdb", allowed: 0 }],
      local_storage: [{ pod: "lab/app", kind: "host-local path", source: "/<files>" }] } });
  await t.c.window.nodePowerReview("host1", "reboot");
  assert.match(t.html(), /Disruption budgets/);
  assert.match(t.html(), /0 disruption\(s\) allowed/);
  assert.match(t.html(), /Drain deletes emptyDir data/);
  assert.match(t.html(), /&lt;files&gt;/);
  assert.doesNotMatch(t.html(), /id="pw_execute"/);
});

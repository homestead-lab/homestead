"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup(response) {
  let html = "", result = { ok: true, created: ["one", "two"] };
  const sent = [], notices = [], fields = { "#composeCapacityConfirm": { checked: false }, "#composeApply": {},
    "#composeApplyResult": {}, "#composeCreate": {}, "#composeText": { value: "draft" } };
  const c = { window: {}, console, $: s => fields[s], document: {}, clearTimeout() {}, setTimeout() {},
    esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    toast: m => notices.push(m), closeModal() {}, go() {},
    childModal: (_, body) => { html = body; },
    api: async (path, options) => {
      if (path === "/api/compose/preview") return response;
      sent.push(JSON.parse(options.body)); return result;
    } };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/compose.js", "utf8"), c);
  return { c, fields, sent, notices, html: () => html, result: r => { result = r; } };
}
const review = { capacity_token: "token", capacity: { status: "fits", blocked: false, pods: 2,
  services: [{ name: "<one>", replicas: 1, pod_memory_gb: 2, pod_request_gb: 1, pod_cpu_request_percent: 20 }],
  nodes: [{ name: "<host>", metrics_available: false }], warnings: ["unknown <data>"], example: [], reasons: [] } };

test("Compose review shows unknowns and requires checked acknowledgement", async () => {
  const t = setup(review);
  await t.c.window.composeReview({ text: "original", variables: "private=value" });
  assert.match(t.html(), /unknown &lt;data&gt;/);
  assert.match(t.html(), /&lt;one&gt;/);
  assert.match(t.html(), /Live RAM unavailable/);
  assert.match(t.html(), /Proceed despite capacity warnings/);
  assert.match(t.html(), /estimates over 100%/);
  assert.doesNotMatch(t.html(), /private=value/);
  await t.c.window.composeConfirm();
  assert.equal(t.sent.length, 0);
});

test("Compose sends the frozen reviewed file, not later edits", async () => {
  const t = setup(review), body = { text: "original", variables: "one=two", namespace: "lab" };
  await t.c.window.composeReview(body);
  body.text = "changed";
  t.fields["#composeCapacityConfirm"].checked = true;
  await t.c.window.composeConfirm();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].text, "original");
  assert.equal(t.sent[0].capacity_token, "token");
  assert.equal(t.sent[0].confirm_capacity, true);
});

test("Missing or incomplete/blocked batch review cannot create", async () => {
  for (const r of [{}, { ...review, capacity: { ...review.capacity, status: "unknown", blocked: true } }]) {
    const t = setup(r);
    await t.c.window.composeReview({ text: "draft" });
    t.fields["#composeCapacityConfirm"].checked = true;
    await t.c.window.composeConfirm();
    assert.equal(t.sent.length, 0);
  }
});

test("An edit invalidates the review immediately, before debounce completes", async () => {
  const t = setup(review);
  await t.c.window.composeReview({ text: "old" });
  vm.runInContext("composeChanged()", t.c);
  t.fields["#composeCapacityConfirm"].checked = true;
  await t.c.window.composeConfirm();
  assert.equal(t.sent.length, 0);
  assert.equal(t.fields["#composeCreate"].disabled, true);
});

test("Partial creation is retained, named, and cannot be blindly replayed", async () => {
  const t = setup(review);
  t.result({ ok: false, created: ["one"], failed: "two", error: "no capacity" });
  await t.c.window.composeReview({ text: "draft" });
  t.fields["#composeCapacityConfirm"].checked = true;
  await t.c.window.composeConfirm();
  await t.c.window.composeConfirm();
  assert.equal(t.sent.length, 1);
  assert.match(t.fields["#composeApplyResult"].innerHTML, /Already created: one/);
  assert.match(t.fields["#composeApplyResult"].innerHTML, /Volumes created before an error are retained/);
  assert.equal(t.fields["#composeApply"].disabled, true);
});

test("Stale preview response cannot re-enable an edited batch", async () => {
  const t = setup(review);
  let resolve;
  t.c.api = () => new Promise(r => { resolve = r; });
  const request = t.c.window.composeReview({ text: "old" });
  vm.runInContext("composeChanged()", t.c);
  resolve(review);
  await request;
  assert.equal(t.html(), "");
});

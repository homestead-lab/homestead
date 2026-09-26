"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const review = { capacity_token: "signed", capacity: { blocked: false, warnings: [],
  move: { node: "<host>", mode: "preferred", target: { blocked: false } } } };
function setup(response = review) {
  let html = "", fail = false;
  const sent = [], fields = { "#hostMoveConfirm": { checked: false }, "#hostMoveGo": {} };
  const c = { window: {}, console, $: s => fields[s], toast() {}, closeModal() {}, setTimeout() {},
    esc: v => String(v).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, deployCapacityHtml: () => "capacity report",
    api: async (url, options) => {
      if (url === "/api/move/preview") return response;
      sent.push(JSON.parse(options.body));
      if (fail) throw new Error("review expired");
      return { ok: true };
    } };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/move.js", "utf8"), c);
  return { c, sent, fields, html: () => html, fail: () => { fail = true; } };
}

test("Move review explains downtime and escapes the destination; consent required", async () => {
  const t = setup();
  await t.c.window.hostMoveReview({ ns: "lab", name: "app", node: "b", pin: false });
  assert.match(t.html(), /&lt;host&gt;/);
  assert.match(t.html(), /Clearing a preference can also restart/);
  assert.match(t.html(), /full desired replica count/);
  assert.match(t.html(), /Proceed despite capacity warnings/);
  await t.c.window.confirmHostMove();
  assert.equal(t.sent.length, 0);
});

test("Move submission uses the reviewed input and token, not changed selection", async () => {
  const t = setup(), body = { ns: "lab", name: "app", node: "b", pin: false };
  await t.c.window.hostMoveReview(body);
  body.node = "c";
  t.fields["#hostMoveConfirm"].checked = true;
  await t.c.window.confirmHostMove();
  await t.c.window.confirmHostMove();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].node, "b");
  assert.equal(t.sent[0].capacity_token, "signed");
  assert.equal(t.sent[0].confirm_capacity, true);
});

test("Missing or blocked move review cannot submit", async () => {
  for (const response of [{}, { ...review, capacity: { ...review.capacity, blocked: true } }]) {
    const t = setup(response);
    await t.c.window.hostMoveReview({ name: "app" });
    t.fields["#hostMoveConfirm"].checked = true;
    await t.c.window.confirmHostMove();
    assert.equal(t.sent.length, 0);
  }
});

test("Changing placement invalidates pending preview responses", async () => {
  const t = setup();
  let resolve;
  t.c.api = () => new Promise(r => { resolve = r; });
  const pending = t.c.window.hostMoveReview({ name: "old" });
  vm.runInContext("invalidateHostMove()", t.c);
  resolve(review);
  await pending;
  assert.equal(t.html(), "");
});

test("Failed move requires a new review, not replay", async () => {
  const t = setup();
  t.fail();
  await t.c.window.hostMoveReview({ name: "app" });
  t.fields["#hostMoveConfirm"].checked = true;
  await t.c.window.confirmHostMove();
  await t.c.window.confirmHostMove();
  assert.equal(t.sent.length, 1);
  assert.equal(t.fields["#hostMoveGo"].textContent, "Review again");
  assert.equal(typeof t.fields["#hostMoveGo"].onclick, "function");
});

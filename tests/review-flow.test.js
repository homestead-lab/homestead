"use strict";

/* UI.reviewFlow() (#619): one review-then-apply flow for every dialog that
   previews, shows, asks for an acknowledgement and sends once. The first
   tests pin the VM power dialogs word for word, so moving them onto the
   shared flow changes nothing a person sees; the rest test the flow. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function power({ blocked = false, missing = false, fail = false, warnings = ["High RAM"], stateInit = false, dialogOpen = true } = {}) {
  const sent = [], toasts = [], asked = [], opened = [];
  const fields = { "#modal": { classList: { contains: () => !dialogOpen } }, "#vmPowerApprove": { checked: false }, "#vmPowerApply": {}, ".modalbox": { scrollTop: 300 } };
  const body = { _html: "", get innerHTML() { return this._html; }, set innerHTML(html) { this._html = html; delete fields["#vmPowerLoading"]; },
    insertAdjacentHTML(_where, html) { this._html = html + this._html; } };
  fields["#mbody"] = body;
  const open = which => (title, html) => { opened.push([which, title, html]); body.innerHTML = html; if (html.includes('id="vmPowerLoading"')) fields["#vmPowerLoading"] = {}; };
  const ctx = { console, Map, Set, Date, Promise, encodeURIComponent, URLSearchParams,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} },
    $: key => fields[key], $$: () => [], esc: v => String(v).replaceAll("&", "&amp;").replaceAll("<", "&lt;"),
    modal: open("modal"), childModal: open("childModal"), modalBack() {}, refresh() {}, setTimeout: fn => fn(),
    ask: async (text, options) => { asked.push([text, JSON.parse(JSON.stringify(options))]); return true; },
    toast: (text, tone) => toasts.push([text, tone]),
    api: async (path, options) => {
      sent.push({ path, body: JSON.parse(options.body) });
      if (path.endsWith("/preview")) return { capacity_token: "review-token", capacity: missing ? null : { blocked,
        pod_request_gb: 4.25, pod_cpu_request_percent: 10, additional: 1, candidates: [], placement: {}, concerns: [], unbounded: [],
        warnings, blockers: blocked ? ["No host has the GPU"] : [],
        vm: { guest_memory_gb: 4, policy_before: "Halted", policy_after: "Always",
          state_initialization: stateInit ? { name: "guest", reason: "No state volume exists yet." } : null } } };
      if (fail) throw new Error("lost response");
      return { ok: true, detail: "starting guest" };
    } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(require("./helpers/workloads-source")(), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  return { ctx, fields, sent, toasts, asked, opened, html: () => body.innerHTML };
}

test("pinned: Shut down and Force off ask in the same words, and Pause does not ask", async () => {
  const t = power();
  await t.ctx.vmPower("lab", "guest", "stop");
  await t.ctx.vmPower("lab", "guest", "force-stop");
  await t.ctx.vmPower("lab", "guest", "pause");
  assert.deepEqual(t.asked, [
    ["Shut down guest? The guest is asked to shut down, as pressing its power button does; it stays off until you start it.", { title: "Shut down", ok: "Shut down" }],
    ["Force off guest? The power is cut at once - the guest is not asked to shut down, so unsaved work in it is lost. Shut down asks it first.", { title: "Force off", ok: "Force off", danger: true }]]);
  assert.deepEqual(t.sent.map(s => [s.path, s.body.action]), [["/api/vm/power", "stop"], ["/api/vm/power", "force-stop"], ["/api/vm/power", "pause"]]);
  assert.deepEqual(t.toasts, [["starting guest", "ok"], ["starting guest", "ok"], ["starting guest", "ok"]]);
});

test("pinned: the review opens in the same dialog with the same title and loading line", async () => {
  for (const [action, title] of [["start", "Start · guest"], ["restart", "Restart · guest"], ["unpause", "Resume · guest"]]) {
    for (const dialogOpen of [true, false]) {
      const t = power({ dialogOpen });
      await t.ctx.vmPower("lab", "guest", action);
      assert.deepEqual(t.opened.map(o => o.slice(0, 2)), [[dialogOpen ? "childModal" : "modal", title]]);
      assert.equal(t.opened[0][2], '<div id="vmPowerLoading" class="empty"><span class="spin2"></span>Checking VM, storage and host capacity…</div>');
      assert.deepEqual(t.sent.map(s => [s.path, s.body]), [["/api/vm/power/preview", { ns: "lab", name: "guest", action }]]);
    }
  }
});

test("pinned: the review reads the same, with its acknowledgement, buttons and restart policy", async () => {
  const t = power();
  await t.ctx.vmPower("lab", "guest", "restart");
  const html = t.html();
  assert.match(html, /Restarts <b>guest<\/b>/);
  assert.match(html, / The guest stops and starts again\. Its restart policy changes from Halted to <b>Always<\/b>\. <span class="ui-help">4 GiB guest memory\.<\/span>/);
  assert.match(html, /id="vmPowerApprove"[^>]*onchange="vmPowerReviewReady\(\)"/);
  assert.match(html, /Restart it anyway/);
  assert.match(html, /<div class="ui-actions-start"><button data-dialog-dismiss="true" class="btn" onclick="modalBack\(\)">Cancel<\/button><\/div><div class="ui-actions-end"><button class="btn pri" id="vmPowerApply" onclick="vmPowerReviewedApply\(\)">Restart<\/button>/);
  assert.equal(t.fields["#vmPowerApply"].disabled, true, "nothing is sent until the warning is accepted");
  await t.ctx.vmPowerReviewedApply();
  assert.deepEqual(t.toasts, [["Tick Restart it anyway to accept the warning", "bad"]]);
  t.fields["#vmPowerApprove"].checked = true;
  assert.equal(t.ctx.vmPowerReviewReady(), true);
  assert.equal(t.fields["#vmPowerApply"].disabled, false);
  await t.ctx.vmPowerReviewedApply();
  assert.equal(t.fields["#vmPowerApply"].textContent, "Sending…");
  assert.deepEqual(t.sent.at(-1), { path: "/api/vm/power", body: { ns: "lab", name: "guest", action: "restart", capacity_token: "review-token", confirm_capacity: true } });
  assert.deepEqual(t.toasts.at(-1), ["starting guest", "ok"]);
});

test("pinned: fresh state asks for its own tick and the typed VM name", async () => {
  const t = power({ stateInit: true });
  await t.ctx.vmPower("lab", "guest", "start");
  assert.match(t.html(), /<b>Initialize fresh VM state\?<\/b>/);
  assert.match(t.html(), /No state volume exists yet\. Cancel and inspect the original state volume and backups if this VM has run before\./);
  assert.match(t.html(), /id="vmPowerStateAck" onchange="vmPowerReviewReady\(\)"> I intend to initialize fresh state, not recover the original TPM\/EFI or backup state/);
  t.fields["#vmPowerApprove"].checked = true;
  t.fields["#vmPowerStateAck"] = { checked: true }; t.fields["#vmPowerStateName"] = { value: "gues", dataset: { typedExpect: "guest" } };
  await t.ctx.vmPowerReviewedApply();
  assert.equal(t.sent.length, 1, "a wrong name sends nothing");
  t.fields["#vmPowerStateName"].value = "guest";
  await t.ctx.vmPowerReviewedApply();
  assert.deepEqual(t.sent.at(-1).body, { ns: "lab", name: "guest", action: "start", ack_state_initialization: true, confirm_state_name: "guest", capacity_token: "review-token", confirm_capacity: true });
});

test("pinned: a blocked review has no apply button, and a missing one says nothing was sent", async () => {
  let t = power({ blocked: true });
  await t.ctx.vmPower("lab", "guest", "start");
  assert.doesNotMatch(t.html(), /id="vmPowerApply"/);
  assert.match(t.html(), /No host has the GPU/);
  t = power({ missing: true });
  await t.ctx.vmPower("lab", "guest", "start");
  assert.ok(t.html().startsWith('<div class="note bad">VM capacity review unavailable. Nothing was sent; refresh before continuing.</div><div class="ui-actions" >'), t.html());
  assert.match(t.html(), /<div class="ui-actions-start"><button data-dialog-dismiss="true" class="btn" onclick="modalBack\(\)">Close<\/button><\/div><div class="ui-actions-end"><\/div><\/div>$/);
});

test("pinned: a failed send explains the uncertain outcome and is not repeated", async () => {
  const t = power({ fail: true });
  await t.ctx.vmPower("lab", "guest", "start");
  t.fields["#vmPowerApprove"].checked = true;
  await t.ctx.vmPowerReviewedApply();
  assert.equal(t.fields["#vmPowerApply"].textContent, "Inspect VM before retrying");
  assert.ok(t.html().startsWith('<div class="note bad">lost response. The request was not repeated. Its outcome may be uncertain: inspect the VM and its Recent jobs entry before trying again. Shut down and Force off remain available.</div>'));
  assert.equal(t.fields[".modalbox"].scrollTop, 0);
  assert.deepEqual(t.toasts, [["lost response", "bad"]]);
});

test("pinned: while a start is being sent, another start is refused but Force off is not", async () => {
  const t = power();
  await t.ctx.vmPower("lab", "guest", "start");
  t.fields["#vmPowerApprove"].checked = true;
  const original = t.ctx.api;
  let finish;
  t.ctx.api = (path, options) => JSON.parse(options.body).action === "start" && !path.endsWith("/preview") ? new Promise(resolve => finish = resolve) : original(path, options);
  const pending = t.ctx.vmPowerReviewedApply();
  await t.ctx.vmPower("lab", "guest", "restart");
  assert.deepEqual(t.toasts, [["A VM power request is being sent; wait for its result", "bad"]]);
  await t.ctx.vmPowerReview({ ns: "lab", name: "guest", action: "start" });
  assert.equal(t.opened.length, 1, "no second review opens while one is sent");
  await t.ctx.vmPower("lab", "guest", "force-stop");
  assert.equal(t.sent.at(-1).body.action, "force-stop");
  finish({ ok: true, detail: "accepted" });
  await pending;
  await t.ctx.vmPower("lab", "guest", "restart");
  assert.equal(t.opened.length, 2, "once the result is in, a new review opens");
});

/* The shared flow itself. */
function flowContext() {
  const fields = { "#ack": null, "#go": { disabled: false } };
  // No $ here: ui.js does not depend on core.js being loaded first.
  const ctx = { esc: String, document: { getElementById: id => fields["#" + id] ?? null } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  return { UI: ctx.UI, fields };
}
const later = () => { let resolve, reject; const promise = new Promise((y, n) => { resolve = y; reject = n; }); return { promise, resolve, reject }; };

test("UI.reviewFlow shows only the newest preview, and only while its dialog is open", async () => {
  const { UI } = flowContext(), shown = [], failed = [], previews = {};
  let live = true;
  const flow = UI.reviewFlow({ preview: input => (previews[input] = later()).promise,
    loading: input => shown.push(["loading", input]), live: input => { shown.push(["live", input]); return live; }, render: (review, input) => shown.push(["render", review, input]),
    failed: (error, input) => failed.push([error.message, input]), apply: async () => {} });
  const first = flow.open("a"), second = flow.open("b");
  previews.a.resolve({ token: "a" }); await first;
  assert.equal(flow.review, null, "an older preview is dropped");
  previews.b.resolve({ token: "b" }); await second;
  assert.deepEqual(shown, [["loading", "a"], ["loading", "b"], ["live", "b"], ["render", { token: "b" }, "b"]]);
  shown.length = 0;
  assert.deepEqual(flow.review, { token: "b" });
  const third = flow.open("c");
  assert.equal(flow.review, null, "opening again forgets the last review");
  live = false;
  previews.c.reject(new Error("gone")); await third;
  assert.deepEqual(failed, [], "a closed dialog shows nothing");
  live = true;
  const fourth = flow.open("d");
  previews.d.reject(new Error("refused")); await fourth;
  assert.deepEqual(failed, [["refused", "d"]]);
  assert.equal(flow.review, null);
});

test("UI.reviewFlow is ready only with a review, not blocked, not busy, acknowledged and with its own checks", async () => {
  const { UI, fields } = flowContext();
  let extra = true;
  const flow = UI.reviewFlow({ preview: async () => ({ blocked: false }), render() {}, blocked: review => review.blocked,
    ack: "ack", button: "go", check: () => extra, apply: async () => {} });
  assert.equal(flow.ready(), false);
  assert.equal(fields["#go"].disabled, true);
  await flow.open();
  assert.equal(flow.ready(), true, "no tickbox means nothing to accept");
  assert.equal(fields["#go"].disabled, false);
  fields["#ack"] = { checked: false };
  assert.equal(flow.ready(), false);
  fields["#ack"].checked = true;
  assert.equal(flow.ready(), true);
  extra = false;
  assert.equal(flow.ready(), false);
  extra = true;
  const blocked = UI.reviewFlow({ preview: async () => ({ blocked: true }), render() {}, blocked: review => review.blocked, apply: async () => {} });
  await blocked.open();
  assert.equal(blocked.ready(), false);
});

test("UI.reviewFlow sends a review once, stays busy while it is sent, and Back forgets it", async () => {
  const { UI } = flowContext(), sent = [], settled = [];
  let reply;
  const flow = UI.reviewFlow({ preview: async input => ({ input }), render() {},
    apply: review => { sent.push(review.input); reply = later(); return reply.promise; }, after: () => settled.push(flow.busy) });
  await flow.open("x");
  const pending = flow.apply();
  assert.equal(flow.busy, true);
  assert.equal(flow.review, null, "the review is used up before the reply");
  assert.equal(flow.ready(), false);
  await flow.open("y");
  assert.equal(flow.review, null, "no new review opens while one is sent");
  assert.equal(flow.back(), false, "Back does nothing while a review is sent");
  reply.reject(new Error("lost"));
  await assert.rejects(pending, /lost/);
  assert.equal(flow.busy, false);
  assert.deepEqual(settled, [false], "after runs once the send settles, failed or not");
  await flow.apply();
  assert.deepEqual(sent, ["x"], "a used review is never sent again");
  await flow.open("z");
  flow.back();
  assert.equal(flow.review, null);
  await flow.apply();
  assert.deepEqual(sent, ["x"]);
  const slow = later(), late = UI.reviewFlow({ preview: () => slow.promise, render: review => sent.push(["late", review]), apply: async () => {} });
  const opening = late.open();
  late.back();
  slow.resolve({}); await opening;
  assert.deepEqual(sent, ["x"], "Back drops a preview still on its way");
});

test("UI.reviewFlow hands a 409 from apply to conflict(), once it is no longer busy", async () => {
  const { UI } = flowContext(), seen = [];
  const conflictError = Object.assign(new Error("changed since the review"), { status: 409 });
  let failWith = conflictError;
  const flow = UI.reviewFlow({ preview: async input => ({ input }), render() {},
    apply: async () => { throw failWith; },
    conflict: async (error, review) => { seen.push([error.message, review.input, flow.busy]); await flow.open(review.input); } });
  await flow.open("x");
  await flow.apply();
  assert.deepEqual(seen, [["changed since the review", "x", false]]);
  assert.deepEqual(flow.review, { input: "x" }, "conflict() can open a fresh review");
  failWith = Object.assign(new Error("refused"), { status: 403 });
  await assert.rejects(flow.apply(), /refused/, "any other error still reaches the caller");
  const plain = UI.reviewFlow({ preview: async () => ({}), render() {}, apply: async () => { throw conflictError; } });
  await plain.open();
  await assert.rejects(plain.apply(), /changed since/, "without conflict() a 409 is an error like any other");
});

test("UI.reviewFlow leaves a 409 to the caller once its review is no longer on screen", async () => {
  const { UI, fields } = flowContext(), seen = [];
  let reply;
  const flow = UI.reviewFlow({ preview: async () => ({}), render() {}, button: "go",
    apply: () => (reply = later()).promise, conflict: () => seen.push("conflict") });
  await flow.open();
  const sending = flow.apply();
  fields["#go"] = null; // Back, or the dialog closed
  reply.reject(Object.assign(new Error("changed since the review"), { status: 409 }));
  await assert.rejects(sending, /changed since/);
  assert.deepEqual(seen, [], "conflict() does not draw over another dialog");
  await flow.open();
  const replaced = flow.apply();
  fields["#go"] = { disabled: false }; // another review's button
  reply.reject(Object.assign(new Error("changed since the review"), { status: 409 }));
  await assert.rejects(replaced, /changed since/);
  assert.deepEqual(seen, []);
});

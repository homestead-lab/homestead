"use strict";
// Start on another host (#441): the review dialog and its run.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const PLAN = { ready: true, blockers: [], node: "node2", down_since: "2026-10-17T10:00:00Z", down_seconds: 1500,
  last_heard: "2026-10-17T09:59:40Z", set: { ns: "data", name: "postgres", wait_mode: false },
  pods: [{ name: "postgres-0", uid: "u0", terminating: false }, { name: "postgres-1", uid: "u1", terminating: true }],
  volumes: [{ pvc: "data-postgres-0", volume: "pvc-aaa", copy_on: "node1" }], review_token: "tok1" };

function setup({ plan = PLAN, post = null } = {}) {
  const fields = { "#mbody": { innerHTML: "" }, "#mtitle": { textContent: "" }, "#sr_confirm": { value: "postgres", dataset: { typedExpect: "postgres" } },
    "#sr_run": { disabled: false, textContent: "Start on another host", onclick: null } };
  const calls = [], toasts = [], requests = [];
  const ctx = { console, Map, Set, Promise, JSON, URLSearchParams, setTimeout, encodeURIComponent, document: { addEventListener() {} },
    STATE: { data: {} }, $: key => fields[key], $$: () => [], can: () => true, tip: t => t,
    toast: (m, k) => toasts.push([m, k]),
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    jsArg: value => JSON.stringify(String(value)),
    modal: (title, html) => { fields["#mbody"].innerHTML = html; fields["#mtitle"].textContent = title; },
    applyRole: () => calls.push(["applyRole", fields["#mbody"].innerHTML, fields["#sr_run"].textContent]),
    closeModal: () => calls.push(["closeModal"]), refresh: () => calls.push(["refresh"]),
    noteOperation: op => calls.push(["noteOperation", op.id]), refreshOperations: () => {},
    api: async (url, opts) => {
      requests.push([url, opts]);
      if (!opts) { if (plan instanceof Error) throw plan; return plan; }
      if (post instanceof Error) throw post;
      return post;
    } };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(fs.readFileSync("web/js/set-restart.js", "utf8"), ctx);
  return { ctx, fields, calls, toasts, requests, html: () => fields["#mbody"].innerHTML };
}

test("the review shows each check as a fact, every pod and volume, and the typed confirmation", async () => {
  const t = setup();
  await t.ctx.setRestartReview("data", "postgres", "node2");
  const html = t.html();
  assert.match(html, /Not ready since/);
  assert.match(html, /25 min/);
  assert.match(html, /Last heard from/);
  assert.match(html, /postgres-0/); assert.match(html, /postgres-1/);
  assert.match(html, /being deleted; made final/);
  assert.match(html, /data-postgres-0/); assert.match(html, /current copy is on[^]*node1/);
  assert.match(html, /data-typed-expect="postgres"/);
  assert.match(html, /id="sr_run"[^>]*disabled/);
  assert.match(html, /data-need="admin"/);
  assert.match(html, /stuck pod is removed; the StatefulSet starts it again on another host/i);
  assert.match(html, /about 6 minutes/);
  assert.doesNotMatch(html, /overrides that once/);
  assert.match(t.requests[0][0], /^\/api\/statefulset\/restart-elsewhere\/plan\?/);
  assert.match(t.requests[0][0], /ns=data&name=postgres&node=node2/);
});

test("the review says plainly what it relied on when Longhorn reported unknown", async () => {
  const relied = "Longhorn reports the engine of pvc-aaa on node2 as unknown, as it does for a host that is down.";
  const t = setup({ plan: { ...PLAN, relied_on: [relied, "<b>x</b>"] } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  const html = t.html();
  assert.match(html, /Relied on/);
  assert.ok(html.includes(relied), html);
  assert.match(html, /&lt;b&gt;x&lt;\/b&gt;/);
  assert.doesNotMatch(html, /no engine of these volumes running/);
});

test("a review without anything relied on shows no such row", async () => {
  const t = setup();
  await t.ctx.setRestartReview("data", "postgres", "node2");
  assert.doesNotMatch(t.html(), /Relied on/);
  assert.match(t.html(), /running none of these volumes/);
});

test("a set that waits by its own setting says this overrides that once", async () => {
  const t = setup({ plan: { ...PLAN, set: { ...PLAN.set, wait_mode: true } } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  assert.match(t.html(), /this app is set to wait for its host to come back; this overrides that once/i);
});

test("a blocked plan shows the blockers in a bad callout and no run button", async () => {
  const t = setup({ plan: { ready: false, blockers: ["Longhorn still sees node2 as up", "<b>x</b>"] } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  const html = t.html();
  assert.match(html, /ui-callout bad/);
  assert.match(html, /Longhorn still sees node2 as up/);
  assert.doesNotMatch(html, /<b>x<\/b>/);
  assert.doesNotMatch(html, /sr_run/);
  assert.doesNotMatch(html, /data-typed-expect/);
});

test("a plan that cannot be read toasts and opens nothing", async () => {
  const t = setup({ plan: new Error("boom") });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  assert.equal(t.toasts[0][1], "bad"); assert.equal(t.html(), "");
});

test("running posts the typed name and the review token, then follows the job", async () => {
  const t = setup({ post: { id: "op1", not_deleted: [{ name: "postgres-1", error: "gone" }] } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  const body = JSON.parse(t.requests[1][1].body);
  assert.deepEqual(body, { ns: "data", name: "postgres", node: "node2", confirm: "postgres", review_token: "tok1" });
  assert.deepEqual(t.calls.find(c => c[0] === "noteOperation"), ["noteOperation", "op1"]);
  assert.ok(t.toasts.some(([m]) => /postgres-1/.test(m) && /gone/.test(m)), "names the pod not deleted");
});

test("nothing is sent when the typed name does not match", async () => {
  const t = setup({ post: { id: "op1" } });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  t.fields["#sr_confirm"].value = "nope";
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.equal(t.requests.length, 1);
});

test("a 409 turns the button into Review again, which reopens the review", async () => {
  const err = Object.assign(new Error("The review changed"), { status: 409, body: { error: "The review changed", plan: PLAN } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.equal(t.fields["#sr_run"].textContent, "Review again");
  assert.equal(t.fields["#sr_run"].disabled, false);
  t.fields["#sr_run"].onclick();
  await new Promise(r => setTimeout(r, 0));
  assert.equal(t.requests.filter(([, o]) => !o).length, 2);
});

// The server's 409 replies carry the job as {"id": ...} only (set_restart._run).
test("a 409 that names a running operation follows it instead", async () => {
  const err = Object.assign(new Error("already running"), { status: 409, body: { error: "already running", operation: { id: "op9" } } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.equal(t.fields["#mtitle"].textContent, "Starting elsewhere · postgres");
  assert.ok(!t.calls.some(c => c[0] === "noteOperation"), "a bare id is not put into Recent jobs as if it were a job");
  assert.notEqual(t.fields["#sr_run"].textContent, "Review again");
});

test("a 409 after deletes follows the other job and names the pods not deleted", async () => {
  const err = Object.assign(new Error("Pods were deleted"), { status: 409, body: { error: "Pods were deleted",
    deleted: [{ name: "postgres-0" }], operation: { id: "op7" }, not_deleted: [{ name: "postgres-1", error: "forbidden" }] } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  assert.equal(t.fields["#mtitle"].textContent, "Starting elsewhere · postgres");
  assert.ok(t.toasts.some(([m, k]) => k === "warn" && /postgres-1 \(forbidden\)/.test(m)), "names the pod not deleted");
});

test("the role is applied after the review is drawn, so only admins see the run button", async () => {
  const t = setup();
  await t.ctx.setRestartReview("data", "postgres", "node2");
  const applied = t.calls.filter(c => c[0] === "applyRole");
  assert.equal(applied.length, 1);
  assert.match(applied[0][1], /id="sr_run"[^>]*data-need="admin"/);
});

test("the role is applied again when the button becomes Review again", async () => {
  const err = Object.assign(new Error("The review changed"), { status: 409, body: { error: "x", plan: PLAN } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  const applied = t.calls.filter(c => c[0] === "applyRole");
  assert.equal(applied.length, 2);
  assert.equal(applied[1][2], "Review again");
});

test("a 502 names the pods that could not be deleted and offers Review again", async () => {
  const err = Object.assign(new Error("Nothing could be deleted"), { status: 502,
    body: { error: "Nothing could be deleted", not_deleted: [{ name: "postgres-0", error: "forbidden" }] } });
  const t = setup({ post: err });
  await t.ctx.setRestartReview("data", "postgres", "node2");
  await t.ctx.setRestartRun("data", "postgres", "node2");
  const [message, kind] = t.toasts.at(-1);
  assert.equal(kind, "bad");
  assert.match(message, /postgres-0 \(forbidden\)/);
  assert.equal(t.fields["#sr_run"].textContent, "Review again");
});

// The demo's own routes (web/js/demo.js) for this action.
async function demoApi(search = "", flags = {}) {
  const ctx = { URL, URLSearchParams, Response, Date, console, setTimeout: () => 0,
    location: { search: `?demo=1&demo-scenario=critical${search}`, origin: "https://demo.example.com" },
    document: { body: null, addEventListener() {} }, ...flags,
    fetch: async () => { throw new Error("demo must never reach a real cluster"); } };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/demo.js", "utf8"), ctx);
  return async (path, body) => (await ctx.fetch(path, body ? { method: "POST", body: JSON.stringify(body) } : undefined)).json();
}

test("the demo job matches the server's kind and title, with the attach note", async () => {
  const api = await demoApi();
  const op = await api("/api/statefulset/restart-elsewhere", { ns: "data", name: "postgres", node: "harvester-node3", confirm: "postgres", review_token: "demo-set-restart" });
  assert.equal(op.kind, "set-restart");
  assert.equal(op.title, "Start postgres on another host");
  assert.match(op.message, /postgres-0: deleted; Kubernetes can take about 6 minutes to release the volume from harvester-node3/);
});

test("the demo can show a blocked review, by URL or by flag", async () => {
  for (const api of [await demoApi("&set-restart=blocked"), await demoApi("", { __demoSetRestartBlocked: true })]) {
    const plan = await api("/api/statefulset/restart-elsewhere/plan?ns=data&name=postgres&node=harvester-node3");
    assert.equal(plan.ready, false);
    assert.ok(plan.blockers.length >= 1);
    assert.equal(plan.review_token, undefined);
  }
  assert.equal((await (await demoApi())("/api/statefulset/restart-elsewhere/plan?ns=data&name=postgres&node=harvester-node3")).ready, true);
});

"use strict";
// Removing a dead host that has a saved disk job: the standard way is refused,
// and gone for good needs the person to accept that the job is left behind.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const JOB = { id: "job-1", kind: "disk-allocation", title: "Grow storage on <node3>",
  text: "node3 has a saved storage allocation job (Grow storage on <node3>). Open that job first; removing the host would leave it with nothing to finish on." };
// What leaving them means comes from the server, per kind (homestead_onboard.stays).
const STAY = "The saved job stays, and keeps blocking Longhorn upgrades, shutdown and OS rollouts until you end it from Jobs once the host is removed.";
const plan = (jobs = [JOB], stay = STAY) => ({ node: "node3", ready: false, roles: [], ok: true, blockers: [], warnings: [],
  lost_volumes: [], rebuilt_volumes: [], lost_detail: [], pinned_volumes: [], pinned_workloads: [], disk_jobs: jobs, disk_jobs_stay: stay,
  stuck: { pods: 0, vms: [], attachments: 0, replicas: 0 },
  steps: ["Stop", "Delete node", "Delete machine", "Delete Longhorn record"],
  gone_steps: ["pods", "vms", "attachments", "replicas", "machine", "No apps", "No volumes"] });

function setup(review = plan()) {
  const sent = [], body = { innerHTML: "" }, mode = { value: "standard" };
  // The page's controls, made when the dialog's HTML names them.
  const controls = {};
  const control = id => {
    if (!body.innerHTML.includes(`id="${id}"`)) return null;
    const label = { hidden: false };
    return controls[id] ??= { id, checked: false, disabled: false, textContent: "", innerHTML: "", closest: () => label };
  };
  const c = { console, setTimeout() {}, toast() {}, closeModal() {}, modalBack() {},
    esc: v => String(v).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    tip: () => "", ask: async () => true,
    childModal: (_, html) => { body.innerHTML = html; },
    $: s => s === "#mbody" ? body : s === 'input[name="rm_mode"]:checked' ? mode : control(s.slice(1)),
    api: async (url, options) => {
      if (url.startsWith("/api/cluster/removal")) return review;
      if (url === "/api/cluster/remove-node") { sent.push(JSON.parse(options.body)); return { ok: true, log: [] }; }
      return { dead_nodes: [], stale_machines: [], stale_longhorn: [], passwords: [] };
    } };
  c.window = c;   // as in the browser: what the page puts on window is global
  vm.createContext(c); require("./helpers/load-ui")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/onboard.js", "utf8"), c);
  const choose = value => { mode.value = value; c.window.nodeRemovalMode(); };
  return { c, sent, body, controls, choose };
}

test("The review lists the host's saved disk jobs, escaped", async () => {
  const t = setup();
  await t.c.window.nodeRemoval("node3");
  assert.match(t.body.innerHTML, /saved storage allocation job \(Grow storage on &lt;node3&gt;\)/);
  assert.doesNotMatch(t.body.innerHTML, /<node3>/);
});

test("A saved disk job refuses the standard removal", async () => {
  const t = setup();
  await t.c.window.nodeRemoval("node3");
  t.choose("standard");
  assert.equal(t.controls.rm_go.disabled, true);
  assert.equal(t.controls.rm_jobs.closest("label").hidden, true, "nothing to accept: the standard way waits");
  t.controls.rm_jobs.checked = true;   // even a stale tick from gone for good does not open it
  t.c.window.nodeRemovalMode();
  assert.equal(t.controls.rm_go.disabled, true);
});

test("Gone for good asks for the acknowledgement and sends the reviewed job ids", async () => {
  const t = setup();
  await t.c.window.nodeRemoval("node3");
  t.choose("gone");
  assert.equal(t.controls.rm_jobs.closest("label").hidden, false);
  assert.match(t.body.innerHTML, /Remove the host anyway\. The saved job stays, and keeps blocking Longhorn upgrades, shutdown and OS rollouts until you end it from Jobs once the host is removed\./);
  assert.equal(t.controls.rm_jobs_hint.textContent, "Gone for good leaves the job as it is. The saved job stays, and keeps blocking Longhorn upgrades, shutdown and OS rollouts until you end it from Jobs once the host is removed.");
  assert.equal(t.controls.rm_go.disabled, true, "not until it is accepted");
  t.controls.rm_jobs.checked = true;
  t.c.window.nodeRemovalMode();
  assert.equal(t.controls.rm_go.disabled, false);
  await t.c.window.nodeRemove("node3");
  assert.deepEqual(t.sent.map(b => [b.gone, b.accept_jobs]), [[true, ["job-1"]]]);
});

test("Several saved jobs are named in the plural", async () => {
  const stay = "The saved jobs stay, and keep blocking Longhorn upgrades, shutdown and OS rollouts until you end them from Jobs once the host is removed.";
  const t = setup(plan([JOB, { ...JOB, id: "job-2" }], stay));
  await t.c.window.nodeRemoval("node3");
  assert.match(t.body.innerHTML, /Saved disk jobs on node3/);
  t.choose("gone");
  assert.equal(t.controls.rm_jobs_hint.textContent, "Gone for good leaves the jobs as they are. " + stay);
  assert.ok(t.body.innerHTML.includes("Remove the host anyway. " + stay));
});

test("A disk retire or V2 cleanup left behind is described as the server says, escaped", async () => {
  const stay = "The saved jobs stay, and keep blocking Longhorn upgrades, shutdown and OS rollouts. The disk retire job does so until it stops; it fails by itself once Longhorn's record of <node3> is deleted.";
  const t = setup(plan([{ ...JOB, kind: "disk-retire" }, { ...JOB, id: "job-2", kind: "longhorn-v2-cleanup" }], stay));
  await t.c.window.nodeRemoval("node3");
  t.choose("gone");
  assert.equal(t.controls.rm_jobs_hint.textContent, "Gone for good leaves the jobs as they are. " + stay);
  assert.ok(t.body.innerHTML.includes("Remove the host anyway. The saved jobs stay, and keep blocking Longhorn upgrades, shutdown and OS rollouts. The disk retire job does so until it stops; it fails by itself once Longhorn's record of &lt;node3&gt; is deleted."));
  assert.doesNotMatch(t.body.innerHTML, /end them from Jobs/);
});

test("Without the tick, gone for good sends no acknowledgement", async () => {
  const t = setup();
  await t.c.window.nodeRemoval("node3");
  t.choose("gone");
  await t.c.window.nodeRemove("node3");
  assert.deepEqual(t.sent.map(b => b.accept_jobs), [[]]);
});

test("A host with no saved disk job is removed as before", async () => {
  const t = setup(plan([]));
  await t.c.window.nodeRemoval("node3");
  t.choose("standard");
  assert.equal(t.controls.rm_go.disabled, false);
  assert.equal(t.controls.rm_jobs, undefined, "no acknowledgement to give");
  await t.c.window.nodeRemove("node3");
  assert.deepEqual(t.sent.map(b => [b.gone, b.accept_jobs]), [[false, []]]);
});

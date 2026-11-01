"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

/* The restart review's "Before draining" section: what would keep the host
   from draining, each with its fix and its own "drain anyway" tick. The
   button waits for every tick, and the request names each one ticked. */
function setup(plan) {
  let html = "";
  const sent = [], notices = [], fetched = [], fields = { "#mbody": { insertAdjacentHTML(_where, body) { html += body; } } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, modal: (_, body) => { html = body; }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      if (path.startsWith("/api/node/power/plan")) { fetched.push(path); return plan; }
      if (path === "/api/node/orphans/remove") { sent.push({ path, body: JSON.parse(options.body) }); return { removed: ["orphan-1"], refused: [] }; }
      sent.push({ path, body: JSON.parse(options.body) });
      return { ok: true, operation: { id: "power-job" }, steps: [] };
    } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle.js", "utf8"), c);
  return { c, fields, sent, notices, fetched, html: () => html };
}
const rows = [
  { id: "engine-orphans:host1", kind: "engine-orphans", what: "3 orphan records for engines that now run on host2",
    why: "Longhorn keeps the instance-manager", fix: "Remove the orphan records: the engines run on host2.", can_remove: true,
    records: [{ name: "orphan-1", uid: "u-1", resourceVersion: "7", engine: "vol-a-e-0", engine_node: "host2" }] },
  { id: "stuck-pod:apps/app-0", kind: "stuck-pod", what: "Pod apps/app-0 on host1 has been terminating for 60 min",
    why: "Its phase is Succeeded", fix: "Force-delete the <pod>.", can_remove: false, records: [] },
];
const plan = { node: "host1", action: "reboot", review_token: "review", ready: true, pods: 1, vms: [], workloads: [], volumes: [],
  drain_blockers: rows, requires_blocker_ack: true };
function typed(t) {
  t.fields["#pw_confirm"] = { value: "host1", dataset: { typedExpect: "host1" } };
  t.fields["#pw_execute"] = { disabled: true };
  rows.forEach((_, i) => { t.fields[`#pw_blocker_${i}`] = { checked: false }; });
}

test("the dialog shows one acknowledgement per drain blocker, with its fix", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  const html = t.html();
  assert.match(html, /Before draining · 2/);
  assert.match(html, /id="pw_blocker_0"[\s\S]*Drain anyway: 3 orphan records for engines that now run on host2/);
  assert.match(html, /id="pw_blocker_1"[\s\S]*Drain anyway: Pod apps\/app-0 on host1 has been terminating for 60 min/);
  assert.match(html, /Remove the orphan records: the engines run on host2\./);
  assert.match(html, /Force-delete the &lt;pod&gt;\./);
  assert.doesNotMatch(html, /id="pw_blocker_2"/);
  // The removal button of a removable row, named by its row, ready to use.
  assert.match(html, /id="pw_blocker_remove_0"[^>]*data-drain-blocker="engine-orphans:host1"/);
  assert.doesNotMatch(html.match(/<button[^>]*id="pw_blocker_remove_0"[^>]*>/)[0], /disabled/);
  assert.doesNotMatch(html, /pw_blocker_remove_1/, "only removable rows get the button");
  // A forced restart sends no drain, so it asks none of this.
  const forced = setup({ ...plan, force: true, drain_blockers: [], requires_blocker_ack: false });
  await forced.c.window.nodePowerReview("host1", "reboot", true);
  assert.doesNotMatch(forced.html(), /Before draining|pw_blocker_/);
});

test("Restart stays disabled until every blocker is ticked", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  typed(t);
  t.c.window.nodePowerReady();
  assert.equal(t.fields["#pw_execute"].disabled, true, "typed, nothing ticked");
  t.fields["#pw_blocker_0"].checked = true;
  t.c.window.nodePowerReady();
  assert.equal(t.fields["#pw_execute"].disabled, true, "one of two ticked");
  await t.c.window.nodePower("host1", "reboot");
  assert.equal(t.sent.length, 0, "a click that slips through still sends nothing");
  assert.match(t.notices.join(" "), /drain anyway/i);
  t.fields["#pw_blocker_1"].checked = true;
  t.c.window.nodePowerReady();
  assert.equal(t.fields["#pw_execute"].disabled, false);
  t.fields["#pw_confirm"].value = "host";
  t.c.window.nodePowerReady();
  assert.equal(t.fields["#pw_execute"].disabled, true, "the typed name still counts");
  t.fields["#pw_confirm"].value = "host1";
  await t.c.window.nodePower("host1", "reboot");
  assert.equal(t.sent.length, 1);
  assert.deepEqual(t.sent[0].body.allow_drain_blockers, ["engine-orphans:host1", "stuck-pod:apps/app-0"]);
  assert.equal(t.sent[0].body.force, false);
});

test("Remove orphan records opens a review listing each record and where its engine runs", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  t.c.window.nodePowerRemoveOrphans(0);
  const html = t.html();
  assert.match(html, /orphan-1/);
  assert.match(html, /vol-a-e-0/);
  assert.match(html, /host2/);
  assert.match(html, /Remove 1 orphan record/);
  assert.equal(0, t.sent.length, "opening the review removes nothing");
});

test("after removing, the restart review is fetched again", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  t.c.window.nodePowerRemoveOrphans(0);
  await t.c.window.nodePowerRemoveOrphansConfirm(0);
  assert.deepEqual(t.sent[0], { path: "/api/node/orphans/remove",
    body: { node: "host1", records: [{ name: "orphan-1", uid: "u-1", resourceVersion: "7" }] } });
  assert.equal(2, t.fetched.length, "the review is fetched again");
});

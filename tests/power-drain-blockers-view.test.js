"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

/* The restart review's "Before draining" section: what would keep the host
   from draining, each with its fix and its own "drain anyway" tick. The
   button waits for every tick, and the request names each one ticked. */
function setup(plan, { role = "admin", removeFails = false } = {}) {
  let html = "";
  const state = { plan };
  const sent = [], notices = [], fetched = [], steps = [], fields = { "#mbody": { insertAdjacentHTML(_where, body) { html += body; } } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    // The ticks in the open dialog: inputs that carry data-drain-ack.
    $$: s => s === "input[data-drain-ack]" ? Object.values(fields).filter(f => f && f.dataset && "drainAck" in f.dataset) : [],
    can: need => need !== "admin" || role === "admin",
    childModal: (title, body) => { steps.push(`open ${title}`); html = body; }, modal: (_, body) => { html = body; },
    modalBack: () => { steps.push("back"); }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    api: async (path, options) => {
      if (path.startsWith("/api/node/power/plan")) { steps.push("plan"); fetched.push(path); return state.plan; }
      if (path === "/api/node/orphans/remove") {
        sent.push({ path, body: JSON.parse(options.body) });
        if (removeFails) throw new Error("HTTP 500");
        return { removed: ["orphan-1"], refused: [] };
      }
      sent.push({ path, body: JSON.parse(options.body) });
      return { ok: true, operation: { id: "power-job" }, steps: [] };
    } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
  return { c, fields, sent, notices, fetched, steps, state, html: () => html };
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
  rows.forEach((row, i) => { t.fields[`#pw_blocker_${i}`] = { checked: false, dataset: { drainAck: row.id } }; });
}

test("the dialog shows one acknowledgement per drain blocker, with its fix", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  const html = t.html();
  assert.match(html, /Before draining · 2/);
  assert.match(html, /id="pw_blocker_0"[\s\S]*3 orphan records for engines that now run on host2/);
  assert.match(html, /id="pw_blocker_1"[\s\S]*Pod apps\/app-0 on host1 has been terminating for 60 min/);
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

test("each tick is keyed by its row's id, and the request names the ticks actually checked", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  assert.match(t.html(), /id="pw_blocker_0" data-drain-ack="engine-orphans:host1"/);
  assert.match(t.html(), /id="pw_blocker_1" data-drain-ack="stuck-pod:apps\/app-0"/);
  typed(t);
  t.fields["#pw_blocker_0"].checked = t.fields["#pw_blocker_1"].checked = true;
  // A stale review brought back by Back: its two ticks are checked, but the
  // plan now holds a row the person never saw in place of the first one.
  const fresh = { id: "stuck-attachment:csi-pv-9", kind: "stuck-attachment", what: "Volume pv-9 is still attached", why: "w", fix: "f", can_remove: false, records: [] };
  // A review opened again for another plan (the plan under review is the review's own).
  t.state.plan = { ...plan, drain_blockers: [fresh, rows[1]] };
  await t.c.window.nodePowerReview("host1", "reboot");
  t.c.window.nodePowerReady();
  assert.equal(t.fields["#pw_execute"].disabled, true, "a tick next to other text acknowledges nothing");
  await t.c.window.nodePower("host1", "reboot");
  assert.equal(t.sent.length, 0);
  assert.match(t.notices.join(" "), /drain anyway/i);
  // The current review: what is posted is the ids of the ticks checked.
  t.state.plan = plan;
  await t.c.window.nodePowerReview("host1", "reboot");
  t.fields["#pw_blocker_2"] = { checked: true, dataset: { drainAck: "stale:row" } };
  await t.c.window.nodePower("host1", "reboot");
  assert.equal(t.sent.length, 1);
  assert.deepEqual([...t.sent[0].body.allow_drain_blockers].sort(), ["engine-orphans:host1", "stale:row", "stuck-pod:apps/app-0"],
    "an extra tick is posted too, so the server's exact-set check refuses it");
});

test("after removing, the orphan dialog is closed before the restart is reviewed again", async () => {
  const t = setup(plan);
  await t.c.window.nodePowerReview("host1", "reboot");
  t.c.window.nodePowerRemoveOrphans(0);
  await t.c.window.nodePowerRemoveOrphansConfirm(0);
  const back = t.steps.lastIndexOf("back"), again = t.steps.lastIndexOf("plan");
  assert.ok(back >= 0 && back < again, `the orphan dialog is popped first: ${t.steps.join(" > ")}`);
});

test("a failed removal gives the button back", async () => {
  const t = setup(plan, { removeFails: true });
  await t.c.window.nodePowerReview("host1", "reboot");
  t.c.window.nodePowerRemoveOrphans(0);
  t.fields["#pw_orphans_remove"] = { disabled: false, textContent: "Remove 1 orphan record" };
  await t.c.window.nodePowerRemoveOrphansConfirm(0);
  assert.equal(t.fields["#pw_orphans_remove"].disabled, false);
  assert.equal(t.fields["#pw_orphans_remove"].textContent, "Remove 1 orphan record");
  assert.match(t.notices.join(" "), /Could not remove the orphan records: HTTP 500/);
  assert.equal(1, t.fetched.length, "nothing is reviewed again");
});

test("only an admin gets the Remove orphan records button; others see why", async () => {
  const t = setup(plan, { role: "operator" });
  await t.c.window.nodePowerReview("host1", "reboot");
  assert.doesNotMatch(t.html(), /pw_blocker_remove_0/);
  assert.match(t.html(), /Remove the orphan records: the engines run on host2\./);
  assert.match(t.html(), /An admin can remove these orphan records/);
});

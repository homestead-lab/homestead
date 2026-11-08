"use strict";
// Host actions' Maintenance: its review, Extend and End, and what a host's card and page say (#535 part 2).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const NOW = () => Math.floor(Date.now() / 1000);
function setup({ answers = {}, uptime = null, nodes = [] } = {}) {
  const shown = { modal: [], toasts: [], sent: [] }, elements = {};
  const element = id => (elements[id] ??= { id, value: "", checked: false, disabled: false, dataset: {} });
  const c = { console, Set, Map, Promise, URLSearchParams, AbortController, setTimeout, clearTimeout, Date, Math,
    document: { addEventListener() {}, getElementById: id => elements[id] || null, visibilityState: "visible" },
    STATE: { data: { nodeUptime: { nodes: { node1: uptime } }, nodes }, platform: {} },
    $: selector => { const id = selector.startsWith("#") ? selector.slice(1) : selector; return elements[id] || null; },
    $$: selector => selector === "[data-mm-ack]" ? Object.values(elements).filter(e => e.dataset.mmAck) : [],
    esc: v => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"), plural: (n, w) => `${n} ${w}`,
    toast: (text, tone) => shown.toasts.push([text, tone]), can: () => true,
    modal: (title, html) => shown.modal.push([title, html]), childModal: (title, html) => shown.modal.push([title, html]),
    closeModal() {}, modalBack() {}, noteNodeUptime() {}, remoteRow: () => null, fmtDay: t => `day${t}`, fmtAt: t => `at${t}`,
    fmtUp: s => `${s}s`, fmtAgo: s => `${s}s ago`,
    api: async (url, options) => {
      const path = String(url).split("?")[0];
      if (options?.body) shown.sent.push([path, JSON.parse(options.body)]);
      const answer = answers[path];
      if (answer === undefined) throw new Error(`no answer for ${path}`);
      return typeof answer === "function" ? answer() : answer;
    } };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-overview.js", "web/js/host-maintenance.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  return { c, shown, element, elements };
}
const review = (extra = {}) => ({ node: "node1", action: "maintenance", review_token: "t", ready: true, pods: 3, vms: [], blockers: [], warnings: ["Check this"],
  hold: [{ id: "Deployment/lab/web", kind: "Deployment", ns: "lab", name: "web", options: ["move", "wait"], default: "move", hosts: ["node2"], why: "" }],
  volumes: [{ name: "v", claim: "lab/data", risk: "resync", healthy_elsewhere: 2 }], workloads: [], stranded: [], drain_blockers: [], maintenance: { budgets: [], local_storage: [] }, ...extra });
const PLAN = "/api/node/power/plan";
const record = (...rows) => ({ days: [], windows: {}, windows_excluding_maintenance: {}, maintenance_s: {}, outages: [], reboots: [], maintenance: rows });
const declared = (extra = {}) => ({ start: NOW() - 60, end: null, reason: "Drive swap", job: "op", ongoing: true, declared: true, until: NOW() + 4800, released: false, ...extra });

test("the review shows what a reboot's does, with a reason and a duration", async () => {
  const t = setup({ answers: { [PLAN]: review() } });
  await t.c.hostMaintenanceReview("node1");
  const [title, html] = t.shown.modal.at(-1);
  assert.match(title, /Maintenance · node1/);
  for (const word of ["Check this", "lab/web", "lab/data", 'id="mm_reason"', 'id="mm_hours"', "Until I end it", 'id="mm_confirm"', "Power is up to you"]) assert.ok(html.includes(word), word);
});

test("the send button stays disabled until the name is typed, a reason is given and blockers are ticked", async () => {
  const plan = review({ requires_data_ack: true, drain_blockers: [{ id: "b1", what: "A budget", why: "w", fix: "f" }] });
  const t = setup({ answers: { [PLAN]: plan } });
  const go = t.element("mm_execute"), reason = t.element("mm_reason"), name = t.element("mm_confirm");
  name.dataset.typedExpect = "node1";
  const blocker = t.element("mm_blocker_0"); blocker.dataset.mmAck = "1";
  await t.c.hostMaintenanceReview("node1");
  const ready = () => { t.c.hostMaintenanceReady(); return !go.disabled; };
  assert.equal(ready(), false);
  name.value = "node1";
  assert.equal(ready(), false, "no reason yet");
  reason.value = "   ";
  assert.equal(ready(), false, "a blank reason is none");
  reason.value = "Drive swap";
  assert.equal(ready(), false, "a blocker is unticked");
  blocker.checked = true;
  assert.equal(ready(), true);
  reason.value = "x".repeat(121);
  assert.equal(ready(), false, "too long");
});

test("a blocked review offers no way to send", async () => {
  const t = setup({ answers: { [PLAN]: review({ ready: false, blockers: ["This host is already in maintenance"], hard_blockers: ["x"] }) } });
  await t.c.hostMaintenanceReview("node1");
  const html = t.shown.modal.at(-1)[1];
  assert.match(html, /already in maintenance/);
  assert.doesNotMatch(html, /mm_execute|mm_reason/);
});

test("it sends the reason, the hours and the review's token", async () => {
  const t = setup({ answers: { [PLAN]: review(), "/api/node/power": { operation: { id: "op-1" } }, "/api/operations": [{ id: "op-1", status: "succeeded", message: "In maintenance: Drive swap" }], "/api/nodes/uptime": {} } });
  const name = t.element("mm_confirm"); name.dataset.typedExpect = "node1"; name.value = "node1";
  t.element("mm_execute"); t.element("mm_reason").value = "  Drive swap "; t.element("mm_hours").value = "4";
  t.element("mm_hold_0").value = "wait";
  await t.c.hostMaintenanceReview("node1");
  await t.c.hostMaintenanceApply();
  const [path, body] = t.shown.sent[0];
  assert.equal(path, "/api/node/power");
  assert.equal(body.action, "maintenance");
  assert.equal(body.reason, "Drive swap");
  assert.equal(body.hours, 4);
  assert.equal(body.review_token, "t");
  assert.equal(body.choices["Deployment/lab/web"], "wait");
});

test("a 409 opens a fresh review", async () => {
  let reviews = 0;
  const conflict = Object.assign(new Error("host impact changed; review the plan again"), { status: 409, body: {} });
  const t = setup({ answers: { [PLAN]: () => { reviews += 1; return review(); }, "/api/node/power": () => { throw conflict; } } });
  const name = t.element("mm_confirm"); name.dataset.typedExpect = "node1"; name.value = "node1";
  t.element("mm_execute"); t.element("mm_reason").value = "Drive swap"; t.element("mm_hours").value = "1";
  await t.c.hostMaintenanceReview("node1");
  await t.c.hostMaintenanceApply();
  assert.equal(reviews, 2);
  assert.match(t.shown.toasts.at(-1)[0], /review the plan again/);
});

test("a reason with markup is shown as text", () => {
  const mark = "<img src=x>";
  const u = record(declared({ reason: mark }));
  const t = setup({ uptime: u });
  const chip = t.c.hostMaintenanceChip("node1"), line = t.c.hostMaintenanceLine(u);
  const row = t.c.hostMaintenanceRow("node1", { reason: mark, until: NOW() + 60, state: "open" });
  for (const html of [chip, line, row]) {
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img src=x>/);
  }
});

test("a ran-out or unfinished reason with markup is text too", () => {
  const mark = "<img src=x>";
  const out = record(declared({ reason: mark, end: NOW() - 60, ongoing: false, until: NOW() - 60 }));
  const ending = record(declared({ reason: mark, end: NOW() - 60, ongoing: false, ending: true }));
  const t = setup();
  for (const html of [t.c.hostMaintenanceLine(out), t.c.hostMaintenanceLine(ending),
    t.c.hostMaintenanceRow("node1", { reason: mark, until: NOW() - 60, state: "ran_out" }),
    t.c.hostMaintenanceRow("node1", { reason: mark, until: NOW() - 60, state: "ending" })]) {
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img src=x>/);
  }
});

test("a reason with markup stays text in the page's log", async () => {
  const u = { ...record(declared({ reason: "<img src=x>" })), windows: { "24h": 100 }, days: [] };
  const t = setup({ uptime: u, answers: { "/api/nodes/uptime": { nodes: { node1: u } } } });
  let html = "";
  t.c.$ = () => ({ set innerHTML(v) { html = v; } });
  await t.c.nodeUptimePaint({ name: "node1", status: "Ready" });
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img/);
});

test("a host in maintenance offers Extend and End instead of the Maintenance button", () => {
  const t = setup();
  const row = t.c.hostMaintenanceRow("node1", { reason: "Drive swap", until: NOW() + 4800, state: "open" });
  assert.match(row, /hostMaintenanceExtend/);
  assert.match(row, /hostMaintenanceEnd/);
  assert.doesNotMatch(row, /hostMaintenanceReview/);
  assert.match(row, /Drive swap · 1h 20m left/);
  assert.match(t.c.hostMaintenanceRow("node1", null), /hostMaintenanceReview/);
});

test("ran out reads as ran out, offering End and Extend", () => {
  const t = setup();
  const row = t.c.hostMaintenanceRow("node1", { reason: "Drive swap", until: NOW() - 10, state: "ran_out" });
  assert.match(row, /ran out/);
  assert.doesNotMatch(row, /cordoned/, "not so on a single host, or after a hand uncordon");
  assert.match(row, /hostMaintenanceExtend/);
  assert.match(row, /hostMaintenanceEnd/);
});

test("an End that did not finish offers End again, and no Extend", () => {
  const t = setup();
  const row = t.c.hostMaintenanceRow("node1", { reason: "Drive swap", until: NOW() + 60, state: "ending" });
  assert.match(row, /did not finish/);
  assert.match(row, /hostMaintenanceEnd/);
  assert.doesNotMatch(row, /hostMaintenanceExtend/);
  const u = record(declared({ end: NOW() - 5, ongoing: false, ending: true }));
  const shown = setup({ uptime: u });
  assert.match(shown.c.hostMaintenanceChip("node1"), /maintenance not finished/);
  assert.match(shown.c.hostMaintenanceLine(u), /did not finish/);
});

test("every button that changes anything is for admins", () => {
  const t = setup();
  const needsAdmin = html => {
    const buttons = html.match(/<button\b[^>]*>/g) || [];
    assert.ok(buttons.length > 0);
    for (const button of buttons) assert.match(button, /data-need="admin"/, button);
  };
  needsAdmin(t.c.hostMaintenanceRow("node1", null));
  needsAdmin(t.c.hostMaintenanceRow("node1", { reason: "x", until: NOW() + 60, state: "open" }));
  needsAdmin(t.c.hostMaintenanceRow("node1", { reason: "x", until: NOW() - 60, state: "ran_out" }));
  needsAdmin(t.c.hostMaintenanceRow("node1", { reason: "x", until: NOW() - 60, state: "ending" }));
});

test("an End that failed says so where it stays, and offers it again", async () => {
  const t = setup({ answers: { "/api/node/maintenance/end": { ok: false, detail: "It is still cordoned", started: [], left: [] }, "/api/nodes/uptime": {} } });
  const name = t.element("mm_end_confirm"); name.dataset.typedExpect = "node1"; name.value = "node1";
  const box = t.element("mm_end_result");
  let closed = 0; t.c.closeModal = () => { closed += 1; };
  await t.c.hostMaintenanceEndGo("node1");
  assert.equal(closed, 0, "the dialog stays");
  assert.match(box.innerHTML, /still cordoned/);
  assert.match(box.innerHTML, /not finished/i);
  assert.ok(!t.shown.toasts.some(([text]) => /^Maintenance ended/.test(text)), "not said to have ended");
  assert.ok(t.shown.toasts.every(([, tone]) => tone !== "ok"));
});

test("End's lead says what it will do, not what has happened", () => {
  const t = setup({ nodes: [{ name: "node1", status: "Ready" }] });
  t.c.hostMaintenanceEnd("node1");
  const html = t.shown.modal.at(-1)[1];
  assert.doesNotMatch(html, /Scheduling is allowed/);
  assert.match(html, /Ending it allows scheduling/);
});

function followed(op) {
  const t = setup({ answers: { "/api/operations": [op], "/api/nodes/uptime": {} } });
  const box = t.element("mmProgress");
  t.c.UI.poll = read => { read({}); return {}; };
  return { t, box };
}

test("a job that failed before opening anything says Maintenance was not opened", async () => {
  const { t, box } = followed({ id: "op-1", status: "failed", message: "Maintenance was not opened. Inspect the host's cordon state: refused" });
  t.c.hostMaintenanceFollow("op-1", "node1");
  await new Promise(done => setTimeout(done, 5));
  assert.match(box.innerHTML, /Maintenance was not opened/);
});

test("a job that failed only at recording it doesn't say it wasn't opened", async () => {
  const { t, box } = followed({ id: "op-1", status: "failed", message: "In maintenance: Drive swap. Its job could not record that (disk full)" });
  t.c.hostMaintenanceFollow("op-1", "node1");
  await new Promise(done => setTimeout(done, 5));
  assert.doesNotMatch(box.innerHTML, /was not opened/);
  assert.match(box.innerHTML, /In maintenance: Drive swap/);
  assert.match(box.innerHTML, /job did not finish/);
});

test("an End that worked closes and says so", async () => {
  const t = setup({ answers: { "/api/node/maintenance/end": { ok: true, detail: "Scheduling is allowed on it again", started: [], left: [] }, "/api/nodes/uptime": {} } });
  const name = t.element("mm_end_confirm"); name.dataset.typedExpect = "node1"; name.value = "node1";
  let closed = 0; t.c.closeModal = () => { closed += 1; };
  await t.c.hostMaintenanceEndGo("node1");
  assert.equal(closed, 1);
  assert.deepEqual(t.shown.toasts.at(-1), ["Maintenance ended. Scheduling is allowed on it again", "ok"]);
});

/* nodeActions itself, so a read that fails is proved to leave it as it is. */
function hostActions({ maintenance, schedulable = true }) {
  const t = setup({ nodes: [{ name: "node1", status: "Ready", schedulable, roles: [] }], answers: {
    "/api/quorum": { members: [], total: 3 }, "/api/node/impact": { workloads: [], stranded: [] },
    ...(maintenance === undefined ? {} : { "/api/node/maintenance": { declared: maintenance } }) } });
  t.c.hostAppsHtml = () => ""; t.c.impactRows = () => "";
  vm.runInContext(fs.readFileSync("web/js/views-lifecycle-node-power.js", "utf8"), t.c);
  return t;
}

test("Host actions opens the same when its maintenance can't be read", async () => {
  const t = hostActions({});
  await t.c.nodeActions("node1");
  const html = t.shown.modal.at(-1)[1];
  assert.match(html, /nodePowerReview/, "the rest is all there");
  assert.doesNotMatch(html, /hostMaintenance/, "and it offers nothing that acts on a guess");
});

test("Host actions offers Maintenance from a read of none", async () => {
  const t = hostActions({ maintenance: null });
  await t.c.nodeActions("node1");
  assert.match(t.shown.modal.at(-1)[1], /hostMaintenanceReview/);
});

test("Uncordon says it doesn't end a maintenance", async () => {
  const on = hostActions({ schedulable: false, maintenance: { reason: "Drive swap", until: NOW() + 600, state: "open" } });
  await on.c.nodeActions("node1");
  assert.match(on.shown.modal.at(-1)[1], /Uncordoning doesn't end its maintenance/);
  const off = hostActions({ schedulable: false, maintenance: null });
  await off.c.nodeActions("node1");
  assert.doesNotMatch(off.shown.modal.at(-1)[1], /doesn't end its maintenance/);
});

test("a failed read leaves Host actions as it is", () => {
  assert.equal(setup().c.hostMaintenanceRow("node1", undefined), "");
});

test("the card shows in maintenance with the reason and time left in its tip", () => {
  const t = setup({ uptime: record(declared()) });
  const chip = t.c.hostMaintenanceChip("node1");
  assert.match(chip, />in maintenance</);
  assert.match(chip, /Drive swap · 1h 20m left/);
});

test("a host with only job-caused maintenance shows no such chip", () => {
  const t = setup({ uptime: record({ start: NOW() - 60, end: null, reason: "OS update", job: "op", ongoing: true }) });
  assert.equal(t.c.hostMaintenanceChip("node1"), "");
  assert.equal(t.c.hostMaintenanceLine(t.c.STATE.data.nodeUptime.nodes.node1), "");
});

test("one that was ended shows nothing, however it ended", () => {
  const t = setup({ uptime: record(declared({ end: NOW() - 5, ongoing: false, released: true })) });
  assert.equal(t.c.hostMaintenanceChip("node1"), "");
});

test("the page's head says in maintenance with reason and time left, or that it ran out and is still cordoned", () => {
  const t = setup();
  assert.match(t.c.hostMaintenanceLine(record(declared())), /In maintenance: Drive swap · 1h 20m left/);
  const out = record(declared({ end: NOW() - 60, ongoing: false, until: NOW() - 60 }));
  assert.match(t.c.hostMaintenanceLine(out), /ran out: Drive swap/);
  assert.doesNotMatch(t.c.hostMaintenanceLine(out), /cordoned/);
});

test("End says what it means on a host that is down", () => {
  const t = setup({ nodes: [{ name: "node1", status: "NotReady" }] });
  t.c.hostMaintenanceEnd("node1");
  assert.match(t.shown.modal.at(-1)[1], /counts as an outage/);
  const up = setup({ nodes: [{ name: "node1", status: "Ready" }] });
  up.c.hostMaintenanceEnd("node1");
  assert.doesNotMatch(up.shown.modal.at(-1)[1], /counts as an outage/);
});

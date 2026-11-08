"use strict";

/* #619 step 6b: VM Edit, New k3s cluster and the VM import's room check run
   on UI.reviewFlow(). The "pinned" tests were written against the dialogs as
   they were and passed there; the move keeps them green. */
const seen = require("./helpers/seen");
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const later = () => { let resolve, reject; const promise = new Promise((y, n) => { resolve = y; reject = n; }); return { promise, resolve, reject }; };
const conflict = message => Object.assign(new Error(message), { status: 409 });

function harness(answer) {
  const sent = [], toasts = [], opened = [], calls = [];
  const fields = {};
  const body = { _html: "", get innerHTML() { return this._html; }, set innerHTML(html) { this._html = html; },
    insertAdjacentHTML(_where, html) { this._html = html + this._html; },
    focused: [], querySelector(selector) { return { focus: () => this.focused.push(selector) }; } };
  fields["#mbody"] = body;
  const ctx = { console, Map, Set, Date, Promise, encodeURIComponent, URLSearchParams,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null }, STATE: { data: {} },
    $: key => fields[key], $$: () => [], esc: v => String(v).replaceAll("&", "&amp;").replaceAll("<", "&lt;"),
    childModal: (title, html) => { opened.push(["childModal", title, html]); body.innerHTML = html; },
    modal: (title, html) => { opened.push(["modal", title, html]); body.innerHTML = html; },
    modalBack: () => calls.push("modalBack"), closeModal: () => calls.push("closeModal"), refresh: () => calls.push("refresh"),
    refreshOperations: () => calls.push("refreshOperations"), go: page => calls.push(`go ${page}`), setTimeout: fn => fn(),
    toast: (text, tone) => toasts.push([text, tone]),
    api: async (path, options) => { const sentBody = JSON.parse(options.body); sent.push({ path, body: sentBody }); return answer(path, sentBody); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(require("./helpers/workloads-source")(), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-vms.js", "utf8"), ctx);
  return { ctx, fields, body, sent, toasts, opened, calls, html: () => body.innerHTML };
}

/* ---------------- VM Edit ---------------- */
function edit({ admission = true, warnings = ["High RAM"], blocked = false, missing = false, fail = null, stateInit = false } = {}) {
  let t;
  t = harness(path => {
    if (path.endsWith("/preview")) return { capacity_token: missing ? null : "edit-token",
      volumes: [{ name: "guest-disk-2", size: "20Gi", storage_class: "fast" }],
      capacity: { blocked, warnings, blockers: blocked ? ["No host has room"] : [], pod_request_gb: 4, pod_cpu_request_percent: 10,
        additional: 1, candidates: [], placement: {}, concerns: [], unbounded: [],
        vm: { admission_needed: admission, policy_before: "Halted", policy_after: "Always",
          state_initialization: stateInit ? { name: "guest", reason: "No state volume exists yet." } : null } } };
    if (fail) throw fail;
    return { ok: true, detail: "saved guest" };
  });
  // The loading line stands in for the dialog: it goes when the review is drawn.
  t.ctx.childModal = (title, html) => { t.opened.push(["childModal", title, html]); t.body.innerHTML = html; if (html.includes('id="vmEditLoading"')) t.fields["#vmEditLoading"] = {}; };
  const set = Object.getOwnPropertyDescriptor(t.body, "innerHTML").set;
  Object.defineProperty(t.body, "innerHTML", { get() { return this._html; }, set(html) { set.call(this, html);
    if (!html.includes('id="vmEditLoading"')) delete t.fields["#vmEditLoading"];
    if (html.includes('id="vmEditApprove"')) t.fields["#vmEditApprove"] = { checked: false };
    if (html.includes('id="vmEditApply"')) t.fields["#vmEditApply"] = {}; } });
  t.restarts = [];
  t.ctx.vmPowerReview = async config => t.restarts.push(JSON.parse(JSON.stringify(config)));
  return t;
}
const change = { ns: "lab", name: "guest", memory: "6Gi", cores: 4 };

test("pinned: VM Edit's review opens over the form with the same title and loading line", async () => {
  const t = edit();
  await t.ctx.vmEditReview(change);
  assert.deepEqual(t.opened.map(o => o.slice(0, 2)), [["childModal", "Save changes · guest"]]);
  assert.equal(t.opened[0][2], '<div id="vmEditLoading" class="empty"><span class="spin2"></span>Checking proposed VM, disks and capacity…</div>');
  assert.deepEqual(t.sent, [{ path: "/api/vm/edit/preview", body: change }]);
});

test("pinned: VM Edit's review reads the same, with and without new room on a host", async () => {
  let t = edit();
  await t.ctx.vmEditReview(change, true);
  // The header names the VM; the sentence says what happens to it.
  assert.match(t.html(), /<span class="ui-head-name">guest<\/span><span class="ui-head-where">VM · lab<\/span>[\s\S]*<p class="ui-lead">Saves it/);
  assert.match(t.html(), /4 vCPUs · 6Gi/);
  assert.match(t.html(), /Halted → Always/);
  assert.match(t.html(), /<span class="mono">guest-disk-2<\/span> · 20Gi · fast/);
  assert.match(t.html(), /After saving, a separate restart review checks the saved VM and the room on its host\. Saving does not restart the VM\./);
  assert.match(t.html(), /id="vmEditApprove"[^>]*onchange="vmEditReviewReady\(\)"/);
  assert.match(t.html(), /<button type="button" class="btn" onclick="vmEditReviewBack\(\)" data-dialog-dismiss="true">Back to edit<\/button><\/div><div class="ui-actions-end"><button type="button" class="btn pri" id="vmEditApply" onclick="vmEditReviewedApply\(\)" disabled >Save changes<\/button>/);
  assert.equal(t.fields["#vmEditApply"].disabled, true);
  t = edit({ admission: false });
  await t.ctx.vmEditReview(change);
  assert.match(t.html(), /<p class="ui-lead">Saves the changes\. No extra room is needed on a host\./);
  assert.match(t.html(), /Saving does not restart the VM\. If a change needs a restart, restart it from its power controls\./);
  assert.match(t.html(), /High RAM/);
  assert.match(t.html(), /id="vmEditApprove"/);
  t = edit({ admission: false, warnings: [] });
  await t.ctx.vmEditReview(change);
  assert.doesNotMatch(t.html(), /vmEditApprove/);
  assert.equal(t.fields["#vmEditApply"].disabled, false, "nothing to accept: Save is ready");
});

test("pinned: VM Edit asks for the tick, saves once, and reviews a restart only after a save", async () => {
  const t = edit();
  await t.ctx.vmEditReview(change, true);
  await t.ctx.vmEditReviewedApply();
  assert.deepEqual(t.toasts, [["Tick Save it anyway to accept the warnings first", "bad"]]);
  t.fields["#vmEditApprove"].checked = true;
  assert.equal(t.ctx.vmEditReviewReady(), true);
  assert.equal(t.fields["#vmEditApply"].disabled, false);
  await t.ctx.vmEditReviewedApply();
  assert.equal(t.fields["#vmEditApply"].textContent, "Saving changes…");
  assert.deepEqual(t.sent.at(-1), { path: "/api/vm/edit", body: { ...change, capacity_token: "edit-token", confirm_capacity: true } });
  assert.deepEqual(t.toasts.at(-1), ["saved guest", "ok"]);
  assert.deepEqual(t.calls, ["closeModal", "refresh", "refreshOperations"]);
  assert.deepEqual(t.restarts, [{ ns: "lab", name: "guest", action: "restart" }]);
  await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent.length, 2, "a used review is not sent again");
});

test("pinned: a failed VM Edit save says what may be saved and is not repeated", async () => {
  const t = edit({ fail: new Error("lost response") });
  await t.ctx.vmEditReview(change, true);
  t.fields["#vmEditApprove"].checked = true;
  await t.ctx.vmEditReviewedApply();
  assert.equal(t.fields["#vmEditApply"].textContent, "Inspect VM before retrying");
  assert.ok(seen(t.html()).startsWith("lost response. Some changes to the VM, its disks or its Secrets may already be saved. No request was repeated, and the VM was not restarted. Close this review and check the VM before editing it again."));
  assert.deepEqual(t.toasts, [["lost response", "bad"]]);
  assert.deepEqual(t.calls, ["refreshOperations"]);
  assert.deepEqual(t.restarts, []);
});

test("pinned: blocked and missing VM Edit reviews send nothing", async () => {
  let t = edit({ blocked: true });
  await t.ctx.vmEditReview(change);
  assert.match(t.html(), /No host has room/);
  t.fields["#vmEditApprove"] = { checked: true };
  assert.equal(t.ctx.vmEditReviewReady(), false);
  t = edit({ missing: true });
  await t.ctx.vmEditReview(change);
  assert.equal(seen(t.html()), "The review is not available. Nothing was saved. Go back and review again. Back to edit");
  assert.match(t.html(), /ui-callout bad[\s\S]*onclick="vmEditReviewBack\(\)">Back to edit<\/button>$/);
  await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent.length, 1);
});

test("pinned: VM Edit's fresh state asks for its tick and the typed name", async () => {
  const t = edit({ stateInit: true });
  await t.ctx.vmEditReview(change);
  assert.match(t.html(), /id="vmEditStateAck" onchange="vmEditReviewReady\(\)"/);
  t.fields["#vmEditApprove"].checked = true;
  t.fields["#vmEditStateAck"] = { checked: true }; t.fields["#vmEditStateName"] = { value: "gues", dataset: { typedExpect: "guest" } };
  assert.equal(t.ctx.vmEditReviewReady(), false);
  t.fields["#vmEditStateName"].value = "guest";
  await t.ctx.vmEditReviewedApply();
  assert.deepEqual(t.sent.at(-1).body, { ...change, ack_state_initialization: true, confirm_state_name: "guest", capacity_token: "edit-token", confirm_capacity: true });
});

test("pinned: VM Edit shows only the newest review, not one for a closed dialog, and none while saving", async () => {
  const t = edit({ warnings: [] }), original = t.ctx.api, previews = [];
  t.ctx.api = (path, options) => { if (path.endsWith("/preview")) { const p = later(); previews.push(p); return p.promise.then(() => original(path, options)); } return original(path, options); };
  const first = t.ctx.vmEditReview({ ...change, memory: "1Gi" }), second = t.ctx.vmEditReview({ ...change, memory: "2Gi" });
  previews[1].resolve(); await second;
  previews[0].resolve(); await first;
  assert.match(t.html(), /4 vCPUs · 2Gi/);
  const third = t.ctx.vmEditReview(change);
  t.ctx.vmEditReviewBack();
  assert.deepEqual(t.calls, ["modalBack"]);
  previews[2].resolve(); await third;
  assert.equal(t.ctx.vmEditReviewReady(), false, "Back drops the preview on its way");
  t.ctx.api = original;
  await t.ctx.vmEditReview(change);
  t.ctx.api = (path, options) => path === "/api/vm/edit" ? new Promise(() => {}) : original(path, options);
  const saving = t.ctx.vmEditReviewedApply();
  const count = t.opened.length;
  await t.ctx.vmEditReview(change);
  assert.equal(t.opened.length, count, "no review opens while a save is sent");
  t.ctx.vmEditReviewBack();
  assert.deepEqual(t.calls, ["modalBack", "modalBack"], "Back still leaves the review while saving");
  void saving;
});

test("a 409 from VM Edit's save offers a fresh review of the same change", async () => {
  const t = edit({ fail: conflict("The VM changed during admission; review it again") });
  await t.ctx.vmEditReview(change, true);
  t.fields["#vmEditApprove"].checked = true;
  await t.ctx.vmEditReviewedApply();
  assert.deepEqual(t.toasts, [["The VM changed during admission; review it again", "bad"]]);
  assert.match(seen(t.html()), /The VM changed during admission\. Review it again\. The review was out of date\. Some changes to the VM, its disks or its Secrets may already be saved\. No request was repeated, and the VM was not restarted\. Review again to see the VM as it is now\./);
  assert.match(t.html(), /onclick="vmEditReviewAgain\(\)">Review again<\/button>/);
  assert.deepEqual(t.body.focused, [".ui-actions-end .btn.pri"], "focus moves to Review again");
  assert.deepEqual(t.restarts, []);
  await t.ctx.vmEditReviewAgain();
  assert.deepEqual(t.sent.at(-1), { path: "/api/vm/edit/preview", body: change }, "the same change, previewed again");
  assert.match(t.html(), /id="vmEditApply"[^>]*disabled/);
  t.fields["#vmEditApprove"].checked = true;
  await t.ctx.vmEditReviewedApply();
  assert.equal(t.sent.filter(s => s.path === "/api/vm/edit").length, 2, "only after a fresh review and tick");
});

test("a 409 after Back from VM Edit's review leaves the edit form alone", async () => {
  const t = edit({ warnings: [] }), original = t.ctx.api;
  let reply;
  t.ctx.api = (path, options) => path === "/api/vm/edit" ? (reply = later()).promise : original(path, options);
  await t.ctx.vmEditReview(change, true);
  const saving = t.ctx.vmEditReviewedApply();
  t.ctx.vmEditReviewBack();
  t.body.innerHTML = '<div id="ve_form">the edit form, with its edits</div>';
  delete t.fields["#vmEditApply"];
  reply.reject(conflict("The VM changed during admission; review it again"));
  await saving;
  assert.equal(t.html(), '<div id="ve_form">the edit form, with its edits</div>');
  assert.deepEqual(t.toasts, [["The VM changed during admission; review it again", "bad"]]);
  assert.deepEqual(t.restarts, []);
});

/* ---------------- New k3s cluster ---------------- */
function k3s({ warnings = ["Memory above 90% on host-1"], blocked = false, ok = true, missing = false, fail = null, stopped = [] } = {}) {
  const t = harness((path, body) => {
    if (path.endsWith("/plan")) return { ok, setup: "homestead", kubevirt: true, url: "https://192.0.2.60",
      config: missing ? null : { ...body, review_id: "frozen", macs: { "demo-server-1": "52:54:00:00:00:01" } }, capacity_token: "k3s-token",
      nodes: [{ name: "demo-server-1", role: "server", address: "192.0.2.60", problem: stopped[0] }, { name: "demo-agent-1", role: "agent", address: "192.0.2.61" }],
      capacity: { blocked, warnings, blockers: [], reasons: [], example: [{ service: "demo-server-1", host: "host-1" }],
        nodes: [{ name: "host-1", metrics_available: true, baseline_gb: 10, upper_gb: 18 }] } };
    if (fail) throw fail;
    return { ok: true };
  });
  const set = Object.getOwnPropertyDescriptor(t.body, "innerHTML").set;
  Object.defineProperty(t.body, "innerHTML", { get() { return this._html; }, set(html) { set.call(this, html);
    t.fields["#k_capacity_confirm"] = html.includes('id="k_capacity_confirm"') ? { checked: false } : undefined;
    t.fields["#k_go"] = html.includes('id="k_go"') ? {} : undefined;
    t.fields["#k3sAgainLoading"] = html.includes('id="k3sAgainLoading"') ? {} : undefined; } });
  t.ctx.k3sBody = () => ({ name: "demo", password: "private-data", servers: 1, agents: 1 });
  return t;
}

test("pinned: New k3s cluster's review reads the same and needs the tick", async () => {
  const t = k3s();
  await t.ctx.k3sReview();
  assert.deepEqual(t.opened.map(o => o.slice(0, 2)), [["childModal", "Create cluster · demo"]]);
  assert.match(t.html(), /Creates 2 VMs for <b>demo<\/b>, which install k3s, Longhorn and Homestead and KubeVirt and join within 10 to 15 minutes\. Its own Homestead is then available at <span class="mono">https:\/\/192\.0\.2\.60<\/span>\./);
  assert.match(t.html(), /52:54:00:00:00:01/);
  assert.match(t.html(), /10 GiB now → up to 18 GiB/);
  assert.match(t.html(), /Example placement: demo-server-1 → host-1/);
  assert.match(t.html(), /id="k_capacity_confirm"[^>]*onchange="k3sReviewReady\(\)"/);
  assert.match(t.html(), /<button data-dialog-dismiss="true" class="btn" onclick="k3sReviewBack\(\)">Back to configuration<\/button><\/div><div class="ui-actions-end"><button class="btn pri" id="k_go" data-need="operator" onclick="k3sCreate\(\)" disabled>Create cluster<\/button>/);
  assert.doesNotMatch(t.html(), /private-data/);
  assert.equal(t.fields["#k_go"].disabled, true);
  await t.ctx.k3sCreate();
  assert.equal(t.sent.length, 1, "nothing is sent without the tick, and nothing is said");
  assert.deepEqual(t.toasts, []);
  t.fields["#k_capacity_confirm"].checked = true;
  assert.equal(t.ctx.k3sReviewReady(), true);
  await t.ctx.k3sCreate();
  assert.equal(t.fields["#k_go"].textContent, "Creating VMs…");
  assert.deepEqual(t.sent.at(-1), { path: "/api/vm/k3s-cluster", body: { name: "demo", password: "private-data", servers: 1, agents: 1, review_id: "frozen",
    macs: { "demo-server-1": "52:54:00:00:00:01" }, capacity_token: "k3s-token", confirm_capacity: true } });
  assert.deepEqual(t.toasts, [["Cluster VMs created - follow them coming up under Jobs", "ok"]]);
  assert.deepEqual(t.calls, ["closeModal", "refreshOperations", "go vms"]);
  await t.ctx.k3sCreate();
  assert.equal(t.sent.length, 2);
});

test("pinned: a stopped, blocked or unusable k3s review cannot create, and a failed plan only toasts", async () => {
  let t = k3s({ ok: false, stopped: ["no address"] });
  await t.ctx.k3sReview();
  assert.match(t.html(), /<b>demo<\/b> can't be created until what is below is resolved\./);
  assert.match(t.html(), /demo-server-1: no address/);
  assert.doesNotMatch(t.html(), /k_capacity_confirm/);
  assert.equal(t.ctx.k3sReviewReady(), false);
  t = k3s({ blocked: true, warnings: [] });
  await t.ctx.k3sReview();
  assert.match(t.html(), /The cluster cannot be created with the current settings\./);
  assert.equal(t.ctx.k3sReviewReady(), false);
  t = k3s({ missing: true });
  await t.ctx.k3sReview();
  assert.deepEqual(t.opened, []);
  assert.deepEqual(t.toasts, [["Complete VM batch review unavailable; nothing can be created yet.", "bad"]]);
});

test("pinned: a failed k3s create is not repeated and says what may exist", async () => {
  const t = k3s({ warnings: [], fail: new Error("lost response") });
  await t.ctx.k3sReview();
  await t.ctx.k3sCreate();
  assert.deepEqual(t.toasts, [["lost response", "bad"]]);
  assert.equal(t.fields["#k_go"].textContent, "Check the cluster before retrying");
  assert.equal(t.fields["#k_go"].disabled, true);
  assert.ok(seen(t.html()).startsWith("lost response. Some VMs, disks or Secrets may already exist. No request was repeated. Check Jobs and Virtual machines before reviewing again."));
  assert.deepEqual(t.calls, ["refreshOperations"]);
  t.fields["#k_go"].disabled = false;
  await t.ctx.k3sCreate();
  assert.equal(t.sent.length, 2);
});

test("pinned: a changed k3s form or Back drops the review, and none opens while creating", async () => {
  const t = k3s({ warnings: [] }), original = t.ctx.api;
  let plan;
  t.ctx.api = (path, options) => { plan = later(); return plan.promise.then(() => original(path, options)); };
  const waiting = t.ctx.k3sReview();
  t.ctx.k3sInvalidateReview();
  plan.resolve(); await waiting;
  assert.deepEqual(t.opened, [], "an answer for an older form is dropped");
  t.ctx.api = original;
  await t.ctx.k3sReview();
  t.ctx.k3sReviewBack();
  assert.deepEqual(t.calls, ["modalBack"]);
  await t.ctx.k3sCreate();
  assert.equal(t.sent.length, 2, "Back withdraws the review");
  await t.ctx.k3sReview();
  t.ctx.api = (path, options) => path === "/api/vm/k3s-cluster" ? new Promise(() => {}) : original(path, options);
  void t.ctx.k3sCreate();
  const count = t.opened.length;
  await t.ctx.k3sReview();
  assert.equal(t.opened.length, count, "no review opens while one is sent");
  t.ctx.k3sReviewBack();
  assert.deepEqual(t.calls, ["modalBack"], "Back waits for the result");
});

test("a 409 from k3s create offers a fresh review of the same batch", async () => {
  const t = k3s({ warnings: [], fail: conflict("VM batch dependencies changed; retain partial resources and review again") });
  await t.ctx.k3sReview();
  await t.ctx.k3sCreate();
  assert.deepEqual(t.toasts, [["VM batch dependencies changed; retain partial resources and review again", "bad"]]);
  assert.match(seen(t.html()), /VM batch dependencies changed\. Retain partial resources and review again\. The review was out of date\. Some VMs, disks or Secrets may already exist\. No request was repeated\. Review again to see the current state\./);
  assert.match(t.html(), /onclick="k3sReviewAgain\(\)">Review again<\/button>/);
  assert.deepEqual(t.body.focused, [".ui-actions-end .btn.pri"], "focus moves to Review again");
  assert.deepEqual(t.calls, ["refreshOperations"]);
  t.ctx.k3sBody = () => { throw new Error("the form is not read again"); };
  const original = t.ctx.api;
  let plan;
  t.ctx.api = (path, options) => { plan = later(); return plan.promise.then(() => original(path, options)); };
  const again = t.ctx.k3sReviewAgain();
  assert.equal(t.html(), '<div id="k3sAgainLoading" class="empty"><span class="spin2"></span>Checking the VMs, hosts and capacity again…</div>');
  plan.resolve(); await again;
  assert.deepEqual(t.sent.at(-1), { path: "/api/vm/k3s-cluster/plan", body: { name: "demo", password: "private-data", servers: 1, agents: 1 } });
  assert.match(t.html(), /id="k_go"/);
  assert.equal(t.ctx.k3sReviewReady(), true);
});

test("a k3s Review again whose plan fails says so in place of its loading line", async () => {
  const t = k3s({ warnings: [], fail: conflict("VM batch dependencies changed; retain partial resources and review again") });
  await t.ctx.k3sReview();
  await t.ctx.k3sCreate();
  t.ctx.icon = () => "";
  t.ctx.api = async () => { throw new Error("timed out"); };
  await t.ctx.k3sReviewAgain();
  assert.doesNotMatch(t.html(), /k3sAgainLoading/);
  assert.match(t.html(), /timed out/);
  assert.match(t.html(), /onclick="k3sReviewAgain\(\)"/);
});

test("a 409 after the k3s review was closed leaves the dialog on screen alone", async () => {
  const t = k3s({ warnings: [] }), original = t.ctx.api;
  let reply;
  t.ctx.api = (path, options) => path === "/api/vm/k3s-cluster" ? (reply = later()).promise : original(path, options);
  await t.ctx.k3sReview();
  const creating = t.ctx.k3sCreate();
  t.body.innerHTML = "<p>another dialog</p>";
  reply.reject(conflict("VM batch dependencies changed; retain partial resources and review again"));
  await creating;
  assert.equal(t.html(), "<p>another dialog</p>");
  assert.deepEqual(t.toasts, [["VM batch dependencies changed; retain partial resources and review again", "bad"]]);
  assert.deepEqual(t.calls, ["refreshOperations"]);
});

/* ---------------- the VM import's room check ---------------- */
function room(answer) {
  const t = harness(answer);
  t.fields["#uvm_room"] = t.body; // the room check draws into its own box
  t.fields["#uvm_go"] = { textContent: "Import", disabled: false };
  t.fields["#uvm_sc"] = { value: "fast" };
  const set = Object.getOwnPropertyDescriptor(t.body, "innerHTML").set;
  Object.defineProperty(t.body, "innerHTML", { get() { return this._html; }, set(html) { set.call(this, html);
    t.fields["#uvm_room_ok"] = html.includes('id="uvm_room_ok"') ? { checked: false } : undefined; } });
  t.ctx.__uvm = { source: "unraid", vm: { id: "v1", name: "vm1", disks: [{ index: 0, virtual: 10, size_gb: 10 }, { index: 1, virtual: 5, size_gb: 5 }] } };
  return t;
}

test("pinned: the import's room check says the class has room, or asks, or blocks", async () => {
  let t = room(() => ({ level: "ok", storage_class: "fast", needed_gb: 10, free_gb: 90, blockers: [], warnings: [] }));
  const checking = t.ctx.uvmRoom();
  assert.equal(t.html(), '<p class="ui-help"><span class="spin2"></span>Checking that the storage class has room for the disks…</p>');
  await checking;
  assert.deepEqual(t.sent, [{ path: "/api/vms/import-unraid/room", body: { storage_class: "fast", disks: [{ virtual: 10, size_gb: 10 }] } }]);
  assert.equal(t.html(), '<p class="ui-help">fast has room: these disks take 10 of the 90 GiB it can still place.</p>');
  assert.equal(t.fields["#uvm_go"].disabled, false);
  t = room(() => ({ level: "tight", blockers: [], warnings: ["Tight on fast"] }));
  await t.ctx.uvmRoom();
  assert.match(t.html(), /Tight on fast/);
  assert.match(t.html(), /id="uvm_room_ok"[^>]*onchange="uvmRoomReady\(\)"/);
  assert.equal(t.fields["#uvm_go"].disabled, true);
  await t.ctx.uvmStart();
  t.fields["#uvm_go"].disabled = false;
  await t.ctx.uvmStart();
  assert.deepEqual(t.toasts, [["Tick Import it anyway to accept the warning first", "bad"]]);
  t.fields["#uvm_room_ok"].checked = true;
  assert.equal(t.ctx.uvmRoomReady(), true);
  t = room(() => ({ level: "full", blockers: ["Not enough room on fast"], warnings: [] }));
  await t.ctx.uvmRoom();
  assert.doesNotMatch(t.html(), /uvm_room_ok/);
  assert.equal(t.fields["#uvm_go"].disabled, true);
  t.fields["#uvm_go"].disabled = false;
  await t.ctx.uvmStart();
  assert.deepEqual(t.toasts, [["Not enough room on fast", "bad"]]);
  t = room(() => { throw new Error("timed out"); });
  await t.ctx.uvmRoom();
  assert.match(t.html(), /The room on the storage class could not be checked\. timed out/);
  assert.equal(t.fields["#uvm_go"].disabled, true, "an unchecked room is accepted first");
});

test("pinned: only the newest room check is shown, none once the dialog is gone, and Import waits for none", async () => {
  const answers = [], t = room(() => { const a = later(); answers.push(a); return a.promise; });
  assert.equal(t.ctx.uvmRoomReady(), true, "until the check answers, the server's own check decides");
  const first = t.ctx.uvmRoom(), second = t.ctx.uvmRoom();
  answers[1].resolve({ storage_class: "fast", needed_gb: 15, free_gb: 90, blockers: [], warnings: [] }); await second;
  answers[0].resolve({ blockers: ["old answer"], warnings: [] }); await first;
  assert.match(t.html(), /take 15 of the 90 GiB/);
  assert.equal(t.ctx.uvmRoomReady(), true);
  const third = t.ctx.uvmRoom();
  delete t.fields["#uvm_room"];
  answers[2].resolve({ blockers: ["closed"], warnings: [] }); await third;
  assert.equal(t.ctx.uvmRoomReady(), true, "an answer for a closed dialog is dropped");
  t.fields["#uvm_go"].textContent = "Starting…"; t.fields["#uvm_go"].disabled = true;
  t.fields["#uvm_room"] = t.body;
  const fourth = t.ctx.uvmRoom();
  answers[3].resolve({ blockers: [], warnings: ["Tight"] }); await fourth;
  assert.equal(t.fields["#uvm_go"].disabled, true, "a button that is starting is left alone");
});

/* ---------------- options that could not be read (#620 wrap-up gap 64) ---------------- */
function unread() {
  const t = harness(path => { if (path === "/api/vm/create-options") throw new Error("timed out"); return {}; });
  const core = require("./helpers/core-source")();
  vm.runInContext(require("./helpers/between")(core, "function mustRead(", "/* ---------------- configurable hardware"), t.ctx);
  t.ctx.api = async path => {
    t.sent.push({ path });
    if (path === "/api/vm/create-options") throw Object.assign(new Error("timed out"), { status: 504 });
    if (path.startsWith("/api/vm?")) return { name: "guest", disks: [], nics: [], cores: 2, memory: "4Gi", status: "Stopped" };
    return { resources: [] };
  };
  // What the old dialogs went on to draw with: enough to show the form they built.
  Object.assign(t.ctx, { tip: () => "", icon: () => "", vmLanNetworks: () => [], vmNetworkNote: () => "No LAN network yet", vmAddressFields: () => "", vmSubnetPicked() {},
    $: key => t.fields[key] || { value: "", checked: false, dataset: {}, closest: () => ({}) } });
  return t;
}

test("VM Edit says it could not read the VM options, and offers no form built on a guess", async () => {
  const t = unread();
  await t.ctx.vmEdit("lab", "guest");
  assert.match(t.html(), /Homestead could not read what this VM can use\. timed out/);
  assert.match(t.html(), /Try again/);
  assert.match(t.html(), /onclick="vmEdit\(/);
  assert.doesNotMatch(t.html(), /ve_strategy|ve_node|Review changes/);
});

test("New k3s cluster says it could not read the VM options, and keeps no empty answer", async () => {
  const t = unread();
  t.ctx.__vmCreateOptions = undefined;
  await t.ctx.k3sCluster();
  assert.match(t.html(), /Homestead could not read what a new k3s cluster can use\. timed out/);
  assert.doesNotMatch(t.html(), /k_name|No LAN network|Review/);
  assert.equal(t.ctx.__vmCreateOptions, undefined, "a failed read is not kept as the options");
});

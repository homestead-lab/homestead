"use strict";

/* VM reviews read like a container start: one sentence, one notice when
   there is something to accept, a short acknowledgement that matches the
   action, and a button that names the result. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const core = require("./helpers/core-source")();

function context() {
  const c = { document: { addEventListener() {}, querySelector() { return null; }, getElementById(id) { return this.querySelector("#" + id); } }, FLEET: { view: {} }, icon: () => "",
    appAvatar: () => "", clusterAttr: () => "", clusterTag: () => "", remoteRow: () => false, platformTag: () => "", hardwareTags: () => "",
    fmtUp: String, svcUrl: () => "", fmtAgo: String, ratePair: () => ["0", "Mb/s"], sizePair: () => "", sizeText: () => "", meter: () => "", sev: () => "" };
  c.window = c; vm.createContext(c);
  require("./helpers/load-time")(c);
  vm.runInContext(between(core, null, "/* Put text on the clipboard."), c);
  for (const file of ["metrics-utils.js", "ui.js", "uptime.js", "autoupdate.js", "schedules.js", ...require("./helpers/workloads-source").names()])
    vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}
const plan = extra => ({ blocked: false, pod_request_gb: 2, pod_cpu_request_percent: 10, additional: 1, candidates: [],
  placement: {}, blockers: [], concerns: [], unbounded: ["home-assistant-os"], ...extra });

test("the acknowledgement names the action it accepts", () => {
  const c = context();
  assert.equal(c.reviewAck("Restarts"), "Restart it anyway");
  assert.equal(c.reviewAck("Resumes"), "Resume it anyway");
  assert.equal(c.reviewAck("Starts 3 pods of"), "Start it anyway");
  const html = c.startReview(plan(), { what: "Restarts", name: "home-assistant-os", ackId: "ok" });
  assert.match(html, /Restart it anyway/);
  assert.doesNotMatch(html, /Start it anyway/);
});

test("a schedule set in another time zone says so in its time line, not as a second notice", () => {
  const c = context(); let html = "";
  c.modal = (title, body) => { html = body; }; c.api = () => new Promise(() => {}); c.schedPreview = () => {};
  c.schedRow = () => ({ schedule: { tz: "Pacific/Chatham", days: [0, 1], start: "07:00", stop: "23:00" } });
  const zone = vm.runInContext("schedZone()", c);
  c.scheduleDialog("vm", "default", "home-assistant-os");
  assert.doesNotMatch(html, /ui-callout/);
  assert.ok(html.includes(`Times are in your time zone (${zone}). These times were set in Pacific/Chatham; saving sets them in ${zone}.`), html);
});

test("several concerns make one notice, in the worst tone, with a line each", () => {
  const c = context();
  assert.equal(c.UI.notices([]), "");
  const html = c.UI.notices([["warn", "Hugepages need a reboot"], ["bad", "GPU output needs UEFI"], ["info", "Serial is on"]], "Check before saving");
  assert.equal((html.match(/class="ui-callout /g) || []).length, 1);
  assert.match(html, /ui-callout bad/);
  assert.equal((html.match(/<li class="ui-issue">/g) || []).length, 3);
  assert.match(c.UI.notices([["warn", "Only one"]]), /ui-callout warn[\s\S]*Only one/);
});

test("the VM hardware pane shows its concerns as one notice", () => {
  const c = context();
  vm.runInContext(fs.readFileSync("web/js/vm-hardware.js", "utf8"), c);
  const box = { innerHTML: "" };
  c.document.querySelector = id => id === "#vh_notes" ? box : id === "#vh" ? {} : null;
  vm.runInContext(`vmHardwareValues = () => ({ cpu: {} }); vmHardwareNotes = () => [["warn", "one"], ["bad", "two"], ["warn", "three"]];`, c);
  c.vmHardwareChanged();
  assert.equal((box.innerHTML.match(/class="ui-callout /g) || []).length, 1);
  assert.doesNotMatch(box.innerHTML, /class="note/);
});

function vmContext(review) {
  const c = context(), shown = {}, body = { innerHTML: "" };
  for (const file of [...require("./helpers/lifecycle-source").names(), "views-vms.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  c.api = async () => review;
  c.childModal = (title, html) => { shown.title = title; shown.html = html; body.innerHTML = html; };
  c.document.querySelector = id => id === "#mbody" ? body : id === "#vmEditLoading" ? {} : null;
  c.toast = (text) => { shown.toast = text; };
  return { c, shown, body };
}
const vmPlan = extra => ({ blocked: false, pod_request_gb: 2.3, pod_cpu_request_percent: 20, additional: 1, candidates: [],
  placement: {}, blockers: [], concerns: [], unbounded: [], vm: { policy_before: "Always", policy_after: "Always" }, ...extra });
const created = { config: { namespace: "default", name: "win11", cores: 4, memory: "8Gi", mac: "02:00:00:00:00:01", network: "pod" },
  capacity_token: "t", volumes: [{ name: "win11-disk", size: "64Gi", access_mode: "RWO", volume_mode: "Block", storage_class: "longhorn-r2" }] };

test("New VM's review reads like a container start", async () => {
  const { c, shown } = vmContext({ ...created, capacity: vmPlan() });
  await c.vmCreateReview({ name: "win11" });
  assert.match(shown.title, /win11/);
  assert.doesNotMatch(shown.html, /reviewbox/);
  assert.match(shown.html, /class="ui-lead"[^>]*>Creates <b>win11<\/b>/);
  assert.match(shown.html, /win11-disk/);
  assert.doesNotMatch(shown.html, /vmCreateApprove/, "nothing to accept, no tickbox");
  assert.match(shown.html, />Create VM</);
  const warned = vmContext({ ...created, capacity: vmPlan({ unbounded: ["win11"] }) });
  await warned.c.vmCreateReview({ name: "win11" });
  assert.equal((warned.shown.html.match(/class="ui-callout /g) || []).length, 1);
  assert.match(warned.shown.html, /Create it anyway/);
});

test("Edit VM's review names its result and asks for nothing when nothing is risky", async () => {
  const { c, body } = vmContext({ capacity_token: "t", capacity: vmPlan({ warnings: [] }), volumes: [] });
  await c.vmEditReview({ ns: "default", name: "win11", cores: 4 });
  assert.doesNotMatch(body.innerHTML, /reviewbox|class="note warn"><\/div>/);
  assert.doesNotMatch(body.innerHTML, /vmEditApprove/);
  assert.match(body.innerHTML, />Save changes</);
  assert.doesNotMatch(body.innerHTML, /Save reviewed changes/);
  const warned = vmContext({ capacity_token: "t", capacity: vmPlan({ warnings: ["Memory changes need a restart to apply"] }), volumes: [] });
  await warned.c.vmEditReview({ ns: "default", name: "win11", memory: "16Gi" });
  assert.equal((warned.body.innerHTML.match(/class="ui-callout /g) || []).length, 1);
  assert.match(warned.body.innerHTML, /Save it anyway/);
});

test("a warning that is also a capacity concern is listed once", () => {
  const c = context();
  const html = c.startReview(plan({ unbounded: [], warnings: ["Partial saves keep disks"] }),
    { what: "Saves", name: "vm", warnings: ["Partial saves keep disks"], ackId: "ok" });
  assert.equal((html.match(/Partial saves keep disks/g) || []).length, 1);
});

const k3sPlan = extra => ({ ok: true, setup: "homestead", kubevirt: false, url: "https://192.0.2.60", capacity_token: "t",
  config: { cores: 2, memory: "4Gi", disk_gb: 40, macs: {} }, input: "{}",
  nodes: [{ name: "k3s-demo-server-1", role: "server", address: "192.0.2.60" }, { name: "k3s-demo-agent-1", role: "agent", address: "192.0.2.61" }],
  capacity: { blocked: false, vm_count: 2, nodes: [], blockers: [], reasons: [], warnings: [], example: [] }, ...extra });

test("New k3s cluster reviews in a child dialog that reads like the others", async () => {
  const { c, shown } = vmContext(k3sPlan());
  vm.runInContext("k3sBody = () => ({ name: 'k3s-demo' })", c);
  await c.k3sReview();
  assert.match(shown.title, /Create cluster · k3s-demo/);
  assert.doesNotMatch(shown.html, /reviewbox|class="note/);
  assert.match(shown.html, /k3s-demo-agent-1/);
  assert.doesNotMatch(shown.html, /k_capacity_confirm/, "nothing to accept, no tickbox");
  assert.match(shown.html, />Create cluster</);
  assert.doesNotMatch(shown.html, /reviewed/);
  const warned = vmContext(k3sPlan({ capacity: { ...k3sPlan().capacity, warnings: ["Memory above 90% on host-1"] } }));
  vm.runInContext("k3sBody = () => ({ name: 'k3s-demo' })", warned.c);
  await warned.c.k3sReview();
  assert.equal((warned.shown.html.match(/class="ui-callout /g) || []).length, 1);
  assert.match(warned.shown.html, /Create it anyway/);
});

test("the New k3s cluster form has no inline review or reviewed button", () => {
  const source = fs.readFileSync("web/js/views-vms.js", "utf8");
  assert.doesNotMatch(source, /Create reviewed cluster|id="k_review"/);
});

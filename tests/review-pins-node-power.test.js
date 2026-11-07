"use strict";
// Pins the host reboot / shut down review before it moves onto UI.reviewFlow (#909): its HTML, the typed-name and
// acknowledgement gates and each toast, the request, the job it follows, a refused send, a plan that is already
// a job, an incomplete plan and an older plan.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins");

const PLAN = { node: "host1", action: "reboot", review_token: "review", ready: true, pods: 1, vms: [], workloads: [], volumes: [] };
function world({ plan = PLAN, fail = false, apiPlan = null } = {}) {
  const log = [], state = { gate: null, fail };
  const fields = { "#pw_confirm": { value: "host1", dataset: { typedExpect: "host1" } }, "#pw_execute": { disabled: false, textContent: "Reboot host", onclick: "orig" } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (title, body) => log.push(["childModal", title, body]), modal: (title, body) => log.push(["modal", title, body]),
    toast: (text, tone) => log.push(["toast", text, tone]), closeModal: () => log.push(["closeModal"]), refresh: () => log.push(["refresh"]),
    setTimeout() {}, STATE: { data: {} },
    api: async (path, options) => {
      if (path.startsWith("/api/node/power/plan")) { log.push(["api", path]); if (apiPlan) return apiPlan(path); return typeof plan === "function" ? plan(path) : plan; }
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("lost reply");
      return { ok: true, operation: state.job === false ? undefined : { id: "power-job" }, steps: ["cordoned"] };
    } };
  c.window = c; vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  c.noteOperation = op => log.push(["noteOperation", op]); c.refreshOperations = () => log.push(["refreshOperations"]);
  for (const f of ["web/js/ui.js", "web/js/gitops.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  vm.runInContext(require("./helpers/workloads-source")(), c);
  vm.runInContext(require("./helpers/lifecycle-source")(), c);
  c.nodePowerFollow = (id, note) => log.push(["nodePowerFollow", id, note || null]);
  const snap = label => log.push(["state", label, fields["#pw_execute"].disabled, fields["#pw_execute"].textContent, String(fields["#pw_execute"].onclick), c.__nodePowerPlan ? c.__nodePowerPlan.node : null]);
  return { c, fields, log, state, snap };
}

pins("node power review", "review-pins-node-power", {
  async gatesAndSendOnce() {
    const w = world(); await w.c.nodePowerReview("host1", "reboot"); w.snap("reviewed");
    w.fields["#pw_confirm"].value = "wrong"; w.fields["#pw_confirm"].dataset.typedExpect = "host1";
    await w.c.nodePower("host1", "reboot"); w.snap("wrong name");
    w.fields["#pw_confirm"].value = "host1"; await w.c.nodePower("host2", "reboot"); w.snap("another host");
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.c.nodePower("host1", "reboot"), b = w.c.nodePower("host1", "reboot"); w.snap("sending");
    release(); await a; await b; w.snap("sent");
    return w.log;
  },
  async outageAndAcknowledgements() {
    const w = world({ plan: { ...PLAN, planned_outage: true, requires_data_ack: true, stranded: [{ ns: "lab", name: "homestead" }] } });
    w.fields["#pw_outage"] = { checked: false }; w.fields["#pw_data"] = { checked: false };
    await w.c.nodePowerReview("host1", "reboot"); w.snap("reviewed");
    await w.c.nodePower("host1", "reboot"); w.snap("no outage tick");
    w.fields["#pw_outage"].checked = true; await w.c.nodePower("host1", "reboot"); w.snap("no data tick");
    w.fields["#pw_data"].checked = true; await w.c.nodePower("host1", "reboot"); w.snap("sent");
    return w.log;
  },
  async strandedMustBeAccepted() {
    const w = world({ plan: { ...PLAN, stranded: [{ ns: "lab", name: "a" }], hold: [{ id: "lab/a", ns: "lab", name: "a", kind: "Deployment", options: ["move"], default: "move", hosts: ["host2"], here: 1 }] } });
    w.fields["#pw_hold_0"] = { value: "move" }; w.fields["#pw_allow"] = { checked: false };
    await w.c.nodePowerReview("host1", "reboot"); await w.c.nodePower("host1", "reboot"); w.snap("not accepted");
    w.fields["#pw_allow"].checked = true; await w.c.nodePower("host1", "reboot"); w.snap("sent");
    return w.log;
  },
  async lostReplyOffersAFreshReview() {
    const w = world({ fail: true }); await w.c.nodePowerReview("host1", "reboot");
    await w.c.nodePower("host1", "reboot"); w.snap("failed"); await w.c.nodePower("host1", "reboot");
    w.fields["#pw_execute"].onclick(); await pins.settle(); w.snap("review again");
    return w.log;
  },
  async sentWithoutAJobShowsTheSteps() {
    const w = world(); w.state.job = false; await w.c.nodePowerReview("host1", "reboot"); await w.c.nodePower("host1", "reboot");
    return w.log;
  },
  async blockedIncompleteAndFailedPlans() {
    const out = [];
    for (const plan of [{ ...PLAN, ready: false, blockers: ["quorum"] }, { ...PLAN, review_token: "" }, {}, { ...PLAN, node: "other" }]) {
      const w = world({ plan }); await w.c.nodePowerReview("host1", "reboot"); w.snap("reviewed"); await w.c.nodePower("host1", "reboot"); out.push(...w.log);
    }
    const w = world({ apiPlan: () => { throw new Error("Not found"); } }); await w.c.nodePowerReview("host1", "reboot"); out.push(...w.log);
    return out;
  },
  async aPlanThatIsAlreadyAJobIsFollowed() {
    const w = world({ plan: { operation: { id: "running" } } }); await w.c.nodePowerReview("host1", "reboot"); w.snap("followed");
    await w.c.nodePower("host1", "reboot");
    return w.log;
  },
  async forcedOverrideReviewsAgain() {
    const w = world({ plan: path => ({ ...PLAN, force: path.includes("force=1"), blockers: path.includes("force=1") ? [] : ["quorum"], ready: path.includes("force=1") }) });
    await w.c.nodePowerReview("host1", "reboot"); await w.c.nodePowerReview("host1", "reboot", true); w.snap("forced");
    await w.c.nodePower("host1", "reboot");
    return w.log;
  },
  async olderPlanCannotReplaceANewerOne() {
    const w = world(); let first; let calls = 0;
    w.c.api = (path) => ++calls === 1 ? new Promise(r => { first = r; }) : Promise.resolve({ ...PLAN, node: "host2" });
    const old = w.c.nodePowerReview("host1", "reboot"); await w.c.nodePowerReview("host2", "reboot"); first(PLAN); await old; w.snap("after the late plan");
    return w.log;
  },
});

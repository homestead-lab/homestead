"use strict";
// Pins the Deploy review (Review deployment / Review shared-pod change) before it moves onto UI.reviewFlow (#909):
// the dialog for a new app and for joining a pod, the two ticks and the button's state, the one request, a refused
// send that needs a fresh review, blocked, missing and unsupported previews, an older review and a double click.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

function world(capacity, { app_profile = undefined, fail = false, impact = {} } = {}) {
  const log = [], state = { gate: null, fail };
  let config = { name: "demo", container_name: "demo", workload_name: "demo", target_mode: "new", image: "demo:1", ports: [], volumes: [] };
  const fields = { "#deployGo": { disabled: true, textContent: "Deploy", onclick: "orig" }, "#deployCapacityConfirm": { checked: false }, "#deployConfirm": { checked: false } };
  const ctx = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null, querySelector: () => null },
    logoReady: async () => {}, $: selector => fields[selector], $$: () => [], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    api: async (path, options) => {
      const body = JSON.parse(options.body); log.push(["api", path, body]);
      if (path === "/api/preview") { if (state.preview) return state.preview(body); return { capacity, capacity_token: "signed-token", impact, app_profile, deployment: {}, service: null }; }
      if (state.gate) await state.gate;
      if (state.fail) throw new Error("rejected");
      return { ok: true, reused_volumes: ["old-claim"] };
    }, modal: (title, body) => log.push(["modal", title, body]), toast: (text, tone) => log.push(["toast", text, tone]),
    volumeListIssue: () => "", closeModal: () => log.push(["closeModal"]), go: view => log.push(["go", view]) };
  vm.createContext(ctx); require("./helpers/core-text")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => (ctx.esc || String)(ctx.jsArg(s));
  for (const file of ["ui.js", "gitops.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), ctx);
  vm.runInContext(require("./helpers/workloads-source")(), ctx);
  ctx.collect = () => config;
  const snap = label => log.push(["state", label, fields["#deployGo"].disabled, fields["#deployGo"].textContent, String(fields["#deployGo"].onclick)]);
  return { ctx, fields, log, state, snap, setConfig: c => { config = { ...config, ...c }; } };
}
const WARNING = { additional: 1, requires_confirmation: true, blocked: false, pod_request_gb: 1, pod_cpu_request_percent: 50, pod_memory_gb: 2, warnings: ["planned <volume>"],
  candidates: [{ name: "host<a>", eligible: true, metrics_available: false, reservations_known: false }] };

pins("deploy review", "review-pins-deploy", {
  async gateFrozenConfigAndSendOnce() {
    const w = world(WARNING); await w.ctx.window.doDeploy(); w.snap("reviewed");
    await w.ctx.window.confirmDeploy(); w.snap("without the tick");
    w.setConfig({ image: "changed:2" }); w.fields["#deployCapacityConfirm"].checked = true; w.ctx.window.deployReviewReady(); w.snap("ticked");
    let release; w.state.gate = new Promise(r => { release = r; });
    const a = w.ctx.window.confirmDeploy(), b = w.ctx.window.confirmDeploy(); w.snap("sending");
    release(); await a; await b; w.snap("sent");
    return w.log;
  },
  async joiningAPodNeedsItsOwnTick() {
    const w = world({ ...WARNING, requires_confirmation: false, rollout: { paused: true, strategy: "Recreate", replicas: 1, owned_pods: [], ownership_known: false } });
    w.setConfig({ target_mode: "existing", target_workload: "web" });
    await w.ctx.window.doDeploy(); w.snap("reviewed");
    await w.ctx.window.confirmDeploy(); w.snap("no tick");
    w.fields["#deployConfirm"].checked = true; w.ctx.window.deployReviewReady(); w.snap("ticked"); await w.ctx.window.confirmDeploy(); w.snap("sent");
    return w.log;
  },
  async noWarningsNeedNoTick() {
    const w = world({ ...WARNING, requires_confirmation: false, warnings: [] }); await w.ctx.window.doDeploy(); w.snap("reviewed");
    await w.ctx.window.confirmDeploy(); w.snap("sent");
    return w.log;
  },
  async blockedMissingAndUnsupportedPreviews() {
    const out = [];
    for (const [capacity, options] of [[{ ...WARNING, blocked: true }, {}], [undefined, {}], [WARNING, { app_profile: { blocked: true, label: "needs a Kubernetes design" } }]]) {
      const w = world(capacity, options); await w.ctx.window.doDeploy(); w.snap("reviewed");
      if (w.fields["#deployCapacityConfirm"]) w.fields["#deployCapacityConfirm"].checked = true; await w.ctx.window.confirmDeploy(); out.push(...w.log);
    }
    return out;
  },
  async refusedSendNeedsAFreshReview() {
    const w = world(WARNING, { fail: true }); await w.ctx.window.doDeploy(); w.fields["#deployCapacityConfirm"].checked = true;
    await w.ctx.window.confirmDeploy(); w.snap("failed"); await w.ctx.window.confirmDeploy(); w.snap("tried again");
    w.fields["#deployGo"].onclick(); await settle(); w.snap("review again");
    return w.log;
  },
  async olderReviewCannotReplaceANewerOne() {
    const w = world(WARNING); let first; const original = w.ctx.api; let n = 0;
    w.ctx.api = (path, options) => path === "/api/preview" && n++ === 0 ? new Promise(r => { first = r; }) : original(path, options);
    const old = w.ctx.window.doDeploy(); await settle(); w.setConfig({ workload_name: "newer" }); await w.ctx.window.doDeploy();
    first({ capacity: WARNING, capacity_token: "stale-token", impact: {}, deployment: {}, service: null }); await old;
    w.fields["#deployCapacityConfirm"].checked = true; await w.ctx.window.confirmDeploy(); w.snap("sent");
    return w.log;
  },
  async aSecondClickWhileTheLogoIsChecked() {
    const w = world(WARNING); const releases = []; w.ctx.logoReady = () => new Promise(r => releases.push(r));
    const first = w.ctx.window.doDeploy(); const second = w.ctx.window.doDeploy(); releases.forEach(r => r()); await first; await second; w.snap("after");
    return w.log;
  },
});

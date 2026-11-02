"use strict";
// Pins the Longhorn V2 upgrade dialogs before their reviews move onto UI.reviewFlow (#909): the upgrade review
// (target, mode, backup tick, the check tick) and the settings review (pause / resume / timeout), the status poll,
// the cross-guards between them (a send blocks the others), a refused send, and a late status reply.
const fs = require("node:fs"), vm = require("node:vm");
const pins = require("./helpers/review-pins"), { settle } = pins;

function world({ report = {} } = {}) {
  const log = [], posts = [], state = {};
  let timer = null, closed = false;
  const fields = { "#lhV2UpgradeBody": { _html: "", get innerHTML() { return this._html; }, set innerHTML(v) { this._html = v; } },
    "#lhV2UpgradeBackup": { checked: false }, "#lhV2UpgradeTimeout": { value: "60" }, "#lhV2SettingsAck": { checked: false }, "#lhV2UpgradeError": { textContent: "" } };
  const base = { installed: "v1.12.2", target: "v1.13.0", v2_volumes: 3, live_ready: true, offline_ready: false, live_blockers: [], offline_blockers: ["Stop and detach disks"],
    upgrade: { enabled: false, timeout: 60, nodes: [] }, ...report };
  const c = { window: null, console, document: { addEventListener() {}, getElementById: id => closed ? null : fields["#" + id] || null }, $: s => closed ? null : fields[s],
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"), can: () => true, icon: () => "",
    modal: (title, h) => { closed = false; log.push(["modal", title, h]); fields["#lhV2UpgradeBody"].innerHTML = h; }, childModal: (title, h) => log.push(["childModal", title, h]),
    closeModal: () => { closed = true; log.push(["closeModal"]); }, toast: (text, tone) => log.push(["toast", text, tone]), clusterComponentsPaint: () => log.push(["clusterComponentsPaint"]),
    setTimeout: f => { timer = f; log.push(["setTimeout"]); return 1; }, clearTimeout: () => log.push(["clearTimeout"]),
    api: async (path, opts) => {
      if (!opts) { log.push(["api", path]); if (state.report) return state.report(path); if (state.reportError) throw new Error(state.reportError); return base; }
      const body = JSON.parse(opts.body); log.push(["api", path, body]); posts.push({ path, body });
      if (path.endsWith("/review")) return { capacity_token: "review-token", plan: base };
      if (state.fail) throw new Error("refused");
      return new Promise(r => { state.resolvePost = r; });
    } };
  c.window = c; vm.createContext(c); vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c); vm.runInContext(fs.readFileSync("web/js/longhorn-v2-upgrade.js", "utf8"), c);
  c.noteOperation = op => log.push(["noteOperation", op]);
  const snap = label => log.push(["state", label, fields["#lhV2UpgradeBody"]?.innerHTML ?? null, posts.length]);
  return { c, fields, log, posts, state, snap, base, poll: () => timer && timer(), close: () => { closed = true; }, resolve: () => state.resolvePost({ detail: "Updated", operation: { id: "op" } }) };
}

pins("lh v2 upgrade review", "review-pins-lh-v2-upgrade", {
  async upgradeGateAndStartOnce() {
    const w = world(); await w.c.lhV2Upgrade("v1.13.0"); w.snap("reviewed");
    await w.c.lhV2UpgradeStart(); w.snap("without the backup tick");
    w.fields["#lhV2UpgradeBackup"].checked = true;
    const a = w.c.lhV2UpgradeStart(), b = w.c.lhV2UpgradeStart(); w.snap("sending");
    w.resolve(); await a; await b; w.snap("started");
    return w.log;
  },
  async offlineBlockersAreDisabledAndEscaped() {
    const w = world({ report: { offline_blockers: ["<script>bad</script>"] } }); await w.c.lhV2Upgrade("v1.13.0"); w.c.lhV2UpgradeMode("offline"); w.snap("offline");
    await w.c.lhV2UpgradeStart(); w.c.lhV2UpgradeMode("nonsense"); w.snap("after");
    return w.log;
  },
  async theUpgradeCheckNeedsItsTick() {
    const w = world(); w.fields["#cmp_check_ack"] = { checked: false };
    vm.runInContext("const STATE={data:{components:{components:[{id:'longhorn',next:'v1.13.0',check:{verdict:'blocked',findings:[]}}]}}};", w.c);
    w.c.componentCheckSection = r => r?.verdict === "blocked" ? "<section>Before you upgrade</section>" : "";
    await w.c.lhV2Upgrade("v1.13.0"); w.fields["#lhV2UpgradeBackup"].checked = true; w.snap("reviewed");
    await w.c.lhV2UpgradeStart(); w.snap("no tick"); w.fields["#cmp_check_ack"].checked = true;
    const pending = w.c.lhV2UpgradeStart(); w.resolve(); await pending; w.snap("started");
    return w.log;
  },
  async aRefusedStart() {
    const w = world(); w.state.fail = true; await w.c.lhV2Upgrade("v1.13.0"); w.fields["#lhV2UpgradeBackup"].checked = true;
    await w.c.lhV2UpgradeStart(); w.snap("refused"); await w.c.lhV2UpgradeStart(); w.snap("again");
    return w.log;
  },
  async statusViewPollsAndStops() {
    const w = world({ report: { installed: "v1.13.0", upgrade: { enabled: false, timeout: 60, current_node: "node-2", nodes: [{ node: "node-2", stage: "waiting-for-healthy-volumes", state: "in-progress", error: "<img src=x>", retries: 2 }] } } });
    await w.c.lhV2Upgrade(); w.snap("status"); await w.poll(); w.snap("polled"); w.state.reportError = "no route"; await w.poll(); w.snap("poll failed");
    w.close(); await w.poll(); w.snap("closed");
    return w.log;
  },
  async statusReportFailure() {
    const w = world(); w.state.reportError = "Longhorn is not answering"; await w.c.lhV2Upgrade("v1.13.0"); w.snap("failed");
    return w.log;
  },
  async settingsReviewApplyOnce() {
    const w = world({ report: { installed: "v1.13.0" } }); await w.c.lhV2Upgrade(); w.c.lhV2UpgradeSettings(true); await w.c.lhV2UpgradeSettingsReview(true); w.snap("reviewed");
    await w.c.lhV2UpgradeSettingsApply(); w.snap("without the tick");
    w.fields["#lhV2SettingsAck"].checked = true;
    const a = w.c.lhV2UpgradeSettingsApply(), b = w.c.lhV2UpgradeSettingsApply(); w.resolve(); await a; await b; w.snap("applied");
    return w.log;
  },
  async settingsRefusedAndTimeoutOnly() {
    const w = world({ report: { installed: "v1.13.0" } }); w.state.fail = true; await w.c.lhV2Upgrade(); w.c.lhV2UpgradeSettings(true, true); w.fields["#lhV2UpgradeTimeout"].value = "90";
    await w.c.lhV2UpgradeSettingsReview(true); w.fields["#lhV2SettingsAck"].checked = true; await w.c.lhV2UpgradeSettingsApply(); w.snap("refused");
    return w.log;
  },
  async settingsReviewStopsAndDropsTheStatusView() {
    const w = world({ report: { installed: "v1.13.0" } });
    let finish; w.state.report = () => new Promise(r => { finish = r; });
    const pending = w.c.lhV2Upgrade(); await settle(); await w.c.lhV2UpgradeSettingsReview(false); finish(w.base); await pending; w.snap("after the late status");
    await w.poll(); w.snap("polled");
    return w.log;
  },
  async oneSendBlocksTheOthers() {
    const w = world({ report: { installed: "v1.13.0" } }); await w.c.lhV2Upgrade("v1.13.0"); w.fields["#lhV2UpgradeBackup"].checked = true;
    const start = w.c.lhV2UpgradeStart(); await settle();
    w.c.lhV2UpgradeSettings(true); await w.c.lhV2UpgradeSettingsReview(true); w.snap("blocked"); w.resolve(); await start; w.snap("done");
    return w.log;
  },
  async statusViewWithNoTargetCannotStart() {
    const w = world({ report: { installed: "v1.13.0" } }); await w.c.lhV2Upgrade(); await w.c.lhV2UpgradeStart(); w.snap("nothing");
    return w.log;
  },
});

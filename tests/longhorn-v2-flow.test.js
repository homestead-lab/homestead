"use strict";
// What the shared review flow does in the Longhorn V2 upgrade dialogs (#909), run against the real dialog stack (core-modal.js), written to
// fail on the code before it: Start still sends after the settings review and Back; a send is held back while the settings review is being
// read; an error from a settings review whose dialog has gone is not shown.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const settle = () => new Promise(r => setImmediate(r));

function world() {
  const log = [], posts = [], state = {};
  // A dialog body that keeps its children when another dialog opens on top of it, as the page's does (pushModal / modalBack).
  const body = { children: [], get firstChild() { return this.children[0] || null; }, set innerHTML(h) { this.children = h ? [{ html: h }] : []; },
    get innerHTML() { return this.children.map(c => c.html).join(""); }, replaceChildren(fragment) { this.children = [...fragment.items]; }, addEventListener() {}, querySelectorAll: () => [], querySelector: () => null, focus() {} };
  const here = id => body.children.some(c => c.html.includes(`id="${id}"`));
  const fields = { "#lhV2UpgradeBody": {}, "#lhV2UpgradeTimeout": { value: "60" }, "#lhV2SettingsAck": { checked: false }, "#lhV2UpgradeBackup": { checked: false },
    "#cmp_check_ack": null, "#lhV2UpgradeError": { textContent: "" } };
  const dialogOnly = new Set(["#lhV2UpgradeBody", "#lhV2UpgradeTimeout", "#lhV2SettingsAck"]);   // present only while its dialog is the one showing
  const $ = sel => {
    if (sel === "#mbody") return body;
    if (sel === "#mtitle") return title; if (sel === "#modal") return { focus() {}, classList: { contains: () => false, add() {}, remove() {}, toggle() {} } };
    if (sel === ".modalbox") return box; if (sel === "#mback" || sel === "#jobTray") return null;
    if (!(sel in fields)) return null;
    return dialogOnly.has(sel) && !here(sel.slice(1)) ? null : fields[sel];
  };
  const title = { textContent: "", focus() {} }, box = { classList: { toggle() {}, contains: () => false }, dataset: {}, scrollTop: 0, focus() {}, querySelector: () => null, style: { setProperty() {} } };
  const report = { installed: "v1.12.2", target: "v1.13.0", v2_volumes: 3, live_ready: true, offline_ready: false, live_blockers: [], offline_blockers: ["Stop and detach disks"], upgrade: { enabled: false, timeout: 60, nodes: [] } };
  const c = { console, Map, Set, Symbol, document: { activeElement: null, createDocumentFragment: () => ({ items: [], appendChild(node) { this.items.push(node); const i = body.children.indexOf(node); if (i >= 0) body.children.splice(i, 1); } }), addEventListener() {} },
    $, $$: () => [], esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"), can: () => true, icon: () => "",
    STATE: {}, enhanceActions() {}, toast: (text, tone) => log.push(["toast", text, tone]), clusterComponentsPaint() {}, setTimeout: () => 1, clearTimeout() {},
    api: async (path, opts) => {
      if (!opts) return report;
      const b = JSON.parse(opts.body); log.push(["api", path, b]); posts.push({ path, b });
      if (path.endsWith("/review")) { if (state.review) return state.review(); return { capacity_token: "review-token", plan: report }; }
      return { detail: "Updated", operation: { id: "op" } };
    } };
  c.window = c; c.window.history = { state: null, replaceState() {} }; c.window.location = { pathname: "/", search: "" };
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  c.normaliseDialogActions = () => {};   // lays out real DOM buttons, which this body does not have
  vm.runInContext(require("./helpers/core-source").text("core-modal.js"), c);
  c.toast = (text, tone) => log.push(["toast", text, tone]);          // the real toast needs the page's toast tray
  vm.runInContext(fs.readFileSync("web/js/longhorn-v2-upgrade.js", "utf8"), c);
  return { c, fields, log, posts, state, body, title };
}
const starts = w => w.posts.filter(p => p.path === "/api/cluster/components/upgrade");

test("Start still sends after the settings review and Back", async () => {
  const w = world(); await w.c.lhV2Upgrade("v1.13.0"); w.fields["#lhV2UpgradeBackup"].checked = true;
  w.c.lhV2UpgradeSettings(true); await w.c.lhV2UpgradeSettingsReview(true);
  assert.match(w.title.textContent, /Apply V2 upgrade settings/);
  w.c.modalBack(); w.c.modalBack();                                   // back to the settings form, then to the upgrade
  assert.match(w.title.textContent, /Upgrade Longhorn to v1\.13\.0/);
  await w.c.lhV2UpgradeStart();
  assert.equal(starts(w).length, 1);
});

test("a send is held back while the settings review is being read", async () => {
  const w = world(); await w.c.lhV2Upgrade("v1.13.0"); w.fields["#lhV2UpgradeBackup"].checked = true;
  let release; w.state.review = () => new Promise(r => { release = () => r({ capacity_token: "t", plan: {} }); });
  w.c.lhV2UpgradeSettings(true);
  const reviewing = w.c.lhV2UpgradeSettingsReview(true); await settle();
  await w.c.lhV2UpgradeStart();
  assert.equal(starts(w).length, 0, "Start waits for the settings review");
  release(); await reviewing;
});

test("an error from a settings review whose dialog has gone is not shown", async () => {
  const w = world(); await w.c.lhV2Upgrade("v1.13.0");
  let fail; w.state.review = () => new Promise((_, reject) => { fail = reject; });
  w.c.lhV2UpgradeSettings(true);
  const reviewing = w.c.lhV2UpgradeSettingsReview(true); await settle();
  w.c.modalBack();                                                    // the settings dialog is closed meanwhile
  fail(new Error("the review failed")); await reviewing;
  assert.deepEqual(w.log.filter(e => e[0] === "toast"), []);
});

"use strict";
// #734: Edit, Move and Rename say when Flux or Argo CD applies the app, and a
// rename of such an app waits for its own tick.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

const FLUX = { by: "Flux", source: "Kustomization flux-system/apps", detected: true };
const capacity = { blocked: false, requires_confirmation: true, candidates: [], warnings: [], additional: 1 };

function setup(review, preview = "/api/edit/preview") {
  let html = "";
  const sent = [], notices = [], fields = { "#editCapacityConfirm": { checked: true }, "#editManagedConfirm": { checked: false },
    "#editGo": {}, "#hostMoveConfirm": { checked: true }, "#hostMoveGo": {}, "#mbody": { insertAdjacentHTML() {} } };
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    childModal: (_, body) => { html = body; }, modal: (_, body) => { html = body; }, toast: m => notices.push(m), closeModal() {}, refresh() {}, setTimeout() {},
    deployCapacityHtml: () => "capacity report", copied: [], copyText: async text => { c.copied.push(text); return true; },
    api: async (path, options) => {
      if (path === preview) return review;
      sent.push({ path, body: JSON.parse(options.body) });
      return { ok: true, name: "web2" };
    } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const file of ["ui.js", "gitops.js", ...require("./helpers/workloads-source").names(), "move.js", ...require("./helpers/lifecycle-source").names()])
    vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return { c, fields, sent, notices, html: () => html };
}

test("The edit review says Flux applies the app and names its Kustomization", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: FLUX });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.match(t.html(), /Flux applies this app \(Kustomization flux-system\/apps\)/);
  assert.match(t.html(), /lasts until Flux next applies it\. To keep it, change it in git\./);
  assert.doesNotMatch(t.html(), /editManagedConfirm/);
});

test("An app nothing else applies gets no note", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: {} });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.doesNotMatch(t.html(), /applies this app|Marked as updated/);
});

test("An app marked as updated elsewhere names the mark, not Flux", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: { by: "Renovate", source: "", detected: false } });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.match(t.html(), /Marked as updated by Renovate/);
  assert.doesNotMatch(t.html(), /Flux/);
});

test("Rename of a Flux app waits for its own tick, then says so to the server", async () => {
  const t = setup({ capacity_token: "t", capacity: { ...capacity, rename: { from: "web", to: "web2" } }, managed: FLUX });
  await t.c.window.editReview({ ns: "lab", name: "web", workload_name: "web2" });
  assert.match(t.html(), /id="editManagedConfirm"/);
  assert.match(t.html(), /Flux will bring back the old name/);
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 0);
  assert.match(t.notices.at(-1), /old name/);
  t.fields["#editManagedConfirm"].checked = true;
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].body.confirm_managed, true);
});

test("Rename of a plain app sends no confirm_managed", async () => {
  const t = setup({ capacity_token: "t", capacity: { ...capacity, rename: { from: "web", to: "web2" } }, managed: {} });
  await t.c.window.editReview({ ns: "lab", name: "web", workload_name: "web2" });
  await t.c.window.confirmEdit();
  assert.equal(t.sent.length, 1);
  assert.equal("confirm_managed" in t.sent[0].body, false);
});

test("The move review carries the same note", async () => {
  const t = setup({ capacity_token: "t", managed: FLUX,
    capacity: { blocked: false, warnings: [], move: { node: "b", mode: "preferred", target: null } } }, "/api/move/preview");
  await t.c.window.hostMoveReview({ ns: "lab", name: "web", node: "b", pin: false });
  assert.match(t.html(), /Flux applies this app/);
});

const YAML = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: lab\nspec:\n  replicas: 2";

test("The edit review offers the change as YAML for git, and copies it", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: {}, yaml: YAML });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.match(t.html(), /YAML for git/);
  assert.match(t.html(), /replicas: 2/);
  const call = t.html().match(/onclick="(gitopsCopy\(\d+\))"/);
  assert.ok(call, "a Copy as YAML for git button");
  await vm.runInContext("window." + call[1], t.c);
  assert.deepEqual(t.c.copied, [YAML]);
});

test("For a Flux app the note itself links to the copy", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: FLUX, yaml: YAML });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  const note = t.html().split("ui-callout-body")[1].split("</details>")[0].split("ui-more")[0];
  assert.match(note, /Copy as YAML for git/);
});

test("No change to commit, no YAML block", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: FLUX, yaml: "" });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.doesNotMatch(t.html(), /YAML for git/);
});

test("Image updates: an app Flux updates offers its image line to copy", async () => {
  const t = setup({});
  t.c.HomesteadUpdateState = require("../web/js/update-state.js");
  t.c.STATE = { data: { imageUpdates: { workloads: [
    { ns: "monitoring", name: "loki", available: true, managed: FLUX, yaml: "kind: Deployment\nimage: grafana/loki:3.5.0", images: [] },
    { ns: "lab", name: "web", available: true, managed: {}, yaml: "", images: [] }] } } };
  t.fields["#modal"] = { classList: { contains: () => false } };
  t.fields["#imageUpdatesLoading"] = {};
  t.c.loadImageUpdates = async () => t.c.STATE.data.imageUpdates;
  await t.c.window.imageUpdateCenter();
  const rows = t.html().split("update-center-row").slice(1);
  assert.match(rows.find(r => r.includes("loki")), /Copy as YAML for git/);
  assert.doesNotMatch(rows.find(r => r.includes("<b>web</b>")), /Copy as YAML for git/);
});

test("YAML that refers to the app's Secret says that Secret must exist in git first", async () => {
  const t = setup({ capacity_token: "t", capacity, managed: {}, yaml: YAML, secret: { name: "web-env", keys: ["main.API_TOKEN"] } });
  await t.c.window.editReview({ ns: "lab", name: "web" });
  assert.match(t.html(), /Secret <span class="mono">web-env<\/span>/);
  assert.match(t.html(), /main\.API_TOKEN/);
  assert.match(t.html(), /SOPS/);
  const plain = setup({ capacity_token: "t", capacity, managed: {}, yaml: YAML, secret: {} });
  await plain.c.window.editReview({ ns: "lab", name: "web" });
  assert.doesNotMatch(plain.html(), /SOPS/);
  assert.match(plain.html(), /public repository/);
});

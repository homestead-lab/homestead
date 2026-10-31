"use strict";
// #841: on a one-host cluster, app volumes that ask for more copies than there
// are hosts read degraded. Cluster Health says why and offers, to an admin,
// Match copies to hosts: a review listing each volume, its app and its count
// before and after. Nothing changes without that click; when hosts join, the
// same row offers to put the copies back.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const H = require("../web/js/insights.js");

const LOWER = { state: "lower", hosts: 1, volumes: [
  { volume: "pvc-a", claim: "photos", namespace: "media", app: "media/immich", from: 3, to: 1 },
  { volume: "pvc-b", claim: "notes", namespace: "docs", app: "docs/notes", from: 2, to: 1 }] };
const RAISE = { state: "raise", hosts: 2, volumes: [
  { volume: "pvc-a", claim: "photos", namespace: "media", app: "media/immich", from: 1, to: 2 }] };
const base = copies => ({ overview: { health_issues: [] }, cluster: {}, protection: { volumes: [] }, nodes: [], self: {}, settings: {}, copies });

test("Cluster Health says why the volumes read degraded and offers the review", () => {
  const row = H.advice(base(LOWER)).find(r => r.id === "one-host-copies");
  assert.equal(row.severity, "medium");
  assert.equal(row.title, "2 volumes ask for more copies than this cluster has hosts");
  assert.match(row.detail, /1 host/);
  assert.match(row.detail, /Match copies to hosts/);
  assert.equal(row.route, "one-host-copies");
});

test("once a second host can hold copies, the row offers to put them back", () => {
  const row = H.advice(base(RAISE)).find(r => r.id === "one-host-copies");
  assert.equal(row.severity, "low");
  assert.equal(row.title, "1 volume can have its copies back");
  assert.match(row.detail, /2 hosts/);
});

test("nothing to offer is no row; a failed read is said, not hidden", () => {
  for (const state of ["none", "n/a"]) assert.equal(H.advice(base({ state, hosts: 2, volumes: [] })).filter(r => r.id === "one-host-copies").length, 0);
  assert.equal(H.advice(base(undefined)).filter(r => r.id.includes("copies")).length, 0);
  const unknown = H.advice(base({ state: "unknown", hosts: 0, volumes: [], error: "Longhorn's nodes could not be read" })).find(r => r.id === "one-host-copies");
  assert.equal(unknown.title, "Could not read volume copies");
  assert.equal(unknown.detail, "Longhorn's nodes could not be read");
  assert.ok(H.advice({ ...base(), copies: null }).some(r => r.id === "unknown:copies"));
});

const SHORT = [{ kind: "Volume", name: "photos", severity: "degraded", one_host: true, reason: "This cluster has one host" },
  { kind: "Volume", name: "notes", severity: "degraded", one_host: true, reason: "This cluster has one host" },
  { kind: "Volume", name: "other", severity: "degraded", reason: "Longhorn is rebuilding or missing a replica" }];

test("the volumes short of hosts are said once, in the offer's row, not once each", () => {
  const s = base(LOWER);
  s.overview.health_issues = SHORT;
  const rows = H.advice(s);
  assert.equal(rows.filter(r => r.id.startsWith("condition:Volume:") && r.id !== "condition:Volume:other").length, 0);
  assert.equal(rows.filter(r => r.id === "one-host-copies").length, 1);
  assert.equal(rows.find(r => r.id === "condition:Volume:other").route, "storage");
});

test("without the offer's row, a volume short of hosts still shows and opens the offer", () => {
  const s = base({ state: "unknown", hosts: 0, volumes: [], error: "x" });
  s.overview.health_issues = SHORT;
  assert.equal(H.advice(s).find(r => r.id === "condition:Volume:photos").route, "one-host-copies");
});

function setup(facts, answer = { ok: true, changed: ["pvc-a", "pvc-b"], message: "2 volumes now keep one copy" }) {
  const sent = [], toasts = [], modals = [], fields = {}, reloads = [];
  const ctx = { console, STATE: { data: {} }, toasts, modals,
    esc: v => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    icon: () => "", $: key => fields[key] || (fields[key] = { innerHTML: "", disabled: false }),
    modal: (title, html) => { modals.push({ title, html }); fields["#mbody"] = { innerHTML: html }; },
    closeModal: () => modals.push({ closed: true }),
    toast: (message, kind) => toasts.push({ message, kind }),
    HealthInsights: { load: force => reloads.push(force) },
    api: async (path, options) => {
      sent.push({ path, method: options?.method || "GET", body: options?.body && JSON.parse(options.body) });
      if (path !== "/api/longhorn/one-host-copies") throw new Error("Unexpected call " + path);
      return options?.method === "POST" ? answer : structuredClone(facts);
    } };
  ctx.window = ctx; vm.createContext(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/one-host-copies.js", "utf8"), ctx);
  return { ctx, sent, toasts, modals, reloads, fields };
}

test("the review lists each volume, its app and its count before and after, and changes nothing", async () => {
  const t = setup(LOWER);
  await t.ctx.oneHostCopiesReview();
  const shown = t.modals.at(-1);
  assert.equal(shown.title, "Match copies to hosts");
  for (const text of ["photos", "media/immich", "notes", "docs/notes", "3 → 1", "2 → 1"]) assert.ok(shown.html.includes(text), text);
  // The button names the result (design.md, Reviews).
  assert.match(shown.html, /data-need="admin"[^>]*>Lower 2</);
  assert.doesNotMatch(shown.html, /healthy/, "no promise the checks cannot keep");
  assert.deepEqual(t.sent.map(s => s.method), ["GET"]);
});

test("the click sends exactly the reviewed rows, then refreshes Cluster Health", async () => {
  const t = setup(LOWER);
  await t.ctx.oneHostCopiesReview();
  await t.ctx.oneHostCopiesApply();
  const post = t.sent.find(s => s.method === "POST");
  assert.deepEqual(post.body, { action: "lower", volumes: [{ volume: "pvc-a", from: 3, to: 1 }, { volume: "pvc-b", from: 2, to: 1 }] });
  assert.deepEqual(t.toasts, [{ message: "2 volumes now keep one copy", kind: "ok" }]);
  assert.deepEqual(t.reloads, [true]);
});

test("putting copies back is the same review, named for what it does", async () => {
  const t = setup(RAISE, { ok: true, changed: ["pvc-a"], message: "1 volume has copies back" });
  await t.ctx.oneHostCopiesReview();
  assert.equal(t.modals.at(-1).title, "Put copies back");
  assert.ok(t.modals.at(-1).html.includes("1 → 2"));
  assert.match(t.modals.at(-1).html, /data-need="admin"[^>]*>Raise 1</);
  await t.ctx.oneHostCopiesApply();
  assert.equal(t.sent.find(s => s.method === "POST").body.action, "raise");
});

test("a refused click says why and changes nothing more", async () => {
  const t = setup(LOWER, { ok: false, changed: [], message: "The volumes changed since the review. Nothing was changed; review again" });
  await t.ctx.oneHostCopiesReview();
  await t.ctx.oneHostCopiesApply();
  assert.equal(t.toasts.at(-1).kind, "bad");
  assert.match(t.toasts.at(-1).message, /changed since the review/);
});

test("with nothing to offer the dialog says so and has no action", async () => {
  const t = setup({ state: "none", hosts: 2, volumes: [] });
  await t.ctx.oneHostCopiesReview();
  assert.doesNotMatch(t.modals.at(-1).html, /oneHostCopiesApply/);
  assert.match(t.modals.at(-1).html, /Nothing to change/);
});

test("where Homestead does not manage copies, the dialog does not claim they fit", async () => {
  const t = setup({ state: "n/a", hosts: 0, volumes: [] });
  await t.ctx.oneHostCopiesReview();
  assert.doesNotMatch(t.modals.at(-1).html, /fit the hosts/);
  assert.match(t.modals.at(-1).html, /Nothing to change on this cluster/);
});

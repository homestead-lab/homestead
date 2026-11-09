"use strict";
// The etcd members on the Cluster page and in the setup guide (#364).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(files) {
  const ctx = { console, Map, Set, document: { addEventListener() {} }, STATE: { data: {} }, $: () => null, $$: () => [],
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    tip: t => t };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  // The real chip; a plain button, so the onclick reads as written.
  ctx.UI.button = (label, run) => `<button onclick="${run}">${label}</button>`;
  for (const f of files) vm.runInContext(fs.readFileSync(f, "utf8"), ctx);
  return ctx;
}

test("the members are listed with a tiebreaker badge", () => {
  const ctx = load(["web/js/views-cluster.js"]);
  const html = ctx.etcdMembersHtml({ etcd_total: 3, members: [{ name: "one", ready: true, role: "server" }, { name: "pi", ready: true, role: "tiebreaker" }] }, true);
  assert.match(html, /pi[^]*tiebreaker[^]*Votes only/);
});

test("two members offer a tiebreaker to an admin", () => {
  const ctx = load(["web/js/views-cluster.js"]);
  ctx.STATE.platform = { distribution: "k3s", harvester: false };
  const cp = { etcd_total: 2, members: [{ name: "one", ready: true, role: "server" }, { name: "two", ready: true, role: "server" }] };
  assert.match(ctx.etcdMembersHtml(cp, true), /platformJoinGuide\('tiebreaker'\)/);
  assert.doesNotMatch(ctx.etcdMembersHtml(cp, false), /platformJoinGuide/);
});

test("the quorum diagram badges a tiebreaker and offers one in the placeholder", () => {
  const ctx = load(["web/js/diagrams.js"]);
  const two = ctx.Diagram.quorum([{ name: "one", ready: true }, { name: "two", ready: true }]);
  assert.match(two, /a third server or a tiebreaker/);
  const three = ctx.Diagram.quorum([{ name: "one", ready: true }, { name: "two", ready: true }, { name: "pi", ready: true, tiebreaker: true }]);
  assert.match(three, /tiebreaker · ready/);
});

test("Harvester has no join guide, so no tiebreaker is offered there", () => {
  const ctx = load(["web/js/views-cluster.js"]);
  ctx.STATE.platform = { distribution: "harvester", harvester: true };
  const cp = { etcd_total: 2, members: [{ name: "one", ready: true, role: "server" }, { name: "two", ready: true, role: "server" }] };
  assert.doesNotMatch(ctx.etcdMembersHtml(cp, true), /platformJoinGuide/);
});

test("the tiebreaker badge is a neutral tag", () => {
  const ctx = load(["web/js/views-cluster.js"]);
  const html = ctx.etcdMembersHtml({ etcd_total: 3, members: [{ name: "pi", ready: true, role: "tiebreaker" }] }, true);
  assert.match(html, /ui-chip neutral slim">tiebreaker/);
});

test("two members on k3s also point to making an app host a server", () => {
  const ctx = load(["web/js/views-cluster.js"]);
  const cp = { etcd_total: 2, members: [{ name: "one", ready: true, role: "server" }, { name: "two", ready: true, role: "server" }] };
  ctx.STATE.platform = { distribution: "k3s", harvester: false };
  assert.match(ctx.etcdMembersHtml(cp, true), /app host[^]*Host actions/);
  ctx.STATE.platform = { distribution: "rke2", harvester: false };
  assert.doesNotMatch(ctx.etcdMembersHtml(cp, true), /app host/);
});

"use strict";

/* Hosts and Apps say when metrics-server's figures are old or missing (#906),
   from the state each row carries, rather than showing nothing or zeros. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const overview = fs.readFileSync("web/js/views-overview.js", "utf8");
const workloads = fs.readFileSync("web/js/views-workloads.js", "utf8");

function notice(rows) {
  const ctx = { esc: s => String(s), fmtAgo: sec => `${Math.floor(sec / 60)}m ago`, document: {} };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(`${between(overview, "function metricsNotice(", "\n}\n", { through: true })}; this.metricsNotice = metricsNotice;`, ctx);
  return ctx.metricsNotice(rows);
}

const row = metrics => ({ name: "node-a", metrics });

test("figures that are current, or still being read, say nothing", () => {
  assert.equal(notice([row({ state: "ok", age_s: 3 })]), "");
  assert.equal(notice([row({ state: "waiting" })]), "");
  assert.equal(notice([row(undefined)]), "");
  assert.equal(notice([]), "");
  assert.equal(notice(undefined), "");
});

test("old figures are flagged with their age and why", () => {
  const html = notice([row({ state: "stale", age_s: 180, detail: "no answer within 2 s" })]);
  assert.match(html, /ui-chip warn/);
  assert.match(html, /CPU and memory from 3m ago/);
  assert.match(html, /metrics-server has not answered since/);
  assert.match(html, /no answer within 2 s/);
});

test("missing figures say metrics-server is not answering", () => {
  const html = notice([row({ state: "unavailable", last_ok: null, detail: "HTTP 503" })]);
  assert.match(html, /ui-chip warn/);
  assert.match(html, /no CPU or memory figures/);
  assert.match(html, /metrics-server is not answering \(HTTP 503\)/);
  assert.match(notice([row({ state: "unavailable", age_s: 600, detail: "HTTP 503" })]), /last answered 10m ago/);
});

test("a cluster without metrics-server says so quietly", () => {
  const html = notice([row({ state: "absent", detail: "HTTP 404" })]);
  assert.doesNotMatch(html, /warn/);
  assert.match(html, /no CPU or memory figures/);
  assert.match(html, /metrics-server is not installed/);
});

test("Hosts and Containers carry it in their subtitles", () => {
  assert.match(between(overview, "async function viewNodes", "\n}\n"), /metricsNotice\(n\)/);
  assert.match(between(workloads, "function renderWorkloads", "\n}\n"), /metricsNotice\(STATE\.data\.wl\)/);
});

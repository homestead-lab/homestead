"use strict";
// What Edit says about a container's recorded use, and the reservation it suggests.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const ctx = { console, URLSearchParams, Map, Set, Date, Promise, encodeURIComponent, clearTimeout,
    document: { addEventListener() {} }, STATE: { data: {} }, $: () => null, $$: () => [], toast: () => {}, tip: t => t,
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx); ctx.jsq = value => ctx.esc(JSON.stringify(value));
  for (const file of ["web/js/metrics-utils.js", ...require("./helpers/lifecycle-source").files()]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  // Top-level consts are not properties of the context; read them by name.
  ctx.editSpan = vm.runInContext("editSpan", ctx);
  return ctx;
}

const week = suggest => ({ days: 7, hours: 168, samples: 2016, rounds: 2016, cpu_p95_m: 118, cpu_max_m: 640, mem_p95_mi: 412, mem_max_mi: 598,
  suggest: { cpu: "180m", memory: "528Mi", capped: [], cpu_now_m: 200, memory_now_mi: 128, differs: true, ...suggest } });

test("a few hours of use are said in hours, not as a day", () => {
  const ctx = setup();
  assert.equal(ctx.editSpan({ days: 2, hours: 3 }), "3 hours");
  assert.equal(ctx.editSpan({ days: 7, hours: 168 }), "7 days");
  assert.match(ctx.editUsageWords({ ...week(), days: 2, hours: 3 }, "cpu"), /over 3 hours/);
});

test("one resource coming down and the other going up are said apart", () => {
  const html = setup().editUsageSuggestion(week({ cpu_change: "lower", memory_change: "raise", lowers: true }), 0);
  assert.match(html, /lower CPU to <b>18%<\/b> \(now 20%\)/);
  assert.match(html, /raise memory to <b>[^<]+<\/b> \(now [^)]+\)/);
  assert.doesNotMatch(html, /lower the reservation/);
});

test("a resource already close to its use is kept as it is", () => {
  const html = setup().editUsageSuggestion(week({ cpu_change: "keep", memory_change: "raise" }), 0);
  assert.match(html, /CPU stays at <b>20%<\/b>/);
});

test("a container reserving nothing is told so", () => {
  const html = setup().editUsageSuggestion(week({ cpu_now_m: 0, memory_now_mi: 0, cpu_change: "raise", memory_change: "raise" }), 0);
  assert.match(html, /raise CPU to <b>18%<\/b> \(none reserved now\)/);
});

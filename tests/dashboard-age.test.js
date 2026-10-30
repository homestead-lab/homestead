"use strict";

/* The Dashboard says how old its numbers are (#705 step 8), from the age the
   overview carries: when the leader's snapshot or the last read was taken. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const source = fs.readFileSync("web/js/views-overview.js", "utf8");

function age(o) {
  const ctx = { esc: s => String(s), fmtAgo: sec => `${Math.floor(sec / 60)}m ago`, document: {} };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(`${between(source, "function dashboardAge(", "\n}\n", { through: true })}; this.dashboardAge = dashboardAge;`, ctx);
  return ctx.dashboardAge(o);
}

test("an overview without an age says nothing about it", () => {
  assert.equal(age({}), "");
});

test("numbers read moments ago are 'updated just now'", () => {
  assert.match(age({ age_s: 4 }), /updated just now/);
  assert.doesNotMatch(age({ age_s: 4 }), /ui-chip/);
});

test("numbers read within the minute say how many seconds ago", () => {
  assert.match(age({ age_s: 20 }), /updated 20s ago/);
  assert.doesNotMatch(age({ age_s: 20 }), /warn/);
});

test("numbers a minute or more old are flagged, and say why", () => {
  const html = age({ age_s: 300 });
  assert.match(html, /ui-chip warn/);
  assert.match(html, /updated 5m ago/);
  assert.match(html, /last numbers Homestead read/);
});

test("the Dashboard's subtitle carries the age", () => {
  assert.match(between(source, "async function viewDash", "historyPaint();"), /dashboardScope\(o\)\s*\+\s*dashboardAge\(o\)/);
});

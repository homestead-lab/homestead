// One way to write a time (#503): the reader's own time, no seconds, the year
// only when it isn't this year, how long ago after a past time, UTC on hover.
"use strict";
process.env.TZ = "Europe/Berlin";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = require("./helpers/core-source")();
const ctx = { Date, Intl, esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;") };
vm.createContext(ctx);
vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
const run = src => vm.runInContext(src, ctx);
const year = new Date().getFullYear();

test("a time this year has no year and no seconds; another year's says its year", () => {
  const now = run(`fmtAt("${year}-03-04T08:09:10Z")`);
  assert.match(now, /09:09/, "08:09 UTC is 09:09 in Berlin in March");
  assert.doesNotMatch(now, /:10\b/, "no seconds");
  assert.doesNotMatch(now, new RegExp(String(year)));
  assert.match(run(`fmtAt("2019-03-04T08:09:10Z")`), /2019/);
});

test("fmtWhen takes an API time as ISO text or epoch seconds, and gives UTC on hover", () => {
  const iso = run(`fmtWhen("2019-03-04T08:09:10Z")`), secs = run(`fmtWhen(${Date.UTC(2019, 2, 4, 8, 9, 10) / 1000})`);
  assert.equal(secs, iso);
  assert.match(iso, /data-tip="2019-03-04 08:09:10 UTC"/);
  assert.match(iso, /ago/);
});

test("a future time is said in the same format, with its weekday and without ago", () => {
  const next = run(`fmtNext(Date.now() + 3 * 86400e3)`);
  assert.doesNotMatch(next, /ago/);
  assert.match(next, /data-tip="[^"]+ UTC"/);
  assert.match(next, /Mon|Tue|Wed|Thu|Fri|Sat|Sun/);
});

test("a date alone and a clock time alone come from the same helpers", () => {
  assert.doesNotMatch(run(`fmtDay("${year}-03-04T08:09:10Z")`), /09:09/);
  assert.match(run(`fmtDay("2019-03-04T08:09:10Z")`), /2019/);
  assert.equal(run(`fmtClock("${year}-03-04T08:09:10Z")`).replace(/\D/g, ""), "0909");
});

test("a duration drops a zero second part", () => {
  assert.equal(run("fmtUp(2 * 86400)"), "2d");
  assert.equal(run("fmtUp(3 * 3600)"), "3h");
  assert.equal(run("fmtUp(2 * 86400 + 5 * 3600)"), "2d 5h");
  assert.equal(run("fmtAgo(2 * 86400)"), "2d ago");
});

test("the zone line names the reader's own zone", () => {
  assert.equal(run("zoneLine()"), "Times are in your time zone (Europe/Berlin)");
});

// Log lines keep their seconds, and a forecast's day key is a calendar day,
// not a moment; everything else formats a time through core-format.js.
const ALLOWED = new Set(["logsearch.js", "operations.js"]);
test("no view formats a time itself", () => {
  const left = [];
  for (const file of fs.readdirSync("web/js").filter(f => f.endsWith(".js") && !["core-format.js", "demo.js"].includes(f) && !ALLOWED.has(f))) {
    fs.readFileSync(`web/js/${file}`, "utf8").split("\n").forEach((line, i) => {
      if (/\.toLocale(Date|Time)?String\(/.test(line) && !/forecastDate/.test(line)) left.push(`${file}:${i + 1}`);
    });
  }
  assert.deepEqual(left, [], "use fmtWhen, fmtAt, fmtNext, fmtDay or fmtClock from core-format.js");
});

test("the uptime card's durations drop a zero second part too", () => {
  const src = fs.readFileSync("web/js/views-overview.js", "utf8");
  const uptimeDuration = Function(`${between(src, "function uptimeDuration", "\n}\n", { through: true })}; return uptimeDuration;`)();
  assert.equal(uptimeDuration(2 * 86400), "2d");
  assert.equal(uptimeDuration(3 * 3600), "3h");
  assert.equal(uptimeDuration(2 * 86400 + 5 * 3600), "2d 5h");
});

test("Schedules names the reader's zone for its next runs, not just \"your time\"", () => {
  const src = require("./helpers/lifecycle-source")();
  assert.doesNotMatch(src, /next runs in your time`/);
  assert.match(src, /next runs in your time zone \(\$\{esc\(yourZone\(\)\)\}\)/);
});

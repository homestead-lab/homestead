"use strict";
/* Log alerts (logalerts.js): the list, the bars, the default name and the
   demo's answers. An alert carries counts only; the page shows the query
   behind sign-in, escaped, and only operators get the buttons that change. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const jsArg = s => JSON.stringify(String(s));
function load(role = "operator") {
  const ctx = { console, esc, jsArg, window: {}, STATE: { data: {} }, Date, Math, can: need => ({ viewer: 0, operator: 1, admin: 2 })[role] >= ({ viewer: 0, operator: 1, admin: 2 })[need] };
  ctx.UI = {
    chip: (label, tone) => `<chip ${tone || ""}>${esc(label)}</chip>`,
    button: (label, onclick, { id = "" } = {}) => `<button${id ? ` id="${id}"` : ""} onclick="${esc(onclick)}">${esc(label)}</button>`,
    callout: (kind, title, body) => `<callout ${kind}>${title}|${body}</callout>`,
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/logalerts.js", "utf8"), ctx);
  return code => vm.runInContext(code, ctx);
}
const RULE = { id: "la-0123456789ab", name: "<b>Door</b>", apps: ["lab/fri\"gate"], query: "<script>x", regex: false, case: false,
  threshold: 10, window: 1, severity: "degraded", counts: [[1, 2], [2, null], [3, 14]], status: { state: "ok", detail: "" } };

test("the default name is the app and a plain label, never the query", () => {
  const run = load();
  assert.equal(run(`logAlertDefaultName("lab/frigate")`), "frigate log alert");
  assert.equal(run(`logAlertDefaultName("")`), "Log alert");
});

test("the bars count minutes not read apart from quiet ones", () => {
  const run = load();
  const svg = run(`logAlertBars([[1, 2], [2, null], [3, 14]], 10, 1)`);
  assert.match(svg, /1 minute over 10/);
  assert.match(svg, /1 minute not read/);
  assert.match(svg, /class="la-unread"/);
  assert.match(run(`logAlertBars([], 10, 1)`), /No counts yet/);
});

test("a rule row escapes what people typed and hands ids to handlers safely", () => {
  const run = load();
  run(`globalThis.R = ${JSON.stringify(RULE)}`);
  const html = run(`logAlertRow(R, false)`);
  assert.doesNotMatch(html, /<script>|<b>Door/);
  assert.match(html, /&lt;b&gt;Door/);
  assert.match(html, /logAlertEdit\(&quot;la-0123456789ab&quot;\)/);
  assert.match(html, /logAlertDelete\(&quot;la-0123456789ab&quot;\)/);
  assert.match(html, /<chip ok>Watching<\/chip>/);
  assert.match(run(`logAlertRow(R, true)`), /<chip bad>Raised<\/chip>/);
});

test("a viewer sees the rule but not the buttons that change it", () => {
  const run = load("viewer");
  run(`globalThis.R = ${JSON.stringify(RULE)}`);
  const html = run(`logAlertRow(R, false)`);
  assert.doesNotMatch(html, /logAlertEdit|logAlertDelete/);
  assert.match(html, /Search these lines/);
});

test("a rule that could not be counted says so, never quiet", () => {
  const run = load();
  run(`globalThis.R = ${JSON.stringify({ ...RULE, status: { state: "unread", detail: "could not read frigate" } })}`);
  const html = run(`logAlertRow(R, false)`);
  assert.match(html, /<chip warn>Could not count<\/chip>/);
  assert.match(html, /could not read frigate/);
  assert.equal(run(`logAlertStatus({ state: "ok" })`), "");
  assert.match(run(`logAlertStatus({ state: "slow", detail: "the expression took more than 2 seconds" })`), /more than 2 seconds/);
});

test("a rule can be paused and resumed, and a paused one says so, never quiet (#929)", () => {
  const run = load();
  run(`globalThis.R = ${JSON.stringify(RULE)}`);
  assert.match(run(`logAlertRow(R, false)`), /<button onclick="logAlertPause\(&quot;la-0123456789ab&quot;,true\)">Pause<\/button>/);
  const note = "Homestead paused it: its expression ran out of time 10 minutes in a row.";
  run(`globalThis.P = ${JSON.stringify({ ...RULE, paused: true, status: { state: "paused", detail: note } })}`);
  const html = run(`logAlertRow(P, false)`);
  assert.match(html, /<chip neutral>Paused<\/chip>/);
  assert.doesNotMatch(html, /Watching|Could not count/);
  assert.match(html, /ran out of time 10 minutes in a row/);
  assert.match(html, /logAlertPause\(&quot;la-0123456789ab&quot;,false\)">Resume</);
  assert.match(run(`logAlertStatus({ state: "paused", detail: "" })`), /Not counted while paused/);
  const viewer = load("viewer");
  viewer(`globalThis.P = ${JSON.stringify({ ...RULE, paused: true, status: { state: "paused", detail: "" } })}`);
  assert.doesNotMatch(viewer(`logAlertRow(P, false)`), /logAlertPause/);
});

test("the demo answers like the server: its own ids, names without the query, an hour of preview", () => {
  const src = fs.readFileSync("web/js/demo.js", "utf8");
  for (const path of ["/api/logalerts", "/api/logalerts/delete", "/api/logalerts/pause", "/api/logalerts/preview"]) {
    assert.ok(src.includes(`responses["${path}"]`), path);
  }
  const block = between(src, "// Log alerts (logalerts.js)", "// End of log alerts.");
  assert.ok(block.length > 100);
  assert.match(block, /la-[0-9a-f]{12}/);
  assert.match(block, /may not repeat the search text/);
  assert.match(block, /no longer exists/);
  assert.doesNotMatch(block, /title: "[^"]*Unable to read/, "the demo's alert carries no query either");
});

test("Containers and the log search open log alerts, and a deep link does too", () => {
  assert.match(fs.readFileSync("web/js/views-workloads.js", "utf8"), /run: "logAlerts\(\)"/);
  const search = fs.readFileSync("web/js/logsearch.js", "utf8");
  assert.match(search, /logAlertFromSearch\(\)/);
  assert.match(fs.readFileSync("web/js/app.js", "utf8"), /params\.panel === "logalerts"/);
  assert.match(fs.readFileSync("web/index.html", "utf8"), /js\/logalerts\.js/);
  const audit = fs.readFileSync("scripts/audit_dialogs.mjs", "utf8");
  for (const name of ["log-alerts", "log-alert-new", "log-alert-edit"]) assert.ok(audit.includes(`["${name}"`), name);
});

test("each count is drawn at its own minute, so gaps stay gaps", () => {
  const run = load();
  const svg = run(`logAlertBars([[41, 1], [100, 2]], 10, 1, { now: 100 })`);
  const xs = [...svg.matchAll(/<rect x="([\d.]+)"/g)].map(m => Number(m[1]));
  assert.equal(xs.length, 2);
  assert.ok(xs[0] < 5, `minute 41 is the hour's first: ${xs[0]}`);
  assert.ok(xs[1] > 290, `minute 100 is now: ${xs[1]}`);
  assert.equal([...run(`logAlertBars([[10, 1], [100, 2]], 10, 1, { now: 100 })`).matchAll(/<rect /g)].length, 1, "older than an hour is left out");
});

test("a rule for an app with nothing running says so", () => {
  const run = load();
  assert.match(run(`logAlertStatus({ state: "absent", detail: "nothing of frigate is running" })`), /Could not count.*nothing of frigate is running/s);
});

test("the list says when counting stopped", () => {
  const run = load();
  assert.equal(run(`logAlertStale(1000, 1100)`), "");
  assert.equal(run(`logAlertStale(null, 1100)`), "");
  assert.match(run(`logAlertStale(1000, 1300)`), /<callout warn>.*not counted for 5 minutes/s);
});

test("the row's count is the fuller of the minute just finished and this one", () => {
  const run = load();
  const now = Math.floor(Date.now() / 60000);
  run(`globalThis.R = ${JSON.stringify({ ...RULE, counts: [[now - 1, 15], [now, 1]] })}`);
  assert.match(run(`logAlertRow(R, true)`), /last minute: 15/);
});

test("the editor keeps an app it cannot list, so saving keeps it", () => {
  const run = load();
  const html = run(`logAlertAppOptions(["lab/frigate"], "kube-system/coredns")`);
  assert.match(html, /<option value="kube-system\/coredns" selected>coredns · kube-system<\/option>/);
  assert.match(run(`logAlertAppOptions(["lab/frigate"], "lab/frigate")`), /value="lab\/frigate" selected/);
  assert.equal((run(`logAlertAppOptions(["lab/frigate"], "lab/frigate")`).match(/lab\/frigate"/g) || []).length, 1);
});

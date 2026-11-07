"use strict";
// Move containers back: the Automatically choice, what it says under each placement
// mode, what it waits for, and Resume (#588).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const MAINTENANCE = { days: [0, 1, 2, 3, 4, 5, 6], start: "02:00", duration_minutes: 120 };
function load(admin = true) {
  const c = { window: {}, console, esc: s => String(s ?? "").replaceAll("<", "&lt;"), can: role => admin || role !== "admin", jsq: s => JSON.stringify(s),
    STATE: { data: {} }, updateWindowText: () => "every day 02:00" };
  vm.createContext(c);
  require("./helpers/load-ui")(c);
  vm.runInContext(between(fs.readFileSync("web/js/views-settings.js", "utf8"), "function rebalanceModeText", "async function settingsAppSave"), c);
  return c;
}

test("the settings page offers Automatically, as value auto", () => {
  assert.match(fs.readFileSync("web/js/views-settings.js", "utf8"), /<option value="auto" [^>]*>Automatically<\/option>/);
});

test("the automatic choice says what it does under each placement mode", () => {
  const c = load();
  const spread = c.rebalanceModeText("auto", MAINTENANCE, { mode: "spread", unreserved: 0 });
  assert.match(spread, /steady for 15 minutes with no Longhorn copy left to rebuild/);
  assert.match(spread, /one at a time/);
  assert.match(spread, /Critical apps wait for the update window \(every day 02:00\)/);
  assert.match(spread, /Apps Flux applies move by restarting; the scheduler picks the host/);
  assert.match(spread, /Apps Argo CD applies stay where they are/);
  assert.match(spread, /host preference[^.]*stays where it is[^.]*about half/, "how far a restart can reach");
  for (const mode of ["default", "standby", ""]) {
    const other = c.rebalanceModeText("auto", MAINTENANCE, { mode, unreserved: 5 });
    assert.match(other, /Apps Flux or Argo CD apply stay where they are/, mode);
    assert.doesNotMatch(other, /move by restarting/, mode);
    assert.doesNotMatch(other, /reserve nothing/, mode);
  }
});

test("the other choices read as they did", () => {
  const c = load();
  assert.match(c.rebalanceModeText("suggest", MAINTENANCE), /nothing moves until someone reviews it/);
  assert.match(c.rebalanceModeText("window", MAINTENANCE), /in the update window \(every day 02:00\)/);
  assert.match(c.rebalanceModeText("off", MAINTENANCE), /Nothing moves back on its own/);
});

test("reservations are offered when many apps reserve nothing", () => {
  const c = load();
  const offered = c.rebalanceModeText("auto", MAINTENANCE, { mode: "spread", unreserved: 3 });
  assert.match(offered, /3 apps reserve nothing/);
  assert.match(offered, /Set reservations from use…/);
  assert.match(offered, /reservationsReview\(\)/);
  assert.doesNotMatch(c.rebalanceModeText("auto", MAINTENANCE, { mode: "spread", unreserved: 2 }), /reservationsReview/);
});

test("Resume shows for an admin only when stopped", () => {
  const stopped = { mode: "auto", paused: { why: "Stopped after moving 1 container" }, undone: null, waiting: [], hosts: [], later: 0, left_out: 0 };
  const admin = load(true).rebalanceStatusHtml(stopped);
  assert.match(admin, /Stopped after moving 1 container/);
  assert.match(admin, /Resume/);
  assert.doesNotMatch(load(false).rebalanceStatusHtml(stopped), /rebalanceResume/);
  assert.match(load(false).rebalanceStatusHtml(stopped), /Stopped after moving 1 container/);
  const fine = { ...stopped, paused: null, waiting: ["b steady in 6 min"], later: 2, left_out: 1 };
  const html = load(true).rebalanceStatusHtml(fine);
  assert.doesNotMatch(html, /rebalanceResume/);
  assert.match(html, /b steady in 6 min/);
  assert.match(html, /2 critical apps wait for the window/);
});

test("apps left out are listed with why", () => {
  const html = load(true).rebalanceStatusHtml({ mode: "auto", paused: null, undone: null, waiting: [], hosts: [], later: 0, left_out: 0,
    kept: [{ id: "lab/web", why: "its host preference keeps it on a" }] });
  assert.match(html, /1 container stays where it is/);
  assert.match(html, /lab\/web/);
  assert.match(html, /its host preference keeps it on a/);
});

test("apps left out are listed in the window too, with the true count", () => {
  const html = load(true).rebalanceStatusHtml({ mode: "window", paused: null, undone: null, waiting: ["A copy is rebuilding on b, so apps Flux applies wait for the next opening"],
    hosts: [], later: 0, left_out: 0, kept: [{ id: "lab/web", why: "no" }], kept_total: 40 });
  assert.match(html, /A copy is rebuilding on b/);
  assert.match(html, /40 containers stay where they are/);
  assert.match(html, /and 39 more/);
});

test("a Balance review names a person's preference it replaces", () => {
  const balance = fs.readFileSync("web/js/views-workloads-balance.js", "utf8");
  const groups = fs.readFileSync("web/js/views-workloads-groups.js", "utf8");
  for (const part of [between(balance, "function balanceContainersHtml", "function balanceVolumesHtml"), between(groups, "window.mvPlan =", "window.mvGo")])
    assert.match(part, /replaces its preference for \$\{esc\([mr]\.replaces\)\}/);
});

test("changing the placement mode refreshes what the automatic choice says", () => {
  const source = fs.readFileSync("web/js/views-settings.js", "utf8");
  assert.match(between(source, "window.placeModeChanged", "\n};\n"), /set_rebalance_text[^]*rebalanceModeText\(/);
});

test("a removed mark can be resumed too, in any mode, and says who removed it", () => {
  const html = load(true).rebalanceStatusHtml({ mode: "window", paused: null, undone: { app: "lab/db", by: "Flux" }, waiting: [], hosts: [], later: 0, left_out: 0 });
  assert.match(html, /Flux removed Homestead's restart mark from lab\/db/);
  assert.match(html, /rebalanceResume/);
});

test("nothing shows outside automatic with nothing stopped, and an unread status says so", () => {
  const c = load();
  assert.equal(c.rebalanceStatusHtml({ mode: "off", paused: null, undone: null, waiting: [], hosts: [] }), "");
  assert.equal(c.rebalanceStatusHtml({ mode: "auto", paused: null, undone: null, waiting: [], hosts: [], later: 0, left_out: 0 }), "");
  assert.match(c.rebalanceStatusHtml({ unread: "no answer" }), /no answer/);
});

test("the settings page reads the status and has somewhere to show it", () => {
  const source = fs.readFileSync("web/js/views-settings.js", "utf8");
  assert.match(source, /placement: \[\(\) => capacityForecastLoad\(\), \(\) => rebalanceStatusLoad\(\)\]/, "the section's own read (#705)");
  assert.match(between(source, "async function rebalanceStatusLoad", "\n}\n"), /api\("\/api\/recovery"\)\.catch/);
  assert.match(source, /id="set_rebalance_status"/);
  assert.match(fs.readFileSync("web/js/demo.js", "utf8"), /"\/api\/recovery"/);
  assert.match(fs.readFileSync("web/js/demo.js", "utf8"), /"\/api\/recovery\/resume"/);
});

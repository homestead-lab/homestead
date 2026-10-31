"use strict";

/* The sidebar's health pill on every page (#940): one paintHealthPill(), fed
   by the Dashboard's overview and by every /api/alerts read, which carries the
   cluster's verdict. CHECKING until the first answer, a pulsing dot while
   apps start or update, dashed once the verdict is a minute old. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const overview = fs.readFileSync("web/js/views-overview.js", "utf8");
const pwa = fs.readFileSync("web/js/pwa.js", "utf8");
const html = fs.readFileSync("web/index.html", "utf8");
const css = fs.readFileSync("web/style.css", "utf8");

function pillContext() {
  let text = "CHECKING";
  const pill = { className: "ui-chip neutral pending", title: "", dataset: { state: "checking" }, writes: 0,
    get textContent() { return text; }, set textContent(value) { text = value; this.writes++; } };
  const ctx = { $: sel => sel === "#healthPill" ? pill : null, fmtAgo: sec => `${Math.floor(sec / 60)}m ago` };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(`${between(overview, "function paintHealthPill(", "\n}\n", { through: true })}; this.paintHealthPill = paintHealthPill;`, ctx);
  vm.runInContext(`${between(overview, "function healthPillChecking(", "\n}\n", { through: true })}; this.healthPillChecking = healthPillChecking;`, ctx);
  return { ctx, pill };
}
function painted(...answers) {
  const { ctx, pill } = pillContext();
  for (const answer of answers) ctx.paintHealthPill(answer);
  return pill;
}
const classes = pill => pill.className.split(/\s+/).sort();

test("a healthy cluster is a green HEALTHY, its summary and age on hover", () => {
  const pill = painted({ state: "healthy", summary: "All nodes are healthy", age_s: 4 });
  assert.deepEqual(classes(pill), ["ok", "ui-chip"]);
  assert.equal(pill.textContent, "HEALTHY");
  assert.equal(pill.title, "All nodes are healthy · checked just now");
  assert.equal(pill.dataset.state, "healthy");
});

test("degraded is amber and critical red", () => {
  assert.deepEqual(classes(painted({ state: "degraded", age_s: 0 })), ["ui-chip", "warn"]);
  assert.deepEqual(classes(painted({ state: "critical", age_s: 0 })), ["bad", "ui-chip"]);
});

test("starting and updating stay neutral but pulse, so they do not read as not loaded", () => {
  for (const state of ["starting", "updating"]) {
    const pill = painted({ state, summary: "Workload lab/web: 0/1 replicas ready", age_s: 3 });
    assert.deepEqual(classes(pill), ["busy", "neutral", "ui-chip"]);
    assert.equal(pill.textContent, state.toUpperCase());
  }
});

test("repairing and maintenance are activities too: neutral, pulsing (#941)", () => {
  for (const state of ["repairing", "maintenance"]) {
    const pill = painted({ state, summary: "Volume data: rebuilding a copy, 40% done", age_s: 3 });
    assert.deepEqual(classes(pill), ["busy", "neutral", "ui-chip"]);
    assert.equal(pill.textContent, state.toUpperCase());
  }
});

test("the tooltip says how many seconds ago within the minute", () => {
  assert.equal(painted({ state: "healthy", summary: "Fine", age_s: 20 }).title, "Fine · checked 20s ago");
});

test("a verdict a minute or more old keeps its tone, dashed, and says how old", () => {
  assert.ok(!classes(painted({ state: "healthy", age_s: 59 })).includes("stale"));
  const pill = painted({ state: "degraded", summary: "A volume is rebuilding", age_s: 120 });
  assert.deepEqual(classes(pill), ["stale", "ui-chip", "warn"]);
  assert.equal(pill.title, "A volume is rebuilding · checked 2m ago");
});

test("a verdict that could not be read is UNKNOWN and dashed, not CHECKING", () => {
  const pill = painted(null);
  assert.deepEqual(classes(pill), ["neutral", "pending", "ui-chip"]);
  assert.equal(pill.textContent, "UNKNOWN");
  assert.match(pill.title, /could not read/i);
  assert.equal(pill.dataset.state, "unknown");
});

test("an answer without a verdict (an older server) leaves the pill as it was", () => {
  const pill = painted({ state: "healthy", age_s: 0 }, undefined);
  assert.equal(pill.textContent, "HEALTHY");
  assert.equal(painted(undefined).textContent, "CHECKING");
});

test("a later answer replaces every state class of an earlier one", () => {
  const pill = painted({ state: "updating", age_s: 90 }, { state: "healthy", age_s: 1 });
  assert.deepEqual(classes(pill), ["ok", "ui-chip"]);
});

test("the pill starts as CHECKING, dashed, and is announced as a status", () => {
  const tag = html.match(/<div id="healthPill"[^>]*>[^<]*<\/div>/)?.[0] || "";
  assert.match(tag, /role="status"/);
  assert.match(tag, /class="ui-chip neutral pending"/);
  assert.match(tag, />CHECKING</);
  assert.match(tag, /data-state="checking"/);
});

test("the Dashboard paints the pill through paintHealthPill, with the overview's age", () => {
  const dash = between(overview, "async function viewDash", "historyPaint();");
  assert.match(dash, /paintHealthPill\(\{ state: o\.health_state \|\| o\.health, summary: o\.health_summary, age_s: o\.age_s \}\)/);
  assert.doesNotMatch(dash, /hp\.className/);
});

function pwaContext(now) {
  const ctx = { PWA_ALERTS: { at: 10_000, health: null }, Date: { now: () => now.value } };
  vm.createContext(ctx);
  vm.runInContext(`${between(pwa, "function pwaHealth(", "\n}\n", { through: true })}; this.pwaHealth = pwaHealth;`, ctx);
  return ctx;
}

test("every alerts read paints the pill, aged by the time since that read", () => {
  assert.match(between(pwa, "function paintPwaAlerts(", "\n}\n"), /paintHealthPill\?\.\(pwaHealth\(report\)\)/);
  const now = { value: 25_500 }, ctx = pwaContext(now);
  assert.equal(ctx.pwaHealth({ health: { state: "healthy", age_s: 8 } }).age_s, 23);
  assert.equal(ctx.pwaHealth({}), undefined, "a server without the field leaves the pill alone");
});

test("no verdict in an answer, or a failed read, ages the last verdict, or is UNKNOWN when there was none", () => {
  const now = { value: 10_000 }, ctx = pwaContext(now);
  assert.equal(ctx.pwaHealth({ health: null }), null);
  assert.equal(ctx.pwaHealth(null), null);
  ctx.pwaHealth({ health: { state: "degraded", summary: "Volume rebuilding", age_s: 5 } });
  now.value = 80_000;
  const aged = ctx.pwaHealth({ health: null });
  assert.equal(aged.state, "degraded");
  assert.equal(aged.age_s, 75);
  assert.equal(ctx.pwaHealth(null).age_s, 75);
  // The failure path paints exactly that.
  assert.match(between(pwa, "}catch(error){", "finally{"), /paintHealthPill\?\.\(pwaHealth\(null\)\)/);
});

test("a server that has no verdict to give paints UNKNOWN dashed, like an unread one", () => {
  const pill = painted({ state: "unknown", summary: "", age_s: 3 });
  assert.deepEqual(classes(pill), ["neutral", "pending", "ui-chip"]);
  assert.equal(pill.textContent, "UNKNOWN");
});

test("the pill's text is rewritten only when it changes, so a screen reader is not told every 20 s", () => {
  const { ctx, pill } = pillContext();
  ctx.paintHealthPill({ state: "healthy", summary: "Fine", age_s: 1 });
  ctx.paintHealthPill({ state: "healthy", summary: "Fine", age_s: 21 });
  assert.equal(pill.writes, 1);
  assert.equal(pill.title, "Fine · checked 21s ago");
});

test("signing out puts the pill back to CHECKING and forgets the last verdict", () => {
  const { ctx, pill } = pillContext();
  ctx.paintHealthPill({ state: "critical", summary: "Node down", age_s: 1 });
  ctx.healthPillChecking();
  assert.deepEqual(classes(pill), ["neutral", "pending", "ui-chip"]);
  assert.equal(pill.textContent, "CHECKING");
  assert.equal(pill.dataset.state, "checking");
  const stop = between(pwa, "window.stopAlertChecks=", "\n");
  assert.match(stop, /PWA_ALERTS\.health=null/);
  assert.match(stop, /healthPillChecking\?\.\(\)/);
});

test("a tab coming back into view reads the alerts again", () => {
  assert.match(pwa, /addEventListener\("visibilitychange",\s*\(\)\s*=>\s*\{\s*if\s*\(!document\.hidden && PWA_ALERTS\.timer\)\s*refreshPwaAlerts\(\);?\s*\}\)/);
});

test("the pulse stops when motion is off or reduced", () => {
  assert.match(css, /#healthPill\.busy::before\{[^}]*animation:hpPulse/);
  assert.match(css, /html\[data-motion="off"\] #healthPill\.busy::before\{animation:none\}/);
  assert.match(css, /@media \(prefers-reduced-motion:reduce\)\{#healthPill\.busy::before\{animation:none\}\}/);
});

test("the dashed states keep a visible border in the pill's own tone", () => {
  assert.match(css, /#healthPill\.pending,#healthPill\.stale\{border-style:dashed;border-color:currentColor;background:transparent\}/);
});

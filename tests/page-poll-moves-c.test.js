"use strict";
// Two Settings page loops moved onto UI.poll (#1406, Pages): a host check running
// (the Host updates card, every 4 s) and Homestead's health (Settings > Homestead,
// every 15 s). Real ui.js (so the real UI.poll) and the view file, over
// tests/helpers/dialog-poll-world.js: a clock the test moves, a tab that can be
// hidden, and every read held until the test answers it. Leaving the page is what
// go() does: the card goes, NAV_TOKEN moves on and the page's polls stop.
// It is NOT a browser: the browser groups open Settings for real.
const test = require("node:test");
const assert = require("node:assert/strict");
const world = require("./helpers/dialog-poll-world");

const GIVE_UP = every => Math.max(5 * every, 30000);   // UI.poll's give-up

function page(file, card, extra = {}) {
  const w = world(file, { NAV_TOKEN: 0, STATE: { view: "settings", data: {} }, fmtAgo: s => `${s} s`, ...extra });
  w.fields[card] = { innerHTML: "" };
  w.leave = () => { delete w.fields[card]; w.ctx.NAV_TOKEN++; w.ctx.UI.stopPagePolls(); };
  return w;
}

/* ---------------- Host updates card: a host check running, every 4 s ---------------- */
const OS_CARD = "#settingsHostUpdates", OS_EVERY = 4000;
const osReport = running => ({ applies: true, every_s: 21600, hosts: {}, rollout: null, last: null, settings: {},
  checking: running ? { running: true, hosts: ["node-a", "node-b"], done: ["node-a"], failed: {} } : { running: false, hosts: [], done: [] } });

function osCard() {
  const w = page("host-os.js", OS_CARD);
  w.checks = () => w.reads.filter(r => r.path === "/api/os-updates");
  return w;
}
// Painted with a check running, its first read answered: the card now follows the check.
async function osFollowing() {
  const w = osCard();
  w.ctx.osUpdatesCardPaint();
  await w.clock.settle();
  w.checks()[0].answer(osReport(true)); await w.clock.settle();
  return w;
}

test("host check: while a check runs the card reads again 4 s after each answer", async () => {
  const w = await osFollowing();
  assert.match(w.fields[OS_CARD].innerHTML, /Checking hosts/);
  await w.clock.advance(OS_EVERY - 1);
  assert.equal(w.checks().length, 1);
  await w.clock.advance(1);
  assert.equal(w.checks().length, 2, "read again 4 s after the answer");
  await w.clock.advance(GIVE_UP(OS_EVERY) - 1000);
  assert.equal(w.checks().length, 2, "no second read beside one still on its way");
  w.checks()[1].answer(osReport(true)); await w.clock.settle();
  await w.clock.advance(OS_EVERY);
  assert.equal(w.checks().length, 3);
});

test("host check: the reads end when the check has finished, and none start without one", async () => {
  const w = await osFollowing();
  await w.clock.advance(OS_EVERY);
  w.checks()[1].answer(osReport(false)); await w.clock.settle();
  assert.match(w.fields[OS_CARD].innerHTML, /Check now/);
  await w.clock.advance(10 * OS_EVERY);
  assert.equal(w.checks().length, 2);
  assert.equal(w.clock.pending(), 0, "no timer is left behind");
  const idle = osCard();
  idle.ctx.osUpdatesCardPaint(); await idle.clock.settle();
  idle.checks()[0].answer(osReport(false)); await idle.clock.settle();
  await idle.clock.advance(10 * OS_EVERY);
  assert.equal(idle.checks().length, 1, "no check running, no follow-up read");
});

test("host check: a hidden tab reads nothing, and reads once when shown", async () => {
  const w = await osFollowing();
  w.document.show(false);
  await w.clock.advance(20 * OS_EVERY);
  assert.equal(w.checks().length, 1, "no read while the tab is hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(w.checks().length, 2, "one read when it is shown again");
});

test("host check: leaving the page reads no more and cancels the read on its way", async () => {
  const w = await osFollowing();
  await w.clock.advance(OS_EVERY);
  const read = w.checks()[1];
  w.leave();
  assert.equal(read.signal?.aborted, true, "the read on its way is cancelled");
  read.answer(osReport(true)); await w.clock.settle();
  await w.clock.advance(20 * OS_EVERY);
  assert.equal(w.checks().length, 2, "nothing reads after the page is left");
  assert.equal(w.clock.pending(), 0, "no timer is left waiting for the page left behind");
});

// As before the move, a failed read shows the failure with Try again and the card stops following.
test("host check: a read with no answer in 30 s is given up and shown, with Try again", async () => {
  const w = await osFollowing();
  await w.clock.advance(OS_EVERY);
  await w.clock.advance(GIVE_UP(OS_EVERY) - 1);
  assert.doesNotMatch(w.fields[OS_CARD].innerHTML, /No response/, "not before its limit");
  await w.clock.advance(1);
  assert.match(w.fields[OS_CARD].innerHTML, /could not be read[\s\S]*No response in 30 s/);
  assert.match(w.fields[OS_CARD].innerHTML, /Try again/);
  await w.clock.advance(10 * OS_EVERY);
  assert.equal(w.checks().length, 2, "a failed read ends the follow, as before the move");
});

test("host check: painted again while it follows (Check now), it runs one loop", async () => {
  const w = await osFollowing();
  w.pollAuto = r => r.answer(osReport(true));
  w.answers.auto = r => w.pollAuto(r);
  w.ctx.osUpdatesCardPaint(); await w.clock.settle();
  w.ctx.osUpdatesCardPaint(); await w.clock.settle();
  const before = w.checks().length;
  await w.clock.advance(4 * OS_EVERY);
  assert.equal(w.checks().length - before, 4, "one read per interval, not two or three");
});

/* ---------------- Homestead's health (Settings > Homestead), every 15 s ---------------- */
const HEALTH_CARD = "#selfHealthCard", HEALTH_EVERY = 15000;
const HEALTH = { loops: [], api: { ok: true, ms: 20 }, version: "1.0.0", replicas: { desired: 1, pods: [] }, leader: true };

function health(section = "homestead") {
  const store = { "homestead.settings.tab": section };
  const w = page("views-settings.js", HEALTH_CARD, { localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } } });
  w.section = name => { store["homestead.settings.tab"] = name; };
  w.checks = () => w.reads.filter(r => r.path === "/api/self/health");
  return w;
}
async function healthShown() {
  const w = health();
  w.ctx.selfHealthPaint(); await w.clock.settle();
  w.checks()[0].answer(HEALTH); await w.clock.settle();
  return w;
}

test("health: read again 15 s after each answer while its section is shown", async () => {
  const w = await healthShown();
  assert.match(w.fields[HEALTH_CARD].innerHTML, /all well/);
  await w.clock.advance(HEALTH_EVERY - 1);
  assert.equal(w.checks().length, 1);
  await w.clock.advance(1);
  assert.equal(w.checks().length, 2);
  w.checks()[1].answer(HEALTH); await w.clock.settle();
  await w.clock.advance(HEALTH_EVERY);
  assert.equal(w.checks().length, 3);
});

test("health: one failed read is shown, and the next one 15 s later replaces it", async () => {
  const w = await healthShown();
  await w.clock.advance(HEALTH_EVERY);
  w.checks()[1].fail(new Error("timed out")); await w.clock.settle();
  assert.match(w.fields[HEALTH_CARD].innerHTML, /Status unavailable[\s\S]*timed out/);
  await w.clock.advance(HEALTH_EVERY);
  assert.equal(w.checks().length, 3);
  w.checks()[2].answer(HEALTH); await w.clock.settle();
  assert.doesNotMatch(w.fields[HEALTH_CARD].innerHTML, /Status unavailable/);
});

test("health: on another Settings section it reads nothing, and reads again at its next turn once the section is back", async () => {
  const w = await healthShown();
  w.section("updates");
  await w.clock.advance(5 * HEALTH_EVERY);
  assert.equal(w.checks().length, 1, "no health read on another section");
  w.section("homestead");
  await w.clock.advance(HEALTH_EVERY);
  assert.equal(w.checks().length, 2);
});

test("health: a hidden tab reads nothing, and reads once when shown, not on another section", async () => {
  const w = await healthShown();
  w.document.show(false);
  await w.clock.advance(20 * HEALTH_EVERY);
  assert.equal(w.checks().length, 1, "no read while the tab is hidden");
  w.document.show(true); await w.clock.settle();
  assert.equal(w.checks().length, 2, "one read when it is shown again");
  w.checks()[1].answer(HEALTH); await w.clock.settle();
  w.section("updates");
  w.document.show(false); await w.clock.advance(HEALTH_EVERY);
  w.document.show(true); await w.clock.settle();
  assert.equal(w.checks().length, 2, "shown again on another section: still no health read");
});

test("health: leaving Settings reads no more and cancels the read on its way", async () => {
  const w = await healthShown();
  await w.clock.advance(HEALTH_EVERY);
  const read = w.checks()[1];
  w.leave();
  assert.equal(read.signal?.aborted, true, "the read on its way is cancelled");
  read.answer(HEALTH); await w.clock.settle();
  await w.clock.advance(20 * HEALTH_EVERY);
  assert.equal(w.checks().length, 2, "nothing reads after Settings is left");
  assert.equal(w.clock.pending(), 0, "no timer is left waiting for the page left behind");
});

test("health: a read with no answer in 75 s is given up, shown, and the next read follows", async () => {
  const w = await healthShown();
  await w.clock.advance(HEALTH_EVERY);
  await w.clock.advance(GIVE_UP(HEALTH_EVERY) - 1);
  assert.doesNotMatch(w.fields[HEALTH_CARD].innerHTML, /No response/, "not before its limit");
  await w.clock.advance(1);
  assert.match(w.fields[HEALTH_CARD].innerHTML, /Status unavailable[\s\S]*No response in 75 s/);
  await w.clock.advance(HEALTH_EVERY);
  assert.equal(w.checks().length, 3, "the card is read again");
});

test("health: painted again (a setting switched, Settings redrawn), it runs one loop", async () => {
  const w = await healthShown();
  w.answers.auto = r => r.answer(HEALTH);
  w.ctx.selfHealthPaint(); await w.clock.settle();
  w.ctx.selfHealthPaint(); await w.clock.settle();
  const before = w.checks().length;
  await w.clock.advance(4 * HEALTH_EVERY);
  assert.equal(w.checks().length - before, 4, "one read per interval, not two or three");
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const CRON = require("../web/js/cron.js");

const iso = dates => dates.map(d => d.toISOString().slice(0, 16));

test("the editor's shapes survive a round trip through cron", () => {
  for (const cron of ["*/15 * * * *", "5 * * * *", "0 */6 * * *", "30 2 * * *", "0 3 * * 0", "0 3 * * 1,2,3,4,5", "0 4 1 * *"]) {
    assert.equal(CRON.fromPlan(CRON.toPlan(cron)), cron);
  }
  assert.equal(CRON.toPlan("0 3 * * 7").days.join(), "0");
  assert.equal(CRON.toPlan("0 0 31 * *").kind, "custom");
  assert.equal(CRON.toPlan("*/7 * * * *").kind, "custom");
});

test("schedules read in words", () => {
  assert.equal(CRON.describe("0 2 * * *"), "Every day at 02:00 UTC");
  assert.equal(CRON.describe("0 * * * *"), "Every hour at :00");
  assert.equal(CRON.describe("0 */6 * * *"), "Every 6 hours at :00");
  assert.equal(CRON.describe("0 3 * * 0"), "Sunday at 03:00 UTC");
  assert.equal(CRON.describe("0 3 * * 1,2,3,4,5"), "Weekdays at 03:00 UTC");
  assert.equal(CRON.describe("0 4 1 * *"), "Monthly on the 1st at 04:00 UTC");
  assert.equal(CRON.describe("0 0 31 * *"), "Custom · 0 0 31 * *");
  assert.equal(CRON.describe("nonsense"), "Invalid · nonsense");
});

test("next runs follow cron's rules in UTC", () => {
  const from = new Date(Date.UTC(2026, 8, 23, 14, 7));   // Wednesday
  assert.deepEqual(iso(CRON.next("0 2 * * *", from, 2)), ["2026-09-24T02:00", "2026-09-25T02:00"]);
  assert.deepEqual(iso(CRON.next("*/15 * * * *", from, 2)), ["2026-09-23T14:15", "2026-09-23T14:30"]);
  assert.deepEqual(iso(CRON.next("0 3 * * 0", from, 1)), ["2026-09-27T03:00"]);
  assert.deepEqual(iso(CRON.next("0 4 1 * *", from, 1)), ["2026-10-01T04:00"]);
  // Both day fields restricted: either matches.
  assert.deepEqual(iso(CRON.next("0 0 1 * 5", from, 2)), ["2026-09-25T00:00", "2026-10-01T00:00"]);
  assert.deepEqual(CRON.next("0 0 30 2 *", from, 1), []);
  assert.deepEqual(CRON.next("61 * * * *", from, 1), []);
});

const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync("web/js/views-settings.js", "utf8");
const start = src.indexOf("const DAYS = ["), end = src.indexOf("window.updateWindowEdit");
const ctx = { Date, String, Math, Number, Set };
vm.createContext(ctx);
vm.runInContext(src.slice(start, end) + ";this.updateWindowText = updateWindowText; this.updateWindowDays = updateWindowDays;", ctx);

test("the days read at a glance", () => {
  assert.equal(ctx.updateWindowDays([0, 1, 2, 3, 4, 5, 6]), "Every day");
  assert.equal(ctx.updateWindowDays([0, 1, 2, 3, 4]), "Weekdays");
  assert.equal(ctx.updateWindowDays([5, 6]), "Weekends");
  assert.equal(ctx.updateWindowDays([1, 2, 3, 4, 5, 6]), "Tue–Sun");
  assert.equal(ctx.updateWindowDays([0, 2, 4]), "Mon, Wed, Fri");
  assert.equal(ctx.updateWindowDays([]), "No days");
});

test("the window says when it ends, in UTC", () => {
  const text = ctx.updateWindowText({ days: [0, 1, 2, 3, 4], start: "02:00", duration_minutes: 120 });
  assert.match(text, /^Weekdays · 02:00–04:00 UTC/);
  assert.match(ctx.updateWindowText({ days: [6], start: "23:30", duration_minutes: 60 }), /23:30–00:30 UTC/, "past midnight");
});

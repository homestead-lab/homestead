const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ctx = { console, esc, jsq: s => esc(JSON.stringify(String(s))), jsArg: s => JSON.stringify(String(s ?? "")),
  icon: name => `<i data-icon="${name}"></i>`, window: {}, STATE: { data: {} }, Date, Intl };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/schedules.js", "utf8"), ctx);
const run = code => vm.runInContext(code, ctx);

test("a schedule reads as words", () => {
  assert.equal(run(`schedWords({stop: "01:00", start: "07:00", days: [0,1,2,3,4]})`), "Stops 01:00, starts 07:00, weekdays");
  assert.equal(run(`schedWords({start: "08:00", days: [5,6]})`), "Starts 08:00, weekends");
  assert.equal(run(`schedWords({stop: "23:30", days: [0,1,2,3,4,5,6]})`), "Stops 23:30, every day");
  assert.equal(run(`schedWords({stop: "23:30", days: [1,3]})`), "Stops 23:30, Tue, Thu");
});

test("the next times count days from Monday and skip days not chosen", () => {
  // Friday 9 October 2026, 08:00 local.
  const next = run(`schedNext({stop: "01:00", start: "07:00", days: [0,1,2,3,4]}, new Date(2026, 9, 9, 8, 0)).map(e => [e.at.getDate(), e.at.getHours(), e.action])`);
  assert.deepEqual(JSON.parse(JSON.stringify(next)), [[12, 1, "stop"], [12, 7, "start"], [13, 1, "stop"]]);
  const today = run(`schedNext({stop: "22:00", days: [4]}, new Date(2026, 9, 9, 8, 0))[0].at.getDate()`);
  assert.equal(today, 9, "later the same day counts");
});

test("a row with a schedule is marked, and says it", () => {
  assert.equal(run(`scheduleTag({})`), "");
  const tag = run(`scheduleTag({schedule: {stop: "01:00", days: [0], tz: "UTC"}})`);
  assert.match(tag, /scheduled/);
  assert.match(tag, /data-tip="Stops 01:00, Mon"/);
});

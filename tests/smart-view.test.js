"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, console };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/views-overview.js", "utf8"), context);

const tone = value => vm.runInContext(`smartTestTone(${JSON.stringify(value)})`, context);

test("SMART self-test results do not mistake 'without error' for a failure", () => {
  assert.equal(tone("Completed without error"), "ok");
  assert.equal(tone("Completed successfully"), "ok");
  assert.equal(tone("Completed: read failure"), "bad");
  assert.equal(tone("Self-test in progress"), "");
});

/* A running self-test is one UI.callout, not the last hand-built .clusteralert
   banner; the banner's styles go with it (#559). */
test("a running self-test is a UI.callout and .clusteralert is gone", () => {
  const source = fs.readFileSync("web/js/views-overview.js", "utf8");
  const line = source.split("\n").find(l => l.includes("Self-test running"));
  assert.ok(line, "the drive dialog says a self-test is running");
  assert.match(line, /UI\.callout\("info", "Self-test running"/);
  for (const file of ["web/style.css", ...fs.readdirSync("web/js").map(f => `web/js/${f}`)])
    assert.doesNotMatch(fs.readFileSync(file, "utf8"), /clusteralert/, `${file} has no .clusteralert`);
});

/* A drive left asleep by the regular SMART read says so, with its reading's age (#974). */
test("an asleep drive says it is asleep and how old its last reading is", () => {
  context.fmtAgo = sec => `${sec}s ago`;
  const text = (smart, health, now) => vm.runInContext(
    `diskAsleepText(${JSON.stringify(smart)}, ${JSON.stringify(health)}, ${now})`, context);
  assert.equal(text({ asleep: true }, { state: "asleep", read_at: 1000 }, 8200), "asleep · last read 7200s ago");
  assert.equal(text({ asleep: true }, { state: "critical", read_at: 1000 }, 1060), "asleep · last read 60s ago");
  assert.equal(text({ asleep: true }, { state: "asleep" }, 1000), "asleep · not read awake yet");
  assert.equal(text({}, { state: "healthy", read_at: 1000 }, 1000), "");
  assert.equal(text(null, null, 1000), "");
  const source = fs.readFileSync("web/js/views-overview.js", "utf8");
  assert.equal((source.match(/diskAsleepText\(/g) || []).length, 2, "used on the drive row and the drive page");
});

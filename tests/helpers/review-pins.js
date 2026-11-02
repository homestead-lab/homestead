"use strict";
// Pins for a review dialog before and after it moves onto UI.reviewFlow (#909). A scenario returns a
// transcript (the log of what the dialog drew, asked, said and did, with snapshots of its buttons); it is
// compared with tests/fixtures/<file>.json, which was recorded from the hand-written version
// (RECORD_PINS=1 re-records). One shared place for the compare, so each flow's test is only its world.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");

module.exports = function pins(label, fixtureName, scenarios) {
  const file = `tests/fixtures/${fixtureName}.json`;
  for (const [name, run] of Object.entries(scenarios)) test(`${label}: ${name}`, async () => {
    const transcript = JSON.parse(JSON.stringify(await run()));
    const all = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
    if (process.env.RECORD_PINS) { all[name] = transcript; fs.writeFileSync(file, JSON.stringify(all, null, 1) + "\n"); return; }
    assert.deepEqual(transcript, all[name]);
  });
};
// A tick of the event loop long enough for a chain of awaits to settle.
module.exports.settle = () => new Promise(resolve => setImmediate(resolve));

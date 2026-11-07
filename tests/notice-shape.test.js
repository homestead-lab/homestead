"use strict";
// Every warning and error goes through UI.callout or UI.notices, which show
// its short first line and keep the rest behind More (design.md, Notices).
// A hand-written .note box has no short form, so none are left to write.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

const files = fs.readdirSync("web/js").filter(f => f.endsWith(".js")).map(f => path.join("web/js", f));
const OLD_BOX = /class="note[^"]*\b(?:warn|bad|crit|dependency-danger)\b|class="note \$\{/;

test("no warning or error is a hand-written .note box", () => {
  const found = files.flatMap(file => fs.readFileSync(file, "utf8").split("\n")
    .map((line, n) => OLD_BOX.test(line) ? `${file}:${n + 1}` : "").filter(Boolean));
  assert.deepEqual(found, [], "use UI.callout(tone, title, body) or UI.notices(items, title)");
});

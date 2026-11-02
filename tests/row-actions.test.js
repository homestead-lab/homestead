"use strict";
// Row actions (#559): every table cell that holds a row's actionBar is marked
// data-actions, so style.css can size it to its buttons and end them at the
// table's right edge, on a desktop as on a phone.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

// Cells that mix an actionBar with text, where a cell as narrow as its buttons
// would squeeze the text: each is left unmarked on purpose.
const MIXED = [
  ["views-settings.js", "ns-kept", "a namespace's Delete, or why it is kept"],
  ["views-lifecycle-import.js", "w.movable", "a move report's app: Move, or why it can't"],
  ["views-lifecycle-import.js", "v.movable", "a move report's volume: Move, or why it can't"],
];

test("every table cell holding an actionBar is a data-actions cell", () => {
  const dir = path.join(__dirname, "..", "web", "js"), unmarked = [];
  for (const file of fs.readdirSync(dir).filter(f => f.endsWith(".js"))) {
    const text = fs.readFileSync(path.join(dir, file), "utf8");
    // Each cell from its opening tag to its </td>, so a conditional or a
    // multi-line actionBar counts too.
    for (const m of text.matchAll(/<td([^>]*)>\$\{([\s\S]*?)<\/td>/g)) {
      if (!m[2].includes("actionBar(") || /\bdata-actions\b/.test(m[1])) continue;
      if (MIXED.some(([f, marker]) => f === file && m[2].includes(marker))) continue;
      unmarked.push(`${file}:${text.slice(0, m.index).split("\n").length}`);
    }
  }
  assert.deepEqual(unmarked, []);
});

test("each mixed cell still exists, so the list can't go stale", () => {
  const dir = path.join(__dirname, "..", "web", "js");
  for (const [file, marker] of MIXED)
    assert.ok(fs.readFileSync(path.join(dir, file), "utf8").includes(marker), `${file} still has ${marker}`);
});

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

/* The demo answers each API path from one object literal. A path written twice
   is not an error in JavaScript - the later one silently wins - which is how a
   cross-cluster move answer once replaced the node-move one and left the Move
   dialog spinning. */
test("the demo answers each API path once", () => {
  const source = fs.readFileSync("web/js/demo.js", "utf8");
  const paths = [...source.matchAll(/^\s{4}"(\/api\/[^"]+)":/gm)].map(match => match[1]);
  const seen = new Set(), twice = [];
  paths.forEach(path => (seen.has(path) ? twice.push(path) : seen.add(path)));
  assert.ok(paths.length > 50, "the route table was found");
  assert.deepEqual(twice, []);
});

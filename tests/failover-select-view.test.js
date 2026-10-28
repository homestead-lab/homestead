"use strict";

/* The workload editor's "If its node fails" choice sits in its own grid, sized
   to its longest option rather than the narrow column the Copies input uses,
   and the explanation beside it follows the choice. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync("web/js/views-lifecycle.js", "utf8");
const form = fs.readFileSync("web/js/app-form.js", "utf8");   // Placement, shared with Deploy (#844)
const css = fs.readFileSync("web/style.css", "utf8");

test("the failover choice is not squeezed into the Copies column", () => {
  assert.match(form, /<div class="place-grid failover"><div class="f">\$\{failoverSelect\(id\("failover"\)/);
  assert.match(css, /\.place-grid\.failover\{grid-template-columns:max-content 1fr/);
  assert.match(css, /\.place-grid\.failover select\{width:auto;min-width:max-content\}/);
});

test("the failover grid stacks on a phone like the others", () => {
  assert.match(css, /@media\(max-width:700px\)\{[^}]*\.place-grid\.failover[^}]*\{grid-template-columns:1fr\}/);
});

test("the explanation follows the chosen failover", () => {
  assert.match(form, /onchange="\$\(\$\{jsq\("#" \+ id\("failover_help"\)\)\}\)\.textContent = FAILOVER_HELP\[this\.value\]/);
  assert.match(form, /id="\$\{id\("failover_help"\)\}"/);
  assert.match(source, /window\.FAILOVER_HELP = FAILOVER_HELP;/);
});

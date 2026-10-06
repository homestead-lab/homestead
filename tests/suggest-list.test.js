"use strict";

/* A text field's suggestions are the app's own list, not the browser's
   <datalist> popup, which sits offset from its field, narrower than it and
   outside its dialog (#290). The <datalist> stays the source of the values. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const ui = fs.readFileSync("web/js/ui.js", "utf8");
const css = fs.readFileSync("web/style.css", "utf8");
const block = ui.slice(ui.indexOf("/* Suggestions for a text field."));

test("every field with a list= is taken off the browser's popup", () => {
  assert.ok(block.length > 0, "the suggestion list lives in ui.js");
  assert.match(block, /input\.dataset\.suggest = input\.getAttribute\("list"\);\s*input\.removeAttribute\("list"\);/);
  // Upgraded on the press and on keyboard focus, before the browser can open its own.
  assert.match(block, /addEventListener\("pointerdown", event => upgrade\(event\.target\), true\)/);
  assert.match(block, /addEventListener\("focusin", event => upgrade\(event\.target\), true\)/);
});

test("the list is placed on its field: same left edge and width, inside the dialog", () => {
  assert.match(block, /pop\.style\.left = `\$\{at\.left\}px`/);
  assert.match(block, /pop\.style\.width = `\$\{at\.width\}px`/);
  assert.match(block, /field\.closest\("\.modalbox"\)/);
  assert.match(block, /const up = wanted > below && above > below;/);
});

test("Escape closes the list before it can close the dialog", () => {
  assert.match(block, /event\.key === "Escape" && isOpen\) \{[^}]*event\.stopPropagation\(\)/);
  assert.match(block, /\}, true\); \/\/ before the page's own Escape/);
});

test("the list is above dialogs and themed by tokens", () => {
  const rule = css.match(/\.suggest-pop\{[^}]*\}/)?.[0] || "";
  assert.match(rule, /position:fixed;z-index:75/);
  assert.match(css, /\.modal\{position:fixed;inset:0;[^}]*z-index:70/);
  assert.match(rule, /background:var\(--surf2\)/);
});

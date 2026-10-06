"use strict";

/* The page behind a dialog, a confirmation, the drawer or the phone menu
   stays where it was while one is open (#293): the page's own scroll is
   held, and a dialog's scroll never chains to the page. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const css = fs.readFileSync("web/style.css", "utf8");

test("every overlay holds the page's scroll while it is open", () => {
  const rule = css.match(/html:has\(#modal:not\(\.hidden\)\)[^{]*\{[^}]*\}/)?.[0] || "";
  for (const overlay of ["#modal:not(.hidden)", ".askdlg", ".drawer.open", "body.navopen"]) {
    assert.ok(rule.includes(`html:has(${overlay})`), `${overlay} holds the page`);
  }
  assert.match(rule, /\{overflow:hidden;scrollbar-gutter:stable\}/);
});

test("the installed app holds .main, the part of it that scrolls", () => {
  assert.match(css, /html\[data-display="standalone"\]:has\(#modal:not\(\.hidden\)\) \.main[^{]*\{overflow:hidden\}/);
});

test("a dialog's own scroll does not chain to the page", () => {
  assert.match(css, /\.modal,\.modalbox,\.askdlg,\.drawer\{overscroll-behavior:contain\}/);
});

"use strict";

/* Back to top (#292), on every page: shown past a screen and a half, at
   once where motion is reduced, out of the way of overlays, the job tray
   and the phone's bottom bar. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const ui = fs.readFileSync("web/js/ui.js", "utf8");
const css = fs.readFileSync("web/style.css", "utf8");
const block = ui.slice(ui.indexOf("/* Back to top (#292)"));

test("it follows whichever part scrolls: the window, or .main in the installed app", () => {
  assert.match(block, /dataset\.display === "standalone"\s*\? document\.querySelector\("\.main"\) : document\.scrollingElement/);
  assert.match(block, /addEventListener\("scroll", soon, \{ passive: true, capture: true \}\)/);
});

test("it shows past a screen and a half and honours reduced motion", () => {
  assert.match(block, /page\.scrollTop > page\.clientHeight \* 1\.5/);
  assert.match(block, /prefers-reduced-motion: reduce/);
  assert.match(block, /behavior: still \? "auto" : "smooth"/);
  assert.match(block, /setAttribute\("aria-label", "Back to top"\)/);
});

test("it keeps clear of overlays, the job tray and the phone's bottom bar", () => {
  assert.match(css, /\.backtop\{position:fixed;right:22px;bottom:calc\(18px \+ var\(--tray-lift, 4px\)\)/);
  // The page's button hides behind a dialog; the dialog's own (#374) does not.
  assert.match(css, /html:has\(#modal:not\(\.hidden\)\) \.backtop:not\(\.dialog-backtop\),[^{]*\{display:none\}/);
  assert.match(css, /\.dialog-backtop\{position:sticky;bottom:16px/);
  assert.match(css, /@media\(max-width:900px\)\{\.backtop\{[^}]*bottom:calc\(84px \+ env\(safe-area-inset-bottom,0px\)/);
});

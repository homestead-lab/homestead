"use strict";
// The page's scripts load in <head> with defer (#906): the browser fetches them
// while it parses the page and paints the shell, then runs them in their order
// once the page is parsed. Only the tiny theme script runs at once, inline.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path");

const WEB = path.join(__dirname, "..", "web");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const [head, body] = html.split("</head>");
const scripts = text => [...text.matchAll(/<script\b[^>]*>/g)].map(m => m[0]);

test("every script tag is in <head> and deferred, written src first", () => {
  assert.deepEqual(scripts(body), [], "no script in <body>");
  const [theme, ...rest] = scripts(head);
  assert.equal(theme, "<script>", "the inline theme script comes first and runs at once");
  assert.ok(rest.length > 0);
  // src first: homestead_static.DEMO_SCRIPT finds demo.js by its opening `<script src="/js/demo.js`.
  assert.deepEqual(rest.filter(tag => !/^<script src="\/js\/[a-z0-9-]+\.js\?v=[^"]+" defer>$/.test(tag)), []);
  assert.ok(head.indexOf("<script>") < head.indexOf('<link rel="stylesheet"'), "the theme is set before the stylesheet paints");
});

test("every web/js file has a deferred tag, demo.js included", () => {
  const tagged = new Set([...head.matchAll(/<script src="\/js\/([^"?]+)\?v=[^"]+" defer>/g)].map(m => m[1]));
  const files = fs.readdirSync(path.join(WEB, "js")).filter(f => f.endsWith(".js"));
  assert.deepEqual(files.filter(f => !tagged.has(f)), []);
});

test("the page has no inline handlers: a click before the scripts run calls nothing undefined", () => {
  assert.deepEqual([...html.matchAll(/\son[a-z]+="/g)].map(m => m[0].trim()), []);
});

test("every script tag carries the same ?v= version, so a stale one is caught", () => {
  const versions = new Set([...html.matchAll(/<script src="\/js\/[a-z0-9-]+\.js\?v=([^"]+)" defer>/g)].map(m => m[1]));
  assert.equal(versions.size, 1, `script tags carry ${[...versions].join(", ")}`);
});

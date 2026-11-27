"use strict";
// One name for each page (#508), from one source: ROUTES.label. The
// sidebar, the phone's bottom bar, the tabs and the page title all say it.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");
const { ROUTES, TABS } = require("../web/js/router.js");
const html = fs.readFileSync("web/index.html", "utf8"), app = fs.readFileSync("web/js/app.js", "utf8");

test("a machine in the cluster is a host: the page is Hosts", () => {
  assert.equal(ROUTES.nodes.label, "Hosts");
  assert.equal(ROUTES.nodes.path, "/nodes", "its address stays, so links keep working");
});

test("app.js keeps no page titles of its own; the title comes from ROUTES", () => {
  const views = between(app, "const VIEWS = {", "};");
  assert.doesNotMatch(views, /\[\s*"[A-Z]/, "no title strings in VIEWS");
  assert.match(app, /HomesteadRouter\.ROUTES\[v\]\.label/);
});

const links = part => [...part.matchAll(/<a href="[^"]*" data-view="(\w+)"><svg[^>]*><use href="[^"]*"\/><\/svg>([^<]+)/g)]
  .map(([, view, text]) => [view, text.trim()]);
const bottom = between(html, 'id="bottombar"', "</nav>");

test("every sidebar link says its page's name", () => {
  const sidebar = links(html.replace(bottom, ""));
  assert.ok(sidebar.length >= 10);
  for (const [view, text] of sidebar) assert.equal(text, ROUTES[view].label, view);
});

test("the phone's bottom bar says Dashboard and Containers, like the sidebar", () => {
  const bar = Object.fromEntries(links(bottom));
  assert.equal(bar.dash, "Dashboard");
  assert.equal(bar.workloads, "Containers");
});

test("a page's tab says the page's name where they are the same page", () => {
  for (const tabs of Object.values(TABS)) for (const [view, label] of tabs)
    if (!["dash", "workloads", "vms", "store", "cluster", "protect"].includes(view)) assert.equal(label, ROUTES[view].label, view);
  assert.deepEqual(TABS.workloads.find(([view]) => view === "images"), ["images", "Image Cache"]);
});

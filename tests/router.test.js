"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const router = require("../web/js/router.js");

test("every view has a unique canonical absolute path", () => {
  const paths = Object.values(router.ROUTES).map(route => route.path);
  assert.equal(new Set(paths).size, paths.length);
  assert.ok(paths.every(path => path.startsWith("/")));
});

test("direct paths resolve to the expected views", () => {
  assert.deepEqual(router.resolve("/containers"), {
    view: "workloads", path: "/containers", known: true,
    route: router.ROUTES.workloads,
  });
  assert.equal(router.resolve("/settings/").view, "settings");
  assert.equal(router.resolve("/").view, "dash");
});

test("unknown paths safely resolve to dashboard without becoming canonical", () => {
  const result = router.resolve("/not-a-real-page");
  assert.equal(result.view, "dash");
  assert.equal(result.known, false);
});

test("URL creation encodes filters and repeated values", () => {
  assert.equal(router.urlFor("storage", { q: "media files", state: ["attached", "faulted"] }),
    "/volumes?q=media+files&state=attached&state=faulted");
});

test("breadcrumbs place a view under its section, not under the dashboard", () => {
  // The dashboard is a sibling page. Containers belongs to Workloads.
  assert.deepEqual(router.breadcrumbs("workloads"), [
    { label: "Workloads", url: "", current: false },
    { label: "Containers", url: "/containers", current: true },
  ]);
  assert.deepEqual(router.breadcrumbs("settings"), [
    { label: "System", url: "", current: false },
    { label: "Settings", url: "/settings", current: true },
  ]);
  assert.deepEqual(router.breadcrumbs("dash"), [
    { label: "Overview", url: "", current: false },
    { label: "Dashboard", url: "/", current: true },
  ]);
  assert.deepEqual(router.breadcrumbs("nodes", "harvester-node1"), [
    { label: "Overview", url: "", current: false },
    { label: "Nodes", url: "/nodes", current: false },
    { label: "harvester-node1", url: "", current: true },
  ]);
});

test("a section is a label rather than a link, having no page of its own", () => {
  for (const view of ["workloads", "storage", "cluster", "dash"]) {
    assert.equal(router.breadcrumbs(view)[0].url, "", `${view} section is not a link`);
  }
});

test("query parsing retains filters and repeated values", () => {
  assert.deepEqual(router.queryParams("?q=media+files&state=attached&state=faulted"), {
    q: "media files", state: ["attached", "faulted"],
  });
});

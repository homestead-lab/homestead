"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const context = {
  window: {},
  console,
  crypto: webcrypto,
  btoa: value => Buffer.from(value, "binary").toString("base64"),
  document: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  WebSocket: { OPEN: 1 },
  $: () => null,
  $$: () => [],
  esc: value => String(value),
};
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), context);

test("an implicit Docker Hub image shows its resolved registry and tag", () => {
  assert.equal(context.imagePullRef("nginx"), "docker.io/library/nginx:latest");
  assert.equal(context.imagePullRef("owner/app"), "docker.io/owner/app:latest");
});

test("OpenSpeedTest's repository named latest is kept and explained", () => {
  assert.equal(context.imagePullRef("openspeedtest/latest"), "docker.io/openspeedtest/latest:latest");
  assert.match(context.imagePullNote("openspeedtest/latest"), /repository is named <b>latest<\/b>/);
});

test("an explicit registry, tag, or digest is not rewritten", () => {
  assert.equal(context.imagePullRef("ghcr.io/example/app:stable"), "ghcr.io/example/app:stable");
  assert.equal(context.imagePullRef("registry.local:5000/example/app@sha256:abc"),
    "registry.local:5000/example/app@sha256:abc");
});

test("an update review names the release it moves between", () => {
  const digest = n => `sha256:${String(n).repeat(64).slice(0, 64)}`;
  assert.deepEqual([...context.imageChangeWords(`ghcr.io/wjcloudy/homestead:2.8.200@${digest(1)}`,
    `ghcr.io/wjcloudy/homestead:2.8.205@${digest(2)}`)], ["2.8.200", "2.8.205"]);
  assert.deepEqual([...context.imageChangeWords("ghcr.io/example/app:1.0.0", "ghcr.io/example/app:1.1.0")], ["1.0.0", "1.1.0"]);
});

test("a new build under the same tag shows the start of each digest", () => {
  assert.deepEqual([...context.imageChangeWords(`nginx:latest@sha256:${"a".repeat(64)}`, `nginx:latest@sha256:${"b".repeat(64)}`)],
    ["latest · aaaaaaa", "latest · bbbbbbb"]);
  assert.deepEqual([...context.imageChangeWords(`nginx@sha256:${"a".repeat(64)}`, `nginx@sha256:${"b".repeat(64)}`)],
    ["latest · aaaaaaa", "latest · bbbbbbb"]);
});

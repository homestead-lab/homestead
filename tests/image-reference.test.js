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

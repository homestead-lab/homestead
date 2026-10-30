"use strict";
// #1001: when the app's own Secret could not be read, Edit says so and its
// Review button is off; a saved container names the variables the form showed
// from the Secret (secret_env), so the server removes only those.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function setup() {
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, getElementById: () => null },
    $: () => ({ value: "", checked: false }), $$: () => [],
    esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;") };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s)); c.tip = () => ""; c.icon = () => "";
  for (const file of ["ui.js", "gitops.js", "views-workloads.js", "views-lifecycle.js"])
    vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  return c;
}

test("an unread Secret is said above the form and Review changes is off", () => {
  const c = setup();
  const unread = { secret_env_unread: "This app's secret settings could not be read" };
  assert.match(vm.runInContext(`editSecretUnreadNotice(${JSON.stringify(unread)})`, c), /secret settings could not be read/);
  assert.match(vm.runInContext(`editReviewButton("lab", "tunnel", ${JSON.stringify(unread)})`, c), /<button[^>]*\sdisabled[\s>]/);
  assert.equal(vm.runInContext(`editSecretUnreadNotice({})`, c), "");
  assert.doesNotMatch(vm.runInContext(`editReviewButton("lab", "tunnel", {})`, c), /disabled/);
});

test("a saved container sends back the variables the form showed from the Secret", () => {
  const c = setup();
  const panel = vm.runInContext(`editContainerPanel(${JSON.stringify({ original_name: "cloudflared", name: "cloudflared",
    image: "cloudflare/cloudflared:2026.1.0", env: { TUNNEL_TOKEN: "first" }, env_refs: [], secret_env: ["TUNNEL_TOKEN"] })}, 0, "basics")`, c);
  const attr = panel.match(/data-secret-env="([^"]*)"/);
  assert.ok(attr, "the panel keeps the list");
  const secretEnv = attr[1].replaceAll("&quot;", '"').replaceAll("&amp;", "&");
  c.selectedHardware = () => []; c.readPrivileges = () => undefined; c.readVolumeRows = () => [];
  const sent = vm.runInContext(`readEditedContainer(${JSON.stringify({ dataset: { index: "0", originalName: "cloudflared", secretEnv } })})`, c);
  assert.deepEqual(JSON.parse(JSON.stringify(sent.secret_env)), ["TUNNEL_TOKEN"]);
  const fresh = vm.runInContext(`readEditedContainer({ dataset: { index: "1", new: "1" } })`, c);
  assert.equal(fresh.secret_env, undefined, "a new container names none");
});

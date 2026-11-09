"use strict";
/* #1294: an App Store template that mounts the host's container runtime
   socket. Details opens for it, as for any app, and a failure there shows an
   error with Try again. Deploy explains the block where Review is, and lifts
   it once the socket row is removed, as the server does. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let elements = {};
const element = () => ({ innerHTML: "", value: "", disabled: false, classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {} });
const context = {
  console, document: { addEventListener() {} }, localStorage: { getItem: () => null, setItem() {} },
  $: selector => elements[selector] || null, $$: () => [], esc: value => String(value ?? ""),
  STATE: { q: "" }, resetPaint() {}, paint() {}, toast() {}, icon: () => "", safeHref: s => s,
  modal: (title, body) => { elements["#mbody"] = element(); elements["#mbody"].innerHTML = body; },
};
context.window = context;
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
vm.runInContext(require("./helpers/workloads-source")(), context);

const SOCKET = { path: "/var/run/docker.sock", source: "/var/run/docker.sock", type: "host", read_only: true };
const blockedProfile = { label: "Needs Kubernetes design", level: "dependency", intent: "safety", blocked: true,
  notes: ["This template mounts the host's container runtime socket (/var/run/docker.sock), which lets it create and control other containers."],
  dependencies: [{ kind: "runtime", name: "Host container-runtime control", required: true, managed: false }] };
const app = profile => ({ key: "homepage", name: "Homepage", repo: "ghcr.io/example/homepage", categories: ["Tools"],
  deploy: { name: "homepage", volumes: [SOCKET], app_profile: profile } });

async function details(answer) {
  context.api = async () => answer;
  await context.storeDetails("homepage");
  return elements["#mbody"].innerHTML;
}

test("Details opens for a blocked template and says why", async () => {
  const html = await details(app(blockedProfile));
  assert.match(html, /Needs Kubernetes design\./);
  assert.match(html, /container runtime socket/);
  assert.match(html, /Configure &amp; deploy/);
  assert.doesNotMatch(html, /reading the template/);
});

test("Details opens for a template that is not blocked", async () => {
  const html = await details(app({ label: "Storage review", level: "review", blocked: false, notes: ["Persistent data paths start as editable claims."], dependencies: [] }));
  assert.match(html, /Storage review\./);
  assert.match(html, /Persistent data paths/);
});

test("a template Details cannot draw shows an error with Try again", async () => {
  const html = await details({ ...app(blockedProfile), categories: 5 });
  assert.match(html, /This app could not be shown/);
  assert.match(html, /storeDetails\(/);
});

test("Deploy finds the socket by the container path or by the host path behind it, in any container", () => {
  assert.equal(context.deployRuntimeSocket({ container_name: "web", volumes: [] }), null);
  assert.equal(context.deployRuntimeSocket({ container_name: "web", volumes: [SOCKET] }).container, "web");
  assert.equal(context.deployRuntimeSocket({ container_name: "web", volumes: [{ path: "/sock", source: "/run/containerd/containerd.sock", kind: "host" }] }).path, "/sock");
  assert.equal(context.deployRuntimeSocket({ container_name: "web", volumes: [{ path: "/data", source: "/var/run/docker.sock", kind: "new-rwo" }] }), null,
    "a volume named like a socket is not the host's socket");
  assert.equal(context.deployRuntimeSocket({ container_name: "web", volumes: [], additional_containers: [{ name: "side", volumes: [SOCKET] }] }).container, "side");
});

test("Summary says why Review is unavailable and how to go on, and says nothing once the row is gone", () => {
  const html = context.deployBlockedHtml({ container_name: "homepage", volumes: [SOCKET] });
  assert.match(html, /Review is not available yet/);
  assert.match(html, /Remove that row under Storage to continue/);
  assert.match(html, /selectSection\('containerDeploy','storage'\)/);
  assert.equal(context.deployBlockedHtml({ container_name: "homepage", volumes: [] }), "");
});

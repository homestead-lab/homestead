"use strict";
// An app card's Deployment → pods → containers list (#873): where each pod
// runs is never the part that is cut, pods of one app tell apart without
// hovering, and an image keeps its tag.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = {
  window: {}, console, document: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} }, WebSocket: { OPEN: 1 }, $: () => null, $$: () => [],
  esc: v => String(v ?? "").replace(/[&<>"']/g, ch => `&#${ch.charCodeAt(0)};`),
};
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => context.esc(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), context);

const digest = "faa8e2b1c0d94f3a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a275f";
const image = `registry.example.com/network/edge-tunnel:2026.10.1@sha256:${digest}`;
const pod = (suffix, node, extra = {}) => ({ name: `edge-tunnel-55dd45c456-${suffix}`, node, phase: "Running", ready: true, restarts: 0,
  containers: [{ name: "tunnel", image, kind: "app", state: "running", ready: true, restarts: 0 }], ...extra });
const app = { name: "edge-tunnel", ns: "lab", kind: "Deployment", pod_count: 3, container_count: 3,
  pods: [pod("hwtkq", "harvester-node1"), pod("x7k2p", "harvester-node2", { restarts: 2 }), pod("m4n9r", "harvester-node3")] };
const html = context.workloadHierarchy(app);
const heads = html.match(/<div class="tree-pod-head">[\s\S]*?<\/div>/g);
const css = fs.readFileSync("web/style.css", "utf8");
const rulesFor = selector => [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .filter(([, sel]) => sel.split(",").some(s => s.includes(selector))).map(([, , body]) => body);

test("each pod row starts with its host, outside anything that cuts text", () => {
  assert.equal(heads.length, 3);
  heads.forEach((head, i) => {
    const host = head.match(/<b class="tree-node"[^>]*>([^<]*)<\/b>/);
    assert.ok(host, `pod ${i} names its host`);
    assert.equal(host[1], `harvester-node${i + 1}`);
    assert.ok(head.indexOf("tree-node") < head.indexOf("ui-chip"), "the host comes before the state");
  });
  assert.match(heads[1], /2 restarts/);
  for (const body of rulesFor(".tree-node")) {
    assert.doesNotMatch(body, /ellipsis|max-width|overflow:\s*hidden/, `.tree-node is never truncated: ${body}`);
  }
});

test("a pod shows the suffix that tells it apart, with its full name on hover", () => {
  const name = heads[0].match(/<span class="mono dim tree-podname" title="([^"]*)">([^<]*)<\/span>/);
  assert.ok(name, "the pod name is shown");
  assert.equal(name[1], "edge-tunnel-55dd45c456-hwtkq");
  assert.equal(name[2], "…-hwtkq");
  assert.equal(context.podShortName("db-0"), "db-0", "a short name is shown whole");
});

test("a container's image keeps its tag, on a line of its own, full on hover", () => {
  const line = html.match(/<div class="mono dim tree-image" title="([^"]*)"><span class="tree-image-path">([^<]*)<\/span><span class="tree-image-name">([^<]*)<\/span><\/div>/);
  assert.ok(line, "the image is a line of its own, split into a path that can be cut and a name that keeps its tag");
  assert.equal(line[1], image);
  assert.equal(line[2], "registry.example.com/network/");
  assert.match(line[3], /^edge-tunnel:2026\.10\.1@sha256:faa8…75f$/);
  // Only the path is cut, and phones show the image too.
  assert.ok(rulesFor(".tree-image-path").some(body => /ellipsis/.test(body)));
  assert.ok(rulesFor(".tree-image-name").some(body => /flex:0 0 auto/.test(body)), "the path gives way first");
  for (const body of rulesFor(".tree-image-name")) assert.doesNotMatch(body, /ellipsis|nowrap/, "the name and tag wrap, never cut");
  assert.doesNotMatch(css, /\.tree-image\{display:none\}|\.tree-hint,\.tree-image\{display:none\}/);
});

test("the wide rows layout also carries each pod's whole name, for screens with room", () => {
  const wide = context.workloadHierarchy(app, { wide: true });
  assert.match(wide, /<span class="tree-podname-short">…-hwtkq<\/span><span class="tree-podname-full">edge-tunnel-55dd45c456-hwtkq<\/span>/);
  assert.match(fs.readFileSync("web/js/views-workloads.js", "utf8"), /workloadHierarchy\(w, \{ wide: true \}\)/, "the rows layout asks for it");
});

test("a row's Pod, Container and Init labels never break inside the word", () => {
  // "CONTAINER" wrapped to "CONTAINE / R" on a phone.
  assert.ok(rulesFor(".tree-branch").some(body => /white-space:\s*nowrap/.test(body)));
});

test("an image with no path starts at the line's start, with no empty path before it", () => {
  const plain = context.workloadHierarchy({ ...app, pods: [{ ...app.pods[0], containers: [{ ...app.pods[0].containers[0], image: "nginx:1.25" }] }] });
  assert.match(plain, /<div class="mono dim tree-image" title="nginx:1.25"><span class="tree-image-name">nginx:1.25<\/span><\/div>/);
});

test("an app whose Services couldn't be read says so, never no listeners", () => {
  assert.match(context.accessPorts([]), /—/);
  const unread = context.accessPorts(null);
  assert.doesNotMatch(unread, /—/);
  assert.match(unread, /couldn(?:'|&#39;)t read/i);
});

"use strict";
// How something works sits in a guide, UI.more or a tip, not in a block
// at the top of a page or the end of a card (#494).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const between = require("./helpers/between");
const read = f => fs.readFileSync(f, "utf8");

test("Image Cache explains how images are kept in a guide, not a note above the table", () => {
  const src = read("web/js/views-lifecycle.js");
  assert.doesNotMatch(src, /<div class="note"[^>]*><b>Removing images\.<\/b>/);
  assert.match(src, /UI\.guide\("How images are kept"/);
});

test("Settings › Monitoring explains in tips, says amber, and says Recommended: 1 once", () => {
  const src = read("web/js/views-settings.js");
  assert.doesNotMatch(src, /changes the metric and node card to yellow/);
  assert.doesNotMatch(src, /<div class="note[^"]*"><b>Device differences are preserved\.<\/b>/);
  assert.match(src, /Warning turns a bar amber, critical red/);
  assert.equal((src.match(/Recommended: 1/g) || []).length, 1);
});

test("the Longhorn storage card keeps its live figure and puts the consequence in a tip", () => {
  const src = read("web/js/views-storage.js");
  assert.doesNotMatch(src, /class="note"[^>]*>Past a node's limit/);
  assert.match(src, /across all hosts now/);
});

test("Data Protection explains restore tests in a guide and says when testing is off", () => {
  const src = read("web/js/views-protect.js");
  assert.match(src, /UI\.guide\("How restore tests work"/);
  assert.match(src, /Off · tests run only when you click Test now/);
});

test("the NFS server's recovery detail is in UI.more, like the SMB card", () => {
  const src = read("web/js/platform.js");
  const body = between(src, "window.nfsRecovery =", "\n};\n");
  assert.match(body, /UI\.more\(/);
  assert.doesNotMatch(body, /<details/);
  assert.doesNotMatch(read("web/js/views-storage.js"), /<div class="dim xs"[^>]*>NFS is a separate opt-in container/);
});

test("a setup step doesn't repeat that Next leaves it unchecked; the intro says it once", () => {
  const src = read("web/js/welcome.js");
  assert.doesNotMatch(src, /<p class="small dim">Next does not mark this step complete\.<\/p>/);
  assert.match(src, /Next moves to the following step without marking the current one complete/);
});

test("Settings › About says the cluster VIP is unknown when Your VIPs couldn't be read (fix/unread-inventory)", () => {
  const src = read("web/js/views-settings.js");
  const fn = src.match(/function clusterVipText\([^]*?\n}/);
  assert.ok(fn, "clusterVipText is defined");
  const text = new Function(`${fn[0]}; return clusterVipText;`)();
  assert.equal(text({ lb_ip: null }, { vip: "192.0.2.5" }, ""), "unknown");
  assert.equal(text({ lb_ip: "192.0.2.9" }, { vip: "192.0.2.5" }, ""), "192.0.2.9");
  assert.equal(text({ lb_ip: "" }, { vip: "192.0.2.5" }, ""), "192.0.2.5");
  assert.equal(text({ lb_ip: "192.0.2.9" }, { vip: "" }, "hosts unread"), "—");
});

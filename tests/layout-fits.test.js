"use strict";
// Labels fit their boxes (#495): what the rules and markup promise. The
// browser checks measure the rendered result.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs");
const css = fs.readFileSync("web/style.css", "utf8"), read = f => fs.readFileSync(f, "utf8");

test("the Storage card's nearly-full tag wraps instead of cutting off its size", () => {
  assert.match(css, /\.lh-tight\{[^}]*white-space:normal/);
});
test("an Access card's VIP line wraps when it doesn't fit", () => {
  assert.match(css, /\.a2vipip\{[^}]*flex-wrap:wrap/);
});
test("a VM or monitored name ends in an ellipsis, with the cluster tag on its own line", () => {
  assert.match(css, /\.vm-name-text>b\{[^}]*text-overflow:ellipsis/);
  const row = read("web/js/views-vms.js");
  assert.doesNotMatch(row, /<b>\$\{esc\(v\.name\)\}<\/b> \$\{clusterTag\(v\)\}\$\{vmClusterTag\(v\)\}/);
  assert.match(row, /<b title="\$\{esc\(v\.name\)\}">/);
  assert.match(read("web/js/uptime.js"), /<b title="\$\{esc\(w\.name\)\}">/);
});
test("Remove container is hidden when there is one container, and sits clear of the first field", () => {
  assert.match(require("./helpers/lifecycle-source")(), /\.hidden = cards\.length <= 1/);
  assert.match(css, /\.edit-container-remove\{[^}]*margin-bottom/);
});
test("Node health stacks one host per row on a phone", () => {
  assert.match(css, /\.node-compact-grid\{[^}]*minmax\(min\(220px,100%\),1fr\)/);
});

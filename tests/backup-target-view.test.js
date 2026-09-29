"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function setup() {
  const ctx = { URL, document: { addEventListener() {} }, STATE: { data: {} },
    esc: s => String(s).replaceAll("<", "&lt;").replaceAll('"', "&quot;") };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/views-protect.js", "utf8"), ctx);
  return ctx;
}

test("a matching bucket on the wrong endpoint offers repair", () => {
  const ctx = setup();
  const html = ctx.objectStoreCard({ deployed: true, ready: true, backup_url: "s3://backups@r/",
    longhorn: { pointed: false, endpoint: "http://old:9000" } }, { url: "s3://backups@r/" });
  assert.match(html, /not pointed here/);
  assert.match(html, /http:\/\/old:9000/);
  assert.match(html, /Point Longhorn at it/);
  assert.match(html, /Storage settings/);
});

test("pending and failed switches explain progress and offer a retry with escaped errors", () => {
  const ctx = setup();
  for (const state of ["pending", "applying", "failed"]) {
    const html = ctx.objectStoreCard({ deployed: true, ready: true,
      longhorn: { pointed: false, state, detail: "<script>bad endpoint</script>" } }, {});
    assert.match(html, /Retry target switch/);
    assert.ok(!html.includes("<script>"));
    assert.equal(html.includes("retries automatically"), state !== "failed");
  }
});

test("confirmed endpoint removes the repair warning", () => {
  const ctx = setup();
  const html = ctx.objectStoreCard({ deployed: true, ready: true, longhorn: { pointed: true } }, {});
  assert.match(html, /pointed here/);
  assert.doesNotMatch(html, /Longhorn is not writing here/);
});

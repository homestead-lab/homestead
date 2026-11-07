// Timestamps from the API are UTC: show them in the reader's own zone, never as a bare cut-down ISO string.
"use strict";
process.env.TZ = "Europe/Berlin";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = require("./helpers/core-source")();

test("a Helm release's history shows when each revision was deployed in local time", async () => {
  const body = { innerHTML: "" };
  const ctx = { console, Date, Map, Set, Promise, JSON, encodeURIComponent, document: { addEventListener() {} }, STATE: { data: {} },
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    modal: () => {}, toast: () => {}, $: id => id === "#mbody" ? body : null, $$: () => [], icon: () => "", tip: () => "",
    api: async () => ({ chart: "frigate", chart_version: "1.0.0", status: "deployed", revision: 2, objects: [], values: "",
      history: [{ revision: 2, status: "deployed", chart_version: "1.0.0", description: "Upgrade complete",
        updated: "2026-09-22T03:00:04.123456789Z" }] }) };
  ctx.window = ctx; vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
  vm.runInContext(fs.readFileSync("web/js/views-helm.js", "utf8"), ctx);
  await ctx.helmRelease("media", "frigate");
  assert.match(body.innerHTML, /05:00/, "03:00 UTC is 05:00 in Berlin");
  assert.match(body.innerHTML, /data-tip="2026-09-22 03:00:04 UTC"/);
  assert.doesNotMatch(body.innerHTML, /2026-09-22 03:00</);
});

test("no view cuts a timestamp down with replace(\"T\", \" \")", () => {
  const left = [];
  for (const file of fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "core-format.js")) {
    fs.readFileSync(`web/js/${file}`, "utf8").split("\n").forEach((line, i) => {
      if (/\.replace\("T", " "\)/.test(line)) left.push(`${file}:${i + 1}`);
    });
  }
  assert.deepEqual(left, [], "use fmtWhen() from core-format.js");
});

"use strict";

/* A read whose answer arrives after you left the page is abandoned: it never
   settles, so the page you left paints nothing over the one you went to and no
   error is shown for it. It must not keep the waiting page alive either: every
   frame awaiting an abandoned read, and all it holds, is free to be collected
   (wrap-up row 19a). One shared never-settling promise kept every such frame
   for the rest of the session. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm"), v8 = require("node:v8");
const between = require("./helpers/between");

// The collector, by node --expose-gc or, failing that, the flag set now.
const gc = globalThis.gc || (v8.setFlagsFromString("--expose-gc"), vm.runInNewContext("typeof gc === 'function' ? gc : null"));

const core = require("./helpers/core-source")();
const reads = between(core, "window.NAV_TOKEN = 0;", "/* A read the caller cannot do without.");

function page() {
  const ctx = { console, AbortController, AbortSignal, Promise };
  ctx.window = ctx;
  // The answer comes back after the page has changed.
  ctx.fetch = async () => {
    ctx.window.NAV_TOKEN++;
    return { ok: true, status: 200, statusText: "OK", headers: { get: name => (name === "content-type" ? "application/json" : null) },
      json: async () => ({ rows: [] }) };
  };
  vm.createContext(ctx);
  vm.runInContext(`${reads}\nwindow.api = api;`, ctx);
  return ctx;
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test("an abandoned read neither answers nor fails", async () => {
  const ctx = page();
  let settled = false;
  ctx.api("/api/workloads").then(() => { settled = true; }, () => { settled = true; });
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(settled, false);
});

test("the page that waited on an abandoned read can be collected", { skip: !gc && "no way to run the collector here" }, async () => {
  const ctx = page();
  let collected = 0;
  const registry = new FinalizationRegistry(() => { collected++; });
  (() => {
    const view = { rows: new Array(1000).fill("a page's data") };
    registry.register(view, "view");
    (async () => { await ctx.api("/api/workloads"); view.rows.length; })();
  })();
  for (let i = 0; i < 20 && !collected; i++) { await tick(); gc(); await tick(); }
  assert.equal(collected, 1, "the frame waiting on the abandoned read was freed");
  assert.ok(ctx.api, "the page's code itself is still alive");
});

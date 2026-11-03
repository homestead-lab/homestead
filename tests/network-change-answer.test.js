"use strict";

/* A bond or bridge change whose script ran past its limit answers ok: false,
   "…needs attention; see its job under Jobs" (#TBD-host-exec-deadline): the
   page shows that as a problem, not as a green success. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

const handlers = [
  ["hostBondGo", between(fs.readFileSync("web/js/host-ports.js", "utf8"), "window.hostBondGo = async node =>", null)],
  ["nodeBridgeGo", between(fs.readFileSync("web/js/views-network.js", "utf8"), "window.nodeBridgeGo = async node =>",
                           "function networkVmNetsList(opts)")],
];

function page(answer) {
  const toasts = [], closed = [];
  const button = { disabled: false, textContent: "" };
  const ctx = {
    window: {}, HOST_BOND: { pending: {} }, toasts, closed,
    typedConfirmMatches: () => true,
    $: selector => selector.endsWith("_confirm") ? { value: "node-a" } : button,
    api: async () => { if (answer instanceof Error) throw answer; return answer; },
    toast: (msg, kind) => toasts.push([msg, kind]),
    closeModal: () => closed.push(true),
  };
  vm.createContext(ctx);
  return ctx;
}

for (const [name, source] of handlers) {
  test(`${name}: a change that needs attention is not shown as a success`, async () => {
    const ctx = page({ ok: false, detail: "node-a's network change needs attention; see its job under Jobs" });
    vm.runInContext(source, ctx);
    await ctx.window[name]("node-a");
    assert.deepEqual(JSON.parse(JSON.stringify(ctx.toasts)),
                     [["node-a's network change needs attention; see its job under Jobs", "bad"]]);
    assert.equal(ctx.closed.length, 1, "the dialog closes: the job holds the host, there is nothing to try again");
  });

  test(`${name}: a change that started is still a success`, async () => {
    const ctx = page({ ok: true, detail: "node-a's network is changing; follow it under Jobs" });
    vm.runInContext(source, ctx);
    await ctx.window[name]("node-a");
    assert.deepEqual(JSON.parse(JSON.stringify(ctx.toasts)), [["node-a's network is changing; follow it under Jobs", "ok"]]);
  });

  test(`${name}: a refusal stays a refusal`, async () => {
    const ctx = page(new Error("type the host's name, node-a, to confirm"));
    vm.runInContext(source, ctx);
    await ctx.window[name]("node-a");
    assert.deepEqual(JSON.parse(JSON.stringify(ctx.toasts)), [["type the host's name, node-a, to confirm", "bad"]]);
    assert.equal(ctx.closed.length, 0);
  });
}

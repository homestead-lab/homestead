"use strict";
// A toast that says success only when the server said ok (gaps 182). Of the
// routes that can answer ok: false, the one handler that toasted it as success
// whatever it said was Skip on the missing-logos list (/api/workloads/logo).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

function logos(answer) {
  const toasts = [], posted = [];
  const ctx = { console, setTimeout, clearTimeout, Promise, JSON,
    esc: s => String(s ?? ""), $: () => null, $$: () => [], toast: (text, kind) => toasts.push([text, kind]),
    api: async (path, init) => { posted.push([path, JSON.parse(init.body)]); return answer; }, closeModal() {} };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/logos.js", "utf8"), ctx);
  vm.runInContext("LOGO.missing = [{ ns: 'lab', name: 'paperless' }]; refreshWorkloadsAfterLogo = async () => {}; logoFixup = () => {};", ctx);
  return { ctx, toasts, posted };
}

test("skipping an app's logo that the server could not save says so", async () => {
  const { ctx, toasts, posted } = logos({ ok: false, done: 0, failed: ["paperless: forbidden"], detail: "0 logos saved; 1 could not be: paperless: forbidden" });
  await ctx.logoSkip(0);
  assert.deepEqual(posted[0], ["/api/workloads/logo", { items: [{ ns: "lab", name: "paperless", skip: true }] }]);
  assert.deepEqual(toasts, [["0 logos saved; 1 could not be: paperless: forbidden", "bad"]]);
});

test("a skip that was saved is still shown as done", async () => {
  const { ctx, toasts } = logos({ ok: true, done: 1, failed: [], detail: "1 logo saved" });
  await ctx.logoSkip(0);
  assert.deepEqual(toasts, [["1 logo saved", "ok"]]);
});

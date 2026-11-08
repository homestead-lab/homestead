"use strict";

/* "If a host fails" shows Longhorn's node-failure policy. When Homestead does
   not know whether Longhorn is installed (/api/platform unread) and the policy
   cannot be read, the dialog says Longhorn may not be there rather than only
   that its policy could not be read (wrap-up row 29b). */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const source = require("./helpers/workloads-source")();
const dialog = between(source, "window.wlFailover = async () => {", "window.wlFailoverSave = ");

async function failover(platform, capacity) {
  const body = { innerHTML: "" };
  const ctx = { window: {}, STATE: { platform, data: {} }, FAILOVER_WORDS: {}, failoverSelect: () => "",
    modal() {}, $: sel => (sel === "#mbody" ? body : null), esc: x => String(x), jsq: JSON.stringify,
    UI: { callout: (tone, title, text) => `[${tone}|${title}|${text}]`, actions: html => html },
    api: path => (path === "/api/workloads" ? Promise.resolve([]) : capacity()) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(dialog, ctx);
  await ctx.wlFailover();
  return body.innerHTML;
}

const fails = () => Promise.reject(new Error("HTTP 404"));

test("platform unread and the policy unread: Longhorn may not be installed", async () => {
  const html = await failover(null, fails);
  assert.match(html, /\[warn\|Could not read whether apps with storage can move off a failed host\|HTTP 404\. Longhorn may not be installed on this cluster\.\]/);
});

test("Longhorn known to be installed: the failed read is said as it is", async () => {
  const html = await failover({ longhorn: true }, fails);
  assert.match(html, /\[warn\|Could not read whether apps with storage can move off a failed host\|HTTP 404\]/);
  assert.doesNotMatch(html, /may not be installed/);
});

test("Longhorn known not to be installed: no policy is offered at all", async () => {
  const html = await failover({ longhorn: false }, () => assert.fail("not asked"));
  assert.doesNotMatch(html, /Longhorn/);
});

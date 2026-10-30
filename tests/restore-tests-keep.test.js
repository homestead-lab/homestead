"use strict";
// Data Protection's Restore tests keep the last list while they are read again,
// and the new list is morphed in, so unchanged rows stay the same elements (#834).
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const src = fs.readFileSync("web/js/views-protect.js", "utf8");

const LIST = { enabled: true, every_days: 30, apps: [{ ns: "lab", name: "paperless", icon: "", running: false, without: [],
  restores: [{ claim: "paperless-data", backup: "backup-1" }], last: { at: 1, ok: true, seconds: 60 } }] };

function view(answer = LIST) {
  const calls = { morph: [], enhanced: 0, roles: 0, icons: [] };
  const host = { innerHTML: "OLD", cloneNode: deep => (assert.equal(deep, false), { innerHTML: "" }) };
  const box = { checked: false };
  const ctx = { document: { addEventListener() {}, getElementById: id => id === "restore-tests" ? host : null, activeElement: null },
    STATE: { data: {} }, setTimeout: () => 0, clearTimeout() {}, toast() {},
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    $: sel => sel === "#rt_on" ? box : null, tip: t => t, icon: () => "", jsArg: s => JSON.stringify(String(s)), fmtAt: String,
    appAvatar: name => `<i>${name}</i>`, actionBar: () => "<span>actions</span>",
    enhanceActions: () => { calls.enhanced++; }, morph: (a, b) => calls.morph.push([a, b]), loadAppIcons: el => calls.icons.push(el),
    api: async path => { if (answer instanceof Error) throw answer; return path === "/api/restore-tests" ? JSON.parse(JSON.stringify(answer)) : { detail: "ok" }; } };
  calls.box = box;
  ctx.jsq = s => ctx.esc(ctx.jsArg(s)); ctx.window = ctx; ctx.applyRole = () => { calls.roles++; };
  vm.createContext(ctx); require("./helpers/load-ui")(ctx);
  vm.runInContext(src, ctx);
  return { ctx, host, calls };
}

test("Restore tests show the placeholder only before the first list", async () => {
  const { ctx } = view();
  assert.match(ctx.restoreTestsSection(), /Finding each app's newest backups/);
  ctx.STATE.data.restoreTests = LIST;
  const html = ctx.restoreTestsSection();
  assert.match(html, /paperless/);
  assert.doesNotMatch(html, /Finding each app's newest backups/);
});

test("the Data Protection page paints the Restore tests section", () => {
  const page = between(src, "function protectBody", "function protectBackups");
  assert.match(page, /<div id="restore-tests" class="restore-tests">\$\{restoreTestsSection\(\)\}<\/div>/);
});

test("a new list is morphed in, prepared as the page paint prepares it", async () => {
  const { ctx, host, calls } = view();
  await ctx.loadRestoreTests();
  assert.equal(host.innerHTML, "OLD", "not rewritten: the rows stay the same elements");
  assert.equal(calls.morph.length, 1);
  assert.equal(calls.morph[0][0], host);
  assert.equal(calls.morph[0][1].innerHTML, ctx.restoreTestsSection(), "the same markup the page paints");
  assert.equal(calls.enhanced, 1, "enhanced as paint() enhances the page, so the two agree");
  assert.equal(calls.roles, 1, "roles applied again, so the admin switch stays hidden from a viewer");
  assert.deepEqual(calls.icons, [host], "app icons wired again, as paint() wires them, so they do not blank");
  assert.equal(calls.box.checked, true, "the switch shows the setting, even after someone clicked it");
});

test("a list that cannot be read is kept as the error, so both paints show the same warning", async () => {
  const { ctx, calls } = view(new Error("timed out"));
  ctx.STATE.data.restoreTests = LIST;
  await ctx.loadRestoreTests();
  assert.match(calls.morph[0][1].innerHTML, /Restore tests could not be listed[^]*timed out/);
  assert.equal(calls.morph[0][1].innerHTML, ctx.restoreTestsSection(), "the next page paint shows the warning too");
  assert.doesNotMatch(ctx.restoreTestsSection(), /paperless/, "not the old list, as if it were current");
});

test("switching restore tests on or off updates the kept list", async () => {
  const { ctx } = view();
  await ctx.loadRestoreTests();
  await ctx.restoreTestsSwitch(false);
  assert.equal(ctx.STATE.data.restoreTests.enabled, false, "the next paint does not tick the box again");
});

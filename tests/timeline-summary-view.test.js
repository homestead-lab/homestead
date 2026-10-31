// The summary push setting (#921) in Settings > You, and the Timeline it opens.
"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = fs.readFileSync("web/js/core.js", "utf8");
const NOW = 1_790_000_000;

function setup(answer, { fail = "", search = "", fields = {} } = {}) {
  const out = { calls: [], posts: [], toasts: [], closed: 0 };
  const els = {};
  const el = id => els[id] ??= { innerHTML: "", value: fields[id] ?? "", hidden: false, disabled: false, textContent: "" };
  const ctx = { console, Date, Map, Set, Promise, JSON, Math, URLSearchParams, Number, String, Array, Object, Intl,
    document: { addEventListener() {}, hidden: false }, STATE: { data: {}, view: "settings" }, ME: "alice",
    location: { search },
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, toast: (text, tone) => out.toasts.push([text, tone]), icon: () => "",
    $: sel => el(sel.replace(/^#/, "")), $$: () => [], closeModal: () => { out.closed++; },
    modal: (title, html) => { out.modal = { title, html }; },
    api: async (path, opts) => {
      if (opts?.method === "POST") { out.posts.push([path, JSON.parse(opts.body)]); return { ...JSON.parse(opts.body), next: NOW + 3600 }; }
      out.calls.push(path);
      if (fail) throw new Error(fail);
      return path.startsWith("/api/timeline/summary") ? answer : { since: NOW - 60, now: NOW, entries: [], counts: {}, unread: [] };
    } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
  vm.runInContext(fs.readFileSync("web/js/timeline.js", "utf8"), ctx);
  return { ctx, out, els, T: vm.runInContext("Timeline", ctx) };
}

test("the card is a Settings card in You with the standard head", () => {
  const { T } = setup({ every: "off" });
  const html = T.summaryCard();
  assert.match(html, /data-tab="device"/);
  assert.match(html, /Summary push/);
  assert.match(html, /summaryEdit\(\)/);
});

test("off, daily and weekly read as one line, with the next send and the last result", async () => {
  let { T, els } = setup({ every: "off" });
  await T.summaryPaint();
  assert.match(els.summaryRows.innerHTML, /Off/);
  ({ T, els } = setup({ every: "weekly", day: 6, hour: 18, tz: "America/New_York", next: NOW + 86400,
    sent_at: NOW - 7 * 86400, result: "sent to 2 devices" }));
  await T.summaryPaint();
  assert.match(els.summaryRows.innerHTML, /Weekly on Sunday at 18:00 \(America\/New_York\)/);
  assert.match(els.summaryRows.innerHTML, /sent to 2 devices/);
  ({ T, els } = setup({ every: "daily", day: 0, hour: 7, tz: "UTC", next: NOW + 60 }));
  await T.summaryPaint();
  assert.match(els.summaryRows.innerHTML, /Daily at 07:00 \(UTC\)/);
});

test("a setting that could not be read is said, never shown as Off", async () => {
  const { T, els } = setup(null, { fail: "Homestead could not read timeline-summary.json" });
  await T.summaryPaint();
  assert.match(els.summaryRows.innerHTML, /Could not read your summary setting/);
  assert.doesNotMatch(els.summaryRows.innerHTML, />Off</);
});

test("Change opens a dialog with the saved choice, and Save sends whole numbers", async () => {
  const { T, ctx, out } = setup({ every: "weekly", day: 2, hour: 9, tz: "Europe/London", next: NOW },
    { fields: { sum_every: "weekly", sum_day: "4", sum_hour: "21", sum_tz: "Europe/Paris" } });
  await T.summaryPaint();
  ctx.summaryEdit();
  assert.equal(out.modal.title, "Summary push");
  assert.match(out.modal.html, /<option value="weekly" selected>/);
  assert.match(out.modal.html, /<option value="2" selected>Wednesday/);
  assert.match(out.modal.html, /<option value="9" selected>09:00/);
  // The zone is typed, with the browser's zones offered: a select of every
  // zone would put 400 names in the dialog's words (audit_dialogs.mjs).
  assert.match(out.modal.html, /<input id="sum_tz"[^>]*list="sum_tz_list"[^>]*value="Europe\/London"/);
  assert.match(out.modal.html, /<datalist id="sum_tz_list">/);
  assert.doesNotMatch(out.modal.html, /<select id="sum_tz"/);
  await ctx.summarySave();
  assert.deepEqual(out.posts, [["/api/timeline/summary", { every: "weekly", day: 4, hour: 21, tz: "Europe/Paris" }]]);
  assert.equal(out.closed, 1);
});

test("Off saves only that", async () => {
  const { ctx, out } = setup({ every: "daily", hour: 8, tz: "UTC" }, { fields: { sum_every: "off", sum_day: "0", sum_hour: "8", sum_tz: "UTC" } });
  await ctx.summarySave();
  assert.deepEqual(out.posts, [["/api/timeline/summary", { every: "off" }]]);
});

test("a refused save keeps the dialog open and says why", async () => {
  const { ctx, out } = setup({ every: "off" }, { fields: { sum_every: "daily", sum_day: "0", sum_hour: "8", sum_tz: "Mars/Olympus" } });
  ctx.api = async () => { throw new Error("the time zone Mars/Olympus is not one Homestead knows"); };
  await ctx.summarySave();
  assert.equal(out.closed, 0);
  assert.deepEqual(out.toasts, [["the time zone Mars/Olympus is not one Homestead knows", "bad"]]);
});

test("the push opens the Timeline from where the summary starts", async () => {
  const { T, out } = setup({}, { search: `?since=${NOW - 86400}` });
  await T.view();
  assert.ok(out.calls.includes(`/api/timeline?since=${NOW - 86400}`), out.calls.join());
});

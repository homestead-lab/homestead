// "Since you were last here" (#748): the dashboard card and the Timeline page.
"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = fs.readFileSync("web/js/core.js", "utf8");
const NOW = 1_790_000_000;

const entry = (at, kind, tone, text, extra = {}) => ({ at: NOW - at, kind, tone, text, app: "", host: "", href: "/containers?q=x", ...extra });
const DATA = {
  since: NOW - 2 * 86400, now: NOW, last_here: NOW - 2 * 86400, clamped: false, more: 0,
  entries: [entry(100, "alert", "ok", "Scheduled job backup ran successfully"),
            entry(900, "schedule", "bad", "backup failed", { app: "apps/backup" }),
            entry(1800, "restart", "warn", "game restarted: Ran out of memory (limit 512 MiB)", { app: "apps/game" }),
            entry(3600, "change", "info", "game changed: Image, by alice", { app: "apps/game" }),
            entry(4000, "host", "info", "node-2 went into maintenance (OS update), back after 7 min", { host: "node-2" }),
            entry(5000, "restart", "warn", "game restarted: Crashed", { app: "apps/game" }),
            entry(6000, "change", "info", "wiki changed: Copies, outside Homestead", { app: "apps/wiki" })],
  counts: { alert: 1, schedule: 1, restart: 2, change: 2, host: 1 },
  unread: [{ source: "audit", label: "Audit log", reason: "shown to admins only" },
           { source: "restarts", label: "App restarts", reason: "Homestead could not read restarts.json <b>" }],
};

function setup(data = DATA, { fail = "" } = {}) {
  const out = { calls: [], posts: [] };
  const ctx = { console, Date, Map, Set, Promise, JSON, Math, URLSearchParams, Number, String, Array, Object,
    document: { addEventListener() {}, hidden: false }, STATE: { data: {}, view: "timeline" }, ME: "alice",
    esc: x => String(x ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    paint: html => { out.html = html; }, toast: () => {}, $: () => null, $$: () => [], icon: () => "",
    api: async (path, opts) => {
      if (opts) { out.posts.push(path); return { ok: true }; }
      out.calls.push(path);
      if (fail) throw new Error(fail);
      return JSON.parse(JSON.stringify(data));
    } };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-ui")(ctx);
  ctx.jsArg = s => JSON.stringify(String(s ?? "")); ctx.jsq = s => ctx.esc(ctx.jsArg(s));
  vm.runInContext(between(core, "const fmtUp", "const upChip"), ctx);
  vm.runInContext(fs.readFileSync("web/js/timeline.js", "utf8"), ctx);
  return { ctx, out };
}

test("the card counts by kind and lists the five that matter most, worst first", async () => {
  const { ctx } = setup();
  const T = vm.runInContext("Timeline", ctx);
  assert.deepEqual(Array.from(T.top(DATA.entries), e => e.text), [
    "backup failed", "game restarted: Ran out of memory (limit 512 MiB)", "game restarted: Crashed",
    "game changed: Image, by alice", "node-2 went into maintenance (OS update), back after 7 min"]);
  await T.load();
  const html = T.widget({ width: 6 });
  assert.match(html, /2 restarts/);
  assert.match(html, /1 scheduled job/);
  assert.match(html, /ui-chip/);
  assert.doesNotMatch(html, /class="(?:pill|tag)\b/);
  assert.equal((html.match(/class="tl-item/g) || []).length, 5);
});

test("a source that could not be read is said, not left out", async () => {
  const { ctx } = setup();
  const T = vm.runInContext("Timeline", ctx);
  await T.load();
  const html = T.widget({ width: 6 });
  assert.match(html, /Could not read App restarts/);
  assert.match(html, /restarts\.json &lt;b>/, "the reason is escaped");
  assert.match(html, /Audit log: shown to admins only/);
});

test("a timeline that could not be read says so, with the reason", async () => {
  const { ctx } = setup(DATA, { fail: "Homestead is not answering" });
  const T = vm.runInContext("Timeline", ctx);
  await T.load();
  assert.match(T.widget({}), /Could not read the timeline/);
  assert.match(T.widget({}), /Homestead is not answering/);
});

test("nothing since then is said in words", async () => {
  const { ctx } = setup({ ...DATA, entries: [], counts: {}, unread: [] });
  const T = vm.runInContext("Timeline", ctx);
  await T.load();
  assert.match(T.widget({}), /Nothing happened since/);
});

test("the page filters by kind, app and host", async () => {
  const { ctx, out } = setup();
  const T = vm.runInContext("Timeline", ctx);
  assert.deepEqual(T.filter(DATA.entries, { kind: "restart" }).length, 2);
  assert.deepEqual(T.filter(DATA.entries, { app: "apps/game" }).length, 3);
  assert.deepEqual(T.filter(DATA.entries, { host: "node-2" }).length, 1);
  await ctx.viewTimeline();
  assert.equal((out.html.match(/<tr class="tl-row/g) || []).length, 7);
  ctx.timelineFilter("app", "apps/game");
  assert.equal((out.html.match(/<tr class="tl-row/g) || []).length, 3);
  assert.match(out.html, /Could not read App restarts/);
});

test("the page's range asks for that window; since you were last here asks for none", async () => {
  const { ctx, out } = setup();
  await ctx.viewTimeline();
  assert.equal(out.calls.at(-1), "/api/timeline");
  await ctx.timelineRange("week");
  assert.match(out.calls.at(-1), /^\/api\/timeline\?since=\d+$/);
});

test("being here is noted at most once every five minutes", async () => {
  const { ctx, out } = setup();
  const T = vm.runInContext("Timeline", ctx);
  await T.markSeen(1_000_000);
  await T.markSeen(1_000_000 + 60_000);
  await T.markSeen(1_000_000 + 5 * 60_000 + 1);
  assert.deepEqual(out.posts, ["/api/timeline/seen", "/api/timeline/seen"]);
  ctx.ME = null;
  await T.markSeen(1_000_000 + 20 * 60_000);
  assert.equal(out.posts.length, 2, "signed out: nothing to note");
});

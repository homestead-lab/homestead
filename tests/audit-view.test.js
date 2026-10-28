"use strict";
/* Events › Audit (#738, part 2): the change log and the agent log, read
   together from /api/audit/log, newest first, filtered and paged by the server. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");
const loadUi = require("./helpers/load-ui");

const SOURCE = fs.readFileSync("web/js/views-storage.js", "utf8");

function load({ admin = true } = {}) {
  const calls = [], painted = [];
  const ctx = { console, URLSearchParams, Date, Number, String, Math, JSON, Object, Array, Set,
    STATE: { q: "", data: {} }, can: role => role !== "admin" || admin,
    esc: s => String(s ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`),
    jsq: s => `'${String(s).replace(/[\\']/g, "\\$&")}'`,
    fmtAgo: s => `${Math.round(s)}s ago`, fmtAt: t => String(t),
    api: async path => { calls.push(path); return typeof ctx.answer === "function" ? ctx.answer(path) : ctx.answer; },
    paint: html => painted.push(html), $: () => null, icon: () => "" };
  ctx.window = ctx;
  vm.createContext(ctx);
  loadUi(ctx);
  // const declarations are not the context's properties: hand the ones tested over.
  vm.runInContext(`${between(SOURCE, "const eventTab = ", "function eventSelect")}
    ${between(SOURCE, "/* ---------------- audit log", "async function viewEvents")}
    Object.assign(globalThis, { eventTab, auditFilter, AUDIT_PAGE });`, ctx);
  return { ctx, calls, painted };
}

test("the Audit tab is offered to admins only, beside Sign-ins", () => {
  const admin = load();
  assert.match(admin.ctx.eventTabs("cluster"), /Sign-ins<\/button>.*Audit<\/button>/s);
  admin.ctx.STATE.eventsTab = "audit";
  assert.equal(admin.ctx.eventTab(), "audit");
  const operator = load({ admin: false });
  operator.ctx.STATE.eventsTab = "audit";
  assert.equal(operator.ctx.eventTab(), "cluster", "a non-admin never lands on it");
  assert.equal(operator.ctx.eventTabs("cluster"), "");
});

test("the filters and the page go to the server", () => {
  const { ctx } = load();
  assert.equal(ctx.auditParams(), `limit=${ctx.AUDIT_PAGE}`);
  Object.assign(ctx.auditFilter(), { source: "agents", person: "robin a", offset: 200 });
  assert.equal(ctx.auditParams(), `limit=${ctx.AUDIT_PAGE}&source=agents&person=robin+a&offset=200`);
});

test("changing a filter starts again at the newest; Older and Newer move a page", async () => {
  const { ctx, calls } = load();
  ctx.answer = path => ({ rows: [], offset: Number(new URLSearchParams(path.split("?")[1]).get("offset")) || 0,
    limit: ctx.AUDIT_PAGE, more: true, partial: false });
  ctx.STATE.eventsTab = "audit";
  ctx.viewEvents = () => ctx.viewAudit();
  await ctx.auditPage(1);
  assert.equal(ctx.auditFilter().offset, ctx.AUDIT_PAGE);
  await ctx.auditPage(1);
  await ctx.auditPage(-1);
  assert.equal(ctx.auditFilter().offset, ctx.AUDIT_PAGE);
  await ctx.auditPage(-1);
  await ctx.auditPage(-1);
  assert.equal(ctx.auditFilter().offset, 0, "never before the newest");
  ctx.auditFilter().offset = 300;
  await ctx.auditFilterSet("person", " ada ");
  assert.deepEqual([ctx.auditFilter().person, ctx.auditFilter().offset], ["ada", 0]);
  assert.match(calls.at(-1), /person=ada$/);
});

test("the page shown is the one the server answered, so Older cannot run past its cap", async () => {
  const { ctx } = load();
  // The server clamps the offset at 200 and says there is no more there (review M1).
  ctx.answer = path => {
    const offset = Math.min(200, Number(new URLSearchParams(path.split("?")[1]).get("offset")) || 0);
    return { rows: [{ source: "changes", at: 1, who: "ada", what: "POST /api/x", result: "ok" }], offset, limit: 100, more: offset < 200 };
  };
  ctx.STATE.eventsTab = "audit";
  ctx.viewEvents = () => ctx.viewAudit();
  ctx.paint = html => { ctx.painted = html; };
  for (let i = 0; i < 4; i++) await ctx.auditPage(1);
  assert.equal(ctx.auditFilter().offset, 200);
  assert.match(ctx.painted, /disabled onclick="auditPage\(1\)"/, "Older is off at the cap");
});

test("the What cell reads as separate words when a phone runs it into one line", () => {
  // A compact stacked row lays the cell's parts inline (first phone screenshot).
  const { ctx } = load();
  const html = ctx.auditRowHtml({ source: "changes", at: 0, who: "ada", what: "POST /api/workload/scale",
    target: "lab/Deployment/app-1", addr: "192.0.2.10", result: "ok", code: 200 });
  const cell = html.match(/<td class="small ev-msg">([\s\S]*?)<\/td>/)[1].replace(/<[^>]+>/g, "");
  assert.equal(cell, "POST /api/workload/scale · lab/Deployment/app-1 · 192.0.2.10");
});

test("an HTTP status is shown only when it says something went wrong", () => {
  const { ctx } = load();
  // An MCP tool refused or failed is answered 200 (review M2).
  const tool = ctx.auditRowHtml({ source: "changes", at: 0, who: "ada", what: "restore_backup", result: "error", code: 200 });
  assert.doesNotMatch(tool, /· 200/);
  const refused = ctx.auditRowHtml({ source: "changes", at: 0, who: "ada", what: "POST /api/x", result: "refused", code: 403 });
  assert.match(refused, /Refused · 403/);
});

test("the demo's audit rows use documentation addresses only", () => {
  const block = between(fs.readFileSync("web/js/demo.js", "utf8"), "const demoChanges = [", 'responses["/api/audit/log"]');
  const addresses = [...block.matchAll(/\b\d{1,3}(?:\.\d{1,3}){3}\b/g)].map(m => m[0]);
  assert.ok(addresses.length);
  assert.deepEqual(addresses.filter(a => !/^(192\.0\.2|198\.51\.100|203\.0\.113)\./.test(a)), []);
});

test("a change row says its log, who, what, the target and the result", () => {
  const { ctx } = load();
  const html = ctx.auditRowHtml({ source: "changes", at: Date.now() / 1000 - 60, who: "ada", via: "browser",
    addr: "192.0.2.10", what: "POST /api/workload/scale", target: "lab/Deployment/app-1", result: "ok", code: 200 });
  assert.match(html, /<b>ada<\/b>/);
  assert.match(html, /Change log/);
  assert.match(html, /POST \/api\/workload\/scale/);
  assert.match(html, /lab\/Deployment\/app-1/);
  assert.match(html, /Done/);
  assert.match(html, /192\.0\.2\.10/);
});

test("an agent row names the key, and a refusal or failure is coloured", () => {
  const { ctx } = load();
  const refused = ctx.auditRowHtml({ source: "agents", at: Date.now() / 1000, who: "ada", via: "mcp", addr: "",
    what: "scale_container", key: "Claude Code", result: "refused", detail: "the key lacks containers:manage" });
  assert.match(refused, /Agent log/);
  assert.match(refused, /Claude Code/);
  assert.match(refused, /the key lacks containers:manage/);
  assert.match(refused, /Refused/);
  assert.match(refused, /warn/);
  const failed = ctx.auditRowHtml({ source: "changes", at: 0, who: "", what: "POST /api/x", result: "error", code: 500 });
  assert.match(failed, /Failed/);
  assert.match(failed, /bad/);
  assert.match(failed, /—/, "no one signed in is a dash");
});

test("what a row holds is escaped", () => {
  const { ctx } = load();
  const html = ctx.auditRowHtml({ source: "changes", at: 0, who: "<img src=x>", what: "<script>", result: "ok",
    detail: "\"><b>", target: "<i>" });
  assert.doesNotMatch(html, /<img|<script|<i>|"><b>/);
});

test("the count says which rows these are, and when the search stopped short", () => {
  const { ctx } = load();
  assert.equal(ctx.auditCount({ rows: [{}, {}], offset: 0, more: false, partial: false }), "2 entries from both logs · newest first");
  ctx.auditFilter().source = "agents";
  assert.match(ctx.auditCount({ rows: [{}, {}], offset: 100, more: true, partial: false }), /^101–102 · agent calls/);
  assert.match(ctx.auditCount({ rows: [], offset: 0, more: false, partial: true }), /only the newest lines/);
});

async function demo() {
  const ctx = { URL, URLSearchParams, Response, Date, console, location: { search: "?demo=1", origin: "https://demo.example.com" },
    HOMESTEAD_DEMO: false, document: { body: null, addEventListener() {} }, fetch: async () => { throw new Error("no cluster"); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/demo.js", "utf8"), ctx);
  return async path => (await ctx.fetch(path)).json();
}

test("the demo answers the audit log, filtered and paged as the server does", async () => {
  const api = await demo();
  const all = await api("/api/audit/log?limit=100");
  assert.ok(all.rows.some(r => r.source === "changes") && all.rows.some(r => r.source === "agents"));
  assert.deepEqual(all.rows.map(r => r.at), [...all.rows.map(r => r.at)].sort((a, b) => b - a), "newest first");
  const agents = await api("/api/audit/log?source=agents");
  assert.ok(agents.rows.length && agents.rows.every(r => r.source === "agents"));
  const alex = await api("/api/audit/log?person=ALEX");
  assert.ok(alex.rows.length && alex.rows.every(r => r.who === "alex"));
  const first = await api("/api/audit/log?limit=2"), second = await api("/api/audit/log?limit=2&offset=2");
  assert.equal(first.rows.length, 2);
  assert.ok(first.more);
  assert.notDeepEqual(first.rows, second.rows);
  assert.equal(second.offset, 2);
});

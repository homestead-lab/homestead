const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

function load(storageAt = 0) {
  const appended = [], replaced = [], timers = [], store = { "homestead.signInReload": String(storageAt) };
  const answers = [];
  const ctx = {
    console, Date, String, Object, Error, JSON, Promise,
    setTimeout: (fn, ms) => { timers.push(fn); return 1; }, clearTimeout: () => {},
    sessionStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
    location: { href: "https://homestead.example.com/containers", replace: url => replaced.push(url) },
    document: { body: { appendChild: el => appended.push(el) }, createElement: () => ({ setAttribute() {}, innerHTML: "", className: "" }),
                getElementById: () => null },
    fetch: async (url, opts) => { answers.push([url, opts]); return answers.next(url, opts); },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  // Only the wrapper and the sign-in handling: the rest of auth.js needs a page.
  const src = fs.readFileSync("web/js/auth.js", "utf8");
  const start = src.indexOf("const _rawFetch"), end = src.indexOf("/* The last answer from /api/auth/state");
  vm.runInContext(src.slice(start, end), ctx);
  return { ctx, appended, replaced, timers, store, answers };
}

test("a redirected API call is a lapsed sign-in: say so once, then reload through it", async () => {
  const t = load(0);
  t.answers.next = () => ({ type: "opaqueredirect", status: 0 });
  await assert.rejects(t.ctx.fetch("/api/workloads"), err => err.signIn === true);
  await assert.rejects(t.ctx.fetch("/api/nodes"), err => err.signIn === true);
  assert.equal(t.appended.length, 1, "one bar, however many calls fail");
  assert.match(t.appended[0].innerHTML, /Signing you in again/);
  assert.equal(t.answers[0][1].redirect, "manual", "API calls do not follow redirects");
  t.timers.forEach(fn => fn());
  assert.deepEqual(t.replaced, ["https://homestead.example.com/containers"]);
  assert.ok(+t.store["homestead.signInReload"] > 0);
});

test("a second lapse within a minute asks instead of looping", async () => {
  const t = load(Date.now() - 10000);
  t.answers.next = () => ({ type: "opaqueredirect", status: 0 });
  await assert.rejects(t.ctx.fetch("/api/workloads"));
  assert.match(t.appended[0].innerHTML, /Sign in<\/button>/);
  assert.equal(t.timers.length, 0, "no automatic reload");
});

test("ordinary answers pass through, and writes carry the auth header", async () => {
  const t = load(0);
  t.answers.next = () => ({ type: "basic", status: 200, ok: true });
  const r = await t.ctx.fetch("/api/workloads/group", { method: "POST", body: "{}" });
  assert.equal(r.status, 200);
  assert.equal(t.answers[0][1].headers["X-Homestead-Auth"], "1");
  assert.equal(t.appended.length, 0);
  await t.ctx.fetch("https://example.com/x");
  assert.equal(t.answers[1][1].redirect, undefined, "other sites are left alone");
});

test("a page older than the Homestead answering it offers to reload, or reloads after its own update", async () => {
  const answer = version => ({ type: "basic", status: 200, headers: { get: name => name === "X-Homestead-Version" ? version : null } });
  const t = load(0);
  t.ctx.HOMESTEAD_VERSION = "2.8.317";
  t.ctx.document.querySelector = () => null;
  const reloads = [];
  t.ctx.location.reload = () => reloads.push(1);
  t.answers.next = () => answer("2.8.317");
  await t.ctx.fetch("/api/workloads");
  assert.equal(t.appended.length, 0, "the same version says nothing");
  t.answers.next = () => answer("2.8.318");
  await t.ctx.fetch("/api/workloads");
  await t.ctx.fetch("/api/nodes");
  assert.equal(t.appended.length, 1, "one bar");
  assert.match(t.appended[0].innerHTML, /updated to v2\.8\.318[\s\S]*Reload/);
  t.timers.forEach(fn => fn());
  assert.equal(reloads.length, 0, "an update made elsewhere is only offered");

  const mine = load(0);
  mine.ctx.HOMESTEAD_VERSION = "2.8.317";
  mine.ctx.document.querySelector = () => null;
  const own = [];
  mine.ctx.location.reload = () => own.push(1);
  mine.store["homestead.selfUpdate"] = String(Date.now());
  mine.answers.next = () => answer("2.8.318");
  await mine.ctx.fetch("/api/workloads");
  assert.match(mine.appended[0].innerHTML, /Loading it/);
  mine.timers.forEach(fn => fn());
  assert.equal(own.length, 1, "right after updating it from this page, it reloads by itself");
});

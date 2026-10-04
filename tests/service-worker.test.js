const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* Runs web/sw.js against a stand-in for the browser's service worker scope. */
function worker({ pending, subscription = { endpoint: "https://fcm.googleapis.com/x" }, windows = [], displayError = false, showing = [] }) {
  const handlers = {};
  const shown = [];
  const fetched = [];
  const opened = [];
  const self = {
    location: new URL("https://home.example.com/"),
    addEventListener: (type, fn) => { handlers[type] = fn; },
    registration: {
      pushManager: { getSubscription: async () => subscription },
      showNotification: async (title, options) => { if(displayError)throw new Error("display failed");shown.push({ title, ...options }); },
      // What is in the notification shade already.
      getNotifications: async () => showing,
    },
    clients: {
      matchAll: async () => windows,
      openWindow: async url => { opened.push(url); },
      claim: async () => {},
    },
    skipWaiting: async () => {},
    navigator: {},
  };
  const context = {
    self, URL, Response, Promise, JSON, atob, Uint8Array,
    caches: { open: async () => ({ add: async () => {}, put: async () => {} }), keys: async () => [], match: async () => null },
    fetch: async (url, options) => {
      fetched.push({ url, options });
      return pending(url, options);
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "..", "web", "sw.js"), "utf8"), context);
  const fire = async (type, extra = {}) => {
    let done;
    const event = { waitUntil: p => { done = p; }, ...extra };
    handlers[type](event);
    await done;
  };
  return { fire, shown, fetched, opened };
}

const answer = body => async () => ({ ok: true, status: 200, json: async () => body });

test("a push shows what Homestead says happened", async () => {
  const sw = worker({ pending: answer({ active: 1, alerts: [
    { key: "health:Node:h1", title: "Node h1 is down", body: "node is NotReady", severity: "critical",
      phase: "raised", href: "/nodes", at: 100 }] }) });
  await sw.fire("push");

  assert.equal(sw.shown.length, 1);
  assert.equal(sw.shown[0].title, "Node h1 is down");
  assert.equal(sw.shown[0].tag, "health:Node:h1", "its resolution replaces it");
  assert.equal(sw.shown[0].requireInteraction, true, "an outage stays until seen");
  assert.equal(sw.shown[0].data.href, "/nodes");
  const call = sw.fetched[0];
  assert.equal(call.url, "/api/alerts/pending");
  assert.equal(call.options.headers["X-Homestead-Auth"], "1");
  assert.equal(JSON.parse(call.options.body).endpoint, "https://fcm.googleapis.com/x");
});

test("many at once become one summary", async () => {
  const alerts = Array.from({ length: 5 }, (_, i) => ({ key: `k${i}`, title: `Alert ${i}`, severity: "degraded", phase: "raised" }));
  const sw = worker({ pending: answer({ alerts }) });
  await sw.fire("push");

  assert.equal(sw.shown.length, 1);
  assert.equal(sw.shown[0].title, "5 Homestead notifications");
});

test("signed out, it still says to look", async () => {
  const sw = worker({ pending: async () => ({ ok: false, status: 401, json: async () => ({}) }) });
  await sw.fire("push");

  assert.equal(sw.shown.length, 1);
  assert.match(sw.shown[0].body, /sign in/);
});

test("nothing new, nothing shown", async () => {
  const sw = worker({ pending: answer({ alerts: [] }) });
  await sw.fire("push");
  assert.equal(sw.shown.length, 0);
});

test("a tap opens the page, or tells the open app where to go", async () => {
  const notification = { data: { href: "/containers?panel=web" }, close() {} };
  const fresh = worker({ pending: answer({}) });
  await fresh.fire("notificationclick", { notification });
  assert.deepEqual(fresh.opened, ["/containers?panel=web"]);

  const messages = [];
  const open = { url: "https://home.example.com/", postMessage: m => messages.push(m), focus: async () => {} };
  const running = worker({ pending: answer({}), windows: [open] });
  await running.fire("notificationclick", { notification });
  assert.deepEqual(JSON.parse(JSON.stringify(messages)), [{ type: "homestead-open", href: "/containers?panel=web" }]);
  assert.deepEqual(running.opened, []);
});

test("a notification cannot send the app to another site", async () => {
  const sw = worker({ pending: answer({}) });
  await sw.fire("notificationclick", { notification: { data: { href: "https://evil.example/" }, close() {} } });
  assert.deepEqual(sw.opened, []);
});


test("delivery is confirmed only after notifications display successfully",async()=>{
 const payload={known:true,latest:7,alerts:[{key:"disk",title:"Drive needs attention",severity:"critical",phase:"worsened"}]};
 const good=worker({pending:answer(payload)});await good.fire("push");
 assert.equal(good.fetched.at(-1).url,"/api/alerts/delivered");assert.equal(JSON.parse(good.fetched.at(-1).options.body).latest,7);
 const failed=worker({pending:answer(payload),displayError:true});await assert.rejects(failed.fire("push"),/display failed/);
 assert.equal(failed.fetched.length,1);assert.equal(good.shown[0].requireInteraction,true);
});
test("recovery is quiet and never requests persistent attention",async()=>{
 const sw=worker({pending:answer({alerts:[{key:"disk",title:"Drive warnings cleared",severity:"critical",phase:"resolved"}]})});
 await sw.fire("push");assert.equal(sw.shown[0].silent,true);assert.equal(sw.shown[0].renotify,false);assert.equal(sw.shown[0].requireInteraction,false);
});
test("connectivity failure does not claim a new cluster incident",async()=>{
 const sw=worker({pending:async()=>{throw new Error("offline");}});await sw.fire("push");
 assert.equal(sw.shown[0].title,"Homestead notification details unavailable");assert.equal(sw.shown[0].renotify,false);assert.doesNotMatch(sw.shown[0].body,/Something changed/);
});
test("a second alert while one shows becomes one summary, so Android does not bundle them under its own icon", async () => {
  const closed = [];
  const earlier = { tag: "health:Node:h1", title: "Node h1 is down", close: () => closed.push("health:Node:h1") };
  const sw = worker({ showing: [earlier], pending: answer({ alerts: [{ key: "disk", title: "Drive needs attention", severity: "degraded", phase: "raised" }] }) });
  await sw.fire("push");
  assert.equal(sw.shown.length, 1);
  assert.equal(sw.shown[0].title, "2 Homestead notifications");
  assert.equal(sw.shown[0].tag, "homestead-summary");
  assert.match(sw.shown[0].body, /Drive needs attention · Node h1 is down/);
  assert.equal(sw.shown[0].badge, "/icons/badge-96.png");
  assert.deepEqual(closed, ["health:Node:h1"]);
  // The summary remembers what it holds, so the next push adds to it.
  const next = worker({ showing: [{ tag: "homestead-summary", title: "2 Homestead notifications", data: sw.shown[0].data, close() {} }],
    pending: answer({ alerts: [{ key: "vip", title: "Address conflict", severity: "degraded", phase: "raised" }] }) });
  await next.fire("push");
  assert.equal(next.shown[0].title, "3 Homestead notifications");
});

test("an update to the one alert showing stays that alert", async () => {
  const sw = worker({ showing: [{ tag: "disk", title: "Drive needs attention", close() { throw new Error("not closed"); } }],
    pending: answer({ alerts: [{ key: "disk", title: "Drive warnings cleared", severity: "degraded", phase: "resolved" }] }) });
  await sw.fire("push");
  assert.equal(sw.shown[0].tag, "disk");
});

test("summaries prioritize critical conditions and bound lock-screen text",async()=>{
 const alerts=Array.from({length:6},(_,i)=>({key:String(i),title:i===0?"Critical disk":"x".repeat(200),severity:i===0?"critical":"degraded",phase:"raised",at:i}));
 const sw=worker({pending:answer({alerts})});await sw.fire("push");
 assert.match(sw.shown[0].body,/^Critical disk/);assert.ok(sw.shown[0].body.length<=300);assert.equal(sw.shown[0].data.href,"/settings?tab=device");
});

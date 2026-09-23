const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* Runs web/sw.js against a stand-in for the browser's service worker scope. */
function worker({ pending, subscription = { endpoint: "https://fcm.googleapis.com/x" }, windows = [] }) {
  const handlers = {};
  const shown = [];
  const fetched = [];
  const opened = [];
  const self = {
    location: new URL("https://home.example.com/"),
    addEventListener: (type, fn) => { handlers[type] = fn; },
    registration: {
      pushManager: { getSubscription: async () => subscription },
      showNotification: async (title, options) => { shown.push({ title, ...options }); },
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
  assert.equal(sw.shown[0].title, "5 updates from Homestead");
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

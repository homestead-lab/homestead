"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");
const { webcrypto } = require("node:crypto");

// The page's elements, by selector; each test starts with fresh ones.
let elements = {};
const element = () => ({ innerHTML: "", value: "", classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {} });

const context = {
  console,
  crypto: webcrypto,
  btoa: value => Buffer.from(value, "binary").toString("base64"),
  document: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  WebSocket: { OPEN: 1 },
  $: selector => elements[selector] || null,
  $$: () => [],
  esc: value => String(value),
  STATE: { q: "" },
  resetPaint() {},
  paint() {},
};
// The page calls window.storeBrowse and friends by their bare names, as a browser allows.
context.window = context;
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => (context.esc || String)(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), context);

const TRADEMARK = /Unraid® is a registered trademark of Lime Technology, Inc\. This application is not affiliated with/;
const DEFAULT_FEED = { url: "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json", default: true };
const CUSTOM_FEED = { url: "https://feed.example.com/f.json", default: false };

function page() {
  elements = { "#s_source": element(), "#s_res": element(), "#s_q": element() };
  return elements["#s_source"];
}

test("the catalogue notice carries the trademark line before the server has said where listings come from", () => {
  const notice = page();
  context.storeSource(undefined);
  assert.match(notice.innerHTML, TRADEMARK);
  assert.doesNotMatch(notice.innerHTML, /Listings are read/, "no feed is named before the server names one");
});

test("the catalogue notice names the public feed and keeps the trademark line", () => {
  const notice = page();
  context.storeSource(DEFAULT_FEED);
  assert.match(notice.innerHTML, /Community Applications feed/);
  assert.match(notice.innerHTML, TRADEMARK);
});

test("the catalogue notice names a custom feed and still keeps the trademark line", () => {
  const notice = page();
  context.storeSource(CUSTOM_FEED);
  assert.match(notice.innerHTML, /feed\.example\.com\/f\.json/);
  assert.match(notice.innerHTML, TRADEMARK);
});

test("the App Store fills its notice as it opens, so it is never an empty box", async () => {
  const notice = page();
  context.api = () => new Promise(() => {});
  await context.viewStore();
  assert.match(notice.innerHTML, TRADEMARK);
});

test("a catalogue search says where its listings come from", async () => {
  const notice = page();
  elements["#s_q"].value = "frigate";
  context.api = async () => ({ total: 0, apps: [], source: CUSTOM_FEED });
  await context.storeSearch();
  assert.match(notice.innerHTML, /feed\.example\.com\/f\.json/);
  assert.match(notice.innerHTML, TRADEMARK);
});

test("the demo's catalogue answers name the default feed", () => {
  const demo = fs.readFileSync("web/js/demo.js", "utf8");
  const answer = between(demo, '"/api/appstore":', '"/api/appstore/app":');
  assert.equal(answer.match(/source: demoStoreSource/g)?.length, 2, "both the browse and the search answer carry the source");
  assert.match(demo, /const demoStoreSource = \{ url: "https:\/\/raw\.githubusercontent\.com\/Squidly271\/AppFeed\/master\/applicationFeed\.json", default: true \}/);
});

test("an app's page opened while the catalogue is still loading offers Try again", async () => {
  elements = { "#mbody": element() };
  context.modal = () => {};
  context.icon = () => "";   // icons.js, which the page loads first
  context.api = async () => {
    throw Object.assign(new Error("the App Store catalogue is still loading"), { status: 503, body: { loading: true } });
  };
  await vm.runInContext(`storeDetails("Plex|example/plex:latest")`, context);
  const html = elements["#mbody"].innerHTML;
  assert.match(html, /still loading/);
  assert.match(html, /Try again/);
  assert.match(html, /storeDetails\(/);
});

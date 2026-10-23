"use strict";
// "What's in this update" in the image-update review (web/js/update-notes.js).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

function setup(answer) {
  const sent = [];
  const ctx = {console, encodeURIComponent, Promise, URL, JSON,
    esc: s => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    icon: name => `<svg data-icon="${name}"></svg>`,
    asSentence: text => String(text).replace(/^./, c => c.toUpperCase()).replace(/\.?$/, "."),
    api: async (path, options) => { sent.push({path, options}); return answer(path, options); }};
  ctx.window = ctx;
  vm.createContext(ctx);
  const core = fs.readFileSync("web/js/core.js", "utf8");
  vm.runInContext(between(core, "const safeHref", "/* Put text") + "\nthis.safeHref = safeHref;", ctx);
  vm.runInContext(fs.readFileSync("web/js/update-notes.js", "utf8"), ctx);
  return {ctx, sent};
}

const none = {ports: {added: [], removed: []}, volumes: {added: [], removed: []},
  env: {added: [], removed: [], changed: [], set_by_app: []}, user: null, build_env_changed: 0, none: true};
const image = (extra = {}) => ({container: "app", from_tag: "1.1", to_tag: "1.2", from_version: "1.1.0", to_version: "1.2.0",
  source: "https://github.com/example/app", links: {}, changes: none, ...extra});
const render = (ctx, images, config = {ns: "lab", name: "app"}) => ctx.UpdateNotes.render([{config, notes: {images}}]);

test("the links come from the answer, through safeHref", () => {
  const {ctx} = setup();
  const html = render(ctx, [image({links: {release: "https://github.com/example/app/releases?q=1.2.0&expanded=true",
    compare: "https://github.com/example/app/compare/a...b", project: "javascript:alert(1)"}})]);
  assert.match(html, /href="https:\/\/github.com\/example\/app\/releases\?q=1.2.0&amp;expanded=true"[^>]*>Release notes for 1.2.0/);
  assert.match(html, /href="https:\/\/github.com\/example\/app\/compare\/a...b"[^>]*>Code changes/);
  assert.match(html, /href="#"[^>]*>Project site/);
  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /rel="noopener noreferrer"/);
});

test("an image without labels says so", () => {
  const {ctx} = setup();
  assert.match(render(ctx, [image()]), /does not say where its source or release notes are/);
});

test("env is shown by name only", () => {
  const {ctx} = setup();
  const html = render(ctx, [image({changes: {...none, none: false,
    env: {added: ["AUTH"], removed: ["LEGACY"], changed: ["DB", "TZ"], set_by_app: ["TZ"]}}})]);
  assert.match(html, /New setting.*<code>AUTH<\/code>/s);
  assert.match(html, /Setting removed.*<code>LEGACY<\/code>/s);
  assert.match(html, /Default changed.*<code>DB<\/code> <code>TZ<\/code>.*value changed/s);
  assert.match(html, /Your app sets <code>TZ<\/code> itself, so its own values still apply/);
  assert.doesNotMatch(html, /<code>[A-Z]+=/);
});

test("ports, volumes and a change of user", () => {
  const {ctx} = setup();
  const html = render(ctx, [image({changes: {...none, none: false, ports: {added: ["8971/tcp"], removed: ["81/tcp"]},
    volumes: {added: ["/media"], removed: []}, user: {was: "root", now: "1000"}, build_env_changed: 3}})]);
  assert.match(html, /New port.*8971\/tcp/s);
  assert.match(html, /Port no longer exposed.*81\/tcp/s);
  assert.match(html, /New volume.*\/media.*not kept unless you add storage for it/s);
  assert.match(html, /Runs as.*root.*1000.*may need new owners/s);
  assert.match(html, /3 build details changed too/);
});

test("the app's own user wins, and the page says so", () => {
  const {ctx} = setup();
  const html = render(ctx, [image({changes: {...none, none: false, user: {was: "root", now: "1000", overridden_by: "568"}}})]);
  assert.match(html, /your app runs it as <code>568<\/code>, so this does not apply/);
  assert.doesNotMatch(html, /may need new owners/);
});

test("nothing changed", () => {
  const {ctx} = setup();
  assert.match(render(ctx, [image()]), /Same ports, volumes and settings as the version running now/);
});

test("an image that could not be read, and an app the last check did not cover", () => {
  const {ctx} = setup();
  assert.match(render(ctx, [{container: "app", error: "registry authentication required"}]), /Registry authentication required\./);
  assert.match(ctx.UpdateNotes.render([{config: {name: "app"}, notes: {images: [], error: "not in the last update check"}}]),
    /Not in the last update check\./);
});

test("the box asks the server for the app being reviewed, on its own cluster", async () => {
  const {ctx, sent} = setup(() => ({images: [image()]}));
  const el = {isConnected: true, innerHTML: ""};
  await ctx.updateNotesInto(el, [{ns: "lab", name: "app", cluster: "c2"}]);
  assert.equal(sent[0].path, "/api/image-updates/notes");
  assert.equal(sent[0].options.method, "POST");
  assert.deepEqual(JSON.parse(sent[0].options.body), {ns: "lab", name: "app"});
  assert.equal(sent[0].options.headers["X-Homestead-Cluster"], "c2");
  assert.match(el.innerHTML, /What's in this update/);
});

test("a failed request leaves a sentence and the review usable", async () => {
  const {ctx} = setup(() => { throw new Error("network down"); });
  const el = {isConnected: true, innerHTML: ""};
  await ctx.updateNotesInto(el, [{ns: "lab", name: "app"}, {ns: "lab", name: "db"}]);
  assert.match(el.innerHTML, /<div class="un-app">app<\/div><p class="dim">Network down\.<\/p>/);
  assert.match(el.innerHTML, /un-app">db/);
});

test("a closed dialog is left alone", async () => {
  const {ctx} = setup(() => ({images: [image()]}));
  const el = {isConnected: false, innerHTML: "before"};
  await ctx.updateNotesInto(el, [{ns: "lab", name: "app"}]);
  assert.doesNotMatch(el.innerHTML, /What's in this update/);
});

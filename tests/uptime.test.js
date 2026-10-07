const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ctx = { console, esc, jsq: s => esc(JSON.stringify(String(s))), window: {}, STATE: { data: {} }, Date };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/uptime.js", "utf8"), ctx);
const run = code => vm.runInContext(code, ctx);

const app = { ns: "lab", name: "paperless" };
const set = state => { ctx.STATE.data.uptime = { apps: { "lab/paperless": state } }; };

test("a row says nothing while an app answers", () => {
  set({ state: "up", last: { ok: true, ms: 30, code: 200 }, strip: [] });
  assert.equal(run(`answerTag(${JSON.stringify(app)})`), "");
  set(null);
  assert.equal(run(`answerTag(${JSON.stringify(app)})`), "", "no report yet is not a problem either");
});

test("down and slow are marked, with what went wrong", () => {
  set({ state: "down", since: 1000, last: { ok: false, error: "HTTP 502" }, strip: [] });
  const down = run(`answerTag(${JSON.stringify(app)})`);
  assert.match(down, /tag bad/);
  assert.match(down, />Down</);
  assert.match(down, /aria-label="Down: /);
  assert.match(down, /HTTP 502/);
  set({ state: "slow", last: { ok: true, ms: 2900, code: 200 }, strip: [] });
  assert.match(run(`answerTag(${JSON.stringify(app)})`), /tag warn[^]*Slow/);
});

test("an error from the app is text, never markup", () => {
  set({ state: "down", since: 1000, last: { ok: false, error: "<img src=x onerror=alert(1)>" }, strip: [] });
  const html = run(`answerTag(${JSON.stringify(app)}) + answerCardRow(${JSON.stringify(app)})`);
  assert.ok(!html.includes("<img src=x"));
});

test("the card shows the day hour by hour and 30 days as a share", () => {
  set({ state: "up", last: { ok: true, ms: 30, code: 200 }, strip: [null, "up", "slow", "down"], uptime_30d: 99.2 });
  const html = run(`answerCardRow(${JSON.stringify(app)})`);
  assert.equal((html.match(/<i class="/g) || []).length - 1, 4, "four hours and the state dot");
  assert.match(html, /class="none"[^]*class="up"[^]*class="slow"[^]*class="down"/);
  assert.match(html, /99\.20%/);
});

test("shares round sensibly", () => {
  assert.equal(run("answerShare(100)"), "100%");
  assert.equal(run("answerShare(99.996)"), "100%");
  assert.equal(run("answerShare(99.2)"), "99.20%");
  assert.equal(run("answerShare(96.54)"), "96.5%");
  assert.equal(run("answerShare(null)"), "—");
});

test("Homestead's own and platform apps get no line", () => {
  set({ state: "off", why: "not an app of yours", strip: [] });
  assert.equal(run(`answerCardRow(${JSON.stringify(app)})`), "");
});


test("the dashboard widget shows more as it widens", () => {
  ctx.STATE.data.wl = [{ ns: "lab", name: "a" }, { ns: "lab", name: "b" }, { ns: "lab", name: "c" }, { ns: "lab", name: "off" }];
  ctx.STATE.data.uptime = { apps: {
    "lab/a": { state: "down", last: { error: "HTTP 502" }, strip: ["up", "down"], uptime_30d: 98 },
    "lab/b": { state: "up", last: { ms: 4 }, strip: ["up", "up"], uptime_30d: 100 },
    "lab/c": { state: "slow", last: { ms: 2500 }, strip: ["up", "slow"], uptime_30d: 99.5 },
    "lab/off": { state: "off", why: "stopped", strip: [] } } };
  ctx.appAvatar = name => `<span class="av">${name}</span>`;
  const count = (html, re) => (html.match(re) || []).length;
  const third = run("monitorWidget({ width: 4 })");
  assert.equal(count(third, /class="mon-item"/g), 2, "one third lists only what is wrong");
  assert.match(third, /HTTP 502/);
  const half = run("monitorWidget({ width: 6 })");
  assert.equal(count(half, /class="mon-item"/g), 3, "half lists every monitored app");
  assert.equal(count(half, /answer-strip/g), 0);
  const wide = run("monitorWidget({ width: 8 })");
  assert.equal(count(wide, /answer-strip mon-mini/g), 3, "two thirds gives each its bars");
  assert.ok(wide.indexOf(">a<") < wide.indexOf(">c<") && wide.indexOf(">c<") < wide.indexOf(">b<"), "down, then slow, then up");
});

test("the monitoring field reads back what it was given", () => {
  assert.match(run('monitoringFieldHtml("e", "/health")'), /value="http" selected[^]*value="\/health"/);
  assert.match(run('monitoringFieldHtml("e", "off")'), /value="off" selected/);
  assert.match(run('monitoringFieldHtml("e", "")'), /value="auto" selected/);
});

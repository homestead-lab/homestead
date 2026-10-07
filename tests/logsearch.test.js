const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const vm = require("vm");

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const ctx = { console, esc, jsq: s => esc(JSON.stringify(String(s))), window: {}, STATE: { data: {} }, Date, RegExp };
ctx.UI = { callout: (kind, title, body) => `<callout ${kind}>${title}|${body}</callout>` };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("web/js/logsearch.js", "utf8"), ctx);
const run = code => vm.runInContext(code, ctx);

test("what matched is marked, and everything is escaped", () => {
  const out = run(`logSearchMark("<b>Error</b>: disk error", "error")`);
  assert.equal(out, "&lt;b&gt;<mark>Error</mark>&lt;/b&gt;: disk <mark>error</mark>");
  assert.equal(run(`logSearchMark("Error error", "error", false, true)`), "Error <mark>error</mark>", "match case");
  assert.equal(run(`logSearchMark("a.b axb", "a.b")`), "<mark>a.b</mark> axb", "plain text is literal");
  assert.equal(run(`logSearchMark("a.b axb", "a.b", true)`), "<mark>a.b</mark> <mark>axb</mark>");
  assert.equal(run(`logSearchMark("x<y", "(?P<n>x)", true)`), "x&lt;y", "an expression the browser cannot read leaves the line unmarked");
  assert.equal(run(`logSearchMark("abc", "x*", true)`), "abc", "an empty match does not loop");
});

test("a partial search says what was missed", () => {
  const gaps = run(`logSearchGaps({skipped_pods: 2, capped: ["frigate"], errors: ["a (a-1): refused"]})`);
  assert.equal(gaps.length, 3);
  assert.match(gaps[0], /2 more containers were not searched/);
  assert.match(gaps[1], /frigate wrote more than Homestead reads at once/);
  assert.match(gaps[2], /a \(a-1\): refused/);
  assert.equal(run(`logSearchGaps({skipped_pods: 0, capped: [], errors: []})`).length, 0);
});

test("results escape the app and line and open by row", () => {
  const html = run(`logSearchResults({matches: [{at: "", ns: "lab", app: "<x>", pod: "p", container: "<x>", line: "<script>err"}],
    total: 1, truncated: false, asked: 3, window: "1h", skipped_pods: 0, capped: [], errors: []})`);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /logSearchOpen\(0\)/);
  assert.match(html, /1 matching line\s+in 3 containers over the last hour/);
  const none = run(`logSearchResults({matches: [], total: 0, asked: 1, window: "15m", skipped_pods: 0, capped: [], errors: []})`);
  assert.match(none, /Nothing in the last 15 minutes matches/);
});

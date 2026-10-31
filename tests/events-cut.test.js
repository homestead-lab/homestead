// The Events page says when Kubernetes holds more events than it shows (#988).
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function load(total, extra = {}) {
  const ctx = { console, Date, Map, Set, Promise, document: { addEventListener() {} },
    STATE: { q: "", data: { eventTotal: total, ...extra } },
    esc: x => String(x), clusterAttr: () => "", fmtAgo: () => "5 h ago", localTime: at => at.toISOString() };
  ctx.window = ctx;
  vm.createContext(ctx);
  require("./helpers/load-time")(ctx);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), ctx);
  vm.runInContext(fs.readFileSync("web/js/cron.js", "utf8"), ctx);
  ctx.CRON = ctx.window.CRON || ctx.CRON;
  for (const file of ["web/js/views-protect.js", "web/js/views-storage.js"]) vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
  return ctx;
}

const rows = count => Array.from({ length: count }, (_, i) => ({ obj: `ev-${i}` }));

test("a cut list names how many events there were", () => {
  assert.equal(load(431).eventsCut(rows(120)), " · the newest 120 of 431");
});

test("a whole list, or one with no total, says nothing more", () => {
  assert.equal(load(3).eventsCut(rows(3)), "");
  assert.equal(load(undefined).eventsCut(rows(3)), "");
});

test("a read that stopped early says the rows are the newest of what it read", () => {
  assert.equal(load(20000, { eventLimited: true }).eventsCut(rows(120)), " · the newest 120 of the first 20000 read");
});

test("an events read that failed says so, never that there are no events", () => {
  const ctx = load(0, { eventRows: [], eventError: "Kubernetes did not answer the events read; refresh to try again" });
  const found = { "#evRows": { innerHTML: "" }, "#evCount": { textContent: "" }, "#evClear": {} };
  ctx.$ = selector => found[selector];
  ctx.eventsRender();
  assert.match(found["#evRows"].innerHTML, /Could not read the cluster's events: Kubernetes did not answer/);
  assert.doesNotMatch(found["#evRows"].innerHTML, /No events/);
  ctx.STATE.data.eventError = "";
  ctx.eventsRender();
  assert.match(found["#evRows"].innerHTML, /No events\./);
});

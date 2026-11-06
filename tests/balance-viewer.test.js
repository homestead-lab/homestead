"use strict";
// Balance hosts for a viewer (#1084): the plan shows read-only. Every switch
// and tick is drawn disabled, one line says who can change it, and the
// handlers refuse too. An operator's dialog is unchanged.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const CONTAINERS = { apps: ["lab/ha", "lab/tiny"], moves: [{ id: "lab/ha", from: "node-a", to: "node-b", cpu_m: 500, mem_gb: 1 }],
  hosts: [{ name: "node-a", takes: true, cpu_before: 80, cpu_after: 50, mem_before: 60, mem_after: 40 }], skipped: [], metrics: true, review_token: "1.a" };
const VOLUMES = { apps: ["lab/ha"], moves: [{ app: "lab/ha", claim: "ha-config", size_gb: 2, from: "node-a", to: "node-b", follows: true }],
  hosts: [{ name: "node-a", takes: true, before_gb: 10, after_gb: 8, capacity_gb: 100 }], skipped: [], review_token: "1.b" };
const LINE = "Operators can change and apply this plan.";
// The lines that tell a person to act, which a viewer can't (#1084 review).
const HINTS = [/Untick a container to leave it where it is/, /Untick an app to leave its volumes where they are/,
  /Starts what is below, as jobs you can follow and stop in Jobs\./];
const PLAIN = "The plan as Homestead suggests it.";

async function open(role) {
  const fields = {}, asked = [];
  let body = "";
  const field = id => fields[id] || (fields[id] = { innerHTML: "", value: "", checked: false, disabled: false });
  const rank = { viewer: 1, operator: 2, admin: 3 };
  const c = { console, URLSearchParams, Map, Set, Promise, setTimeout: f => f(), clearTimeout() {}, document: { addEventListener() {} },
    STATE: { data: {} }, $: s => field(s), $$: () => [], esc: v => String(v ?? "").replaceAll("<", "&lt;"),
    modal: (_, html) => { body = html; }, toast() {}, closeModal() {}, refresh() {}, fmtAgo: s => `${s}s ago`,
    can: need => rank[role] >= rank[need],
    api: async path => { asked.push(path); return path.startsWith("/api/workloads/") ? CONTAINERS : VOLUMES; } };
  c.window = c; vm.createContext(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  for (const f of ["web/js/ui.js", "web/js/views-workloads.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  await c.balanceHosts();
  const panes = () => ["#balContainers", "#balVolumes", "#balReview"].map(id => field(id).innerHTML).join("\n");
  return { c, asked, body: () => body, panes, balance: () => vm.runInContext("BALANCE", c) };
}

const inputs = html => html.match(/<input\b[^>]*>/g) || [];

test("a viewer sees every switch and tick in Balance hosts disabled, and one line saying who can change it", async () => {
  const t = await open("viewer");
  const all = inputs(t.panes());
  // Two switches, the volume follow switch, three ticks and the restart tick.
  assert.equal(all.length, 7, all.join("\n"));
  for (const input of all) {
    assert.match(input, /\sdisabled\b/, `not disabled for a viewer: ${input}`);
    assert.match(input, /aria-describedby="balReadOnly"/, `not described by the line: ${input}`);
  }
  assert.match(t.body(), /<p class="ui-help" id="balReadOnly">Operators can change and apply this plan\.<\/p>/);
  assert.ok(all.some(i => /id="balFollow"/.test(i)), "the follow switch is drawn");
  assert.equal(t.body().split(LINE).length - 1, 1, "the line shows once");
});

test("a viewer is not told to untick or start anything: the plan is described plainly", async () => {
  const t = await open("viewer");
  for (const hint of HINTS) assert.doesNotMatch(t.panes(), hint);
  assert.match(t.panes(), new RegExp(PLAIN.replace(".", "\\.")));
  assert.match(t.panes(), /So it reads its data locally there/, "the follow switch keeps its explanation");
});

test("a viewer's switch, tick or follow change is refused by its handler too", async () => {
  const t = await open("viewer");
  const before = t.asked.length;
  t.c.balanceToggle("containers", false);
  t.c.balanceToggle("volumes", false);
  await t.c.balanceItem("containers", "lab/ha", false);
  await t.c.balanceItem("volumes", "lab/ha", false);
  t.c.balanceFollow(false);
  const b = t.balance();
  assert.equal(b.containers.on, true);
  assert.equal(b.volumes.on, true);
  assert.equal(b.volumes.follow, true);
  assert.deepEqual([...b.containers.exclude, ...b.volumes.exclude], []);
  assert.equal(t.asked.length, before, "no plan is asked for again");
  assert.ok(b.containers.plan && b.volumes.plan, "the plans stay shown");
});

test("an operator's Balance hosts is unchanged: nothing disabled, no line, the handlers work", async () => {
  const t = await open("operator");
  const all = inputs(t.panes());
  assert.equal(all.length, 7);
  for (const input of all) assert.doesNotMatch(input, /\sdisabled\b|aria-describedby/, input);
  for (const hint of HINTS) assert.match(t.panes(), hint);
  assert.doesNotMatch(t.panes(), new RegExp(PLAIN.replace(".", "\\.")));
  assert.doesNotMatch(t.body(), new RegExp(LINE));
  t.c.balanceFollow(false);
  assert.equal(t.balance().volumes.follow, false);
  const before = t.asked.length;
  await t.c.balanceItem("containers", "lab/tiny", false);
  assert.deepEqual([...t.balance().containers.exclude], ["lab/tiny"]);
  assert.ok(t.asked.length > before, "the plan is worked out again");
  t.c.balanceToggle("volumes", false);
  assert.equal(t.balance().volumes.on, false);
});

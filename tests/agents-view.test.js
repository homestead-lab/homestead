"use strict";
// The AI agent requests page (#417): what waits for a person is read first,
// Approve is unavailable to someone who may not decide it, and approving asks
// before it runs.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const request = (id, over = {}) => ({ id, tool: "delete_container", title: "Delete a container", status: "pending", reason: "Clean up",
  created: 1000, expires: 4600, ended: null, decided_by: null, detail: "", role: "operator", escalated: false, key: "Claude Code",
  owner: "sam", addr: "192.0.2.31", action: "Delete the container lab/old-wiki",
  summary: { action: "Delete the container lab/old-wiki", replicas: 0, volumes_kept: ["old-wiki-data"] }, args: { name: "old-wiki" },
  can_decide: true, ...over });

function setup(requests) {
  const painted = [], toasts = [], modals = [], posts = [], fields = {};
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { console, esc, jsq: s => esc(JSON.stringify(String(s))), jsArg: s => JSON.stringify(String(s ?? "")), icon: name => `<i data-icon="${name}"></i>`, document: { getElementById: () => null },
    STATE: { data: {} }, location: { search: "" }, URLSearchParams, JSON, Date, Math, setTimeout: () => 0, clearTimeout() {},
    HomesteadRouter: { urlFor: v => "/" + v }, agoText: at => "ago " + at,
    paint: html => painted.push(html), toast: (m, k) => toasts.push([m, k]), modal: (t, b) => modals.push([t, b]), closeModal() {},
    $: s => fields[s] || (fields[s] = { disabled: false, checked: false }),
    api: async (path, init) => {
      if (init) { posts.push([path, JSON.parse(init.body)]); return { id: JSON.parse(init.body).id, status: "done", detail: "Deleted" }; }
      return { requests, pending: requests.filter(r => r.status === "pending").length };
    } };
  c.window = c; c.globalThis = c;
  vm.createContext(c);
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-agents.js", "utf8"), c);
  return { c, painted, toasts, modals, posts };
}

test("requests waiting for a person come before the answered ones", async () => {
  const t = setup([request("p1"), request("d1", { status: "done", detail: "Deleted it", ended: 2000, decided_by: "sam" })]);
  await t.c.viewAgents();
  const html = t.painted[0];
  assert.ok(html.indexOf("Waiting for approval") < html.indexOf("Recent requests"));
  assert.ok(html.indexOf('id="agent-req-p1"') > 0, "the waiting request is a card");
  assert.match(html, /1 waiting for a person/);
  assert.match(html, /Deleted it/, "an answered request keeps its outcome");
  assert.match(html, /Raw details/, "the raw plan is one fold away");
  assert.match(html, /Volumes kept/, "the plan is shown as readable facts");
});

test("Approve is unavailable, with the reason, when the role is too low", async () => {
  const t = setup([request("p1", { role: "admin", escalated: true, can_decide: false })]);
  await t.c.viewAgents();
  const html = t.painted[0];
  assert.match(html, /Needs the admin role/);
  assert.match(html, /<button[^>]*disabled[^>]*onclick="agentApprove\(/);
  assert.match(html, /admin only/);
});

test("a request named in the address opens marked, even when it is answered", async () => {
  const t = setup([request("p1"), request("d1", { status: "denied", detail: "denied by sam", ended: 2000 })]);
  t.c.location.search = "?request=d1";
  await t.c.viewAgents();
  assert.match(t.painted[0], /This request[\s\S]*agent-req target" id="agent-req-d1"/);
});

test("approving asks first, and runs only after the acknowledgement", async () => {
  const t = setup([request("p1")]);
  await t.c.viewAgents();
  t.c.agentApprove("p1");
  assert.equal(t.posts.length, 0, "opening the dialog changes nothing");
  assert.match(t.modals[0][1], /id="agentGo"[^>]*disabled|disabled[^>]*id="agentGo"/);
  assert.match(t.modals[0][1], /type="checkbox" id="agentOk"/);
  assert.match(t.modals[0][1], /onclick="agentApproveRun\(&quot;p1&quot;,this\)"/, "the id is escaped once, so the click runs");
  await t.c.agentApproveRun("p1", null);
  assert.deepEqual(t.posts[0], ["/api/agents/requests/approve", { id: "p1" }]);
  // The dialog closes at once and says it is running; the result comes when it has finished.
  assert.deepEqual(t.toasts[0], ["Running the request", "ok"]);
  assert.deepEqual(t.toasts.at(-1), ["Deleted", "ok"]);
});

test("an approval the server could not run is reported as a failure", async () => {
  const t = setup([request("p1")]);
  t.c.api = async () => ({ id: "p1", status: "failed", detail: "The cluster changed since you reviewed this" });
  await t.c.agentApproveRun("p1", null);
  assert.deepEqual(t.toasts.at(-1), ["The cluster changed since you reviewed this", "bad"]);
});

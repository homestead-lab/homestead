"use strict";

/* Small wording rules: counts read as words, device names and Kubernetes
   states read as people write them, and one action has one label. */
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const between = require("./helpers/between");

const core = require("./helpers/core-source")();
const ui = fs.readdirSync("web/js").filter(f => f.endsWith(".js") && f !== "demo.js")
  .map(f => [`web/js/${f}`, fs.readFileSync(`web/js/${f}`, "utf8")]);
const where = (pattern, files = ui) => files.flatMap(([file, source]) => source.split("\n")
  .map((line, i) => pattern.test(line) ? `${file}:${i + 1}` : null).filter(Boolean));

function coreHelpers() {
  const ctx = { String, Number };
  vm.createContext(ctx);
  const words = between(core, "const plural", "const stateWords") + between(core, "const stateWords", "\n\n");
  vm.runInContext(`${words}; this.plural = plural; this.stateWords = stateWords;`, ctx);
  return ctx;
}

test("plural() counts in words", () => {
  const { plural } = coreHelpers();
  assert.equal(plural(1, "pod"), "1 pod");
  assert.equal(plural(3, "pod"), "3 pods");
  assert.equal(plural(0, "replica"), "0 replicas");
  assert.equal(plural(2, "variable"), "2 variables");
});

test("no count is written as word(s)", () => {
  assert.deepEqual(where(/(\}|\d) ([A-Za-z]+ ){0,3}[A-Za-z]+\(s\)|"[a-z ]+\(s\)"/), []);
  assert.deepEqual(where(/\} vars`/), [], "the Edit dialog counts variables with plural()");
});

test("Kubernetes states read as words", () => {
  const { stateWords } = coreHelpers();
  assert.equal(stateWords("NotReady"), "Not ready");
  assert.equal(stateWords("ErrorUnschedulable"), "Can't be scheduled");
  assert.equal(stateWords("Ready"), "Ready");
  assert.equal(stateWords("CrashLoopBackOff"), "Crash loop back off");
  assert.equal(stateWords(""), "");
});

test("Nodes and the dashboard word node and VM states", () => {
  const overview = ui.filter(([file]) => /views-overview|insights/.test(file));
  assert.deepEqual(where(/>\$\{esc\(n\.status\)\}<|\?n\.status \|\| "Not ready"|vm\?r\.status \|\| "Unknown"/, overview), []);
});

test("drive names are shown as the system reports them", () => {
  assert.deepEqual(where(/d\.device[^;]*\.toUpperCase\(\)/), []);
});

test("an AI agent request labels its fields as people write them", () => {
  const source = fs.readFileSync("web/js/views-agents.js", "utf8");
  const ctx = { String, JSON, Array, esc: s => String(s ?? "") };
  vm.createContext(ctx);
  const labels = between(source, "const AGENT_LABELS", "const AGENT_STATUS");
  const value = between(source, "function agentValue", "/* The plan to review");
  vm.runInContext(`${labels}${value}; this.agentLabel = agentLabel; this.agentValue = agentValue;`, ctx);
  assert.equal(ctx.agentLabel("uid"), "UID");
  assert.equal(ctx.agentLabel("load_balancer_ip"), "Load balancer IP");
  assert.equal(ctx.agentLabel("why_admin"), "Admin because");
  assert.equal(ctx.agentLabel("replicas"), "Replicas");
  assert.match(ctx.agentValue("2f6c-11aa", "uid"), /class="mono/);
  assert.match(ctx.agentValue("192.0.2.10", "load_balancer_ip"), /class="mono/);
  assert.doesNotMatch(ctx.agentValue("Restarting after a crash", "why_admin"), /mono/);
});

test("Settings › Updates says Check now everywhere", () => {
  assert.deepEqual(where(/↻ Check now|"Check every host now"|\}Check<\/button>/), []);
});

test("toasts start with a capital letter", () => {
  assert.deepEqual(where(/toast\(["'`](?!vBIOS)[a-z]/), []);
});

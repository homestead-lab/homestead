"use strict";
// Deploy and Edit draw one app form (#544): the same sections in the same
// order, and the same container block, from web/js/app-form.js.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const between = require("./helpers/between");

const context = { window: {}, console, document: { addEventListener() {} },
  esc: value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  icon: name => `<svg data-icon="${name}"></svg>` };
vm.createContext(context);
context.jsArg = s => JSON.stringify(String(s ?? "")); context.jsq = s => context.esc(context.jsArg(s));
vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), context);
context.UI = context.window.UI;
vm.runInContext(fs.readFileSync("web/js/app-form.js", "utf8"), context);
const run = code => vm.runInContext(code, context);
// Values from the vm are another realm's arrays: compare plain copies.
const plain = value => JSON.parse(JSON.stringify(value));

const KEYS = ["basics", "hardware", "environment", "storage", "placement", "address", "monitoring"];
const TITLES = ["Basics", "Hardware and access", "Environment values", "Storage", "Placement", "Address", "Monitoring"];
const html = Object.fromEntries([...KEYS, "summary"].map(key => [key, `<p>${key}</p>`]));

test("one section list, in one order", () => {
  const sections = run("APP_FORM_SECTIONS");
  assert.deepEqual(plain(sections.map(s => s.key)), KEYS);
  assert.deepEqual(plain(sections.map(s => s.title)), TITLES);
  assert.equal(Object.isFrozen(sections), true);
  assert.deepEqual(plain(run("APP_FORM_CONTAINER_SECTIONS")), ["basics", "hardware", "environment", "storage", "address"]);
});

test("deploy adds Summary last; edit does not", () => {
  const deploy = context.appFormSections("deploy", html);
  assert.deepEqual(plain(deploy.map(s => s.key)), [...KEYS, "summary"]);
  assert.equal(deploy.at(-1).title, "Summary");
  assert.equal(deploy.at(-1).html, "<p>summary</p>");
  const edit = context.appFormSections("edit", html);
  assert.equal(edit.length, 7);
  assert.deepEqual(plain(edit.map(s => s.html)), KEYS.map(key => `<p>${key}</p>`));
});

test("a section's lead comes from the caller", () => {
  const sections = context.appFormSections("deploy", html, { leads: { basics: "Name the app." } });
  assert.equal(sections[0].lead, "Name the app.");
  assert.equal(sections[1].lead, undefined);
});

test("a later section says when", () => {
  const sections = context.appFormSections("deploy", html, { later: { placement: "Set after deploying" } });
  assert.deepEqual(plain(sections.filter(s => s.later).map(s => [s.key, s.later])), [["placement", "Set after deploying"]]);
});

test("a missing section is an error, not a blank pane", () => {
  const { address, ...rest } = html;
  assert.throws(() => context.appFormSections("edit", rest), /no html for address/);
});

test("the container block is the same for both forms", () => {
  const edit = context.appFormContainerBlock("e", "edit", "P");
  assert.match(edit, /id="e_basics_containers">P</);
  assert.match(edit, /onclick="containerAdd\(&quot;edit&quot;\)"|onclick="containerAdd\('edit'\)"/);
  assert.match(edit, /id="e_removed_containers"/);
  assert.match(edit, /under Placement/);
  assert.match(edit, /Containers in each pod/);
  const deploy = context.appFormContainerBlock("d", "deploy", "");
  assert.match(deploy, /id="d_basics_containers"/);
  assert.match(deploy, /containerAdd\((&quot;|')deploy(&quot;|')\)/);
  assert.match(deploy, /Set pod copies under Placement/);
});

test("a container gets one panel in each section that holds containers", () => {
  const hosts = {}, calls = [];
  context.document.getElementById = id => (hosts[id] ||= { id, html: "", insertAdjacentHTML(where, h) { this.html += h; } });
  context.editContainerPanel = (container, index, section, mode) => { calls.push([container.name, index, section, mode]); return `[${section}]`; };
  context.appFormAddPanels("d", { name: "helper" }, 7, "deploy");
  assert.deepEqual(calls.map(c => c[2]), ["basics", "hardware", "environment", "storage", "address"]);
  assert.ok(calls.every(c => c[0] === "helper" && c[1] === 7 && c[3] === "deploy"));
  assert.equal(hosts.d_storage_containers.html, "[storage]");
  assert.equal(hosts.d_placement_containers, undefined, "placement holds no container panel");
});

test("Edit builds its sections from the shared list", () => {
  const text = fs.readFileSync("web/js/views-lifecycle.js", "utf8");
  const edit = between(text, "window.wlEdit = async", "\n};\n");
  assert.match(edit, /appFormSections\("edit"/);
  assert.match(edit, /appFormContainerBlock\("e"/);
  assert.doesNotMatch(edit, /title: "Placement"/);
});

test("containerAdd uses the shared panels", () => {
  const text = fs.readFileSync("web/js/views-lifecycle.js", "utf8");
  assert.match(between(text, "window.containerAdd =", "\n};"), /appFormAddPanels\(/);
});

test("the page loads app-form.js after ui.js and before the views that use it", () => {
  const page = fs.readFileSync("web/index.html", "utf8");
  const at = name => page.indexOf(`/js/${name}?v=`);
  assert.ok(at("app-form.js") > at("ui.js"), "after ui.js");
  assert.ok(at("app-form.js") < at("views-workloads.js") && at("app-form.js") < at("views-lifecycle.js"), "before the views");
});

test("Deploy builds its sections from the shared list", () => {
  const workloads = fs.readFileSync("web/js/views-workloads.js", "utf8");
  const deploy = between(workloads, "async function viewDeploy", "\n}\n");
  assert.match(deploy, /appFormSections\("deploy"/);
  assert.doesNotMatch(deploy, /later:/, "Placement is live in Deploy (#844)");
  assert.doesNotMatch(deploy, /later:\s*\{[^}]*monitoring/, "Monitoring stays settable in Deploy");
  assert.match(deploy, /appFormContainerBlock\("d"/);
  assert.doesNotMatch(deploy, /key:\s*"containers"/);
  assert.doesNotMatch(workloads, /d_extra_containers|d_extra_wrap|d_extra_join/);
  assert.match(between(workloads, "function renderDeployContainers", "\n}\n"), /appFormAddPanels\("d"/);
});

test("Review deployment names no field when another container's panel is not found", async () => {
  const asked = [], notices = [];
  const c = { window: {}, console, URLSearchParams, Map, document: { addEventListener() {}, querySelector: s => { asked.push(s); return null; } },
    logoReady: async () => {}, $: () => null, $$: () => [], toast: m => notices.push(m), volumeListIssue: () => "",
    esc: s => String(s), api: async () => { throw new Error("no review expected"); } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/views-workloads.js", "utf8"), c);
  c.collect = () => ({ name: "a", workload_name: "a", container_name: "a", image: "a:1", target_mode: "new", volumes: [],
    additional_containers: [{ name: "b", image: "" }] });
  await c.window.doDeploy();
  assert.deepEqual(notices, ["Every container needs a name and image."]);
  assert.ok(asked.every(s => !/undefined/.test(String(s))), `asked for ${asked.join(", ")}`);
});

test("both forms say which containers share the pod in the same words", () => {
  const host = { innerHTML: "" };
  context.$ = sel => sel === "#d_pod_containers" ? host : null;
  context.UI.chip = (text) => `[${text}]`;
  context.appFormPodContainers("d", ["web", ""]);
  assert.equal(host.innerHTML, "[web] [unnamed] run together in every pod, sharing its network and volumes.");
  const lifecycle = fs.readFileSync("web/js/views-lifecycle.js", "utf8");
  assert.match(between(lifecycle, "function editContainerListChanged", "\n}\n"), /appFormPodContainers\("e",/);
});

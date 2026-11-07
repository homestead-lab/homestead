"use strict";
// #920: Deploy's review offers the app as YAML for git, with Copy and Download,
// in place of the raw JSON manifest preview.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const workloads = () => require("./helpers/workloads-source")();

const capacity = { additional: 1, requires_confirmation: false, blocked: false, pod_request_gb: 1,
  pod_cpu_request_percent: 10, pod_memory_gb: 1, warnings: [], candidates: [] };
const NEW_YAML = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: demo\n---\napiVersion: v1\nkind: PersistentVolumeClaim\n";

function setup(preview) {
  let html = "";
  const notices = [], saved = [], fields = { "#deployGo": {}, "#deployCapacityConfirm": { checked: false }, "#deployConfirm": { checked: false } };
  const config = { name: "demo", container_name: "demo", workload_name: "demo", target_mode: "new", image: "demo:1", ports: [], volumes: [] };
  const c = { window: {}, console, URLSearchParams, Map, setTimeout() {},
    document: { addEventListener() {}, body: { appendChild() {} },
      createElement: () => { const link = { click() { saved.push({ name: link.download, href: link.href }); }, remove() {} }; return link; } },
    URL: { createObjectURL: blob => `blob:${saved.length}:${blob.text}`, revokeObjectURL() {} },
    Blob: class { constructor(parts, options) { this.text = parts.join(""); this.type = options?.type; } },
    logoReady: async () => {}, volumeListIssue: () => "", closeModal() {}, go() {},
    $: s => fields[s], esc: s => String(s).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
    api: async path => { if (path === "/api/preview") return { capacity, capacity_token: "t", impact: {}, ...preview }; return { ok: true }; },
    modal: (_, body) => { html = body; }, toast: m => notices.push(m),
    copied: [], copyText: async text => { c.copied.push(text); return true; } };
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => (c.esc || String)(c.jsArg(s));
  for (const file of ["ui.js", "gitops.js"]) vm.runInContext(fs.readFileSync(`web/js/${file}`, "utf8"), c);
  vm.runInContext(workloads(), c);
  c.collect = () => ({ ...config, ...(preview.config || {}) });
  return { c, html: () => html, saved, notices };
}

const onclick = (html, call) => (html.match(new RegExp(`onclick="(${call}\\([^"]*\\))"`)) || [])[1];

test("The deploy form has no raw manifest preview any more", () => {
  const deploy = workloads();
  assert.doesNotMatch(deploy, /Preview manifest|previewYaml|Manifest preview/);
});

test("A new app's review shows the whole app as YAML for git, with Copy and Download as <app>.yaml", async () => {
  const t = setup({ yaml: NEW_YAML, yaml_file: "demo.yaml", secret: { name: "demo-env", keys: ["demo.DB_PASSWORD"] } });
  await t.c.window.doDeploy();
  const html = t.html();
  assert.match(html, /YAML for git/);
  assert.match(html, /kind: PersistentVolumeClaim/);
  assert.match(html, /The whole app in one file: its Deployment, Service and new volume claims/);
  assert.doesNotMatch(html, /Services and volumes are not in it/);
  assert.match(html, /Secret <span class="mono">demo-env<\/span> \(keys <span class="mono">demo\.DB_PASSWORD<\/span>\)/);
  assert.doesNotMatch(html, /JSON\.stringify|&quot;deployment&quot;/);
  vm.runInContext(`window.${onclick(html, "gitopsCopy")}`, t.c);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(t.c.copied, [NEW_YAML]);
  assert.match(html, /Download demo\.yaml/);
  vm.runInContext(`window.${onclick(html, "gitopsDownload")}`, t.c);
  assert.equal(t.saved.length, 1);
  assert.equal(t.saved[0].name, "demo.yaml");
  assert.match(t.saved[0].href, /kind: PersistentVolumeClaim/);
});

test("Adding a container gives the patch, downloaded as <app>-patch.yaml", async () => {
  const t = setup({ yaml: "kind: Deployment\nspec: {}\n", yaml_file: "web-patch.yaml", secret: {},
    config: { target_mode: "existing", target_workload: "web", container_name: "sidecar" } });
  await t.c.window.doDeploy();
  const html = t.html();
  assert.match(html, /Only what adding this container changes/);
  assert.match(html, /Download web-patch\.yaml/);
  vm.runInContext(`window.${onclick(html, "gitopsDownload")}`, t.c);
  assert.equal(t.saved[0].name, "web-patch.yaml");
});

test("The edit review's YAML still says it is only the change", () => {
  const t = setup({});
  const html = t.c.gitopsYamlBlock("kind: Deployment\n");
  assert.match(html, /Only the fields this change sets\. Services and volumes are not in it\./);
  assert.doesNotMatch(html, /gitopsDownload/);
});

// What Homestead adds when it deploys that the file cannot hold, named only when it applies.
const reviewWith = async adds => {
  const t = setup({ yaml: NEW_YAML, yaml_file: "demo.yaml", secret: {}, ...(adds ? { deploy_adds: adds } : {}) });
  await t.c.window.doDeploy();
  return t.html();
};

test("An app with a LAN address and a named user's volume lists both deploy-time additions", async () => {
  const html = await reviewWith([{ what: "a volume-owner init container", certain: true }, { what: "a LAN network attachment", certain: true }]);
  assert.match(html, /Homestead also adds at deploy time: a volume-owner init container; a LAN network attachment\. Applying this file elsewhere won't add them\./);
});

test("An owner only the image could name is a may", async () => {
  const html = await reviewWith([{ what: "a volume-owner init container", certain: false }]);
  assert.match(html, /At deploy time Homestead may add a volume-owner init container\. Applying this file elsewhere won't add it\./);
});

test("A LAN app whose owner is unknown says the attachment and the may", async () => {
  const html = await reviewWith([{ what: "a volume-owner init container", certain: false }, { what: "a LAN network attachment", certain: true }]);
  assert.match(html, /Homestead also adds at deploy time: a LAN network attachment, and may add a volume-owner init container\. Applying this file elsewhere won't add them\./);
});

test("An app with neither has no deploy-time line", async () => {
  for (const adds of [[], undefined]) assert.doesNotMatch(await reviewWith(adds), /at deploy time|At deploy time/);
});

// The demo answers as the server does (#920 review): a non-root user the deploy names is
// a sure init container, root none, an owner only the image would name a "may".
test("The demo's deploy-time additions follow run_as_user as the server's do", () => {
  const source = require("./helpers/between")(fs.readFileSync("web/js/demo.js", "utf8"), "function demoDeployYaml(", "function demoPorts(");
  const demo = vm.runInNewContext(`(${source.trim()})`);
  const volume = [{ type: "pvc", create: true, path: "/config", source: "demo-config" }];
  const plain = value => JSON.parse(JSON.stringify(value));      // out of the vm's own realm
  const adds = extra => plain(demo({ namespace: "lab", image: "demo:1", volumes: volume, ...extra }, "demo", "demo", false).deploy_adds);
  assert.deepEqual(adds({ run_as_user: 1000 }), [{ what: "a volume-owner init container", certain: true }]);
  assert.deepEqual(adds({ run_as_user: 0 }), []);
  assert.deepEqual(adds({}), [{ what: "a volume-owner init container", certain: false }]);
  assert.deepEqual(adds({ privileged: true }), []);
  assert.deepEqual(plain(demo({ namespace: "lab", volumes: [] }, "demo", "demo", false).deploy_adds), []);
});

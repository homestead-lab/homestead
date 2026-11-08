"use strict";
// Add a host: the role chosen decides the command shown.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

const GUIDE = { distribution: "k3s", server: "192.0.2.5", version: "1.31.4+k3s1", token_file: "/var/lib/rancher/k3s/server/node-token",
  agent: "AGENT-CMD", server_join: "SERVER-CMD", tiebreaker: "TIEBREAKER-CMD", settings_known: true,
  shared_flags: ["--cluster-cidr", "--default-local-storage-path"], longhorn: "LONGHORN-CMD" };

function setup(guide = GUIDE) {
  const fields = { "#mbody": { innerHTML: "" } };
  const ctx = { console, Promise, document: { addEventListener() {} }, STATE: { data: {} },
    $: key => fields[key] || (fields[key] = { innerHTML: "" }), $$: () => [],
    esc: value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    modal: () => {}, guideCopy: cmd => `<pre>${cmd}</pre>`, api: async () => guide,
    UI: { button: (label, onclick, { kind = "" } = {}) => `<button class="btn ${kind}" onclick="${onclick}">${label}</button>` } };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync("web/js/platform.js", "utf8"), ctx);
  return { ctx, fields };
}

test("the guide opens on an app host when no role is asked for", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  assert.match(t.fields["#mbody"].innerHTML, /value="app" checked/);
  assert.match(t.fields["#mbody"].innerHTML, /AGENT-CMD/);
});

test("a role asked for is chosen and its command shown", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide("tiebreaker");
  assert.match(t.fields["#mbody"].innerHTML, /value="tiebreaker" checked/);
  assert.match(t.fields["#mbody"].innerHTML, /TIEBREAKER-CMD/);
  assert.doesNotMatch(t.fields["#mbody"].innerHTML, /AGENT-CMD/);
});

test("choosing another role redraws the command", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  t.ctx.platformJoinRole("control-plane");
  assert.match(t.fields["#joinCmd"].innerHTML, /SERVER-CMD/);
});

test("without the servers' settings the guide names the flags to copy", () => {
  const html = setup().ctx.joinCommandHtml({ ...GUIDE, settings_known: false }, "tiebreaker");
  assert.match(html, /--default-local-storage-path/);
  assert.match(html, /copy/i);
});

test("the roles use the shared answer-mode choice, not unstyled markup", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  assert.match(t.fields["#mbody"].innerHTML, /<div class="answer-modes">[^]*<label class="answer-mode"><input type="radio" name="joinRole" value="app" checked/);
});

test("on RKE2 a server or tiebreaker joins through the installer, never as an agent", () => {
  const rke2 = { distribution: "rke2", agent: "RKE2-AGENT-CMD", config: "server: x", check: "CHECK-CMD" };
  const html = setup().ctx.joinCommandHtml(rke2, "tiebreaker");
  assert.doesNotMatch(html, /RKE2-AGENT-CMD/);
  assert.match(html, /HS_ROLE=tiebreaker/);
  assert.match(setup().ctx.joinCommandHtml(rke2, "control-plane"), /HS_ROLE=server/);
  assert.match(setup().ctx.joinCommandHtml(rke2, "app"), /RKE2-AGENT-CMD/);
});

test("the installer command carries the verified storage setting, except for a tiebreaker", () => {
  const g = { ...GUIDE, distribution: "rke2", storage_path: "/var/lib/longhorn", storage_supported: true,
    installer_url: "https://example.com/pinned/install.sh" };
  const ctx = setup().ctx;
  assert.match(ctx.joinCommandHtml(g, "control-plane"), /pinned\/install.sh[^]*HS_DIST=rke2 HS_ROLE=server HS_LONGHORN_DATA_PATH=\/var\/lib\/longhorn/);
  assert.doesNotMatch(ctx.joinCommandHtml(g, "tiebreaker"), /HS_LONGHORN_DATA_PATH/);
  assert.doesNotMatch(ctx.joinCommandHtml({ ...g, storage_path: "/custom/$(id)" }, "control-plane"), /HS_LONGHORN_DATA_PATH/);
  assert.match(ctx.joinLonghornHtml(g, "app"), /give it exactly this[^]*\/var\/lib\/longhorn/);
  assert.match(ctx.joinLonghornHtml({ ...g, storage_path: null }, "app"), /could not be read[^]*don.t guess/);
});

test("a server or tiebreaker is checked before it joins", () => {
  const html = setup().ctx.joinCommandHtml({ ...GUIDE, check: "CHECK-CMD" }, "tiebreaker");
  assert.match(html, /CHECK-CMD[^]*TIEBREAKER-CMD/);
  assert.doesNotMatch(setup().ctx.joinCommandHtml({ ...GUIDE, check: "CHECK-CMD" }, "app"), /CHECK-CMD/);
});

// #833: the role comes before the Longhorn step, and the step follows the role.
const headings = html => [...html.matchAll(/<h4><span>(\d)<\/span>([^<]*)<\/h4>/g)].map(m => `${m[1]} ${m[2]}`);

test("the role is chosen before the Longhorn step, and the join command comes after it", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  assert.deepEqual(headings(t.fields["#mbody"].innerHTML), ["1 Read the join token", "2 Choose what it does",
    "3 Install Longhorn's host packages", "4 Join it", "5 Watch it join"]);
});

test("an app host is told to install Longhorn's packages, why, and where to look if it sticks", async () => {
  for (const role of ["app", "control-plane"]) {
    const t = setup();
    await t.ctx.platformJoinGuide(role);
    const step = t.fields["#mbody"].innerHTML.split("<h4><span>3</span>")[1].split("<h4><span>4</span>")[0];
    assert.match(step, /LONGHORN-CMD/);
    assert.match(step, /Every host that runs apps needs these/);
    assert.match(step, /Longhorn.s pods keep restarting\? See the wiki/);
    assert.match(step, /<a class="linkish" href="https:\/\/github\.com\/homestead-lab\/homestead\/wiki\/Installing-on-k3s#if-something-is-stuck"/);
  }
});

test("on RKE2 the Longhorn step links the RKE2 page", () => {
  const html = setup().ctx.joinLonghornHtml({ distribution: "rke2", longhorn: "LONGHORN-CMD" }, "app");
  assert.match(html, /wiki\/Installing-on-RKE2#if-something-is-stuck/);
  assert.match(html, /LONGHORN-CMD/);
});

test("a tiebreaker is told it needs no Longhorn packages, straight away", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide("tiebreaker");
  const html = t.fields["#mbody"].innerHTML;
  assert.match(html, /Not needed: a tiebreaker runs no apps and holds no storage\./);
  assert.doesNotMatch(html, /LONGHORN-CMD/);
});

test("changing the role redraws the Longhorn step as well as the command", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  t.ctx.platformJoinRole("tiebreaker");
  assert.match(t.fields["#joinLonghorn"].innerHTML, /Not needed/);
  assert.doesNotMatch(t.fields["#joinLonghorn"].innerHTML, /LONGHORN-CMD/);
  assert.match(t.fields["#joinCmd"].innerHTML, /TIEBREAKER-CMD/);
  t.ctx.platformJoinRole("control-plane");
  assert.match(t.fields["#joinLonghorn"].innerHTML, /LONGHORN-CMD/);
});

// Found while testing #833: the role radios choose what the guide shows, they
// are not an edit, so closing it after a role change asks nothing.
test("dismissing the guide after a role change does not ask about unsaved changes", async () => {
  const t = setup();
  t.ctx.modal = () => { t.ctx.__modalGuard = null; };   // as core.js modal() does
  await t.ctx.platformJoinGuide();
  t.ctx.platformJoinRole("tiebreaker");
  assert.equal(typeof t.ctx.__modalGuard, "function");
  assert.equal(t.ctx.__modalGuard(), "");
});

test("the last step opens the Hosts page, as the Harvester guide's View hosts does", async () => {
  const t = setup();
  await t.ctx.platformJoinGuide();
  const step = t.fields["#mbody"].innerHTML.split("<h4><span>5</span>")[1];
  assert.match(step, /<button class="btn pri" onclick="closeModal\(\);go\('nodes'\)">View hosts<\/button>/);
});

test("the wiki section the Longhorn step links to exists on both pages", () => {
  for (const page of ["Installing-on-k3s", "Installing-on-RKE2"]) {
    assert.match(fs.readFileSync(`docs/wiki/${page}.md`, "utf8"), /^## If something is stuck$/m, `${page} has "If something is stuck"`);
  }
});

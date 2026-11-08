"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");

/* The OS updates dialog's rows and its review (#1174): real ui.js and
   host-os.js over a stand-in for the page, answering as the server does. */
function setup({ report = {}, previews = {}, role = "admin", start = null } = {}) {
  const sent = [], toasts = [], steps = [], reads = [], timers = [];
  const fields = { "#mbody": { innerHTML: "" }, "#osu_status": { innerHTML: "" }, "#osu_hosts": { innerHTML: "" }, "#osu_run": { innerHTML: "" },
    "#settingsHostUpdates": { innerHTML: "" }, "#osu_picked": { hidden: true, textContent: "" } };
  const picks = [];
  const c = { console, URLSearchParams, Map, Intl, Date, Math,
    document: { addEventListener() {}, getElementById: id => fields["#" + id] || null },
    $: s => fields[s] || null, $$: s => s === "input[data-osu-pick]" ? picks : (fields[s] ? [fields[s]] : []),
    esc: s => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"),
    can: need => need !== "admin" || role === "admin",
    icon: () => "", tip: () => "", fmtAgo: s => `${Math.round(s)}s ago`, fmtUp: s => `${Math.round(s)}s`, sizeText: x => `${x}`,
    modal: (title, body) => { steps.push(`modal ${title}`); fields["#mbody"].innerHTML = body; },
    childModal: (title, body) => {
      steps.push(`child ${title}`); fields["#mbody"].innerHTML = body;
      if (/osuReviewLoading/.test(body)) fields["#osuReviewLoading"] = {};
    },
    modalBack: () => steps.push("back"), closeModal: () => steps.push("close"), toast: (m, kind) => toasts.push([kind, m]),
    ask: async () => true, setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    noteOperation: op => steps.push(`job ${op.id}`),
    api: async (path, options) => {
      reads.push(path);
      if (path.startsWith("/api/os-updates/preview")) {
        const names = new URLSearchParams(path.split("?")[1]).getAll("node");
        const answer = typeof previews === "function" ? previews(names) : previews;
        if (answer instanceof Error) throw answer;
        return answer;
      }
      if (path === "/api/os-updates/start") {
        sent.push(JSON.parse(options.body));
        const reply = start && start(JSON.parse(options.body));
        if (reply instanceof Error) throw reply;
        return reply || { ok: true, detail: "Updating", operation: { id: "job-1" } };
      }
      if (path === "/api/os-updates") return typeof report === "function" ? report() : report;
      throw new Error("unexpected " + path);
    } };
  c.window = c;
  vm.createContext(c); require("./helpers/core-text")(c);
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => c.esc(c.jsArg(s));
  vm.runInContext(fs.readFileSync("web/js/ui.js", "utf8"), c);
  vm.runInContext(fs.readFileSync("web/js/host-os.js", "utf8"), c);
  return { c, fields, sent, toasts, steps, reads, picks, timers, html: () => fields["#mbody"].innerHTML };
}

const pkg = (name, security = false) => ({ name, security });
const HOSTS = {
  "node-a": { os: "Ubuntu", updates: [pkg("linux-image-6.8", true), pkg("curl")], security: 1, reboot: false, auto: { tool: "unattended-upgrades", on: true } },
  "node-b": { os: "Ubuntu", updates: [], security: 0, reboot: true, auto: {} },
  "node-c": { os: "Ubuntu", updates: [], security: 0, reboot: false, auto: {} },
  "node-d": { os: "Ubuntu", updates: [pkg("vim")], security: 0, reboot: false, auto: {}, upgrading: true },
};
const report = (extra = {}) => ({ applies: true, every_s: 21600, checking: {}, hosts: HOSTS, last: null, rollout: null,
  settings: { schedule: { enabled: false, days: ["sun"], hour: 3 }, reboot: "when-needed", single_copy: false, manage: "ubuntu" }, ...extra });
const running = { id: "os-1", status: "running", nodes: ["node-a", "node-c"], index: 0, node: { name: "node-a" }, message: "node-a: installing", results: [] };
const previewHost = (node, extra = {}) => ({ node, ready: true, updates: [pkg("curl")], security: 0, reboot: false, reboot_for: "", at: 1,
  homestead_here: false, restart_refusal: "", restart_unread: "", busy: "", ...extra });
const preview = (hosts, extra = {}) => ({ hosts, restart_policy: "when-needed", single_copy: false, blocked: "", review_token: "tok", ...extra });
// The cell of one host's row in the painted table.
const row = (html, name) => (html.split("<tr>").find(tr => tr.includes(`<b>${name}</b>`)) || "").split("</tr>")[0];
const button = (html, id) => (html.match(new RegExp(`<button[^>]*id="${id}"[^>]*>`)) || [""])[0];

async function opened(options = {}) {
  const t = setup({ report: report(options.state), ...options });
  await t.c.window.osUpdates();
  return t;
}
async function reviewed(answer, options = {}) {
  const t = setup({ report: report(), previews: answer, ...options });
  await t.c.window.osUpdatesReview(answer.hosts ? answer.hosts.map(h => h.node) : ["node-a"]);
  return t;
}
function reviewGo(t) {
  t.fields["#osu_review_go"] = { disabled: false, textContent: "" };
  t.fields["#osu_review_restart"] = { value: t.restart || "when-needed" };
  return t.c.window.osUpdatesReviewedApply();
}

test("each row offers what it can: Update…, Restart… or nothing", async () => {
  const t = await opened();
  const a = row(t.html(), "node-a"), b = row(t.html(), "node-b"), c = row(t.html(), "node-c");
  assert.match(a, />Update…</);
  assert.match(a, /osUpdatesReview\(\[&quot;node-a&quot;\]\)/);
  assert.match(b, />Restart…</);
  assert.match(b, /nodePowerReview\(&quot;node-b&quot;,&quot;reboot&quot;\)/, "the Host actions review");
  assert.doesNotMatch(c, /class="btn/, "nothing to do");
  assert.doesNotMatch(row(t.html(), "node-d"), /class="btn/, "already installing");
  assert.match(row(t.html(), "node-d"), /installing now/);
});

test("a host's name links to its page", async () => {
  const t = await opened();
  assert.match(row(t.html(), "node-c"), /<button type="button" class="linkish"[^>]*closeModal\(\);nodeDetail\(&quot;node-c&quot;\)/);
});

test("only hosts with updates waiting can be ticked", async () => {
  const t = await opened();
  assert.match(row(t.html(), "node-a"), /<input[^>]*data-osu-pick="node-a"/);
  for (const name of ["node-b", "node-c", "node-d"]) assert.doesNotMatch(row(t.html(), name), /data-osu-pick/, name);
});

test("a viewer's controls are the ones the role hides; the table and links stay", async () => {
  const t = await opened({ role: "viewer" });
  const a = row(t.html(), "node-a");
  assert.match(a, /<label[^>]*data-need="admin"[^>]*><input[^>]*data-osu-pick/);
  assert.match(a, /<button[^>]*data-need="admin"[^>]*>Update…/);
  assert.match(row(t.html(), "node-b"), /<button[^>]*data-need="admin"[^>]*>Restart…/);
  assert.match(button(t.html(), "osu_picked"), /data-need="admin"/);
  assert.match(a, /nodeDetail/);
  assert.doesNotMatch(a.match(/<button type="button" class="linkish"[^>]*>/)[0], /data-need/);
});

test("during a run no row has a button or a tick box: updating now, next", async () => {
  const t = await opened({ state: { rollout: running } });
  const html = t.html();
  assert.doesNotMatch(html.split("</table>")[0], /data-osu-pick|class="btn/);
  assert.match(row(html, "node-a"), /updating now/);
  assert.match(row(html, "node-c"), /next/);
  assert.doesNotMatch(row(html, "node-b"), /updating now|next</);
  assert.doesNotMatch(html, /id="osu_picked"/);
});

test("a host already done in this run says nothing; one left out when stopping is not next", async () => {
  const later = { ...running, index: 1, node: { name: "node-c" }, nodes: ["node-a", "node-c", "node-b"] };
  const t = await opened({ state: { rollout: later } });
  assert.doesNotMatch(row(t.html(), "node-a"), /updating now|next</);
  assert.match(row(t.html(), "node-c"), /updating now/);
  assert.match(row(t.html(), "node-b"), /next</);
  const stopping = await opened({ state: { rollout: { ...later, stop: true } } });
  assert.doesNotMatch(row(stopping.html(), "node-b"), /next</);
});

test("a run keeps the rows up to date as it follows", async () => {
  let state = report({ rollout: running });
  const t = setup({ report: () => state });
  await t.c.window.osUpdates();
  state = report({ rollout: { ...running, index: 1, node: { name: "node-c" } } });
  await t.timers.pop()();
  assert.match(row(t.fields["#osu_hosts"].innerHTML, "node-c"), /updating now/);
  state = report({ rollout: { ...running, status: "succeeded", index: 2, results: [] } });
  await t.timers.pop()();
  assert.match(row(t.fields["#osu_hosts"].innerHTML, "node-b"), />Restart…</, "the buttons are back");
});

test("the Settings card's table stays a table", async () => {
  const t = setup({ report: report() });
  await t.c.window.osUpdatesCardPaint();
  const html = t.fields["#settingsHostUpdates"].innerHTML;
  assert.match(html, /<b>node-a<\/b>/, "painted, not its error");
  assert.doesNotMatch(html, /data-osu-pick|osUpdatesReview|nodePowerReview|nodeDetail/);
});

test("ticking a host shows the button with how many", async () => {
  const t = await opened();
  assert.match(button(t.html(), "osu_picked"), /\bhidden\b/, "nothing ticked");
  t.picks.push({ checked: true, dataset: { osuPick: "node-a" } }, { checked: false, dataset: { osuPick: "node-d" } });
  t.c.window.osUpdatesPicked();
  assert.equal(t.fields["#osu_picked"].hidden, false);
  assert.equal(t.fields["#osu_picked"].textContent, "Update 1 selected host…");
  t.picks[1].checked = true;
  t.c.window.osUpdatesPicked();
  assert.equal(t.fields["#osu_picked"].textContent, "Update 2 selected hosts…");
  t.picks[0].checked = t.picks[1].checked = false;
  t.c.window.osUpdatesPicked();
  assert.equal(t.fields["#osu_picked"].hidden, true);
});

test("the ticked hosts go to one review", async () => {
  const t = await opened({ previews: names => preview(names.map(n => previewHost(n))) });
  t.picks.push({ checked: true, dataset: { osuPick: "node-a" } }, { checked: false, dataset: { osuPick: "node-c" } }, { checked: true, dataset: { osuPick: "node-d" } });
  await t.c.window.osUpdatesReviewPicked();
  assert.deepEqual(t.reads.filter(r => r.includes("preview")), ["/api/os-updates/preview?node=node-a&node=node-d"]);
  assert.ok(t.steps.includes("child Update 2 hosts"));
});

test("one host's review says what installs and what the checks say today", async () => {
  const t = await reviewed(preview([previewHost("node-a", { updates: [pkg("linux-image-6.8", true), pkg("curl")], security: 1 })]));
  const html = t.html();
  assert.ok(t.steps.includes("child Update node-a"));
  assert.match(html, /<b>node-a<\/b>/);
  assert.match(html, /2 waiting/);
  assert.match(html, /1 security/);
  assert.match(html, /likely/, "a kernel update is in the list");
  assert.match(html, /it can go ahead/);
  assert.match(button(html, "osu_review_go"), /Update node-a|osUpdatesReviewedApply/);
  assert.match(html, />Update node-a</);
  assert.doesNotMatch(html, /Homestead runs|runs on this host/);
});

test("a restart the checks would refuse: the review offers no restart, and says the row will", async () => {
  const t = await reviewed(preview([previewHost("node-a", { restart_refusal: "Something would keep this host from draining: 3 orphan records." })]));
  const html = t.html();
  assert.match(html, /will not do it by itself/);
  assert.match(html, /3 orphan records\./);
  assert.doesNotMatch(html, /3 orphan records\.\./);
  assert.match(html, /waits for you to choose Restart…/);
  assert.doesNotMatch(html, /nodePowerReview|nodeActions/, "a restart before installing would be the wrong order");
  assert.equal((html.match(/<button/g) || []).length, 2, "go and cancel");
  assert.doesNotMatch(button(html, "osu_review_go"), /\bdisabled\b/, "the update itself goes ahead");
});

test("what may clear by itself says it may wait first, not that it won't restart", async () => {
  const hosts = [previewHost("node-a", { restart_refusal: "Something would keep this host from draining: engines still listed", restart_may_clear: true })];
  const t = await reviewed(preview(hosts));
  assert.match(t.html(), /may wait up to 15 minutes first/);
  assert.match(t.html(), /engines still listed/);
  assert.doesNotMatch(t.html(), /will not do it by itself/);
  const several = await reviewed(preview([...hosts, previewHost("node-b")]));
  assert.match(several.html(), /may wait up to 15 minutes first/);
});

test("a window that may still run says so; one that was lost says skipped", async () => {
  const blip = { why: "Homestead could not start it (HTTP 503)", first: 1, last_try: 1, blip: true };
  const t = await opened({ state: { window_skip: blip } });
  assert.match(t.html(), /Window not started yet: Homestead could not start it \(HTTP 503\)/);
  assert.match(t.html(), /looks again/);
  assert.doesNotMatch(t.html(), /Window skipped/);
  const lost = await opened({ state: { window_skip: { ...blip, blip: false } } });
  assert.match(lost.html(), /Window skipped because Homestead could not start it/);
});

test("checks that could not be read are not called a refusal", async () => {
  const t = await reviewed(preview([previewHost("node-a", { restart_unread: "no route to host" })]));
  assert.match(t.html(), /could not be read/);
  assert.match(t.html(), /no route to host/);
  assert.doesNotMatch(t.html(), /it can go ahead|will not do it by itself/);
});

test("Homestead's own host is allowed, with the note", async () => {
  const one = await reviewed(preview([previewHost("node-a", { homestead_here: true })]));
  assert.match(one.html(), /runs on this host/);
  assert.match(one.html(), /stop this page answering for a few minutes/);
  assert.match(one.html(), /carries on/);
  assert.doesNotMatch(one.html(), /goes last/);
  assert.doesNotMatch(button(one.html(), "osu_review_go"), /\bdisabled\b/);
  const two = await reviewed(preview([previewHost("node-b"), previewHost("node-a", { homestead_here: true })]));
  assert.match(two.html(), /goes last/);
});

test("the restart choice starts from the setting and is sent for this run", async () => {
  const on = await reviewed(preview([previewHost("node-a")]));
  assert.match(on.html(), /<option value="when-needed" selected>/);
  const off = await reviewed(preview([previewHost("node-a")], { restart_policy: "never" }));
  assert.match(off.html(), /<option value="never" selected>/);
  off.restart = "never";
  await reviewGo(off);
  assert.deepEqual(off.sent, [{ nodes: ["node-a"], restart: "never", review_token: "tok" }]);
  assert.ok(off.steps.includes("job job-1"));
  assert.deepEqual(off.toasts, [["ok", "Updating"]]);
});

test("several hosts: one review, each with its count and verdict, in the order they would run", async () => {
  const t = await reviewed(preview([previewHost("node-b", { updates: [pkg("vim"), pkg("sudo", true)], security: 1 }),
    previewHost("node-a", { homestead_here: true, restart_refusal: "some workloads have no other host to run on" })]));
  const html = t.html();
  assert.ok(t.steps.includes("child Update 2 hosts"));
  assert.ok(html.indexOf("node-b") < html.indexOf("node-a"), "the server's order");
  assert.match(html, /<b>node-b<\/b>, then <b>node-a<\/b>/);
  assert.match(html, /<h4>node-b<\/h4>/);
  assert.match(html, /<h4>node-a<\/h4>/);
  assert.match(html, /2 waiting/);
  assert.match(html, /it can go ahead/);
  assert.match(html, /Some workloads have no other host to run on/);
  assert.match(html, />Update 2 hosts</);
  assert.equal((html.match(/id="osu_review_restart"/g) || []).length, 1, "one choice for the run");
  await reviewGo(t);
  assert.deepEqual(t.sent[0].nodes, ["node-b", "node-a"]);
});

test("a review has one notice at most: several hosts' verdicts are plain lines", async () => {
  const notices = html => (html.match(/class="ui-callout /g) || []).length;
  const hosts = [previewHost("node-b"), previewHost("node-a", { restart_refusal: "no other host" })];
  assert.equal(notices((await reviewed(preview(hosts))).html()), 0);
  assert.equal(notices((await reviewed(preview(hosts, { blocked: "An OS update is running" }))).html()), 1);
  assert.equal(notices((await reviewed(preview([hosts[1]]))).html()), 1);
  assert.equal(notices((await reviewed(preview([hosts[1]], { blocked: "An OS update is running" }))).html()), 1);
});

test("the dialog's rows leave the next check to the Settings card", async () => {
  const hosts = { "node-a": { ...HOSTS["node-a"], at: Date.now() / 1000 - 60 } };
  const t = await opened({ state: { hosts } });
  assert.match(row(t.html(), "node-a"), /read /);
  assert.doesNotMatch(row(t.html(), "node-a"), /next check/);
  const card = setup({ report: report({ hosts }) });
  await card.c.window.osUpdatesCardPaint();
  assert.match(card.fields["#settingsHostUpdates"].innerHTML, /next check/);
});

test("when it can't start, the review says why and its button stays off", async () => {
  const t = setup({ previews: preview([previewHost("node-a", { busy: "node-a has a job running: SMART test" })], { blocked: "node-a has a job running: SMART test" }) });
  t.fields["#osu_review_go"] = { disabled: false };   // the button render() drew
  await t.c.window.osUpdatesReview(["node-a"]);
  assert.match(t.html(), /node-a has a job running: SMART test/);
  assert.equal(t.fields["#osu_review_go"].disabled, true);
  await t.c.window.osUpdatesReviewedApply();
  assert.equal(t.sent.length, 0, "a click that slips through sends nothing");
});

test("a review out of date is shown again, fresh, and nothing started", async () => {
  let reads = 0;
  const stale = Object.assign(new Error("Something changed since this was reviewed; review it again"), { status: 409 });
  const t = await reviewed(preview([previewHost("node-a")]), {
    previews: () => { reads += 1; return preview([previewHost("node-a", { updates: reads > 1 ? [pkg("a"), pkg("b"), pkg("c")] : [pkg("a")] })], { review_token: `tok${reads}` }); },
    start: () => stale });
  await reviewGo(t);
  assert.equal(reads, 2, "read again");
  assert.match(t.html(), /3 waiting/);
  assert.ok(t.toasts.some(([, m]) => /review it again/.test(m)));
  assert.equal(t.steps.filter(s => s.startsWith("job")).length, 0);
  assert.equal(t.steps.filter(s => s.startsWith("child")).length, 1, "the same dialog, not another on top");
  assert.equal(t.steps.filter(s => s === "close").length, 0);
});

test("a refusal when sending says so and reads the review again; a review is sent once", async () => {
  let reads = 0;
  const t = await reviewed(preview([previewHost("node-a")]), {
    previews: () => { reads += 1; return preview([previewHost("node-a")], reads > 1 ? { blocked: "An OS update is running already" } : {}); },
    start: () => Object.assign(new Error("An OS update is running already"), { status: 400 }) });
  await reviewGo(t);
  assert.ok(t.toasts.some(([kind, m]) => kind === "bad" && /running already/.test(m)));
  assert.equal(reads, 2);
  assert.match(t.html(), /running already/);
  await t.c.window.osUpdatesReviewedApply();
  assert.equal(t.sent.length, 1);
});

test("a redraw keeps the person's restart choice, whichever way it went", async () => {
  const stale = Object.assign(new Error("review it again"), { status: 409 }), refused = Object.assign(new Error("no"), { status: 400 });
  for (const [setting, chosen, other] of [["when-needed", "never", "when-needed"], ["never", "when-needed", "never"]]) {
    for (const error of [stale, refused]) {
      const t = await reviewed(preview([previewHost("node-a")], { restart_policy: setting }), { start: () => error });
      assert.match(t.html(), new RegExp(`<option value="${setting}" selected>`), "it starts from the setting");
      t.restart = chosen;
      await reviewGo(t);
      assert.match(t.html(), new RegExp(`<option value="${chosen}" selected>`), `${chosen} kept after a ${error.status}`);
      assert.doesNotMatch(t.html(), new RegExp(`<option value="${other}" selected>`));
      await t.c.window.osUpdatesReviewedApply();
      assert.equal(t.sent.at(-1).restart, chosen, "and it is what is sent next");
    }
  }
});

test("a review opened afresh starts from the setting again", async () => {
  const t = await reviewed(preview([previewHost("node-a")]), { start: () => Object.assign(new Error("again"), { status: 409 }) });
  t.restart = "never";
  await reviewGo(t);
  delete t.fields["#osu_review_go"]; delete t.fields["#osu_review_restart"];    // Cancel: the review has gone
  await t.c.window.osUpdatesReview(["node-a"]);
  assert.match(t.html(), /<option value="when-needed" selected>/);
});

test("a host with updates and a restart waiting offers both", async () => {
  const hosts = { "node-a": { ...HOSTS["node-a"], reboot: true } };
  const r = { id: "os-2", status: "succeeded", reason: "asked-hosts", nodes: ["node-a"], message: "x",
    results: [{ node: "node-a", ok: true, note: "updates installed; restart not done: no other host", restart_needed: true }] };
  const t = await opened({ state: { hosts, rollout: r } });
  const a = row(t.html(), "node-a");
  assert.match(a, />Update…</);
  assert.match(a, />Restart…</);
  assert.match(a, /nodePowerReview\(&quot;node-a&quot;/);
  assert.match(a, /no other host/);
  assert.doesNotMatch(row((await opened({ state: { hosts, rollout: running } })).html(), "node-a"), />Restart…</, "not during a run");
});

test("Update every host's confirm says a host may wait", async () => {
  const t = await opened();
  let said = "";
  t.c.ask = async text => { said = text; return false; };
  await t.c.window.osUpdatesStart();
  assert.match(said, /keeps its updates and waits for you/);
  assert.equal(t.sent.length, 0);
});

test("a preview that fails says so in the dialog", async () => {
  const t = setup({ previews: () => new Error("HTTP 500") });
  await t.c.window.osUpdatesReview(["node-a"]);
  assert.match(t.html(), /could not be checked/);
  assert.match(t.html(), /HTTP 500/);
});

test("Last run after a smaller run is one line naming its hosts; after every host it stays as it was", async () => {
  const smaller = await opened({ state: { rollout: { id: "os-2", status: "succeeded", reason: "asked-hosts", nodes: ["node-a", "node-c"], message: "2 hosts done; 1 restarted", results: [] } } });
  assert.match(smaller.fields["#mbody"].innerHTML, /Last run: node-a, node-c, succeeded/);
  assert.match(smaller.html(), /2 hosts done; 1 restarted/);
  assert.doesNotMatch(smaller.html().match(/Last run[\s\S]*?<\/div>/)[0], /<br>/, "one line");
  const every = await opened({ state: { rollout: { id: "os-3", status: "succeeded", reason: "asked", nodes: ["node-a", "node-b", "node-c"], message: "3 hosts done", results: [] } } });
  assert.match(every.html(), /<b>Last run succeeded<\/b>/);
  const card = setup({ report: report({ rollout: { id: "os-2", status: "failed", reason: "asked-hosts", nodes: ["node-a"], message: "1 host done", results: [] } }) });
  await card.c.window.osUpdatesCardPaint();
  assert.match(card.fields["#settingsHostUpdates"].innerHTML, /Last run: node-a, failed/);
});

test("a refused restart's reason shows under the host, and the row offers Restart…", async () => {
  const t = await opened({ state: { rollout: { id: "os-2", status: "succeeded", reason: "asked-hosts", nodes: ["node-b"], message: "x",
    results: [{ node: "node-b", ok: true, note: "updates installed; restart not done: Something would keep this host from draining: 3 orphan records", restart_needed: true }] } } });
  const b = row(t.html(), "node-b");
  assert.match(b, /Something would keep this host from draining: 3 orphan records/);
  assert.doesNotMatch(b, /updates installed; restart not done/);
  assert.match(b, />Restart…</);
});

test("a reason from the last run goes once the host has been restarted", async () => {
  const hosts = { ...HOSTS, "node-b": { ...HOSTS["node-b"], reboot: false } };
  const t = await opened({ state: { hosts, rollout: { id: "os-2", status: "succeeded", reason: "asked-hosts", nodes: ["node-b"], message: "x",
    results: [{ node: "node-b", ok: true, note: "updates installed; restart not done: 3 orphan records", restart_needed: true }] } } });
  assert.doesNotMatch(row(t.html(), "node-b"), /orphan records/);
});

test("a hold that keeps failing is said during the run; a moment's Busy is not", async () => {
  const noted = await opened({ state: { rollout: { ...running, hold_failed: { "node-b": { since: 1, error: "Busy", noted: true } } } } });
  assert.match(noted.html(), /Automatic updates could not be held on node-b/);
  assert.match(noted.html(), /Busy/);
  const brief = await opened({ state: { rollout: { ...running, hold_failed: { "node-b": { since: 1, error: "Busy" } } } } });
  assert.doesNotMatch(brief.html(), /could not be held/);
});

test("a skipped window is said beside Last run, or alone; not over a run going", async () => {
  const window_skip = { why: "Prepare disk is in the way (Finish disk preparation)", first: 1, last_try: 1 };
  const last = { id: "os-2", status: "succeeded", reason: "asked", nodes: ["node-a"], message: "1 host done", results: [] };
  const beside = await opened({ state: { window_skip, rollout: last } });
  assert.match(beside.html(), /Last run succeeded[\s\S]*Window skipped because Prepare disk is in the way \(Finish disk preparation\)/);
  assert.equal((beside.html().match(/class="(?:note|ui-callout) /g) || []).length, 1, "one notice");
  const alone = await opened({ state: { window_skip } });
  assert.match(alone.html(), /Window skipped because/);
  const going = await opened({ state: { window_skip, rollout: running } });
  assert.doesNotMatch(going.html(), /Window skipped/);
  for (const rollout of [last, null]) {
    const card = setup({ report: report({ window_skip, rollout }) });
    await card.c.window.osUpdatesCardPaint();
    assert.match(card.fields["#settingsHostUpdates"].innerHTML, /Window skipped because Prepare disk/);
  }
});

test("while a host waits for its restart, Stop is offered, a run of one included, and follows the wait", async () => {
  const one = { ...running, nodes: ["node-a"] };
  const waiting = { ...one, node: { name: "node-a", waiting: true } };
  const t = await opened({ state: { rollout: waiting } });
  assert.match(t.html(), /Stop and leave this host to me/);
  let state = report({ rollout: one });
  const f = setup({ report: () => state });
  await f.c.window.osUpdates();
  assert.doesNotMatch(f.html(), /Stop/);
  state = report({ rollout: waiting });
  await f.timers.pop()();
  assert.match(f.fields["#osu_run"].innerHTML, /Stop and leave this host to me/, "appears as the wait starts");
});

test("once the wait is over and the restart is under way, the wait's Stop has gone", async () => {
  // The node as test_os_rollout_hosts' test_a_cleared_wait_leaves_no_trace_in_restarting pins it.
  const node = { name: "node-a", attempts: 1, copies_since: 1000, cordoned_before: false, op: "op-node-a", security: 0, since: 1020, updates: 2 };
  const one = await opened({ state: { rollout: { ...running, nodes: ["node-a"], phase: "restarting", node } } });
  assert.doesNotMatch(one.html(), /Stop and leave this host to me/);
  assert.doesNotMatch(one.html(), /Stop after this host/, "a run of one has nothing to stop after");
  const two = await opened({ state: { rollout: { ...running, phase: "restarting", node } } });
  assert.doesNotMatch(two.html(), /Stop and leave this host to me/);
  assert.match(two.html(), /Stop after this host/);
  let state = report({ rollout: { ...running, nodes: ["node-a"], node: { ...node, waiting: true } } });
  const f = setup({ report: () => state });
  await f.c.window.osUpdates();
  state = report({ rollout: { ...running, nodes: ["node-a"], phase: "restarting", node } });
  await f.timers.pop()();
  assert.doesNotMatch(f.fields["#osu_run"].innerHTML, /Stop/, "and the button goes as the wait ends");
});

test("a run of one has nothing to stop after, and says its host", async () => {
  const one = await opened({ state: { rollout: { ...running, nodes: ["node-a"] } } });
  assert.doesNotMatch(one.html(), /Stop after this host/);
  assert.match(one.html(), /Updating node-a/);
  const two = await opened({ state: { rollout: running } });
  assert.match(two.html(), /Stop after this host/);
});

test("Update every host stays, and finishing while stopping still reads as finished", async () => {
  const t = await opened();
  assert.match(t.html(), />Update every host now</);
  t.c.api = async () => { throw new Error("No OS update is running"); };
  await t.c.window.osUpdatesStop();
  assert.deepEqual(t.toasts[0], ["ok", "The update had already finished"]);
});

test("a host name is escaped wherever it is shown", async () => {
  const name = 'a"<b>';
  const t = await opened({ state: { hosts: { [name]: { ...HOSTS["node-a"] } } } });
  assert.doesNotMatch(t.html(), /data-osu-pick="a"/);
  const r = await reviewed(preview([previewHost(name)]));
  assert.doesNotMatch(r.html(), /<b>a"<b>/);
});

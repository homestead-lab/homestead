"use strict";
// Network shares after the Nov 2026 mockup (views-storage-shares-details.js):
// one summary line, the servers a line each, Edit share and ⋯ on a row, the
// share's details sidebar and its access hover card.
const test = require("node:test"), assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");

function page() {
  const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const c = { window: {}, console, Set, Map, Promise, JSON, Date, document: { addEventListener() {} }, STATE: { data: {}, q: "" },
    esc, meter: () => "", tip: () => "?", toast() {}, fmtAgo: s => `${s}s ago`, can: () => true, plural: (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`,
    icon: n => `<svg data-icon="${n}"></svg>`, HOVER_CARDS: {} };
  c.jsArg = s => JSON.stringify(String(s ?? "")); c.jsq = s => esc(c.jsArg(s));
  vm.createContext(c);
  for (const f of ["web/js/ui.js", "web/js/details-panel.js", ...require("./helpers/storage-source").files()]) vm.runInContext(fs.readFileSync(f, "utf8"), c);
  c.DETAIL_KINDS = vm.runInContext("DETAIL_KINDS", c);
  Object.assign(c.STATE.data, {
    samba: { installed: true, enabled: true, ready: 1, desired: 1, name: "homestead-smb", address: "192.0.2.245", served_shares: ["media", "secure"], in_sync: true },
    nfs: { installed: true, enabled: true, ready: 1, desired: 1, name: "homestead-nfs", address: "192.0.2.246", exports: ["media"] },
    shares: [
      { name: "media", pvc: "share-media", size_gb: 250, public: true, user: "lab", read_only: false, recycle: true, recycle_days: 30, nfs_clients: "192.0.2.0/24" },
      { name: "secure", pvc: "share-secure", size_gb: 20, public: false, user: "lab", read_only: true }] });
  return c;
}

test("the summary says how many shares are served and where Windows finds them", () => {
  const c = page(), { shares, samba, nfs } = c.STATE.data;
  assert.match(c.sharesSummary(shares, samba, nfs), /^<span class="sum-wide"><b>2 shares, all served\.<\/b> Windows reaches them at \\\\192\.0\.2\.245\.<\/span>/);
  const late = c.sharesSummary([...shares, { name: "photos", pvc: "p" }], samba, nfs);
  assert.match(late, /<b>3 shares, 2 served\.<\/b>[\s\S]*onclick="openDetails\('share',&quot;photos&quot;\)">photos is not served yet<\/button>/);
});

test("the servers are a line each: ready, where, how many, and their buttons", () => {
  const c = page(), { shares, samba, nfs } = c.STATE.data;
  const html = c.sharesServers(shares, samba, nfs, [{ user: "lab" }, { user: "backup" }]);
  assert.match(html, /<b>SMB server<\/b> <span class="dim small">ready at \\\\192\.0\.2\.245 · 2 shares<\/span>/);
  assert.match(html, /<b>NFSv4 server<\/b> <span class="dim small">ready at 192\.0\.2\.246 · 1 export<\/span>/);
  assert.match(html, /data-plain="true"[\s\S]*?>SMB users · 2<\/button>/);
  assert.match(html, /How it is managed/, "what the card explained is in ⋯");
});

test("a share's sidebar: Edit share and Copy Windows path first, guests who can change files said plainly, and its access in words", () => {
  const c = page();
  const kind = c.DETAIL_KINDS.share;
  assert.deepEqual([...kind.tabs.map(t => t.label)], ["Overview", "Access", "Activity"]);
  assert.deepEqual([...kind.actions("media").slice(0, 2).map(i => i.label)], ["Edit share", "Copy Windows path"]);
  assert.equal(kind.head("media").sub, "Network share · share-media · 250 GiB");
  const overview = kind.tabs[0].html("media");
  assert.match(overview, /Guests can change files[\s\S]*Change access/);
  assert.match(overview, /<dt>Windows path<\/dt><dd><span class="mono">\\\\192\.0\.2\.245\\media<\/span>/);
  assert.match(overview, /<dt>NFS<\/dt><dd><span class="mono">192\.0\.2\.246:\/media<\/span>/);
  assert.doesNotMatch(kind.tabs[0].html("secure"), /Guests can change files/, "a private share has no such notice");
  const card = c.HOVER_CARDS["share-access"]("media");
  assert.match(card, /Access · media[\s\S]*Guests allowed[\s\S]*Guests[\s\S]*Read and write, no password[\s\S]*Deleted files kept 30 days[\s\S]*Also over NFS/);
});

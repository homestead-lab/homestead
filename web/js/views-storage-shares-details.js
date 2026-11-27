/* Homestead Network shares, after the Nov 2026 mockup: the page's summary
   line, the two servers in one card, a share's actions, its details sidebar
   and its access hover card. A classic script, loaded after
   views-storage-shares.js, whose helpers it uses. */
const shareByName = name => (STATE.data.shares || []).find(s => s.name === name) || null;
const shareWindowsPath = s => STATE.data.samba?.address ? `\\\\${STATE.data.samba.address}\\${s.name}` : "";
const shareNfsPath = s => s.nfs_clients && STATE.data.nfs?.address ? `${STATE.data.nfs.address}:/${s.name}` : "";
const shareOffline = s => (STATE.data.samba?.offline_shares || []).find(row => row.name === s.name) || null;
const shareCount = n => `${n} share${n === 1 ? "" : "s"}`;

/* What is wrong with a share, if anything: {tone, word, line}. A guest share
   that can change files is not a fault, but it is said in its sidebar. */
function shareProblem(s, smb = STATE.data.samba || {}) {
  const off = shareOffline(s);
  if (off) return { tone: "bad", word: "is offline", line: `Its storage is unavailable: ${off.reason}` };
  if (smb.installed && smb.served_shares && !smb.served_shares.includes(s.name)) return { tone: "warn", word: "is not served yet", line: "The SMB server has not picked it up yet. Repair SMB server makes it reread the shares." };
  return null;
}

/* The page's one line: how many shares are served, where Windows finds them,
   then each share or server that needs a person, as a link. */
function sharesSummary(shares, smb = {}, nfs = {}) {
  const served = shares.filter(s => !shareProblem(s, smb)).length;
  const head = !shares.length ? "No shares yet."
    : `${shareCount(shares.length)}${served === shares.length ? `, ${shares.length === 1 ? "served" : "all served"}` : `, ${served} served`}.`;
  const where = smb.address && shares.length ? ` Windows reaches ${shares.length === 1 ? "it" : "them"} at \\\\${esc(smb.address)}.` : "";
  const troubled = shares.map(s => [s, shareProblem(s, smb)]).filter(([, p]) => p);
  return UI.summary({ head: `<b>${head}</b>${where}`, parts: [
    ...troubled.slice(0, 3).map(([s, p]) => ({ text: `${s.name} ${p.word}`, tone: p.tone, run: `openDetails('share',${jsq(s.name)})` })),
    troubled.length > 3 && { text: `${troubled.length - 3} more`, note: true },
    smb.error ? { text: "The SMB server's state could not be read", tone: "warn" }
      : smb.installed && smb.enabled && (smb.ready || 0) < (smb.desired || 1) && { text: "The SMB server is not ready", tone: "bad" },
    smb.installed && !smb.in_sync && !smb.error && { text: "The SMB server is out of sync", tone: "warn", run: can("admin") ? "repairSamba()" : "" },
    nfs.installed && nfs.enabled && (nfs.ready || 0) < (nfs.desired || 1) && { text: "The NFS server is not ready", tone: "bad" },
  ], narrow: { head: `<b>${head}</b>` } });
}

/* The servers, a line each: whether each is ready, where, how many it serves,
   and its buttons. What the cards used to explain is in ⋯. */
function sharesServers(shares, smb, nfs, users) {
  const dot = ok => `<i class="share-dot ${ok ? "ok" : "bad"}" aria-hidden="true"></i>`;
  const smbOk = !smb.error && smb.installed && smb.enabled && (smb.ready || 0) >= (smb.desired || 1);
  const smbWords = smb.error ? `Its state could not be read. ${esc(smb.error)}` : !smb.installed ? "not installed · creating the first share installs it"
    : !smb.enabled ? "stopped" : `${smbOk ? "ready" : `${smb.ready || 0} of ${smb.desired || 1} ready`}${smb.address ? ` at \\\\${esc(smb.address)}` : ", waiting for an address"} · ${shareCount(smb.served_shares?.length ?? shares.length)}`;
  const nfsOk = !nfs.error && nfs.installed && nfs.enabled && (nfs.ready || 0) >= (nfs.desired || 1);
  const nfsCount = (nfs.exports || []).length;
  const nfsWords = nfs.error ? `Its state could not be read. ${esc(nfs.error)}` : !nfs.installed ? "not installed" : !nfs.enabled ? "stopped"
    : `${nfsOk ? "ready" : `${nfs.ready || 0} of ${nfs.desired || 1} ready`}${nfs.address ? ` at ${esc(nfs.address)}` : ", waiting for an address"} · ${nfsCount} export${nfsCount === 1 ? "" : "s"}`;
  const admin = can("admin");
  const smbActions = !smb.error && admin ? actionBar([
    smb.installed && (!smb.in_sync || Object.keys(smb.recovery_failures || {}).length) && { label: "Repair SMB server", run: "repairSamba(this)", plain: true },
    users && { label: `SMB users · ${users.length}`, run: "smbUsers()", plain: true },
    { label: "Manage add-on", run: "settingsTab('cluster');go('settings')", icon: "sliders" },
    { label: "How it is managed", run: "sharesExplain('smb')", icon: "info" },
  ].filter(Boolean), { shown: smb.installed && (!smb.in_sync || Object.keys(smb.recovery_failures || {}).length) ? 2 : users ? 1 : 0, label: "More for the SMB server" }) : "";
  const nfsActions = actionBar([{ label: "Manage add-on", run: "settingsTab('cluster');go('settings')", plain: true },
    { label: "How it works", run: "sharesExplain('nfs')", icon: "info" }], { shown: 1, label: "More for the NFS server" });
  return `<div class="card flat share-servers">
    <div class="share-server">${dot(smbOk)}<div><b>SMB server</b> <span class="dim small">${smbWords}</span></div>${smbActions}</div>
    <div class="share-server">${dot(nfsOk || !nfs.installed)}<div><b>NFSv4 server</b> <span class="dim small">${nfsWords}</span></div>${nfsActions}</div></div>`;
}
/* What the server cards explained, in a dialog from ⋯. */
if (typeof window === "object") window.sharesExplain = kind => kind === "nfs"
  ? modal("How the NFS server works", `${nfsRecovery(STATE.data.nfs || {}, true, "<p>The NFS server is optional and separate from the SMB server. Only the chosen shared volumes are exported, and only to their allowed client networks. The SMB server and the volumes are not affected.</p>")}${UI.actions(UI.cancel("Close"))}`)
  : modal("How the SMB server is managed", `<p>Homestead manages the SMB server, its image and its volumes from the list of shares. Turn the server on or off, or remove it, under Settings → Hardware and storage → Add-ons. Volumes are kept. A volume that has been unavailable for one minute is left out for now. It is added back when it has been available again for two minutes. Both changes restart the SMB server. Shares still need their storage and a working cluster to recover.</p>${UI.actions(UI.cancel("Close"))}`);

/* A share's actions: Edit share first, the rest in ⋯ under their groups. */
function shareActionItems(s) {
  const name = jsq(s.name), path = shareWindowsPath(s);
  const item = (label, run, iconName, options = {}) => ({ label, run, icon: iconName, ariaLabel: `${label} ${s.name}`, ...options });
  return [
    item("Edit share", `editShare(${name})`, "edit", { need: "admin", group: "Change", tip: "Make this share larger or change who can use it" }),
    path && item("Copy Windows path", `ipamCopy(${jsq(path)})`, "copy", { group: "Use", tip: path }),
    item("NFS export…", `nfsExport(${name})`, "link", { need: "admin", group: "Change", tip: "Set up this share's NFSv4 export and its allowed clients" }),
    item("Remove share…", `rmShare(${name})`, "trash", { need: "admin", danger: true, group: "Remove" }),
  ].filter(Boolean);
}

/* Who can use a share, in words: guests or a user, read or write, the bin, NFS. */
function shareAccessRows(s) {
  const writes = s.read_only ? "Read only" : "Read and write";
  return [
    s.public ? ["Guests", `${writes}, no password`, "user"] : [`${s.user} users`, writes, "user"],
    s.public && s.user && [`${s.user} users`, writes, "user"],
    !s.read_only && ["Recycle bin", !s.recycle ? "Off: deleted files are gone at once" : s.recycle_days ? `Deleted files kept ${s.recycle_days} days` : "Deleted files kept until the bin is emptied", "trash"],
    s.nfs_clients && ["Also over NFS", `<span class="mono">${esc(shareNfsPath(s) || s.nfs_clients)}</span>`, "network"],
  ].filter(Boolean).map(([label, value, iconName]) => [label, value.startsWith("<") ? value : esc(value), iconName]);
}

/* Overview (the mockup): what to look at first, then where it is reached and stored. */
function shareOverview(name) {
  const s = shareByName(name);
  if (!s) return "";
  const p = shareProblem(s);
  const guests = s.public && !s.read_only && UI.callout("warn", "Guests can change files", `Anyone on the network can read and change its files without a password.
    <div class="row" style="margin-top:8px"><button type="button" class="btn sm" data-need="admin" onclick="editShare(${jsq(s.name)})">Change access</button></div>`);
  return [p && UI.callout(p.tone, `${s.name} ${p.word}`, esc(p.line)), guests].filter(Boolean).join("")
    + UI.facts([
      ["Windows path", shareWindowsPath(s) ? `<span class="mono">${esc(shareWindowsPath(s))}</span>` : '<span class="dim">Waiting for an address</span>'],
      ["NFS", shareNfsPath(s) ? `<span class="mono">${esc(shareNfsPath(s))}</span>` : '<span class="dim">Not exported</span>'],
      ["Storage", esc(`${s.pvc || "—"}${s.sub_path ? `/${s.sub_path}` : ""}${s.size_gb ? `, ${s.size_gb} GiB` : ""}`)],
      ["Users", esc(s.public ? (s.user ? `Guests, ${s.user}` : "Guests") : s.user || "—")],
    ], { icons: false });
}

/* Access: who can read and write, the recycle bin and NFS, and where each is changed. */
function shareAccessTab(name) {
  const s = shareByName(name);
  if (!s) return "";
  return UI.facts(shareAccessRows(s))
    + `<div class="details-foot">${actionBar([{ label: "Change access", run: `editShare(${jsq(s.name)})`, need: "admin", plain: true },
      can("admin") && { label: "SMB users", run: "smbUsers()", plain: true },
      { label: "NFS export…", run: `nfsExport(${jsq(s.name)})`, need: "admin", plain: true }].filter(Boolean), { shown: 3 })}</div>`;
}

/* Activity: what Kubernetes said lately about its volume and the servers. */
function shareActivityTab(name) {
  const s = shareByName(name);
  if (!s) return "";
  const servers = [STATE.data.samba?.name || "homestead-smb", STATE.data.nfs?.name || "homestead-nfs"];
  const mine = e => e.obj === s.pvc || servers.some(n => String(e.obj || "").startsWith(n));
  return detailsSlot(detailsData("events", clusterEventsRead), d => UI.activity((d.events || []).filter(mine).map(e => ({ at: e.time, title: eventWords(e.reason), detail: e.msg, tone: e.type === "Warning" ? "warn" : "" })),
    { empty: "Nothing has been recorded for this share's volume or the servers lately." }), { reading: "Reading what happened…", failed: "The cluster's events could not be read" })
    + '<p class="dim xs">Kubernetes keeps its events for about an hour. Who changed the share is in Events › Audit log.</p>';
}

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.share = {
  head: name => {
    const s = shareByName(name);
    return s && { title: s.name, sub: ["Network share", s.pvc, s.size_gb ? `${s.size_gb} GiB` : ""].filter(Boolean).join(" · ") };
  },
  actions: name => { const s = shareByName(name); return s ? shareActionItems(s) : []; },
  shown: 2,
  tabs: [
    { id: "overview", label: "Overview", html: shareOverview },
    { id: "access", label: "Access", html: shareAccessTab },
    { id: "activity", label: "Activity", html: shareActivityTab },
  ],
};

/* The access chips' card: who can read and write, the bin and NFS. */
if (typeof HOVER_CARDS === "object") HOVER_CARDS["share-access"] = name => {
  const s = shareByName(name);
  if (!s) return "";
  return UI.card({ title: `Access · ${s.name}`, chipHtml: s.public ? UI.chip("Guests allowed", "warn", { size: "slim" }) : UI.chip("Private", "neutral", { size: "slim" }),
    rows: shareAccessRows(s), link: { label: "Edit share", onclick: `editShare(${JSON.stringify(s.name)})` } });
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  shareByName, shareWindowsPath, shareNfsPath, shareOffline, shareCount, shareProblem, sharesSummary, sharesServers, shareActionItems,
  shareAccessRows, shareOverview, shareAccessTab, shareActivityTab,
};

/* The setup guide: /setup, laid out as Settings is - chapters and their steps
   down the side, one step open beside them; a list on a phone. Each step says
   in two lines why it matters, shows the cluster as it is (a diagram where
   the idea is spatial), and offers one thing to do. Homestead decides what is
   done by looking, every time, so a step done and since undone shows again;
   appearance is a person's confirmation on their device. Admins see every chapter; everyone sees the
   steps that are theirs - how it looks, their phone, their notifications.

   Until it is completed or reminders are dismissed, it opens over the
   Dashboard after an administrator signs in, and a pulsing book button
   offers it. The button remains available afterwards, on every cluster. */

const SETUP_CHAPTERS = [
  ["Cluster health", ["health", "quorum", "placement", "clocks"]],
  ["Access", ["address", "https", "hostname"]],
  ["LAN networking", ["lan"]],
  ["Storage and backups", ["disks", "storage", "smb", "backups", "config", "osupdates"]],
  ["Preferences and users", ["appearance", "phone", "notifications", "people"]],
  ["Connections", ["unifi", "ipam", "unraid", "homeassistant", "linked"]],
  ["Applications and console", ["starter", "console"]],
];
const SETUP_PERSONAL = ["appearance", "phone", "notifications"];
const setupDemoVisit = window.HOMESTEAD_DEMO === true || new URLSearchParams(location.search).get("demo") === "1";
const setupDemo = () => setupDemoVisit;
const setupLocal = (key, value) => {
  try {
    if (value === undefined) return localStorage.getItem(`homestead.setup.${key}`);
    localStorage.setItem(`homestead.setup.${key}`, value);
  } catch (e) { /* this visit only */ }
  return null;
};
const isAddress = host => /^\d+\.\d+\.\d+\.\d+$/.test(host) || host.includes(":") || host === "localhost";

// A configuration visit belongs to this tab, including across a reload.
// Keep demo visits separate from a real cluster on the same origin.
function setupVisit(value) {
  const key = `homestead.setup.visit.${setupDemo() ? "demo" : "live"}`;
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key);
  } catch (_) { /* storage may be unavailable */ }
  return value;
}

window.setupNavigation = view => {
  if (STATE.view === "setup" && view !== "setup" && view !== "dash") {
    const step = new URLSearchParams(location.search).get("step");
    if (SETUP_STEPS[step]) setupVisit(step);
  }
  if (view === "setup" || view === "dash") setupVisit("");
  const step = setupVisit(), host = $("#setupReturn");
  if (!host) return;
  host.classList.toggle("hidden", !SETUP_STEPS[step]);
  host.innerHTML = SETUP_STEPS[step] ? `<div class="setup-return-copy"><b>Setup in progress</b><div>${esc(SETUP_STEPS[step].title)}</div></div>
    <div class="row">${UI.button("Return to setup", `setupOpen(${jsArg(step)})`, { kind: "pri" })}<button class="iconbtn" type="button" onclick="setupDismissVisit()" aria-label="Dismiss setup return bar" title="Dismiss"><svg aria-hidden="true"><use href="#i-x"/></svg></button></div>` : "";
};
window.setupDismissVisit = () => { setupVisit(""); $("#setupReturn")?.classList.add("hidden"); };

// Say exactly what is observed; configuration is not proof of end-to-end health.
const SETUP_CHECKS = {
  health: "Checks the current health reports for hosts, apps, VMs and volumes.",
  quorum: "Checks the number of servers that manage the cluster. One server is accepted for a lab.",
  placement: "Checks that a way of sharing the work is chosen. Kubernetes' default counts. It does not check where apps run now.",
  clocks: "Checks the time-sync reports available from hosts.",
  address: "Checks whether Homestead uses a reserved virtual IP (VIP).",
  smb: "Checks that the SMB server is installed and enabled, not that clients can access its shares.",
  https: "Uses HTTPS in this browser or an address you previously checked. A saved address is not retested automatically.",
  hostname: "Checks whether this browser opened Homestead by name.",
  lan: "Checks for saved LAN networks for VMs and apps. It checks the settings, not the connection, and does not test host network cards or DHCP.",
  disks: "Checks for unused non-system disks in the disk inventory.",
  storage: "Checks the default storage class and its number of copies against the ready hosts.",
  backups: "Checks that a backup target is configured. This does not verify a backup or restore.",
  config: "Records a settings export. Homestead cannot check where the file is saved.",
  osupdates: "Checks that the OS update schedule is enabled, not that every host has updated.",
  appearance: "A choice saved on this device. Homestead does not check how it looks.",
  phone: "Checks whether this browser runs Homestead as an installed app. Other devices are not checked.",
  notifications: "Checks for a registered notification device on this account, not delivery.",
  people: "Checks that at least two administrator accounts exist.",
  unifi: "Checks that a UniFi address is configured, not that the connection is healthy.",
  ipam: "Checks that subnets are added and scanned, and synced from UniFi when it is connected. It does not check that every address is recorded.",
  unraid: "Checks that an import source is added, not that an import has succeeded.",
  homeassistant: "Checks for an unexpired API key, not a working Home Assistant connection.",
  linked: "Checks that another cluster is linked.",
  starter: "Checks that an app other than Homestead exists, not that it is healthy.",
  console: "Checks that the host console is enabled, not that every host screen is working.",
};

function setupIntroHtml(ids, status) {
  const next = ids.find(id => ["attention", "todo"].includes(status[id])) || ids[0];
  return `<section class="card flat setup-card" data-step="intro">
    ${UI.moduleHeader(`Welcome to Homestead`, `A guide to your cluster and your own preferences`, ``)}
    ${UI.lead("Work through the basics, or pick a step from the list. Optional steps can be skipped and revisited later.")}
    ${UI.steps([
      { title: "Review the checks", detailHtml: "Automatic checks use cluster reports. Each step explains what is checked. Some steps need a confirmation." },
      { title: "Set up the cluster", detailHtml: "Use Return to setup after visiting a settings page. Next moves to the following step without marking the current one complete." },
      { title: "Return when needed", detailHtml: "This guide opens each time an administrator signs in, until it is finished. The book icon in the top bar always opens it." },
    ])}
    ${UI.actions(UI.button(next ? `Begin: ${SETUP_STEPS[next].title}` : "Back to the Dashboard", next ? `setupOpen(${jsArg(next)})` : "go('dash')", { kind: "pri" }))}
  </section>`;
}

/* What the browser alone can tell, laid over what the cluster said. */
function setupFacts(state) {
  const steps = { ...(state.steps || {}) };
  const secure = location.protocol === "https:";
  steps.https = { ...(steps.https || {}), done: !!steps.https?.done || secure, here: secure, applies: steps.https?.applies !== false };
  steps.hostname = { ...(steps.hostname || {}), done: !isAddress(location.hostname), host: location.hostname, applies: !!state.admin };
  steps.appearance = { done: setupLocal("appearance") === "1", applies: true };
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  steps.phone = { done: standalone, installed: standalone, secure: window.isSecureContext, applies: true };
  steps.notifications = { ...(steps.notifications || {}), secure: window.isSecureContext,
    permission: "Notification" in window ? Notification.permission : "unsupported", applies: true };
  return steps;
}

function setupStatus(id, steps, skips) {
  const s = steps[id];
  if (!s || s.applies === false) return "n/a";
  if (s.done) return "done";
  if (skips.includes(id)) return "skipped";
  return s.error || ["health", "quorum", "disks", "storage"].includes(id) ? "attention" : "todo";
}

function setupStatusLabel(id, status, facts = {}) {
  if (status === "done") {
    if (id === "appearance") return "Confirmed";
    if (id === "https" && !facts.here) return "Previously checked";
    if (id === "config") return "Export recorded";
    if (["placement", "lan", "smb", "backups", "osupdates", "unifi", "ipam", "unraid", "homeassistant", "linked", "starter", "console"].includes(id)) return "Configuration found";
    return "Check passed";
  }
  if (status === "skipped") return "Skipped by choice";
  if (status === "attention") return "Needs attention";
  return id === "appearance" ? "Needs confirmation" : "Not yet complete";
}

function setupVisible(state) {
  return SETUP_CHAPTERS.map(([title, ids]) => [title, ids.filter(id => (state.admin || SETUP_PERSONAL.includes(id))
    && (state.steps[id] || {}).applies !== false)]).filter(([, ids]) => ids.length);
}

/* ---------------- the steps ---------------- */
const SETUP_STEPS = {
  health: {
    title: "Cluster check", lead: s => s.done ? "No health issues are reported for hosts, apps, VMs or attached volumes." :
      "Review the reported issues before continuing with setup.",
    body: s => s.done ? "" : `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Resource</th><th>Severity</th><th>Details</th></tr></thead><tbody>
      ${(s.issues || []).map(i => `<tr><td><b>${esc(i.name || i.kind)}</b><div class="dim xs">${esc(i.kind)}</div></td>
        <td data-label="Severity">${UI.chip(i.severity, i.severity === "critical" ? "bad" : "warn")}</td>
        <td data-label="Details" class="small">${esc(i.reason)}</td></tr>`).join("")}</tbody></table></div>`,
    actions: () => [{ label: "Open the cluster check", run: "go('cluster')", pri: true }],
    more: "<p>The Cluster page shows these checks in detail. Reported issues also appear in notifications.</p>",
  },
  quorum: {
    title: "Hosts and quorum", lead: s => s.servers === 2
      ? "The cluster needs a majority of its servers to run. With two servers, both must be available. A third server, or a small machine that only votes as a tiebreaker, allows one to fail."
      : s.servers >= 3 ? `The cluster has ${s.servers} servers, and keeps running if ${Math.max(0, s.servers - (Math.floor(s.servers / 2) + 1))} of them fail.`
      : "A single server is suitable for a lab, but the cluster stops if it fails. Use three servers for redundancy.",
    body: s => window.Diagram ? Diagram.quorum((s.members?.length ? s.members : (s.nodes || []).map(n => n.name)).map(name =>
      ({ name, ready: (s.ready || []).includes(name) || (s.nodes || []).some(n => n.name === name && n.ready),
        tiebreaker: (s.tiebreakers || []).includes(name) }))) : "",
    actions: s => [s.servers === 2 && ["k3s", "rke2"].includes(window.STATE?.platform?.distribution)
      ? { label: "Add a tiebreaker", run: "platformJoinGuide('tiebreaker')", pri: true }
      : { label: "Add a host", run: "platformJoinGuide()", pri: true }],
    more: "<p>Worker hosts run apps and can store volume copies. They do not count towards the majority of servers.</p>",
  },
  placement: {
    title: "How hosts share the work", lead: s => s.mode === "standby"
      ? `Hot spare. ${esc((s.hosts || []).filter(h => h.standby).map(h => h.name).join(", ") || "no host")} take${(s.hosts || []).filter(h => h.standby).length === 1 ? "s" : ""} apps only when the others cannot.`
      : s.mode === "spread" ? `Spread by capacity. Each app goes to the host with the most room left, based on what apps reserve.${s.unreserved ? ` ${s.unreserved} app${s.unreserved === 1 ? " reserves" : "s reserve"} nothing yet.` : ""}`
      : s.mode ? "Kubernetes default. Apps are placed one by one in Kubernetes' usual way."
      : "With more than one host, choose how they share the apps: in proportion to their size, or with a host kept free for when another fails.",
    body: s => UI.facts((s.hosts || []).map(h => [h.name, h.standby ? (h.tainted ? "standby" : "standby · not marked yet")
      : h.takes_work ? "takes apps" : "not taking work now"])),
    actions: s => [{ label: s.mode ? "Change in Settings" : "Choose in Settings", run: "settingsTab('placement');go('settings')", pri: !s.mode }],
    more: "<p><b>Spread by capacity</b> needs apps to reserve CPU and memory, because apps are placed by what they reserve, not by what they use. <b>Hot spare</b> keeps the chosen hosts free while other hosts have room. No app is changed. In both cases, apps that moved while a host was away can move back when it returns, under Settings › Placement › When a host comes back.</p>",
  },
  clocks: {
    title: "Time synchronisation", lead: s => s.done ? "Available host reports show synchronised clocks." :
      `Accurate clocks are needed for certificates and for the cluster to work together. Hosts without confirmed time synchronisation: ${esc((s.unsynced || []).join(", ") || "unknown")}.`,
    actions: () => [{ label: "Open Hosts", run: "go('nodes')", pri: true }],
    more: "<p>Check time synchronisation on the host's page under Hosts. The installer's diagnostic tool can turn it on where it is off.</p>",
  },
  address: {
    title: "Cluster address", lead: s => s.done ? `Homestead uses <span class="mono">${esc(s.url)}</span>. This virtual IP (VIP) moves to another host if its host fails. Apps can share it on separate ports.`
      : s.harvester ? "Harvester assigned Homestead an address during installation."
      : "Reserve a virtual IP (VIP) outside the router's DHCP range. It moves to another host if its host fails, and Homestead and apps can share it.",
    body: s => window.Diagram ? Diagram.vip(s.done ? (s.url || "").replace(/^https?:\/\//, "").replace(/:\d+$/, "") : "",
      [{ port: (s.url || "").match(/:(\d+)$/)?.[1] || "8080", app: "Homestead" }]) : "",
    actions: s => s.done ? [] : ["kube-vip", "metallb"].includes(s.load_balancer)
      ? [s.vips ? { label: "Move Homestead to a virtual IP", run: "go('network');setTimeout(() => window.selfAddressMove && selfAddressMove(), 800)", pri: true }
        : { label: "Add a virtual IP", run: "go('network');setTimeout(() => window.vipAdd && vipAdd(), 800)", pri: true }]
      : [{ label: "Install kube-vip", run: "settingsTab('hardware');go('settings')", pri: true }],
  },
  https: {
    title: "HTTPS and remote access", lead: s => s.done
      ? s.here ? "This browser uses HTTPS. Homestead can be installed as an app, and notifications turned on, on supported devices."
        : `An HTTPS address was checked before: <span class="mono">${esc(s.url)}</span>. Open it on each device to confirm access.`
      : "HTTPS allows Homestead to be installed as an app and to send notifications. Use Cloudflare Tunnel for a public address without port forwarding on the router, or Tailscale for private access from known devices.",
    body: s => `${window.Diagram ? Diagram.remote(s.done ? (s.tunnels?.length ? "your tunnel" : "HTTPS") : "", s.url || (s.here ? location.origin : ""), location.host) : ""}
      ${s.done && s.url ? "" : `<div class="ui-field" style="margin-top:10px"><label>Check an existing HTTPS address</label>
        <div class="row" style="gap:8px;flex-wrap:nowrap"><input id="setup_https" class="mono" placeholder="https://homestead.example.com" value="${esc(s.url || "")}">
        <button class="btn" onclick="setupHttpsCheck(this)">Check address</button></div></div>`}
      <p class="small dim">If Cloudflare Access protects the address, open it and sign in, then reopen this guide there. The server check cannot sign in through Access.</p>
      ${(s.tunnels || []).length ? `<div class="dim xs" style="margin-top:6px">Configured connectors: ${esc(s.tunnels.join(", "))}</div>` : ""}`,
    actions: () => [{ label: "Set up a Cloudflare Tunnel", run: "setupTunnel('cloudflare')", pri: true }, { label: "Set up Tailscale", run: "setupTunnel('tailscale')" }],
    more: "<p><b>Cloudflare Tunnel</b> needs a Cloudflare account and a domain on Cloudflare. Select Set up a Cloudflare Tunnel for the account, domain, connector and access steps.</p><p><b>Tailscale</b> needs a reusable auth key from its admin console. The devices must join the same tailnet. Turn on HTTPS certificates and set up <span class=\"mono\">tailscale serve</span> for an HTTPS address.</p>",
  },
  hostname: {
    title: "Hostname", lead: s => s.done ? `This browser opened Homestead at <span class="mono">${esc(s.host)}</span>.`
      : `This browser opened Homestead at <span class="mono">${esc(s.host)}</span>. A hostname makes the address easier to remember and supports HTTPS certificates.`,
    actions: () => [],
    more: "<p>Add a DNS record for the virtual IP on the router or local DNS server, for example homestead.lan, or use the tunnel's public hostname. Update the DNS record if the virtual IP changes.</p>",
  },
  lan: {
    title: "LAN networks",
    lead: () => "A LAN network gives a VM or app its own address on the local network. Set one up before creating apps and VMs that need direct LAN access. Apps that only need published ports can use a shared virtual IP instead.",
    body: s => `${UI.steps([
      { title: "Choose the host interface", detailHtml: "Choose the network card or bridge connected to the LAN. It must exist on every host that will run them." },
      { title: "Choose the network and addresses", detailHtml: "Use an untagged network for the usual LAN, or a VLAN set up on the switch. VMs can use the router’s DHCP. Reserve static addresses outside its DHCP range when assigning addresses by hand." },
      { title: "Create and test the network", detailHtml: "A host bridge works for both VMs and apps. Networks for only VMs or only apps are also possible. After saving, test an app’s address and connection before relying on it." },
    ])}<div class="about-grid"><div><span>VM networks</span><b>${esc((s.vms || []).join(", ") || "Not configured")}</b></div><div><span>Container networks</span><b>${esc((s.containers || []).join(", ") || "Not configured")}</b></div></div>`,
    actions: () => [{ label: "Configure LAN networks", run: "go('network', { params: { section: 'lan' } })", pri: true }],
    more: "<p>VM-only networks (macvtap) cannot be used by apps, and app-only networks (macvlan) cannot be used by VMs. With either, the host may not be able to reach its own VMs or apps directly. A host bridge avoids this. Creating a bridge can briefly interrupt the host's network, so review the checks on the network page before applying it.</p>",
  },
  disks: {
    title: "Disks for Longhorn", lead: s => s.done ? "No unused disks, apart from system disks, are reported."
      : `${(s.unused || []).length} unused disk${(s.unused || []).length === 1 ? " is" : "s are"} reported. Check each disk before giving it to Longhorn. Disk tags let storage classes choose HDD, SSD or NVMe disks.`,
    body: s => (s.unused || []).length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Disk</th><th>Host</th><th>Size</th><th></th></tr></thead><tbody>
      ${s.unused.map(d => `<tr><td><b class="mono">${esc(d.device)}</b> ${d.kind ? UI.chip(d.kind, "neutral") : ""}</td><td data-label="Node">${esc(d.node)}</td>
        <td class="mono" data-label="Size">${esc(d.size_gb ?? "?")} GiB</td>
        <td data-actions>${actionBar([{ label: "Set up", run: `nodeDetail(${jsq(d.node)});setTimeout(() => window.nodeSectionGo && nodeSectionGo('storage'), 900)`, need: "admin" }])}</td></tr>`).join("")}</tbody></table></div>` : "",
    actions: () => [],
  },
  storage: {
    title: "Default storage", lead: s => s.done ? `New volumes use <b>${esc(s.default)}</b> by default, with ${s.copies} cop${s.copies === 1 ? "y" : "ies"}.`
      : !s.default ? "Choose a default storage class, or choose a class each time a volume is created."
      : s.provisioner !== "driver.longhorn.io" ? `The default class, ${esc(s.default)}, uses a different storage provider. Review that provider's redundancy, or choose a Longhorn class.`
      : `The default class, ${esc(s.default)}, keeps ${s.copies} cop${s.copies === 1 ? "y" : "ies"}, and ${s.nodes} host${s.nodes === 1 ? " is" : "s are"} ready. ${s.copies > s.nodes ? "There are not enough ready hosts for that number of copies." : "More copies would improve redundancy."} The suggested number is ${s.target}.`,
    body: s => (window.Diagram && s.default ? Diagram.copies(Array.from({ length: s.nodes }, (_, i) => `host ${i + 1}`), s.copies || 1) : "")
      + '<div id="setupStorageSuggestions" aria-live="polite"><p class="small dim">Loading storage suggestions…</p></div>',
    actions: () => [{ label: "Custom storage class", run: "setupStorageCustom()" },
      { label: "Manage storage classes", run: "settingsTab('hardware');go('settings')" }],
    more: "<p>The suggestions use Longhorn V1, allow resizing and keep the data when a volume is removed. Copies go on different hosts, and a single copy gives no redundancy. Review a suggestion to change its settings before creating it. The SSD and HDD suggestions choose disks by their Longhorn tags, not by detecting the drive type.</p><p>Changing the default affects only new volumes. Existing storage classes and volumes keep their settings. To move an existing volume to another class, use Volumes.</p>",
  },
  smb: {
    title: "SMB", lead: s => s.error ? "Review the SMB server and share configuration in Settings and Network shares." : s.done ? "The SMB server is installed and turned on. Add or review shares, then check access from a computer on the LAN."
      : s.installed ? "The SMB server is installed but turned off. Turn it on in Settings to serve the network shares."
      : "Turn on SMB so that computers on the LAN can use network shares. Choose its address, then add a share.",
    body: s => s.installed ? UI.facts([["Server", s.enabled ? "Enabled" : "Off"], ["Configured shares", esc(s.shares ?? 0)],
      ["Address", s.address ? `<span class="mono">${esc(s.address)}</span>` : "Not reported"]]) : "",
    actions: () => [{ label: "Network shares", run: "go('shares')" },
      { label: "Configure SMB", run: "settingsTab('hardware');go('settings')", pri: true }],
    more: "<p>The SMB server is managed under Settings → Hardware and storage → Add-ons. Creating the first share can also install it. Network shares is where a volume is added and SMB users or guest access are chosen. Check the address and permissions before connecting a computer.</p>",
  },
  backups: {
    title: "Backups", lead: s => s.done ? "A backup destination is set up. Set a backup schedule and test a restore before relying on it." :
      "Choose the built-in backup storage or an S3 or NFS destination. Volume copies protect against a failed disk or host. Backups also need a separate destination to protect against losing the whole cluster or site.",
    actions: s => s.done ? [] : [{ label: "Set up backups", run: "go('protect');setTimeout(() => window.objectStoreSetup && objectStoreSetup(), 600)", pri: true }],
  },
  config: {
    title: "Settings backup", lead: s => s.done ? `Settings were exported ${esc(agoText(s.at))}. Export again after significant changes.` :
      "Export Homestead's users, network settings, IP records and portal configuration to an encrypted file. Keep the file and its passphrase available for recovery.",
    actions: () => [{ label: "Export settings", run: "settingsTab('homestead');go('settings');setTimeout(() => window.configBackup && configBackup(), 900)", pri: true }],
  },
  osupdates: {
    title: "OS updates", lead: s => s.done ? "The host OS update schedule is enabled. Review host status and recent update results." :
      "Schedule host OS updates in a maintenance window. Hosts update one at a time. Apps and VMs are moved off each host first, and the host restarts when needed.",
    actions: () => [{ label: "Review the update schedule", run: "osUpdates()", pri: true }],
  },
  appearance: {
    title: "Appearance", lead: () => "Choose a theme, page density and card or row layout. These preferences apply to this device.",
    actions: s => [{ label: "Open appearance", run: "settingsTab('you');go('settings')", pri: !s.done }, { label: s.done ? "Undo confirmation" : "Mark as done on this device", run: `setupLocalDone('appearance', ${!s.done})` }],
  },
  phone: {
    title: "Install the app", lead: s => s.installed ? "This browser is running Homestead as an installed app." : s.secure
      ? "Install Homestead for a home-screen icon, a full-screen view and notifications on supported devices."
      : "Open Homestead over HTTPS to install it as an app. Set up HTTPS in the Access section first.",
    body: s => `<div class="tblwrap"><table class="tbl stack dense"><tbody>
      <tr><td><b>iPhone and iPad</b></td><td class="small">In Safari, choose Share, then Add to Home Screen. Notifications need iOS 16.4 or later, with the app opened from the home screen.</td></tr>
      <tr><td><b>Android</b></td><td class="small">In Chrome: the menu, then Install app.</td></tr>
      <tr><td><b>A computer</b></td><td class="small">In Chrome or Edge: the install icon in the address bar.</td></tr></tbody></table></div>`,
    actions: s => s.installed ? [] : s.secure ? [{ label: "Install now", run: "pwaInstall()", pri: true }] : [{ label: "Get HTTPS", run: "setupOpen('https')", pri: true }],
  },
  notifications: {
    title: "Notifications", lead: s => s.done ? "At least one notification device is registered for this account. Send a test from Settings to confirm delivery." : !s.secure
      ? "Open Homestead over HTTPS before enabling notifications on this device."
      : s.permission === "denied" ? "This browser blocks notifications. Allow them in the browser's site settings, then return here."
      : "Get alerts for failed disks, unavailable hosts and waiting updates, and choose which notifications to get.",
    actions: s => s.done ? [{ label: "Choose notification types", run: "pwaCategories()" }] : s.secure && s.permission !== "denied" ? [{ label: "Enable notifications", run: "pwaEnable().then(() => viewSetup())", pri: true }] : [],
  },
  people: {
    title: "Users", lead: s => s.done ? "At least two administrator accounts exist." :
      "Add a second administrator to reduce the risk of losing access. Give other people their own operator or viewer accounts with the permissions they need.",
    actions: () => [{ label: "Manage users", run: "settingsTab('access');go('settings')", pri: true }],
  },
  unifi: {
    title: "UniFi", lead: s => s.done ? "A UniFi Network address is set up. Check the connection in Settings." :
      "Connect UniFi Network to import devices and address reservations, and to help avoid address clashes when choosing virtual IPs.",
    actions: s => s.done ? [] : [{ label: "Connect UniFi", run: "settingsTab('connections');go('settings')", pri: true }],
  },
  ipam: {
    title: "IP addresses",
    lead: s => s.done ? "The subnets are added and scanned. Networking › IP addresses shows what each address is used for, and Homestead suggests free addresses for virtual IPs and VMs."
      : "Keep track of the LAN's addresses. Add the subnets with their DHCP ranges, import what UniFi knows, and scan to see which addresses are in use. Homestead then warns about clashes and suggests free addresses for virtual IPs and VMs.",
    body: s => {
      const subnets = s.subnets || [];
      const unscanned = subnets.filter(n => !n.scanned);
      const stage = !subnets.length ? 0 : s.unifi && !s.synced ? 1 : unscanned.length ? 2 : 3;
      return UI.steps([
        { title: "Add the subnets", detailHtml: subnets.length
          ? `${subnets.map(n => `<span class="mono">${esc(n.cidr)}</span>${n.name ? ` ${esc(n.name)}` : ""}`).join(", ")}. Add each subnet's gateway and DHCP range so addresses inside it are not offered as static ones.`
          : "Add each subnet, up to /22, with its gateway and DHCP range. The subnets the hosts are on are suggested." },
        { title: "Sync from UniFi", detailHtml: !s.unifi ? "Optional. Connect UniFi in the previous step to import its networks, devices and DHCP reservations."
          : s.synced ? `Synced ${esc(fmtAgo(Math.max(1, Math.round(Date.now() / 1000 - s.synced))))}. Sync again after changing reservations in UniFi.`
          : "Bring in UniFi's networks, devices and DHCP reservations." },
        { title: "Scan for devices", detailHtml: !subnets.length ? "After adding a subnet, scan it to find the devices on it."
          : unscanned.length ? `${unscanned.length} subnet${unscanned.length === 1 ? " has" : "s have"} not been scanned yet. A scan takes a minute or two and runs in the background.`
          : "Every subnet has been scanned. Scan again from the page whenever devices change." },
      ], stage);
    },
    actions: s => {
      const subnets = s.subnets || [];
      const unscanned = subnets.filter(n => !n.scanned);
      const open = "go('network', { params: { tab: 'ip' } })";
      return [
        { label: subnets.length ? "Edit subnets" : "Add subnets", run: `${open};setTimeout(() => window.ipamSubnets && ipamSubnets(), 900)`, pri: !subnets.length },
        s.unifi ? { label: "Sync UniFi", run: `${open};setTimeout(() => window.ipamSync && ipamSync(), 900)`, pri: subnets.length && !s.synced } : null,
        subnets.length ? { label: unscanned.length ? "Scan subnets" : "Scan again",
          run: `${open};setTimeout(() => ${JSON.stringify((unscanned.length ? unscanned : subnets).map(n => n.id))}.forEach(id => window.ipamScan && ipamScan(id)), 900)`,
          pri: !!unscanned.length && (!s.unifi || !!s.synced) } : null,
      ].filter(Boolean);
    },
  },
  unraid: {
    title: "Unraid imports", lead: s => s.done ? "An import server has been added. Check its connection before importing apps or VMs." :
      "Add an Unraid server to import apps with their settings and appdata, or VMs with their disks. Check the settings before starting an import.",
    actions: s => s.done ? [{ label: "Import containers", run: "go('imports')" }, { label: "Import VMs", run: "go('vmimport')" }] : [{ label: "Add your Unraid server", run: "go('imports');setTimeout(() => window.srcAdd && srcAdd(), 700)", pri: true }],
  },
  homeassistant: {
    title: "Home Assistant", lead: s => s.done ? "A valid API key exists. Set up the integration in Home Assistant and test the connection." :
      "Create an API key so that Home Assistant can read the cluster's status or control apps and VMs. Limit its permissions and allowed addresses to what the integration needs.",
    actions: () => [{ label: "Create an API key", run: "settingsTab('access');go('settings');setTimeout(() => window.apiKeyNew && apiKeyNew(), 900)", pri: true }],
  },
  linked: {
    title: "Linked clusters", lead: s => s.done ? "Another cluster is linked. Review its connection and access from Settings." :
      "Link another Homestead cluster to view its pages and move apps between clusters.",
    actions: s => s.done ? [] : [{ label: "Link a cluster", run: "settingsTab('fleet');go('settings')" }],
  },
  starter: {
    title: "Applications", lead: s => s.done ? "An app other than Homestead has been added. Check its status on the Containers page." :
      "Browse the App Store to deploy an app. An uptime monitor or a service dashboard is a good first app.",
    actions: () => [{ label: "Uptime monitor", run: "setupStore('uptime-kuma')", pri: true }, { label: "Dashboard", run: "setupStore('homepage')" }, { label: "Open the App Store", run: "go('store')" }],
  },
  console: {
    title: "Host console", lead: s => s.done ? "The host console is enabled. Review each host's installation status in Settings." :
      "Show CPU, memory, disks, addresses and cluster status on each host's own screen. The console can be closed to reach the sign-in prompt.",
    actions: () => [{ label: "Review the host console", run: "settingsTab('hardware');go('settings')", pri: true }],
  },
};

/* Suggestions are recipes to review, never writes made by opening the guide. */
const setupStorageInventory = new WeakMap();
function setupStorageHosts(inv, tags) {
  if (!inv) return null;
  return Object.values(inv.nodes || {}).filter(disks => disks.some(d => (d.longhorn || []).some(x =>
    x.scheduling && x.ready !== false && !x.missing && !x.failed && x.type !== "block"
      && tags.every(t => (x.tags || []).includes(t))))).length;
}
function setupStorageRecipes(s, classes, inv) {
  const target = Math.max(1, Math.min(3, s.target || 1));
  const sameTags = (actual, wanted) => actual.length === wanted.length && wanted.every(t => actual.includes(t));
  const specs = [1, 2, 3].map(replicas => ({ id: `r${replicas}`, title: `${replicas} replica${replicas === 1 ? "" : "s"}`, replicas, disk_tags: [] }));
  for (const tag of ["ssd", "hdd"]) {
    const hosts = setupStorageHosts(inv, [tag]);
    specs.push({ id: tag, title: `${tag.toUpperCase()} disks`, replicas: hosts === null ? target : Math.max(1, Math.min(target, hosts)), disk_tags: [tag] });
  }
  return specs.map(spec => {
    const matches = classes.filter(c => !c.internal && !c.made_for && c.provisioner === "driver.longhorn.io"
      && +c.replicas === spec.replicas && (c.engine || "v1") === "v1" && !c.migratable && !c.encrypted
      && c.reclaim === "Retain" && c.expandable && !(c.node_tags || []).length
      && c.parameters?.replicaSoftAntiAffinity !== "enabled" && sameTags(c.disk_tags || [], spec.disk_tags));
    const match = matches.find(c => c.default) || matches[0];
    const base = `longhorn-${spec.disk_tags[0] || spec.id}`;
    let name = base;
    if (classes.some(c => c.name === name)) name = `${base}-containers`;
    for (let i = 2; classes.some(c => c.name === name); i++) name = `${base}-containers-${i}`;
    return { ...spec, name: match?.name || name, existing: match || null, hosts: setupStorageHosts(inv, spec.disk_tags),
      recommended: s.target > 0 && !spec.disk_tags.length && spec.replicas === target,
      copies: "hosts", reclaim_policy: "Retain", engine: "v1", expandable: true, migratable: false,
      node_tags: [], default: s.target > 0 && !s.default && !classes.some(c => c.default) && !spec.disk_tags.length && spec.replicas === target };
  });
}
function setupStorageSuggestionsHtml(recipes, classes = []) {
  return '<p class="small dim">Choose a suggestion to review. Every setting can be changed before the class is created.</p>'
    + homesteadClassLine(classes)
    + '<div class="setup-storage-grid">' + recipes.map((r, i) => {
      const status = r.hosts === null ? "Disk availability could not be checked. Check the disk tags before creating."
        : !r.hosts ? (r.disk_tags.length ? `No usable V1 disk has the ${r.disk_tags[0]} tag. Tag disks first, or change the tag in the editor.` : "No usable V1 disks are reported.")
        : `${r.hosts} suitable host${r.hosts === 1 ? "" : "s"} reported.` + (r.hosts < r.replicas ? ` This is fewer than ${r.replicas}, so volumes may have fewer copies than asked for. Change the disk tags or the number of copies.` : "");
      const action = r.existing ? r.existing.default ? UI.chip("Current default", "ok")
        : UI.button("Make default", `setupStorageDefault(${i}, this)`)
        : UI.button("Review and create", `setupStorageReview(${i})`);
      return `<section class="setup-storage-recipe" data-storage-recipe="${r.id}">
        <div class="row"><b>${esc(r.title)}</b>${r.recommended ? UI.chip("Suggested", "ok") : ""}</div>
        <span class="mono small setup-storage-name">${esc(r.name)}</span>
        <p class="small">${r.replicas} replica${r.replicas === 1 ? " · no redundancy" : "s · different hosts"}${r.disk_tags.length ? ` · ${esc(r.disk_tags[0])} tag` : ""}</p>
        <p class="xs ${r.hosts !== null && r.hosts < r.replicas ? "badtext" : "dim"}">${esc(status)}</p>
        ${r.existing ? '<p class="xs dim">A matching class already exists.</p>' : ""}<div class="setup-storage-action">${action}</div>
      </section>`;
    }).join("") + '</div>' + UI.button("Refresh suggestions", "setupStorageLoad(true)");
}
window.setupStorageLoad = async (force = false) => {
  const state = STATE.data.setup, host = $("#setupStorageSuggestions");
  if (!state || !host || STATE.view !== "setup") return;
  let entry = setupStorageInventory.get(state);
  if (!entry || force) {
    entry = { promise: Promise.allSettled([api("/api/storage/classes"), api("/api/disks")]) };
    setupStorageInventory.set(state, entry);
  }
  const [classes, disks] = await entry.promise;
  if (STATE.data.setup !== state || STATE.view !== "setup" || $("#setupStorageSuggestions") !== host || setupStorageInventory.get(state) !== entry) return;
  if (classes.status === "rejected") {
    host.innerHTML = `<p class="small badtext">The storage classes could not be loaded. ${esc(classes.reason?.message || "")}</p>` + UI.button("Try again", "setupStorageLoad(true)");
    return;
  }
  entry.recipes = setupStorageRecipes(state.steps.storage || {}, classes.value, disks.status === "fulfilled" ? disks.value : null);
  host.innerHTML = setupStorageSuggestionsHtml(entry.recipes, classes.value);
  if (window.applyRole) applyRole();
};
async function setupStorageRefresh() {
  if (STATE.view === "setup") await viewSetup();
  else await storageClassesPaint();
}
window.setupStorageReview = i => {
  const recipe = setupStorageInventory.get(STATE.data.setup)?.recipes?.[i];
  if (recipe && !recipe.existing) return storageClassCreate(recipe, setupStorageRefresh);
};
window.setupStorageCustom = () => storageClassCreate({}, setupStorageRefresh);
window.setupStorageDefault = async (i, button) => {
  const recipe = setupStorageInventory.get(STATE.data.setup)?.recipes?.[i];
  if (!recipe?.existing || recipe.existing.default) return;
  if (button) button.disabled = true;
  try {
    const result = await storageClassDefault(recipe.name);
    if (result) await setupStorageRefresh();
  } finally { if (button) button.disabled = false; }
};

/* ---------------- the page ---------------- */
async function viewSetup() {
  let state;
  try { state = await api("/api/setup"); }
  catch (e) { paint(UI.error(e, { retry: "viewSetup()" })); return; }
  state.steps = setupFacts(state);
  STATE.data.setup = state;
  setupOffer(state);
  const chapters = setupVisible(state);
  const ids = chapters.flatMap(([, list]) => list);
  const status = Object.fromEntries(ids.map(id => [id, setupStatus(id, state.steps, state.skips)]));
  // A step skipped by choice is dealt with: it counts, as a done one does.
  const done = ids.filter(id => status[id] === "done" || status[id] === "skipped").length;
  const wanted = new URLSearchParams(location.search).get("step");
  const open = ids.includes(wanted) ? wanted : "intro";
  const mark = (s, id) => `<span class="setup-mark ${s}${id === "appearance" && s === "done" ? " confirmed" : ""}" aria-hidden="true">${s === "done" || s === "skipped" ? "✓" : s === "attention" ? "!" : ""}</span>`;
  const nav = UI.workspaceNav([{key:"intro",label:"Start here",markerHtml:mark("todo")}, ...chapters.flatMap(([title,list],c) => list.map(id => ({
    key:id,label:SETUP_STEPS[id].title,group:`${c + 1} · ${title}`,markerHtml:mark(status[id],id),
    ariaLabel:SETUP_STEPS[id].title + ': ' + setupStatusLabel(id,status[id],state.steps[id])
  })))], {label:"Setup steps",selected:open,guide:true,onSelect:key => `setupOpen(${jsArg(key)})`});
  paint(`${UI.pageHeader(`Setup`, `${done} of ${ids.length} steps complete or skipped${state.admin ? "" : " · your preferences"}${state.completed ? " · guide completed" : ""}`, `<span class="setup-meter" aria-hidden="true"><span style="width:${ids.length ? Math.round(done / ids.length * 100) : 0}%"></span></span>
        ${actionBar([{ label: state.hidden || state.completed ? "Show reminders again" : "Don’t show again", run: `setupReminders(${!(state.hidden || state.completed)})` }])}`)}
    ${UI.workspace(nav, setupStepHtml(open, state, status, ids), {id:"setupPage",guide:true,contentId:"setupStep",pickerHtml:`<label class="setup-picker">Setup step<select id="setupSelect" onchange="setupOpen(this.value)"><option value="intro"${open === "intro" ? " selected" : ""}>Start here</option>${chapters.map(([title, list]) => `<optgroup label="${esc(title)}">${list.map(id => `<option value="${id}"${id === open ? " selected" : ""}>${esc(SETUP_STEPS[id].title)} · ${esc(setupStatusLabel(id, status[id], state.steps[id]))}</option>`).join("")}</optgroup>`).join("")}</select></label>`})}`);
  if (open === "storage") setupStorageLoad();
}
window.viewSetup = viewSetup;

function setupStepHtml(id, state, status, ids) {
  if (id === "intro" || !ids.includes(id)) return setupIntroHtml(ids, status);
  if (id === "https" && setupCloudflareStage() !== null) return setupCloudflareHtml(state);
  const step = SETUP_STEPS[id], s = state.steps[id] || {}, st = status[id];
  const next = ids[ids.indexOf(id) + 1];
  const actions = (step.actions(s) || []).filter(Boolean);
  // One primary, last on the right: the step's main action, or else Next.
  // Its other actions wait in a menu, so the row fits a phone.
  const mainAction = actions.find(a => a.pri);
  const main = mainAction ? UI.button(mainAction.label, mainAction.run, { kind: "pri" }) : "";
  const others = actions.filter(a => a !== mainAction).map(a => ({ label: a.label, run: a.run }));
  const rest = others.length ? actionBar(others, { shown: 0, label: `More for ${step.title}` }) : "";
  const onward = next ? UI.button(`Next: ${SETUP_STEPS[next].title}`, `setupOpen(${jsArg(next)})`, { kind: main ? "" : "pri" })
    : UI.button("Finish guide", "setupFinish()", { kind: main ? "" : "pri" }) + tip("Finish guide stops the reminders. It does not mark unfinished checks as passed.");
  const skip = st === "done" ? "" : st === "skipped"
    ? UI.button("Undo skip", `setupSkip(${jsArg(id)}, false)`) : UI.button("Skip this step", `setupSkip(${jsArg(id)}, true)`);
  return `<section class="card flat setup-card" data-step="${id}">
      ${UI.moduleHeader(`${esc(step.title)}`, `${UI.chip(setupStatusLabel(id, st, s), st === "done" && id !== "appearance" ? "ok" : st === "attention" ? "warn" : "")}`, ``)}
      <p class="small dim">${esc(SETUP_CHECKS[id])}${st === "skipped" ? " A skipped step is not checked." : ""}</p>
      ${UI.lead(step.lead(s))}
      ${s.error ? UI.callout("warn", "Homestead could not check this", esc(s.error)) : ""}
      ${step.body ? `<div class="setup-body">${step.body(s)}</div>` : ""}
      ${step.more ? UI.more("More information", step.more) : ""}
      ${UI.actions(rest + onward + main, skip)}
    </section>`;
}

window.setupOpen = id => {
  const state = STATE.data.setup;
  if (STATE.view !== "setup" || !state) return go("setup", { params: { step: id } });
  const visible = setupVisible(state).flatMap(([, list]) => list);
  if (id !== "intro" && !visible.includes(id)) id = "intro";
  const url = new URL(location.href);
  url.searchParams.set("step", id);
  history.replaceState(history.state, "", url.pathname + url.search);
  const ids = setupVisible(state).flatMap(([, list]) => list);
  const status = Object.fromEntries(ids.map(x => [x, setupStatus(x, state.steps, state.skips)]));
  $("#setupStep").innerHTML = setupStepHtml(id, state, status, ids);
  if ($("#setupSelect")) $("#setupSelect").value = id;
  $$("#setupPage .setup-step").forEach(b => {
    b.classList.toggle("on", b.dataset.step === id);
    if (b.dataset.step === id) b.setAttribute("aria-current", "step"); else b.removeAttribute("aria-current");
  });
  if (matchMedia("(max-width: 900px)").matches) $("#setupStep").scrollIntoView({ block: "start" });
  $("#setupStep .stepper-chip.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  if (window.applyRole) applyRole();
  if (id === "storage") setupStorageLoad();
};

window.setupSkip = async (step, skip) => {
  try {
    await api("/api/setup/skip", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step, skip }) });
  } catch (e) { return toast(e.message, "bad"); }
  await viewSetup();
  setupOpen(step);
};

window.setupReminders = async hidden => {
  try {
    await api("/api/setup/hide", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hidden }) });
    if (!hidden) await api("/api/setup/complete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ completed: false }) });
    toast(hidden ? "Setup reminders stopped. The book icon still opens the guide." : "Setup reminders turned on", "ok");
    await viewSetup();
  } catch (e) { toast(e.message, "bad"); }
};

window.setupFinish = async () => {
  try {
    await api("/api/setup/complete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ completed: true }) });
    await welcomeCheck();
    toast("Guide completed. It can be opened again at any time from the book icon.", "ok");
    go("dash");
  } catch (e) { toast(e.message, "bad"); }
};
window.setupLocalDone = (id, done = true) => { if (id !== "appearance") return; setupLocal(id, done ? "1" : "0"); viewSetup().then(() => setupOpen(id)); };

window.setupHttpsCheck = async button => {
  const url = $("#setup_https").value.trim();
  button.disabled = true; button.textContent = "Checking…";
  try {
    const r = await api("/api/setup/https-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    toast(`${r.url} reaches Homestead over HTTPS`, "ok");
    await viewSetup(); setupOpen("https");
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.textContent = "Check"; }
};

// Remember only the guide position in this tab, never account details or tokens.
let setupCloudflareMemory = null;
function setupCloudflareStage(value) {
  const key = `homestead.setup.cloudflare.${setupDemo() ? "demo" : "live"}`;
  try {
    if (value === undefined) {
      const saved = sessionStorage.getItem(key);
      return /^[0-5]$/.test(saved || "") ? Number(saved) : null;
    }
    setupCloudflareMemory = value;
    if (value === null) sessionStorage.removeItem(key);
    else if (Number.isInteger(value) && value >= 0 && value <= 5) sessionStorage.setItem(key, String(value));
  } catch (_) { if (value === undefined) return setupCloudflareMemory; setupCloudflareMemory = value; }
  return value;
}

window.setupCloudflareOpen = stage => { setupCloudflareStage(stage); setupOpen("https"); };

function setupCloudflareHtml(state) {
  const stage = setupCloudflareStage() ?? 0;
  const link = (url, label) => `<a class="linkish" href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  const dashboard = link("https://dash.cloudflare.com/", "Open Cloudflare");
  const origin = state.steps.address?.service_url || state.steps.address?.url || "";
  const steps = [
    { title: "Account and domain", lead: "This needs a Cloudflare account and a domain. Cloudflare's Free plan is enough. Registering or renewing a domain is a separate cost.",
      body: `<ol><li>Create an account or sign in to Cloudflare.</li><li>Add the domain and choose the Free plan. Check its DNS records, including mail records.</li><li>At the domain registrar, replace the nameservers with the ones Cloudflare gives. Wait until the domain shows as Active.</li></ol><p>${dashboard} · ${link("https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/", "Domain setup instructions")}</p>` },
    { title: "Create the tunnel", lead: "Create a tunnel and copy its connector token. Homestead runs the connector.",
      body: `<ol><li>In Cloudflare, open Networking › Tunnels and create a tunnel named Homestead.</li><li>Under Setup Environment, choose Docker.</li><li>Copy only the token after <span class="mono">--token</span> in the command. Keep it private, and paste it into the deployment form in the next step.</li></ol><p>${dashboard} · ${link("https://developers.cloudflare.com/tunnel/get-started/", "Tunnel setup instructions")}</p>` },
    { title: "Deploy the connector", lead: "Open the prepared deployment, paste the tunnel token and review the settings before deploying.",
      body: "<p>Use Return to setup to come back here. In Cloudflare, wait for the tunnel to show Healthy. The connector needs outgoing internet access on port 7844. No port forwarding is needed on the router.</p>",
      action: UI.button("Open connector deployment", "setupTunnelDeploy('cloudflare')", { kind: "pri" }) },
    { title: "Control access", lead: "Set up Cloudflare Access before publishing the address, to limit who can reach Homestead.",
      body: `<ol><li>Open Zero Trust and choose the Free plan if asked. Cloudflare may request payment details for account setup.</li><li>Go to Access controls › Applications. Create a Self-hosted and private application, and add the public address to use, for example <span class="mono">homestead.example.com</span>.</li><li>Add an Allow policy with the email addresses that may sign in. One-time PIN lets those users sign in with a code sent by email.</li></ol><p>Users still sign in to Homestead with their Homestead account.</p><p>${link("https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/", "Access setup instructions")} · ${link("https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/", "Email sign-in instructions")}</p>${UI.more("Additional protection", `<p>Homestead can also validate Cloudflare Access tokens. See the ${link("https://github.com/homestead-lab/homestead/blob/main/docs/reference.md#publishing-through-a-cloudflare-tunnel", "deployment security guide")} for the required settings.</p>`)}` },
    { title: "Publish the hostname", lead: "Add a route from the public address to Homestead's address inside the cluster.",
      body: `<ol><li>In Networking › Tunnels, choose the tunnel. Under Routes, add a Published application.</li><li>Use the same address as the Access application.</li><li>Set the Service URL to ${origin ? `<span class="mono">${esc(origin)}</span>` : "the address and port shown in Cluster address"}, then save the route.</li></ol>
        ${origin ? "" : `<p>Set up Homestead's cluster address before publishing the route. ${UI.button("Review cluster address", "setupOpen('address')")}</p>`}
        <p>The connector must be able to reach this address. Use the address above, including its port. The cluster's virtual IP (VIP) also works. <span class="mono">localhost</span> means the connector itself.</p>` },
    { title: "Test in a browser", lead: "Open the new HTTPS address, sign in through Cloudflare Access, then sign in to Homestead.",
      body: "<p>Open this guide again from the book icon at the new address. The HTTPS step checks that the browser uses HTTPS. Also test from a device outside the home network.</p><p>The server's address check cannot sign in through Cloudflare Access, so use the browser test when Access is on.</p>",
      action: UI.button("Return to HTTPS overview", "setupCloudflareOpen(null)", { kind: "pri" }) },
  ];
  const current = steps[stage];
  return `<section class="card flat setup-card" data-step="https" data-cloudflare-stage="${stage}">
    ${UI.moduleHeader(`Set up Cloudflare Tunnel`, `Step ${stage + 1} of ${steps.length} · ${esc(current.title)}`, ``)}
    ${UI.sectionNavigation("cloudflare-guide", steps, {current:stage, guide:true, onSelect:key => `setupCloudflareOpen(${Number(key)})`, onChange:"setupCloudflareOpen(Number(this.value))"})}
    ${UI.lead(current.lead)}<div class="setup-body small">${current.body}</div>
    <p class="small dim">These steps describe changes in Cloudflare. Homestead does not check them or mark them complete when Next is chosen.</p>
    ${UI.actions((current.action || "") + (stage > 0 ? UI.button("Back", `setupCloudflareOpen(${stage - 1})`, { attrs: 'data-dialog-dismiss="true"' }) : "") + (stage < steps.length - 1 ? UI.button(`Next: ${steps[stage + 1].title}`, `setupCloudflareOpen(${stage + 1})`) : ""), UI.button("Back to HTTPS options", "setupCloudflareOpen(null)"))}
  </section>`;
}

window.setupTunnel = kind => kind === "cloudflare" ? setupCloudflareOpen(0) : setupTunnelDeploy(kind);

// The Deploy page provides its normal review and capacity checks.
window.setupTunnelDeploy = kind => {
  if (kind === "cloudflare") setupCloudflareStage(2);
  const pre = kind === "cloudflare"
    ? { name: "cloudflared", image: "cloudflare/cloudflared:latest", args: ["tunnel", "--no-autoupdate", "run"], network_mode: "internal",
        cpu: "50m", memory: "64Mi", env: { TUNNEL_TOKEN: "" },
        env_meta: [{ key: "TUNNEL_TOKEN", label: "Tunnel connector token (from Cloudflare Networking › Tunnels)", required: true, masked: true }] }
    // Its state kept in the container, not a Kubernetes Secret it may not
    // write: a reusable auth key joins it again after a restart.
    : { name: "tailscale", image: "tailscale/tailscale:latest", network_mode: "internal", cpu: "50m", memory: "128Mi",
        env: { TS_AUTHKEY: "", TS_HOSTNAME: "homestead", TS_USERSPACE: "true", TS_KUBE_SECRET: "", TS_STATE_DIR: "/tmp/tailscale" },
        env_meta: [{ key: "TS_AUTHKEY", label: "Auth key, reusable (from the Tailscale admin console › Settings › Keys)", required: true, masked: true }] };
  window.__deployPrefill = pre;
  go("deploy");
};

window.setupStore = q => { STATE.q = q; go("store", { keepSearch: true }); };

/* ---------------- opening it ---------------- */
// After an administrator signs in, the guide opens itself until it is
// finished or hidden.
// The live demo never opens it: the top bar offers it instead.
window.welcomeCheck = async (force = false) => {
  if (force) return go("setup");
  let state;
  try { state = await api("/api/setup"); } catch (e) { return; }
  setupOffer(state);
  // At each sign-in, for an administrator, until the guide is finished or
  // they choose Don't show again - and only over the Dashboard, never over a
  // page a link asked for.
  if (setupDemo() || !state.admin || state.hidden || state.completed || STATE.view !== "dash") return;
  try { await api("/api/setup/opened", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { return; }
  if (STATE.view === "dash") go("setup");
};

function setupOffer(state = STATE.data.setup) {
  const button = $("#setupbtn");
  if (!button) return;
  button.classList.remove("hidden");
  button.classList.toggle("pulse", !state?.hidden && !state?.completed);
  button.onclick = () => go("setup");
}
window.setupOffer = setupOffer;

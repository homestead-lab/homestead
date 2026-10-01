/* The setup guide: /setup, laid out as Settings is - chapters and their steps
   down the side, one step open beside them; a list on a phone. Each step says
   in two lines why it matters, shows the cluster as it is (a diagram where
   the idea is spatial), and offers one thing to do. Homestead decides what is
   done by looking, every time, so a step done and since undone shows again;
   only skipping is remembered. Admins see every chapter; everyone sees the
   steps that are theirs - how it looks, their phone, their notifications.

   It opens itself once, the first time an administrator signs in. On the
   live demo it never does: a pulsing button in the top bar offers it. */

const SETUP_CHAPTERS = [
  ["Is it healthy?", ["health", "quorum", "probe", "clocks"]],
  ["Reach it", ["address", "https", "hostname"]],
  ["Keep data safe", ["disks", "storage", "backups", "config", "osupdates"]],
  ["Make it yours", ["appearance", "phone", "notifications", "people"]],
  ["Connect", ["unifi", "unraid", "homeassistant", "linked"]],
  ["First apps", ["starter", "console"]],
];
const SETUP_PERSONAL = ["appearance", "phone", "notifications"];
const setupDemo = () => window.HOMESTEAD_DEMO === true || new URLSearchParams(location.search).get("demo") === "1";
const setupLocal = (key, value) => {
  try {
    if (value === undefined) return localStorage.getItem(`homestead.setup.${key}`);
    localStorage.setItem(`homestead.setup.${key}`, value);
  } catch (e) { /* this visit only */ }
  return null;
};
const isAddress = host => /^\d+\.\d+\.\d+\.\d+$/.test(host) || host.includes(":") || host === "localhost";

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

function setupVisible(state) {
  return SETUP_CHAPTERS.map(([title, ids]) => [title, ids.filter(id => (state.admin || SETUP_PERSONAL.includes(id))
    && (state.steps[id] || {}).applies !== false)]).filter(([, ids]) => ids.length);
}

/* ---------------- the steps ---------------- */
const SETUP_STEPS = {
  health: {
    title: "Cluster check", lead: s => s.done ? "Every node, workload and attached volume is healthy." :
      "Something needs a look before the rest: a node, a workload or a volume that is not healthy.",
    body: s => s.done ? "" : `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>What</th><th>How bad</th><th>Why</th></tr></thead><tbody>
      ${(s.issues || []).map(i => `<tr><td><b>${esc(i.name || i.kind)}</b><div class="dim xs">${esc(i.kind)}</div></td>
        <td data-label="How bad"><span class="pill ${i.severity === "critical" ? "crit" : "med"}">${esc(i.severity)}</span></td>
        <td data-label="Why" class="small">${esc(i.reason)}</td></tr>`).join("")}</tbody></table></div>`,
    actions: () => [{ label: "Open the cluster check", run: "go('cluster')", pri: true }],
    more: "<p>The same checks run on the Cluster page and raise alerts in the bell. Fixing one here is fixing it there.</p>",
  },
  quorum: {
    title: "Nodes and quorum", lead: s => s.servers === 2
      ? "Kubernetes carries on while more than half its servers agree. With two, losing either stops the cluster, so the second adds risk and no safety until there is a third."
      : s.servers >= 3 ? `With ${s.servers} servers, ${Math.max(0, s.servers - (Math.floor(s.servers / 2) + 1))} can fail and the cluster carries on.`
      : "One server: simple and fine for a lab, with nothing to fail over to. A third server later gives it room to lose one.",
    body: s => window.Diagram ? Diagram.quorum((s.members?.length ? s.members : (s.nodes || []).map(n => n.name)).map(name =>
      ({ name, ready: (s.ready || []).includes(name) || (s.nodes || []).some(n => n.name === name && n.ready) }))) : "",
    actions: () => [{ label: "Add a node", run: "platformJoinGuide()", pri: true }],
    more: "<p>A second machine can join as a worker instead of a server: it runs apps and holds Longhorn copies, without a vote. Then the cluster is no more fragile than with one server.</p>",
  },
  probe: {
    title: "Node probe", lead: () => "Temperatures, drive health and each host's devices and network cards. It runs a small privileged pod on every node.",
    actions: s => s.done ? [] : [{ label: "Install the node probe", run: "probeInstallConfirm()", pri: true }],
  },
  clocks: {
    title: "Clocks in sync", lead: s => s.done ? "Every node's clock is kept in time." :
      `Certificates and etcd fail on clocks that drift. Not in sync: ${esc((s.unsynced || []).join(", ") || "unknown")}.`,
    actions: () => [{ label: "Open Nodes", run: "go('nodes')", pri: true }],
    more: "<p>Each host's page shows its OS, and the installer's doctor turns on time synchronisation where it is off.</p>",
  },
  address: {
    title: "An address", lead: s => s.done ? `Homestead is at <span class="mono">${esc(s.url)}</span>, on a VIP that moves to another node if one goes down. New apps share it, each on its own port.`
      : s.harvester ? "Harvester gave Homestead its address when it was installed."
      : "A VIP moves to another node when one goes down; a node's own address does not. Reserve one address outside your router's DHCP range: Homestead and new apps share it.",
    body: s => window.Diagram ? Diagram.vip(s.done ? (s.url || "").replace(/^https?:\/\//, "").replace(/:\d+$/, "") : "",
      [{ port: (s.url || "").match(/:(\d+)$/)?.[1] || "8080", app: "Homestead" }]) : "",
    actions: s => s.done ? [] : ["kube-vip", "metallb"].includes(s.load_balancer)
      ? [s.vips ? { label: "Put Homestead on a VIP", run: "go('network');setTimeout(() => window.selfAddressMove && selfAddressMove(), 800)", pri: true }
        : { label: "Add a VIP", run: "go('network');setTimeout(() => window.vipAdd && vipAdd(), 800)", pri: true }]
      : [{ label: "Install kube-vip", run: "settingsTab('hardware');go('settings')", pri: true }],
  },
  https: {
    title: "From anywhere, over HTTPS", lead: s => s.done
      ? `Homestead answers over HTTPS${s.url ? ` at <span class="mono">${esc(s.url)}</span>` : ""}, so a phone can install it and get notifications.`
      : "A phone installs Homestead as an app and gets notifications only over HTTPS. A Cloudflare Tunnel gives it an HTTPS name with no ports opened at home; Tailscale reaches it privately from your own devices.",
    body: s => `${window.Diagram ? Diagram.remote(s.done ? (s.tunnels?.length ? "your tunnel" : "HTTPS") : "", s.url || (s.here ? location.origin : ""), location.host) : ""}
      ${s.done && s.url ? "" : `<div class="ui-field" style="margin-top:10px"><label>Already have one? Check it reaches Homestead</label>
        <div class="row" style="gap:8px;flex-wrap:nowrap"><input id="setup_https" class="mono" placeholder="https://homestead.example.com" value="${esc(s.url || "")}">
        <button class="btn" onclick="setupHttpsCheck(this)">Check</button></div></div>`}
      ${(s.tunnels || []).length ? `<div class="dim xs" style="margin-top:6px">Running: ${esc(s.tunnels.join(", "))}</div>` : ""}`,
    actions: () => [{ label: "Set up a Cloudflare Tunnel", run: "setupTunnel('cloudflare')", pri: true }, { label: "Set up Tailscale", run: "setupTunnel('tailscale')" }],
    more: `<p><b>Cloudflare Tunnel</b>: make a tunnel in Cloudflare's Zero Trust dashboard (Networks › Tunnels), point a public hostname at
      <span class="mono">${esc(location.host)}</span>, and copy its token. Homestead runs the connector. Put Cloudflare Access in front of it if anyone outside should be kept out.</p>
      <p><b>Tailscale</b>: an auth key from the admin console's Keys page. The node joins your tailnet, so your own devices reach Homestead privately; for HTTPS with a name, turn on HTTPS certificates in Tailscale and use <span class="mono">tailscale serve</span>.</p>`,
  },
  hostname: {
    title: "A name", lead: s => s.done ? `You are using a name, <span class="mono">${esc(s.host)}</span>, rather than an address.`
      : `You opened Homestead at an address, <span class="mono">${esc(s.host)}</span>. A name survives the address changing, and HTTPS needs one.`,
    actions: () => [],
    more: "<p>Give the VIP a name on your router or local DNS (homestead.lan, say), or use the tunnel's public name from the step before. Nothing in Homestead needs changing.</p>",
  },
  disks: {
    title: "Disks for Longhorn", lead: s => s.done ? "Every disk is in use: Longhorn has the room your nodes can give it."
      : `${(s.unused || []).length} disk${(s.unused || []).length === 1 ? " is" : "s are"} not used yet. Given to Longhorn, each is tagged by what it is - hdd, ssd or nvme - for storage classes to choose by.`,
    body: s => (s.unused || []).length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Disk</th><th>Node</th><th>Size</th><th></th></tr></thead><tbody>
      ${s.unused.map(d => `<tr><td><b class="mono">${esc(d.device)}</b> ${d.kind ? `<span class="tag">${esc(d.kind)}</span>` : ""}</td><td data-label="Node">${esc(d.node)}</td>
        <td class="mono" data-label="Size">${esc(d.size_gb ?? "?")} GB</td>
        <td>${actionBar([{ label: "Set up", run: `nodeDetail(${jsq(d.node)});setTimeout(() => window.nodeSectionGo && nodeSectionGo('storage'), 900)`, need: "admin" }])}</td></tr>`).join("")}</tbody></table></div>` : "",
    actions: () => [],
  },
  storage: {
    title: "Default storage", lead: s => s.done ? `New volumes use <b>${esc(s.default)}</b>, with ${s.copies} cop${s.copies === 1 ? "y" : "ies"}: one on each of ${s.target === s.nodes ? "your" : s.target} nodes.`
      : !s.default ? "No storage class is the default, so a new volume has to name one."
      : s.provisioner !== "driver.longhorn.io" ? `The default, ${esc(s.default)}, keeps volumes on one node's disk: a failed node takes its data with it. A Longhorn class keeps copies on several.`
      : `The default, ${esc(s.default)}, keeps ${s.copies} cop${s.copies === 1 ? "y" : "ies"} with ${s.nodes} node${s.nodes === 1 ? "" : "s"}: ${s.copies > s.nodes ? "some copies can never be placed" : "fewer than the nodes could hold"}. ${s.target} fits.`,
    body: s => window.Diagram && s.default ? Diagram.copies(Array.from({ length: s.nodes }, (_, i) => `node ${i + 1}`), s.copies || 1) : "",
    actions: s => s.done ? [] : s.candidates?.length ? [{ label: `Make ${s.candidates[0]} the default`, run: `storageClassDefault(${jsq(s.candidates[0])}).then(() => viewSetup())`, pri: true }]
      : [{ label: `A class with ${s.target} copies`, run: "settingsTab('hardware');go('settings');setTimeout(() => window.storageClassCreate && storageClassCreate(), 900)", pri: true }],
    more: "<p>Changing the default affects new volumes only. A volume already made keeps its class; Volumes moves one to another class.</p>",
  },
  backups: {
    title: "Backups", lead: s => s.done ? "Longhorn copies volumes to backup storage off the cluster." :
      "Longhorn's copies survive a failed disk, not a failed house. Backups go somewhere else: the built-in backup storage, or an S3 or NFS target you have.",
    actions: s => s.done ? [] : [{ label: "Set up backups", run: "go('protect');setTimeout(() => window.objectStoreSetup && objectStoreSetup(), 600)", pri: true }],
  },
  config: {
    title: "Homestead's own settings", lead: s => s.done ? `Exported ${esc(agoText(s.at))}. Export again after big changes.` :
      "Users, VIPs, IP records, the portal and the rest, in one file encrypted with a passphrase you choose: what a rebuild needs that a volume backup does not.",
    actions: () => [{ label: "Export settings", run: "settingsTab('homestead');go('settings');setTimeout(() => window.configBackup && configBackup(), 900)", pri: true }],
  },
  osupdates: {
    title: "OS updates", lead: s => s.done ? "Each host updates in a weekly window, one at a time." :
      "Each host's own OS updates, one host at a time in a window you choose, draining and restarting a host when an update needs it.",
    actions: s => s.done ? [] : [{ label: "Choose a window", run: "osUpdates()", pri: true }],
  },
  appearance: {
    title: "Appearance", lead: () => "Light or dark, how dense the pages are, and cards or rows. Yours alone, on this device.",
    actions: s => [{ label: "Open appearance", run: "settingsTab('you');go('settings')", pri: !s.done }, s.done ? null : { label: "It looks right", run: "setupLocalDone('appearance')" }],
  },
  phone: {
    title: "On your phone", lead: s => s.installed ? "This is the installed app." : s.secure
      ? "Install Homestead as an app: its own icon, full screen, and notifications."
      : "On this address a phone can open Homestead but not install it: that needs HTTPS (Reach it, From anywhere).",
    body: s => `<div class="tblwrap"><table class="tbl stack dense"><tbody>
      <tr><td><b>iPhone and iPad</b></td><td class="small">In Safari: Share, then Add to Home Screen. Notifications need iOS 16.4 or later, and the app opened from the home screen.</td></tr>
      <tr><td><b>Android</b></td><td class="small">In Chrome: the menu, then Install app.</td></tr>
      <tr><td><b>A computer</b></td><td class="small">In Chrome or Edge: the install icon in the address bar.</td></tr></tbody></table></div>`,
    actions: s => s.installed ? [] : s.secure ? [{ label: "Install now", run: "pwaInstall()", pri: true }] : [{ label: "Get HTTPS", run: "setupOpen('https')", pri: true }],
  },
  notifications: {
    title: "Notifications", lead: s => s.done ? "This account gets notifications on at least one device." : !s.secure
      ? "Notifications reach a phone or computer only over HTTPS. Set that up first (Reach it), then turn them on here."
      : s.permission === "denied" ? "This browser was told to block Homestead's notifications. Allow them in the browser's site settings, then come back."
      : "A failed disk, a node down, an update waiting: on your phone or computer as it happens. You choose which.",
    actions: s => s.done ? [{ label: "Choose what you hear about", run: "pwaCategories()" }] : s.secure && s.permission !== "denied" ? [{ label: "Turn on notifications", run: "pwaEnable().then(() => viewSetup())", pri: true }] : [],
  },
  people: {
    title: "People", lead: s => s.done ? "More than one person can administer Homestead." :
      "One admin account is one forgotten password from a locked door. Add a second admin, and accounts of their own for anyone else - an operator, or a viewer for the TV.",
    actions: () => [{ label: "Manage users", run: "manageUsers()", pri: true }],
  },
  unifi: {
    title: "UniFi", lead: s => s.done ? "UniFi Network fills in devices and addresses." :
      "If your network is UniFi: devices, their names and fixed addresses come into IP addresses, and new VIPs avoid the DHCP range.",
    actions: s => s.done ? [] : [{ label: "Connect UniFi", run: "settingsTab('connections');go('settings')", pri: true }],
  },
  unraid: {
    title: "Coming from Unraid", lead: s => s.done ? "An Unraid server is added: its containers and VMs can be imported." :
      "Containers with their settings and appdata, and VMs with their disks: brought across from your Unraid server, mapped for you.",
    actions: s => s.done ? [{ label: "Import containers", run: "go('imports')" }, { label: "Import VMs", run: "go('vmimport')" }] : [{ label: "Add your Unraid server", run: "go('imports');setTimeout(() => window.srcAdd && srcAdd(), 700)", pri: true }],
  },
  homeassistant: {
    title: "Home Assistant", lead: s => s.done ? "An API key exists for something that talks to Homestead." :
      "An API key lets Home Assistant read Homestead's status and start or stop containers, limited to what you allow and to its own address.",
    actions: () => [{ label: "Make an API key", run: "settingsTab('access');go('settings');setTimeout(() => window.apiKeyNew && apiKeyNew(), 900)", pri: true }],
  },
  linked: {
    title: "Other Homesteads", lead: s => s.done ? "This Homestead is linked with others: one sign-in, and the view of them all." :
      "Another cluster - a second site, a test cluster - linked here: one sign-in, every cluster's pages, and moving apps between them.",
    actions: s => s.done ? [] : [{ label: "Link a cluster", run: "settingsTab('fleet');go('settings')" }],
  },
  starter: {
    title: "Your first apps", lead: s => s.done ? "Apps of your own are running." :
      "The App Store has Unraid's community catalogue. To start with: an uptime monitor, a dashboard of your services, a password manager.",
    actions: () => [{ label: "Uptime monitor", run: "setupStore('uptime-kuma')", pri: true }, { label: "Dashboard", run: "setupStore('homepage')" }, { label: "Open the App Store", run: "go('store')" }],
  },
  console: {
    title: "A screen on each machine", lead: s => s.done ? "Each host's own screen shows its health, before the login prompt." :
      "A machine's own screen shows CPU, memory, disks, its addresses and the cluster's health, before the login prompt.",
    actions: s => s.done ? [] : [{ label: "Turn on the host console", run: "settingsTab('hardware');go('settings')", pri: true }],
  },
};

/* ---------------- the page ---------------- */
async function viewSetup() {
  let state;
  try { state = await api("/api/setup"); }
  catch (e) { paint(`<div class="empty">${esc(e.message)}</div>`); return; }
  state.steps = setupFacts(state);
  STATE.data.setup = state;
  const chapters = setupVisible(state);
  const ids = chapters.flatMap(([, list]) => list);
  const status = Object.fromEntries(ids.map(id => [id, setupStatus(id, state.steps, state.skips)]));
  const done = ids.filter(id => status[id] === "done").length;
  const wanted = new URLSearchParams(location.search).get("step");
  const open = ids.includes(wanted) ? wanted : ids.find(id => ["attention", "todo"].includes(status[id])) || ids[0];
  const mark = s => `<span class="setup-mark ${s}" aria-hidden="true">${s === "done" ? "✓" : s === "attention" ? "!" : ""}</span>`;
  const nav = chapters.map(([title, list], c) => `<div class="setup-chapter">${c + 1} · ${esc(title)}</div>${list.map(id =>
    `<button class="setup-step${id === open ? " on" : ""}" data-step="${id}" onclick="setupOpen(${jsq(id)})">${mark(status[id])}<span>${esc(SETUP_STEPS[id].title)}</span></button>`).join("")}`).join("");
  paint(`<div class="phead"><div><h2>Setup</h2>
      <p>${done} of ${ids.length} done${state.admin ? "" : " · the steps that are yours"}</p></div>
      <div class="row"><span class="setup-meter" aria-hidden="true"><span style="width:${ids.length ? Math.round(done / ids.length * 100) : 0}%"></span></span>
        ${actionBar([{ label: state.hidden ? "Show on the Dashboard" : "Hide from the Dashboard", run: `setupHide(${!state.hidden})` }])}</div></div>
    <div class="settings-layout setup-layout" id="setupPage">
      <nav class="settings-nav setup-nav" aria-label="Setup steps">${nav}</nav>
      <div class="settings-main" id="setupStep">${setupStepHtml(open, state, status, ids)}</div>
    </div>`);
}
window.viewSetup = viewSetup;

function setupStepHtml(id, state, status, ids) {
  const step = SETUP_STEPS[id], s = state.steps[id] || {}, st = status[id];
  const next = ids[ids.indexOf(id) + 1];
  const actions = (step.actions(s) || []).filter(Boolean);
  const main = actions.filter(a => a.pri).map(a => UI.button(a.label, a.run, { kind: "pri" })).join("");
  const rest = actions.filter(a => !a.pri).map(a => UI.button(a.label, a.run)).join("");
  const skip = st === "done" ? "" : st === "skipped"
    ? UI.button("Bring it back", `setupSkip(${jsq(id)}, false)`) : UI.button("Skip", `setupSkip(${jsq(id)}, true)`);
  return `<section class="card flat setup-card" data-step="${id}">
      <div class="settings-card-head"><div><div class="ctitle">${esc(step.title)}</div>
        <div class="csub">${st === "done" ? UI.chip("done", "ok") : st === "skipped" ? UI.chip("skipped") : st === "attention" ? UI.chip("needs a look", "warn") : ""}</div></div></div>
      ${UI.lead(step.lead(s))}
      ${s.error ? UI.callout("warn", "Homestead could not check this just now", esc(s.error)) : ""}
      ${step.body ? `<div class="setup-body">${step.body(s)}</div>` : ""}
      ${step.more ? UI.more("How this works", step.more) : ""}
      ${UI.actions(rest + main + (next ? UI.button(`Next: ${SETUP_STEPS[next].title}`, `setupOpen(${jsq(next)})`) : UI.button("Back to the Dashboard", "go('dash')")), skip)}
    </section>`;
}

window.setupOpen = id => {
  const state = STATE.data.setup;
  if (STATE.view !== "setup" || !state) return go("setup", { params: { step: id } });
  const url = new URL(location.href);
  url.searchParams.set("step", id);
  history.replaceState(history.state, "", url.pathname + url.search);
  const ids = setupVisible(state).flatMap(([, list]) => list);
  const status = Object.fromEntries(ids.map(x => [x, setupStatus(x, state.steps, state.skips)]));
  $("#setupStep").innerHTML = setupStepHtml(id, state, status, ids);
  $$("#setupPage .setup-step").forEach(b => b.classList.toggle("on", b.dataset.step === id));
  if (matchMedia("(max-width: 900px)").matches) $("#setupStep").scrollIntoView({ block: "start" });
  if (window.applyRole) applyRole();
};

window.setupSkip = async (step, skip) => {
  try {
    await api("/api/setup/skip", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step, skip }) });
  } catch (e) { return toast(e.message, "bad"); }
  await viewSetup();
  setupOpen(step);
};

window.setupHide = async hidden => {
  try {
    await api("/api/setup/hide", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hidden }) });
    STATE.data.setupDash = null;
    toast(hidden ? "Setup is off the Dashboard; Settings › Homestead has it" : "Setup is on the Dashboard again", "ok");
    viewSetup();
  } catch (e) { toast(e.message, "bad"); }
};

window.setupLocalDone = id => { setupLocal(id, "1"); viewSetup().then(() => setupOpen(id)); };

window.setupHttpsCheck = async button => {
  const url = $("#setup_https").value.trim();
  button.disabled = true; button.textContent = "Checking…";
  try {
    const r = await api("/api/setup/https-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
    toast(`${r.url} reaches Homestead over HTTPS`, "ok");
    await viewSetup(); setupOpen("https");
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.textContent = "Check"; }
};

/* The two ways in from outside, as a deploy already filled in - the review
   and capacity check are the Deploy page's own. */
window.setupTunnel = kind => {
  const pre = kind === "cloudflare"
    ? { name: "cloudflared", image: "cloudflare/cloudflared:latest", args: ["tunnel", "--no-autoupdate", "run"], network_mode: "internal",
        cpu: "50m", memory: "64Mi", env: { TUNNEL_TOKEN: "" },
        env_meta: [{ key: "TUNNEL_TOKEN", label: "Tunnel token (from Cloudflare Zero Trust › Networks › Tunnels)", required: true, masked: true }] }
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
// The first time an administrator signs in, the guide opens itself, once.
// The live demo never opens it: the top bar offers it instead.
window.welcomeCheck = async (force = false) => {
  if (force) return go("setup");
  if (setupDemo()) return setupOffer();
  if (typeof ROLE === "undefined" || ROLE !== "admin") return;
  let state;
  try { state = await api("/api/setup"); } catch (e) { return; }
  if (state.opened) return;
  try { await api("/api/setup/opened", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); } catch (e) { return; }
  go("setup");
};

function setupOffer() {
  const button = $("#setupbtn");
  if (!button) return;
  button.classList.remove("hidden");
  button.classList.toggle("pulse", setupLocal("offered") !== "1");
  button.onclick = () => { setupLocal("offered", "1"); button.classList.remove("pulse"); go("setup"); };
}
window.setupOffer = setupOffer;

/* The Dashboard's line: "Setup: 9 of 21", until done or hidden. */
// Asked again at most every five minutes: the Dashboard repaints often.
window.setupDashItem = async () => {
  const cached = STATE.data.setupDash;
  if (cached && Date.now() - cached.at < 300000) return cached.html;
  let state;
  try { state = await api("/api/setup", { keep: true }); } catch (e) { return ""; }
  const html = setupDashLine(state);
  STATE.data.setupDash = { at: Date.now(), html };
  return html;
};
function setupDashLine(state) {
  if (state.hidden) return "";
  state.steps = setupFacts(state);
  const ids = setupVisible(state).flatMap(([, list]) => list);
  const done = ids.filter(id => setupStatus(id, state.steps, state.skips) === "done").length;
  const left = ids.filter(id => ["attention", "todo"].includes(setupStatus(id, state.steps, state.skips))).length;
  return left ? `<a class="linkish" onclick="go('setup')">Setup: ${done} of ${ids.length}</a>` : "";
}

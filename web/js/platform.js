/* What this cluster is - Harvester, or plain k3s / RKE2 / Kubernetes - and
   what it has, so pages that need Longhorn or KubeVirt say so instead of
   failing, and the sidebar only offers what can work. */

const PLATFORM_NEEDS = {
  longhorn: { name: "Longhorn", why: "volumes, snapshots and backups are Longhorn's",
    fix: "Homestead can install it here, as it can under Settings → Cluster. Each node needs open-iscsi and an NFS client (nfs-common) first." },
  kubevirt: { name: "KubeVirt", why: "virtual machines run on KubeVirt",
    fix: "Homestead can install it with CDI, which fills VM disks from images - here or under Settings → Cluster. The machines need hardware virtualisation to run VMs at full speed." },
};

async function loadPlatform(force = false) {
  try { STATE.platform = await api(`/api/platform${force ? "?force=1" : ""}`, { keep: true }); } catch (e) { STATE.platform = null; return; }
  const p = STATE.platform;
  // The sidebar offers what this cluster can do.
  const hide = { vms: !p.kubevirt, protect: !p.longhorn };
  Object.entries(hide).forEach(([view, off]) => { const a = $(`#nav a[data-view="${view}"]`); if (a) a.hidden = off; });
}
window.loadPlatform = loadPlatform;

/* k3s's ServiceLB puts every Service on every node's own address: there is
   no VIP to choose, so the forms offer that one choice and say so. */
const nodeAddressesOnly = () => STATE.platform?.load_balancer === "servicelb";
const NODE_ADDRESS_TIP = "k3s's ServiceLB puts every Service on every node's own address, at the LAN port you give it - so each needs a port no other Service uses (Traefik has 80 and 443).";
function nodeAddressOption() {
  return `<option value="shared" selected>Every node's own address · k3s ServiceLB</option>`;
}
/* With kube-vip beside it, ServiceLB still puts a Service on the nodes' own
   addresses: offered as well as a VIP of its own. */
const nodeAddressBeside = () => !!STATE.platform?.servicelb && STATE.platform?.load_balancer === "kube-vip";
function nodeAddressChoice(selected = false) {
  return nodeAddressBeside() ? `<option value="nodes" ${selected ? "selected" : ""}>Every node's own address · ServiceLB</option>` : "";
}
window.nodeAddressBeside = nodeAddressBeside;
window.nodeAddressesOnly = nodeAddressesOnly;

/* true (and the page says why) when this cluster lacks what a page needs. */
function platformLacks(need, title) {
  const p = STATE.platform;
  if (!p || p[need] !== false) return false;
  const info = PLATFORM_NEEDS[need];
  paint(`<div class="phead"><div><h2>${esc(title)}</h2><p>${esc(platformName(p))} · ${esc(info.name)} is not installed</p></div></div>
    <div class="empty platform-missing"><b>This page needs ${esc(info.name)}</b>, because ${esc(info.why)}.
      <p class="dim small" style="max-width:560px;margin:10px auto 0">${esc(info.fix)}</p>
      ${can("admin") && p.helm_controller ? `<div class="row" style="justify-content:center;margin-top:12px">
        <button class="btn pri" onclick="addonInstall('${need}')">Install ${esc(info.name)}</button></div>` : ""}</div>`);
  return true;
}
window.platformLacks = platformLacks;

function platformName(p) {
  return { harvester: "Harvester", k3s: "k3s", rke2: "RKE2", kubernetes: "Kubernetes" }[p?.distribution] || "Kubernetes";
}
window.platformName = platformName;

/* ---------- add-ons: Longhorn and KubeVirt where the cluster lacks them ----------
   Installed through the Helm controller k3s and RKE2 run, so they are
   ordinary HelmCharts afterwards. Harvester brings both. */
const ADDONS = {
  longhorn: { name: "Longhorn", what: "Replicated volumes, snapshots and backups - the Volumes and Data protection pages",
    needs: "Each node needs open-iscsi and an NFS client (nfs-common) installed and iscsid running; the k3s script does that. Volumes keep one copy per node, up to three." },
  "kube-vip": { name: "kube-vip", what: "VIPs for apps: a container can have a LAN address of its own, handed out from Networking > Your VIPs, as on Harvester",
    needs: "Announces each VIP from one node with ARP. Beside k3s's ServiceLB it takes only the Services given a VIP; the rest stay on the nodes' own addresses." },
  multus: { name: "Multus", what: "Second networks for pods - what LAN networks need, so a container or VM can have an address of its own on your LAN",
    needs: "Installed on every node from the chart RKE2 uses for it; apps already running are left as they are." },
  kubevirt: { name: "KubeVirt", what: "Virtual machines, with CDI to fill their disks from images",
    needs: "The newest KubeVirt and CDI releases are installed. VMs run at full speed where a node has hardware virtualisation (/dev/kvm); without it KubeVirt emulates, many times slower." },
};

window.addonsPaint = async () => {
  const card = $("#addonsCard");
  if (!card) return;
  let s, health, nfs;
  try { [s, health, nfs] = await Promise.all([api("/api/addons"), api("/api/self/health").catch(() => ({})),
    api("/api/shares/nfs/server").catch(error => ({ error: error.message }))]); }
  catch (e) { card.hidden = true; return; }
  card.hidden = false;
  const probe = health.probe || {}, smb = health.samba || {};
  STATE.data.sambaInstalled = !!smb.installed;
  const kvmLine = !s.kvm_known ? '<span class="dim">The node probe has not said whether the nodes have hardware virtualisation.</span>'
    : s.kvm_everywhere ? "Every node has hardware virtualisation."
    : s.kvm_nowhere ? '<b>No node has hardware virtualisation</b>: KubeVirt will emulate, and VMs run slowly.'
    : `Only ${Object.entries(s.kvm).filter(([, on]) => on).map(([n]) => esc(n)).join(", ")} ha${Object.values(s.kvm).filter(Boolean).length === 1 ? "s" : "ve"} hardware virtualisation; VMs run there.`;
  const row = (key, state) => {
    const a = ADDONS[key];
    const pill = state.installed ? '<span class="pill ok">installed</span>'
      : state.installing ? '<span class="pill med">installing</span>' : '<span class="pill">not installed</span>';
    const button = state.installed || state.installing ? ""
      : s.helm_controller ? `<button class="btn sm pri" data-need="admin" onclick="addonInstall('${key}')">Install ${esc(a.name)}</button>`
      : '<span class="dim xs">needs the Helm controller k3s and RKE2 run</span>';
    const diagnostic = key === "multus" && state.installing && state.diagnostic_command
      ? `<div class="note warn addon-diagnostic"><b>Taking longer than expected?</b> SSH to a server node and gather the Helm job log:</div>
         ${guideCopy(state.diagnostic_command)}` : "";
    const emulation = key === "kubevirt" && state.installed && state.emulation != null
      ? `<div class="dim xs" style="margin-top:4px">${state.emulation
          ? "Software fallback is allowed when /dev/kvm is missing."
          : "Hardware virtualisation (/dev/kvm) is required."}</div>` : "";
    const emulationButton = key === "kubevirt" && state.installed && !state.emulation && !s.kvm_everywhere && can("admin")
      ? '<button class="btn sm" onclick="kubevirtEmulation(true)">Allow software fallback</button>' : "";
    return `<div class="addon-row"><div><b>${esc(a.name)}</b> ${pill}<div class="dim small">${esc(a.what)}</div>
        ${state.installed ? "" : `<div class="dim xs" style="margin-top:4px">${esc(a.needs)}</div>`}
        ${key === "kubevirt" ? `<div class="xs" style="margin-top:4px">${kvmLine}</div>${emulation}` : ""}
        ${diagnostic}</div>
      <div class="row">${button}${emulationButton}</div></div>`;
  };
  const probePill = !probe.installed ? '<span class="pill">not installed</span>'
    : probe.ready < probe.desired || probe.reporting < probe.desired ? `<span class="pill med">${probe.ready || 0}/${probe.desired || 0} ready</span>`
    : '<span class="pill ok">installed</span>';
  const probeButton = !can("admin") ? "" : !probe.installed
    ? '<button class="btn sm pri" onclick="probeInstallConfirm()">Install node probe</button>'
    : '<button class="btn sm" onclick="probeRemove()">Remove</button>';
  const probeRow = `<div class="addon-row"><div><b>Node probe</b> ${probePill}
      <div class="dim small">Host hardware, /dev/kvm, temperatures, physical disks, SMART health and per-disk throughput</div>
      <div class="dim xs" style="margin-top:4px">A lightweight read-only probe runs on every node; SMART tests use its separate privileged sidecar.</div></div>
    <div class="row">${probeButton}</div></div>`;
  const smbPill = !smb.installed ? '<span class="pill">not installed</span>'
    : !smb.enabled ? '<span class="pill med">off</span>'
    : smb.ready < smb.desired ? '<span class="pill med">starting</span>' : '<span class="pill ok">serving</span>';
  const smbRow = `<div class="addon-row"><div><b>SMB network shares</b> ${smbPill}
      <div class="dim small">Windows, macOS and Linux file shares from the saved Network Shares inventory</div>
      <div class="dim xs" style="margin-top:4px">${smb.shares || 0} configured · ${smb.address ? esc(smb.address) : "address not assigned"} · stopping or removing SMB keeps all PVCs and share settings</div></div>
    <div class="row"><button class="btn sm" onclick="go('shares')">Shares</button>
      ${can("admin") ? `<label class="switch"><input type="checkbox" ${smb.enabled ? "checked" : ""} onchange="sambaToggle(this)"> ${smb.enabled ? "On" : "Off"}</label>
        ${smb.installed ? '<button class="btn sm danger" onclick="sambaRemove()">Remove server</button>' : ""}` : ""}</div></div>`;
  STATE.data.nfs = nfs;
  const nfsPill = nfs.error ? '<span class="pill crit">status unavailable</span>' : !nfs.installed ? '<span class="pill">not installed</span>'
    : !nfs.enabled ? '<span class="pill med">off</span>'
    : nfs.ready < nfs.desired ? '<span class="pill med">starting</span>' : '<span class="pill ok">serving</span>';
  const nfsRow = `<div class="addon-row"><div><b>NFSv4 network shares</b> ${nfsPill}
      <div class="dim small">A separate, opt-in NFS container serving selected RWX shares to allowed client networks</div>
      <div class="dim xs" style="margin-top:4px">${(nfs.exports || []).length} exports · ${nfs.address ? esc(nfs.address) : "address not assigned"} · removing the server keeps the shares and volumes</div></div>
    <div class="row"><button class="btn sm" onclick="go('shares')">Exports</button>
      ${can("admin") && !nfs.error ? `<label class="switch"><input type="checkbox" ${nfs.enabled ? "checked" : ""} onchange="nfsToggle(this)"> ${nfs.enabled ? "On" : "Off"}</label>
        ${nfs.installed ? '<button class="btn sm danger" onclick="nfsRemove()">Remove server</button>' : ""}` : ""}</div></div>`;
  const clusterRows = s.harvester ? "" : `${row("longhorn", s.longhorn)}${row("kubevirt", s.kubevirt)}${s.multus ? row("multus", s.multus) : ""}${s.kube_vip ? row("kube-vip", s.kube_vip) : ""}`;
  card.innerHTML = `<div class="settings-card-head"><div><div class="ctitle">Add-ons</div>
      <div class="csub">${s.harvester ? "Optional Homestead services; Harvester already provides storage, VM and network add-ons"
        : `What this ${esc(platformName(STATE.platform || { distribution: s.distribution }))} cluster can add`}</div></div></div>
    ${probeRow}${smbRow}${nfsRow}${clusterRows}`;
  if (window.applyRole) applyRole();
};

window.kubevirtEmulation = async enabled => {
  if (enabled && !confirm("Allow KubeVirt's software fallback? VMs can then start on nodes without /dev/kvm, but run much more slowly there. Running VMs are not restarted.")) return;
  try {
    const result = await api("/api/addons/kubevirt/emulation", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }) });
    toast(result.detail, "ok");
    addonsPaint();
  } catch (e) { toast(e.message, "bad"); }
};

window.addonInstall = async key => {
  const a = ADDONS[key];
  let body = {};
  if (key === "kubevirt") {
    const s = await api("/api/addons").catch(() => ({}));
    if (s.kvm_nowhere && !confirm("No node has hardware virtualisation (/dev/kvm), so KubeVirt will emulate: VMs work, but many times slower. Install anyway?")) return;
    body = {};
  } else if (key === "kube-vip") {
    const s = await api("/api/addons").catch(() => ({}));
    const where = s.kube_vip?.interface ? `It announces VIPs on ${s.kube_vip.interface}, the interface each node's default route uses.`
      : "The nodes did not agree on one network interface (or the node probe has not said), so kube-vip finds it itself.";
    if (!confirm(`Install kube-vip? ${a.needs} ${where}`)) return;
  } else if (!confirm(`Install ${a.name}? ${a.needs}`)) return;
  try {
    const r = await api(`/api/addons/${key}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail || `${a.name} is being installed`, "ok");
    if (window.refreshOperations) refreshOperations(true);
    addonsPaint();
    // The pages that need it appear once it is there.
    const watch = setInterval(async () => {
      await loadPlatform(true);
      if (key === "kube-vip" ? STATE.platform?.load_balancer === "kube-vip" : STATE.platform?.[key]) {
        clearInterval(watch);
        toast(key === "multus" ? "Multus is ready - LAN networks can be made now"
          : key === "kube-vip" ? "kube-vip is ready - apps can be given VIPs now" : `${a.name} is ready - its pages are in the sidebar`, "ok");
        addonsPaint();
      }
    }, 20000);
    setTimeout(() => clearInterval(watch), 20 * 60000);
  } catch (e) { toast(e.message, "bad"); }
};

/* Adding a host to a k3s or RKE2 cluster: the distribution's own installer,
   this cluster's address, and where the token lives - which Homestead
   cannot read, since it is a file on the server, not in the API. */
window.platformJoinGuide = async () => {
  modal("Add a host", '<div class="empty"><span class="spin2"></span>reading the cluster</div>', true);
  let g;
  try { g = await api("/api/platform/join"); } catch (e) { $("#mbody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  const k3s = g.distribution === "k3s";
  if (!["k3s", "rke2"].includes(g.distribution)) {
    $("#mbody").innerHTML = `<p class="muted small">This cluster's distribution could not be told from its nodes, so Homestead cannot give its join steps. Use the installer that set the cluster up.</p>`;
    return;
  }
  $("#mbody").innerHTML = `<p class="muted small">This cluster runs <b>${k3s ? "k3s" : "RKE2"} v${esc(g.version)}</b>; the new host joins with the same release. Run these on the new machine, as root or with sudo.</p>
    <section class="guide-step"><h4><span>1</span>Read the join token</h4>
      <p>It is on the server${g.server ? ` at <span class="mono">${esc(g.server)}</span>` : ""}; Homestead cannot read it for you.</p>
      ${guideCopy(`sudo cat ${g.token_file}`)}
      <p class="dim xs">Anyone holding it can join a machine to this cluster, so keep it out of chats and notes.</p></section>
    <section class="guide-step"><h4><span>2</span>If it will hold Longhorn volumes</h4>${guideCopy(g.longhorn)}</section>
    ${k3s ? `<section class="guide-step"><h4><span>3</span>Join as a worker</h4>${guideCopy(g.agent)}
        <p class="dim xs">Or, to add a server that also runs the control plane (a cluster with an embedded etcd):</p>${guideCopy(g.server_join)}</section>`
      : `<section class="guide-step"><h4><span>3</span>Install the agent</h4>${guideCopy(g.agent)}
        <p class="dim xs">with <span class="mono">/etc/rancher/rke2/config.yaml</span> saying:</p>${guideCopy(g.config)}</section>`}
    <section class="guide-step"><h4><span>4</span>Watch it join</h4><p class="dim small">It appears on the Nodes page within a minute or two, and turns Ready once its networking is up.</p></section>`;
};

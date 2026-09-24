/* What this cluster is - Harvester, or plain k3s / RKE2 / Kubernetes - and
   what it has, so pages that need Longhorn or KubeVirt say so instead of
   failing, and the sidebar only offers what can work. */

const PLATFORM_NEEDS = {
  longhorn: { name: "Longhorn", why: "volumes, snapshots and backups are Longhorn's",
    fix: "Install Longhorn from the Helm page (chart longhorn from charts.longhorn.io, namespace longhorn-system). Each node needs open-iscsi and nfs-common first." },
  kubevirt: { name: "KubeVirt", why: "virtual machines run on KubeVirt",
    fix: "Harvester includes it. On another cluster, install KubeVirt and CDI following kubevirt.io." },
};

async function loadPlatform() {
  try { STATE.platform = await api("/api/platform", { keep: true }); } catch (e) { STATE.platform = null; return; }
  const p = STATE.platform;
  // The sidebar offers what this cluster can do.
  const hide = { vms: !p.kubevirt, protect: !p.longhorn };
  Object.entries(hide).forEach(([view, off]) => { const a = $(`#nav a[data-view="${view}"]`); if (a) a.hidden = off; });
}
window.loadPlatform = loadPlatform;

/* true (and the page says why) when this cluster lacks what a page needs. */
function platformLacks(need, title) {
  const p = STATE.platform;
  if (!p || p[need] !== false) return false;
  const info = PLATFORM_NEEDS[need];
  paint(`<div class="phead"><div><h2>${esc(title)}</h2><p>${esc(platformName(p))} · ${esc(info.name)} is not installed</p></div></div>
    <div class="empty platform-missing"><b>This page needs ${esc(info.name)}</b>, because ${esc(info.why)}.
      <p class="dim small" style="max-width:560px;margin:10px auto 0">${esc(info.fix)}</p>
      ${need === "longhorn" && can("admin") ? `<div class="row" style="justify-content:center;margin-top:12px"><button class="btn pri" onclick="platformInstallLonghorn()">Install Longhorn</button></div>` : ""}</div>`);
  return true;
}
window.platformLacks = platformLacks;

function platformName(p) {
  return { harvester: "Harvester", k3s: "k3s", rke2: "RKE2", kubernetes: "Kubernetes" }[p?.distribution] || "Kubernetes";
}
window.platformName = platformName;

window.platformInstallLonghorn = async () => {
  go("helm");
  await helmInstall();
  $("#hi_q").value = "longhorn";
  await helmSearch();
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

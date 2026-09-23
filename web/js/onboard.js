/* ---------------- Adding and removing Harvester hosts ----------------
   Adding a host is Harvester's own installer, run from its ISO: an unattended
   install would need the new machine's disk and network card named in advance,
   which nobody knows until the machine is in front of them. So this is a guide
   - the right ISO, the join token's whereabouts, and each installer screen with
   this cluster's answer - that then watches for the host to appear. The
   cleanup side removes a node that is gone, and the records it leaves behind,
   in Harvester's documented order. */

const ONBOARD = { timer: null, known: null };

function guideCopy(value) {
  return `<span class="guide-copy"><code>${esc(value)}</code><button class="btn sm"
    onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent).then(() => toast('Copied', 'ok'))">Copy</button></span>`;
}

function guideScreens(g) {
  const role = g.management_count < 3
    ? `<b>Default Role</b>. This cluster has ${g.management_count} management node${g.management_count === 1 ? "" : "s"};
       Harvester promotes new hosts to management until there are three.`
    : "<b>Default Role</b> - with three management nodes already, it joins as a worker. Pick <b>Worker Role</b> to say so outright.";
  const rows = [
    ["Installation mode", "<b>Join an existing Harvester cluster</b>"],
    ["Node role", role],
    ["Password", "A password for the <span class=\"mono\">rancher</span> user on this host - for SSH and the console. Your other hosts' is fine."],
    ["Installation disk", "The disk Harvester goes on. <b>Everything on it is erased.</b> An SSD of 250 GB or more."],
    ["Data disk", "A second disk for VM and volume data, if the machine has one. On a single-disk machine, leave it as the installation disk."],
    ["Persistent size", "Leave the default (150 GiB)."],
    ["Hostname", g.hostname ? `${guideCopy(g.hostname)} <span class="dim xs">- the next free name here; any unique lowercase name works</span>`
      : "A unique lowercase name, such as the next number after your other hosts."],
    ["Management network", `The network card that is plugged into your LAN - the installer shows which have a link. Add a second
      for a bond if you cabled two. <b>VLAN</b>: none, unless your other hosts use one. <b>Bond mode</b>: active-backup.
      <b>IPv4</b>: DHCP with a reservation on your router, or Static with an address outside the DHCP range. <b>MTU</b>: 1500.`],
    ["DNS servers", "Your LAN's DNS - usually your router. Only asked for a static address."],
    ["Management address", g.vip ? `${guideCopy(g.vip)} <span class="dim xs">- this cluster's VIP</span>` : "This cluster's VIP (Homestead could not read it)."],
    ["Cluster token", "The token from step 2."],
    ["NTP servers", g.ntp.length ? guideCopy(g.ntp.join(",")) : "Leave the default."],
    ["Proxy address", g.proxy ? guideCopy(g.proxy) : "Leave empty."],
    ["SSH keys", "Optional - a URL such as <span class=\"mono\">https://github.com/&lt;you&gt;.keys</span>."],
    ["Remote config URL", "Leave empty."],
    ["Confirm", "Check the summary and install. It takes 10-20 minutes, then reboots and joins by itself."],
  ];
  return `<div class="guide-screens">${rows.map(([screen, answer]) =>
    `<div class="guide-screen"><span>${screen}</span><div>${answer}</div></div>`).join("")}</div>`;
}

window.clusterOnboarding = async () => {
  modal("Add a Harvester host", '<div class="empty"><span class="spin2"></span>reading the cluster</div>', true, "onboard");
  let g;
  try { g = await api("/api/onboard/guide"); } catch (e) {
    $("#mbody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return;
  }
  ONBOARD.known = new Set(g.nodes.map(n => n.name));
  $("#mbody").innerHTML = `
    <p class="muted small">Harvester's own installer adds the host. You pick its disk and network card on the machine
      itself; everything else is below, with this cluster's answers.</p>
    <section class="guide-step"><h4><span>1</span>Boot the installer</h4>
      ${g.iso ? `<p>This cluster runs <b>Harvester v${esc(g.version)}</b>; the new host needs the same release.</p>
        <div class="row"><a class="btn sm pri" href="${esc(g.iso)}" target="_blank" rel="noopener">Harvester v${esc(g.version)} ISO (${esc(g.arch)})</a>
          <a class="btn sm" href="${esc(g.checksums)}" target="_blank" rel="noopener">Checksums</a></div>`
        : '<p>Homestead could not read this cluster\'s Harvester version - use the ISO matching the version on the Cluster page.</p>'}
      <p class="dim xs">Write it to a USB stick with Rufus (in DD mode) or balenaEtcher, or mount it as virtual media from the
        server's BMC (iDRAC, iLO, IPMI). Boot from it and choose <b>Harvester Installer</b>.</p></section>
    <section class="guide-step"><h4><span>2</span>Read the cluster token</h4>
      <p>It is on every management node. Sign in as <span class="mono">rancher</span>, with the password set when that host was installed:</p>
      ${guideCopy(`ssh rancher@${g.token_host || "<management-node>"}`)}
      ${guideCopy(g.token_command)}
      <p class="dim xs">Copy the value after <span class="mono">token:</span>. Anyone holding it can join a machine to this cluster,
        so keep it out of chats and notes.${g.token_host ? "" : ` Management nodes: ${esc(g.nodes.filter(n => n.management).map(n => n.name).join(", ") || "none found")}.`}</p></section>
    <section class="guide-step"><h4><span>3</span>Answer the installer</h4>
      <p class="dim xs">In the order Harvester asks; some releases put the password or role a screen earlier.</p>
      ${guideScreens(g)}</section>
    <section class="guide-step"><h4><span>4</span>Watch it join</h4><div id="guideWatch"></div></section>`;
  guideWatch();
};

async function guideWatch() {
  clearTimeout(ONBOARD.timer);
  const host = $("#guideWatch");
  if (!host || $("#modal").classList.contains("hidden")) return;
  let nodes = null;
  try { nodes = await api("/api/nodes", { keep: true }); } catch (e) { nodes = null; }
  if (!$("#guideWatch")) return;
  const fresh = (nodes || []).filter(n => !ONBOARD.known.has(n.name));
  host.innerHTML = fresh.length ? fresh.map(n => `<div class="guide-joined ${n.status === "Ready" ? "ok" : ""}">
      <b>${esc(n.name)}</b><span>${n.status === "Ready" ? "joined and Ready" : "registered - finishing setup"}</span>
      <button class="btn sm" onclick="closeModal();go('nodes')">Nodes</button></div>`).join("")
    : '<div class="dim xs"><span class="spin2"></span> Waiting for a new host. It appears here as soon as Harvester registers it, about a minute after it reboots.</div>';
  ONBOARD.timer = setTimeout(guideWatch, 10000);
}

/* ---------------- cleanup ---------------- */
/* What is left over after hosts come and go, shown on the Cluster page. */
window.clusterCleanupPaint = async () => {
  const host = $("#clusterCleanup");
  if (!host) return;
  const report = await api("/api/cluster/cleanup").catch(() => null);
  if (!$("#clusterCleanup")) return;
  if (!report) { host.innerHTML = ""; return; }
  const rows = [
    ...report.dead_nodes.map(n => `<div class="cleanup-row bad"><div><b>${esc(n.name)} is not ready</b>
      <span>${esc((n.roles || []).join(" · ") || "worker")}${n.since ? ` · since ${esc(new Date(n.since).toLocaleString())}` : ""}</span></div>
      <button class="btn sm danger" data-need="admin" onclick="nodeRemoval('${esc(n.name)}')">Remove…</button></div>`),
    ...report.stale_machines.map(m => `<div class="cleanup-row"><div><b>Leftover machine ${esc(m.name)}</b>
      <span>${m.stuck ? `Being deleted, but waiting on ${esc(m.node || "its host")}, which will not answer`
        : `Cluster API still lists it${m.node ? ` for ${esc(m.node)}, which is gone` : ` (${esc(m.phase)})`}`}</span></div>
      <button class="btn sm ${m.stuck ? "danger" : ""}" data-need="admin" onclick="clusterCleanup('machine','${esc(m.name)}',${m.stuck})">${m.stuck ? "Force" : "Delete"}</button></div>`),
    ...report.stale_longhorn.map(n => `<div class="cleanup-row"><div><b>Longhorn still lists ${esc(n.name)}</b>
      <span>${n.replicas ? `${n.replicas} replica${n.replicas === 1 ? "" : "s"} still recorded there. They rebuild elsewhere on their own; force it if the host is gone for good` : "No replicas left; safe to delete"}</span></div>
      <button class="btn sm ${n.replicas ? "danger" : ""}" data-need="admin" onclick="clusterCleanup('longhorn','${esc(n.name)}',${!!n.replicas})">${n.replicas ? "Force" : "Delete"}</button></div>`),
  ];
  // Kept, so the next refresh draws it straight away instead of the page
  // shrinking while it is fetched again and growing back when it arrives.
  STATE.data.cleanupHtml = `<div class="sec">Hosts coming and going ${tip("What a removed host can leave behind: a node that stopped reporting, a Cluster API machine with no node, a Longhorn node record with no host.")}</div>
    <div class="card flat cleanup-card">${rows.join("") || '<div class="cleanup-row"><div><b>Nothing to tidy</b><span>Every node is ready and nothing is left over.</span></div></div>'}</div>`;
  host.innerHTML = STATE.data.cleanupHtml;
  if (window.applyRole) window.applyRole();
};

window.clusterCleanup = async (kind, name, force = false) => {
  const what = kind === "machine" ? `the leftover Cluster API machine ${name}` : `Longhorn's record of ${name}`;
  const forced = kind === "machine"
    ? "\n\nIts deletion is waiting on a host that will not answer. Forcing clears the finalizers holding it; only do this if the host is gone for good."
    : "\n\nThe replica records Longhorn keeps there are deleted too, and it rebuilds them from the remaining copies. Only do this if the host is gone for good.";
  if (!confirm(`${force ? "Force-delete" : "Delete"} ${what}?${force ? forced : ""}`)) return;
  try {
    const result = await api("/api/cluster/cleanup/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, name, force }) });
    toast(result.message || "Done", "ok");
    clusterCleanupPaint();
  } catch (e) { toast(e.message, "bad"); }
};

/* Removing a node: what it costs, checked first, then Harvester's order. */
window.nodeRemoval = async name => {
  childModal(`Remove · ${name}`, '<div class="empty"><span class="spin2"></span>checking what removing it would do</div>', true);
  let plan;
  try { plan = await api(`/api/cluster/removal?node=${encodeURIComponent(name)}`); }
  catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Could not check</b><br><span class="dim small">${esc(e.message)}</span></div>`; return; }
  $("#mbody").innerHTML = `
    ${plan.blockers.length ? `<div class="note bad"><b>Not now.</b><ul>${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
    ${plan.warnings.length ? `<div class="note warn"><b>Worth knowing first.</b><ul>${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    ${plan.ok ? `<div class="sec">Is it coming back?</div>
    <div class="removal-modes">
      <label class="removal-mode"><input type="radio" name="rm_mode" value="standard" checked onchange="nodeRemovalMode()">
        <div><b>It might</b><span>Remove it the standard way. Pods, VMs and volumes still bound to it are left to
          Kubernetes, Longhorn and KubeVirt, which move them once they give up on it.</span></div></label>
      <label class="removal-mode"><input type="radio" name="rm_mode" value="gone" onchange="nodeRemovalMode()">
        <div><b>It is gone for good</b><span>Dead and not coming back. Also let go of what it still holds, so
          nothing waits on a host that will never answer.</span></div></label>
    </div>
    ${plan.stuck && (plan.stuck.pods || plan.stuck.vms.length || plan.stuck.attachments || plan.stuck.replicas) ? `
      <div class="removal-stuck">Still bound to ${esc(name)}:
        <span>${plan.stuck.pods} pod${plan.stuck.pods === 1 ? "" : "s"}</span>
        <span>${plan.stuck.vms.length} VM${plan.stuck.vms.length === 1 ? "" : "s"}</span>
        <span>${plan.stuck.attachments} volume attachment${plan.stuck.attachments === 1 ? "" : "s"}</span>
        <span>${plan.stuck.replicas} replica record${plan.stuck.replicas === 1 ? "" : "s"}</span></div>` : ""}` : ""}
    <div class="sec">What happens</div>
    <ol class="removal-steps" id="rm_steps">${plan.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>
    <p class="muted small">Homestead cannot reach a dead host to wipe it. If it comes back, reinstall it before it
      rejoins; a host that still has its old install will try to register again.</p>
    ${plan.lost_volumes.length && plan.ok ? `<label class="switch dependency-confirm"><input type="checkbox" id="rm_loss"
      onchange="document.getElementById('rm_go').disabled=!this.checked"> I accept losing ${plan.lost_volumes.length}
      volume${plan.lost_volumes.length === 1 ? "" : "s"} that only ${esc(name)} held</label>` : ""}
    <div class="modalactions"><button class="btn" onclick="modalBack()">Cancel</button>
      ${plan.ok ? `<button class="btn danger" id="rm_go" data-need="admin" ${plan.lost_volumes.length ? "disabled" : ""}
        onclick="nodeRemove('${esc(name)}')">Remove ${esc(name)}</button>` : ""}</div>`;
  window.__removalPlan = plan;
  if (window.applyRole) window.applyRole();
};

/* Gone for good adds its own steps ahead of the standard ones. */
window.nodeRemovalMode = () => {
  const plan = window.__removalPlan, list = $("#rm_steps");
  if (!plan || !list) return;
  const gone = $('input[name="rm_mode"]:checked')?.value === "gone";
  // In the order remove_node runs them: VMs, pods and attachments before the
  // node goes; the machine's finalizers and the replica records after.
  const [pods, vms, attachments, replicas, machine] = plan.gone_steps;
  const steps = gone ? [plan.steps[0], vms, pods, attachments, plan.steps[1], plan.steps[2], machine, replicas, plan.steps[3]]
    : plan.steps;
  list.innerHTML = steps.map(s => `<li>${esc(s)}</li>`).join("");
  const button = $("#rm_go");
  if (button) button.textContent = gone ? `Remove ${plan.node} for good` : `Remove ${plan.node}`;
};

window.nodeRemove = async name => {
  const gone = $('input[name="rm_mode"]:checked')?.value === "gone";
  if (!confirm(gone
    ? `Remove ${name} for good?\n\nIts pods and VMs are force-stopped so they start elsewhere, its volume attachments and replica records are let go, and its machine is deleted even if something still waits on it. This cannot be undone from Homestead.`
    : `Remove ${name} from the cluster?\n\nThis cannot be undone from Homestead.`)) return;
  const button = $("#rm_go");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spin2"></span> removing'; }
  try {
    const result = await api("/api/cluster/remove-node", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: name, accept_loss: !!$("#rm_loss")?.checked, gone }) });
    $("#mbody").innerHTML = `<div class="note good"><b>${esc(name)} is out of the cluster.</b></div>
      <ol class="removal-steps done">${result.log.map(l => `<li>${esc(l)}</li>`).join("")}</ol>
      <div class="modalactions"><button class="btn pri" onclick="closeModal(); if (STATE.view === 'cluster') { resetPaint(); viewCluster(); }">Done</button></div>`;
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = `Remove ${name}`; }
  }
};

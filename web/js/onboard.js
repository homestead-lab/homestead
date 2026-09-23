/* ---------------- Adding and removing Harvester hosts ----------------
   A join plan holds everything the installer needs for one host. It can be
   delivered three ways - a USB stick that network-boots from Homestead, a
   temporary PXE responder for that host's MAC address alone, or the Harvester
   ISO given the config address by hand - and its progress comes back through
   the installer's own webhooks. The cleanup side removes a node that is gone,
   and the records it leaves behind, in Harvester's documented order. */

const ONBOARD = { timer: null };
const ONBOARD_OPEN = ["waiting", "installing"];

const onboardStatusPill = plan => {
  const tone = { waiting: "low", installing: "warn", joined: "ok", failed: "crit", expired: "neutral", cancelled: "neutral" }[plan.status] || "low";
  return `<span class="pill ${tone}">${esc(plan.status)}</span>`;
};

window.clusterOnboarding = async () => {
  modal("Onboard a Harvester node", '<div class="empty"><span class="spin2"></span>reading the cluster</div>', true, "onboard");
  const [defaults, plans] = await Promise.all([
    api("/api/onboard/defaults").catch(() => ({})),
    api("/api/onboard/plans").catch(() => []),
  ]);
  const advice = STATE.data.cluster?.onboarding || {};
  const open = plans.filter(p => ONBOARD_OPEN.includes(p.status));
  const origin = window.location.origin.startsWith("http://") ? window.location.origin : "";
  $("#mbody").innerHTML = `
    <div class="onboard-role"><span>Recommended role</span><b>${esc(advice.recommended_role || "Review required")}</b>
      <p>${esc(advice.reason || "")}</p></div>
    ${open.length ? `<div class="sec">Open join plans</div><div class="onboard-plans">${open.map(p => `
      <button class="onboard-planrow" onclick="onboardPlan('${esc(p.id)}')"><b>${esc(p.hostname)}</b>
        <span class="dim xs">${esc(p.message)}</span>${onboardStatusPill(p)}</button>`).join("")}</div>` : ""}
    <div class="sec">New join plan</div>
    <p class="muted small">Everything Harvester's installer asks, answered once. The plan gives you a USB image,
      a network-boot service and an ISO boot line that all install this host and join it without questions.</p>
    <div class="onboard-form">
      <fieldset><legend>The host</legend>
        <div class="f3">
          <div class="f"><label>Hostname</label><input id="ob_host" placeholder="node4" autocomplete="off"></div>
          <div class="f"><label>Role ${tip("Default lets Harvester promote the host to the control plane when it needs one. Management always joins it there; worker never; witness holds an etcd vote and nothing else.")}</label>
            <select id="ob_role"><option value="default">Default · promoted when needed</option><option value="management">Management</option>
              <option value="worker">Worker</option><option value="witness">Witness</option></select></div>
          <div class="f"><label>Plan valid for</label><select id="ob_hours"><option value="6">6 hours</option>
            <option value="24" selected>24 hours</option><option value="72">3 days</option></select></div>
        </div>
        <div class="f2">
          <div class="f"><label>Install disk ${tip("Erased and used for Harvester. With several disks, /dev/disk/by-id/… names the right one for certain.")}</label><input id="ob_device" value="/dev/sda"></div>
          <div class="f"><label>Data disk <span class="dim xs">optional</span> ${tip("A second disk for VM and volume data. Left empty, the install disk holds both.")}</label><input id="ob_data" placeholder="/dev/sdb"></div>
        </div>
      </fieldset>
      <fieldset><legend>Management network</legend>
        <div class="f2">
          <div class="f"><label>Port name ${tip("The network port the host reaches the cluster on, as Linux names it: eno1, enp3s0…")}</label><input id="ob_nic" placeholder="eno1"></div>
          <div class="f"><label>MAC address ${tip("Needed for network booting: the PXE service answers this address and no other. Printed on the port or in the host's firmware.")}</label><input id="ob_mac" placeholder="52:54:00:12:34:56"></div>
        </div>
        <div class="f3">
          <div class="f"><label>Address</label><select id="ob_method" onchange="onboardMethod(this.value)"><option value="dhcp">DHCP</option><option value="static">Static</option></select></div>
          <div class="f ob-static" hidden><label>Address / prefix</label><input id="ob_addr" placeholder="192.0.2.54/24"></div>
          <div class="f ob-static" hidden><label>Gateway</label><input id="ob_gw" placeholder="192.0.2.1"></div>
        </div>
        <details class="onboard-more"><summary>DNS, NTP, VLAN and MTU</summary>
          <div class="f2"><div class="f"><label>DNS servers</label><input id="ob_dns" placeholder="1.1.1.1, 8.8.8.8"></div>
            <div class="f"><label>NTP servers</label><input id="ob_ntp" placeholder="pool.ntp.org"></div></div>
          <div class="f2"><div class="f"><label>VLAN</label><input id="ob_vlan" type="number" min="1" max="4094" placeholder="none"></div>
            <div class="f"><label>MTU</label><input id="ob_mtu" type="number" placeholder="1500"></div></div>
        </details>
      </fieldset>
      <fieldset><legend>Signing in to the host</legend>
        <div class="f2">
          <div class="f"><label>Password for the rancher user</label><input id="ob_pass" type="password" autocomplete="new-password"></div>
          <div class="f"><label>SSH keys <span class="dim xs">optional</span></label><textarea id="ob_keys" rows="2" placeholder="ssh-ed25519 AAAA… or github:username"></textarea></div>
        </div>
      </fieldset>
      <fieldset><legend>The cluster</legend>
        <div class="f2">
          <div class="f"><label>Cluster address</label><input id="ob_server" value="${esc(defaults.server_url || "")}" placeholder="https://192.0.2.240:443"></div>
          <div class="f"><label>Harvester version ${tip("The new host must install the release the cluster runs.")}</label><input id="ob_version" value="${esc(defaults.version || "")}" placeholder="1.4.1"></div>
        </div>
        <div class="f"><label>Cluster token</label><input id="ob_token" type="password" autocomplete="off" spellcheck="false"></div>
        <div class="note onboard-token">Homestead does not read the token from the cluster. On any management node, run
          <code>sudo yq eval .token /etc/rancher/rancherd/config.yaml</code> and paste the result. It is kept in a
          Secret for this plan only, and deleted when the node joins, the plan expires or you cancel it.</div>
      </fieldset>
      <fieldset><legend>Delivery</legend>
        <div class="f2">
          <div class="f"><label>Homestead, as the new host reaches it ${tip("iPXE and the installer fetch from this address over plain HTTP, so it must be one the new host can reach on the LAN.")}</label>
            <input id="ob_home" value="${esc(origin)}" placeholder="http://192.0.2.242:8080"></div>
          <div class="f"><label>Mirror <span class="dim xs">optional</span> ${tip("A local copy of the Harvester release files, for a LAN without internet. Left empty, they come from releases.rancher.com.")}</label>
            <input id="ob_mirror" placeholder="http://mirror.lan/harvester/v1.4.1"></div>
        </div>
        <label class="switch"><input type="checkbox" id="ob_confirm" checked> Ask at the host before anything is erased</label>
        <label class="switch"><input type="checkbox" id="ob_skip"> Skip Harvester's hardware checks ${tip("For a lab host below Harvester's minimum CPU, memory or disk. The installer refuses such hosts otherwise.")}</label>
      </fieldset>
    </div>
    <div class="modalactions"><button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn pri" data-need="admin" onclick="onboardCreate()">Create join plan</button></div>`;
  if (window.applyRole) window.applyRole();
};

window.onboardMethod = value => $$(".ob-static").forEach(el => { el.hidden = value !== "static"; });

window.onboardCreate = async () => {
  const value = id => ($("#" + id)?.value || "").trim();
  const body = {
    hostname: value("ob_host"), role: value("ob_role"), hours: +value("ob_hours"),
    device: value("ob_device"), data_disk: value("ob_data"), nic: value("ob_nic"), mac: value("ob_mac"),
    method: value("ob_method"), address: value("ob_addr"), gateway: value("ob_gw"),
    dns: value("ob_dns"), ntp: value("ob_ntp"), vlan: value("ob_vlan"), mtu: value("ob_mtu"),
    password: $("#ob_pass").value, ssh_keys: value("ob_keys"),
    server_url: value("ob_server"), version: value("ob_version"), token: $("#ob_token").value.trim(),
    homestead_url: value("ob_home"), mirror: value("ob_mirror"),
    confirm_wipe: $("#ob_confirm").checked, skipchecks: $("#ob_skip").checked,
  };
  try {
    const plan = await api("/api/onboard/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    toast(`Join plan for ${plan.hostname} created`, "ok");
    onboardPlan(plan.id);
  } catch (e) { toast(e.message, "bad"); }
};

/* One plan: how to boot the host, and what has happened so far. */
window.onboardPlan = async (id, quiet = false) => {
  const plans = await api("/api/onboard/plans").catch(() => []);
  const plan = plans.find(p => p.id === id);
  if (!plan) return toast("That join plan is gone", "bad");
  const open = ONBOARD_OPEN.includes(plan.status) && !plan.expired;
  const title = `Join · ${plan.hostname}`;
  const expires = Math.max(0, Math.round((plan.expires * 1000 - Date.now()) / 3600000));
  const body = `
    <div class="onboard-status ${esc(plan.status)}">
      <div><b>${esc(plan.message)}</b><span>${open ? `Plan open for about ${expires} more hour${expires === 1 ? "" : "s"}` : "This plan is closed; its addresses no longer answer"}</span></div>
      ${onboardStatusPill(plan)}</div>
    <div class="onboard-facts">
      <span><small>Role</small><b>${esc(plan.role)}</b></span>
      <span><small>Erases</small><b class="mono">${esc([plan.device, plan.data_disk].filter(Boolean).join(", "))}</b></span>
      <span><small>Network</small><b class="mono">${esc(plan.network.method === "static" ? plan.network.ip : "DHCP")}${plan.mac ? ` · ${esc(plan.mac)}` : ""}</b></span>
      <span><small>Harvester</small><b class="mono">v${esc(plan.version)}</b></span>
    </div>
    ${open ? `<div class="onboard-ways">
      <section><h4>${icon("disk")}USB stick</h4>
        <p>A 16 MB image with iPXE on it. It gets an address by DHCP and fetches the installer from Homestead,
          so nothing changes on your network. Boot it in <b>UEFI mode with Secure Boot off</b>.</p>
        <ol><li>Download the image.</li><li>Write it to any USB stick with balenaEtcher, Rufus (DD mode) or
          <code>dd if=homestead-join-${esc(plan.hostname)}.img of=/dev/sdX bs=4M</code>.</li><li>Boot the host from it.</li></ol>
        <a class="btn pri" href="${esc(plan.urls.usb)}" download>${icon("import")}Download USB image</a></section>
      <section><h4>${icon("network")}Network boot · no USB</h4>
        <p>The host's own PXE boot, nothing to plug in. A temporary proxy-DHCP service on one node answers
          <b>${esc(plan.mac || "only a known MAC address")}</b> and nothing else, adds boot instructions without
          handing out addresses, and stops by itself.</p>
        <ol><li>Start it below.</li><li>In the host's firmware, put <b>network (PXE, IPv4)</b> boot first, or pick it
          from the one-time boot menu.</li><li>Power the host on.</li></ol>
        <div id="ob_pxe">${plan.mac ? '<span class="dim xs"><span class="spin2"></span> checking</span>' : '<div class="note">Add the host\'s MAC address to the plan to use this: start a new plan with it filled in.</div>'}</div>
        <details class="onboard-more"><summary>Already run a PXE server?</summary>
          <p>Leave this off and chain Homestead's boot script from yours: a netboot.xyz custom entry, pfSense or
            OPNsense network boot, or dnsmasq serving iPXE.</p>
          <pre class="onboard-args">chain ${esc(plan.urls.script)}</pre></details></section>
      <section><h4>${icon("vm")}Harvester ISO</h4>
        <p>Write the <a href="https://releases.rancher.com/harvester/v${esc(plan.version)}/harvester-v${esc(plan.version)}-amd64.iso" target="_blank" rel="noopener">official ISO</a>
          to a stick. At its boot menu, press <b>e</b> on the install entry, add this to the end of the line starting
          <code>linux</code>, then press <b>F10</b>:</p>
        <pre class="onboard-args">harvester.install.automatic=true harvester.install.config_url=${esc(plan.urls.config)}</pre>
        <button class="btn sm" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent).then(() => toast('Copied', 'ok'))">Copy</button></section>
    </div>` : ""}
    <div class="sec">What has happened</div>
    <ol class="onboard-timeline">${(plan.events || []).slice().reverse().map(e => `<li class="${esc(e.kind)}">
      <span>${esc(new Date(e.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}</span><b>${esc(e.message)}</b>${e.from ? `<small>${esc(e.from)}</small>` : ""}</li>`).join("")}</ol>
    <details class="onboard-more"><summary>The configuration the installer receives</summary>
      <pre id="ob_config" class="onboard-config">loading…</pre></details>
    <div class="modalactions">${open ? `<button class="btn danger" data-need="admin" onclick="onboardRevoke('${esc(plan.id)}')">Cancel plan</button>` : ""}
      <button class="btn" onclick="modalBack()">Back</button></div>`;
  if (quiet && $("#mtitle").textContent === title) {
    const details = $("#mbody details.onboard-more");
    const wasOpen = details?.open;
    $("#mbody").innerHTML = body;
    if (wasOpen) $("#mbody details.onboard-more").open = true;
  } else {
    childModal(title, body, true, "onboard");
  }
  if (window.applyRole) window.applyRole();
  api(`/api/onboard/config?id=${encodeURIComponent(id)}`).then(text => { const pre = $("#ob_config"); if (pre) pre.textContent = text; })
    .catch(e => { const pre = $("#ob_config"); if (pre) pre.textContent = e.message; });
  if (open && plan.mac) onboardPxeStatus(plan);
  clearTimeout(ONBOARD.timer);
  // Follow the host while the plan is open and this dialog is on screen.
  if (open) ONBOARD.timer = setTimeout(() => {
    if (!$("#modal").classList.contains("hidden") && $("#mtitle").textContent === title) onboardPlan(id, true);
  }, 5000);
};

async function onboardPxeStatus(plan) {
  const host = $("#ob_pxe");
  if (!host) return;
  const status = await api(`/api/onboard/pxe?id=${encodeURIComponent(plan.id)}`).catch(() => ({ running: false, log: [] }));
  const nodes = (STATE.data.cluster?.nodes || []).filter(n => n.ready).map(n => n.name);
  if (!$("#ob_pxe")) return;
  $("#ob_pxe").innerHTML = status.running
    ? `<div class="onboard-pxe on"><b>Running on ${esc(plan.pxe?.node || "a node")}</b>
        <span class="dim xs">${esc(plan.pxe?.interface || "")} · ${esc(plan.pxe?.subnet || "")} · ${esc(status.phase)}</span>
        <button class="btn sm danger" data-need="admin" onclick="onboardPxe('${esc(plan.id)}', false)">Stop</button></div>
       ${(status.log || []).length ? `<pre class="onboard-log">${esc(status.log.slice(-8).join("\n"))}</pre>` : '<div class="dim xs">Waiting for the host to ask for a boot.</div>'}`
    : `<div class="onboard-pxeform">
        <div class="f"><label>Run on</label><select id="ob_pxenode">${nodes.map(n => `<option>${esc(n)}</option>`).join("")}</select></div>
        <div class="f"><label>Interface</label><input id="ob_pxeif" value="mgmt-br"></div>
        <div class="f"><label>Subnet <span class="dim xs">optional</span></label><input id="ob_pxenet" placeholder="192.0.2.0/24"></div></div>
       <button class="btn" data-need="admin" onclick="onboardPxe('${esc(plan.id)}', true)">Start network boot</button>`;
  if (window.applyRole) window.applyRole();
}

window.onboardPxe = async (id, start) => {
  try {
    if (start) {
      await api("/api/onboard/pxe/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, node: $("#ob_pxenode")?.value, interface: $("#ob_pxeif")?.value.trim(),
          subnet: $("#ob_pxenet")?.value.trim() }) });
      toast("Network boot is on; power the host on and boot it from the network", "ok");
    } else {
      await api("/api/onboard/pxe/stop", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }) });
      toast("Network boot stopped", "ok");
    }
    onboardPlan(id, true);
  } catch (e) { toast(e.message, "bad"); }
};

window.onboardRevoke = async id => {
  if (!confirm("Cancel this join plan?\n\nIts USB stick and boot addresses stop working, network boot stops, and the cluster token is deleted.")) return;
  try {
    await api("/api/onboard/revoke", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast("Join plan cancelled", "ok");
    onboardPlan(id, true);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- cleanup ---------------- */
/* What is left over after hosts come and go, shown on the Cluster page. */
window.clusterCleanupPaint = async () => {
  const host = $("#clusterCleanup");
  if (!host) return;
  const [report, plans] = await Promise.all([
    api("/api/cluster/cleanup").catch(() => null),
    api("/api/onboard/plans").catch(() => []),
  ]);
  if (!$("#clusterCleanup")) return;
  if (!report) { host.innerHTML = ""; return; }
  const open = plans.filter(p => ONBOARD_OPEN.includes(p.status));
  const rows = [
    ...open.map(p => `<div class="cleanup-row"><div><b>Joining ${esc(p.hostname)}</b><span>${esc(p.message)}</span></div>
      <button class="btn sm" onclick="clusterOnboardingOpen('${esc(p.id)}')">Open</button></div>`),
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
  host.innerHTML = `<div class="sec">Hosts coming and going ${tip("Join plans in progress, and what a removed host can leave behind: a node that stopped reporting, a Cluster API machine with no node, a Longhorn node record with no host.")}</div>
    <div class="card flat cleanup-card">${rows.join("") || '<div class="cleanup-row"><div><b>Nothing to tidy</b><span>Every node is ready and nothing is left over.</span></div></div>'}
    ${report.finished_plans ? `<div class="cleanup-row"><div><b>${report.finished_plans} finished join plan${report.finished_plans === 1 ? "" : "s"}</b><span>Kept for their history</span></div>
      <button class="btn sm" data-need="admin" onclick="clusterCleanup('plans','')">Clear</button></div>` : ""}</div>`;
  if (window.applyRole) window.applyRole();
};

window.clusterOnboardingOpen = async id => { await clusterOnboarding(); onboardPlan(id); };

window.clusterCleanup = async (kind, name, force = false) => {
  const what = kind === "machine" ? `the leftover Cluster API machine ${name}` : kind === "longhorn"
    ? `Longhorn's record of ${name}` : "the finished join plans";
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

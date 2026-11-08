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
  // Each screen's answer in a few words; the why is on hover (design.md, Verbosity budget).
  const role = g.management_count < 3
    ? `<b>Default Role</b> ${tip(`This cluster has ${g.management_count} management host${g.management_count === 1 ? "" : "s"}; Harvester promotes new hosts to management until there are three.`)}`
    : `<b>Default Role</b>: it joins as a worker ${tip("With three management hosts already, a new one joins as a worker. Worker Role says so outright.")}`;
  const rows = [
    ["Installation mode", "<b>Join an existing Harvester cluster</b>"],
    ["Node role", role],
    ["Password", "The <span class=\"mono\">rancher</span> password for SSH and the console."],
    ["Installation disk", "An SSD of 240 GiB or more. <b>It is erased.</b>"],
    ["Data disk", `A second disk, if it has one ${tip("For VM and volume data. On a single-disk machine, leave it as the installation disk.")}`],
    ["Persistent size", "The default (150 GiB)."],
    ["Hostname", g.hostname ? `${guideCopy(g.hostname)} <span class="dim xs">- the next free name</span>`
      : "A unique lowercase name."],
    ["Management network", `The card on your LAN; active-backup, MTU 1500 ${tip("The installer shows which cards have a link; add a second for a bond if you cabled two. VLAN: none, unless your other hosts use one. IPv4: DHCP with a reservation on your router, or Static with an address outside the DHCP range.")}`],
    ["DNS servers", "Your router, for a static address."],
    ["Management address", g.vip ? `${guideCopy(g.vip)} <span class="dim xs">- this cluster's VIP</span>` : "This cluster's VIP (Homestead could not read it)."],
    ["Cluster token", "The token from step 2."],
    ["NTP servers", g.ntp.length ? guideCopy(g.ntp.join(",")) : "The default."],
    ["Proxy address", g.proxy ? guideCopy(g.proxy) : "Empty."],
    ["SSH keys", "Optional: <span class=\"mono\">https://github.com/&lt;you&gt;.keys</span>."],
    ["Remote config URL", "Empty."],
    ["Confirm", "Install. It takes 10-20 minutes, then reboots and joins."],
  ];
  return `<div class="guide-screens">${rows.map(([screen, answer]) =>
    `<div class="guide-screen"><span>${screen}</span><div>${answer}</div></div>`).join("")}</div>`;
}

window.clusterOnboarding = async () => {
  // Not Harvester: its installer is not how hosts join; k3s's or RKE2's is.
  if (STATE.platform && !STATE.platform.harvester) return platformJoinGuide();
  modal("Add a Harvester host", '<div class="empty"><span class="spin2"></span>reading the cluster</div>', true, "onboard");
  let g;
  try { g = await api("/api/onboard/guide"); } catch (e) {
    $("#mbody").innerHTML = `${UI.callout("bad", "", `${esc(e.message)}`)}`; return;
  }
  ONBOARD.known = new Set(g.nodes.map(n => n.name));
  $("#mbody").innerHTML = `
    <p class="muted small">Install Harvester on the new host, then join this cluster using the settings below.</p>
    <section class="guide-step"><h4><span>1</span>Boot the installer</h4>
      ${g.iso ? `<p>This cluster runs <b>Harvester v${esc(g.version)}</b>; the new host needs the same release.</p>
        <div class="row"><a class="btn sm pri" href="${safeHref(g.iso)}" target="_blank" rel="noopener">Harvester v${esc(g.version)} ISO (${esc(g.arch)})</a>
          <a class="btn sm" href="${safeHref(g.checksums)}" target="_blank" rel="noopener">Checksums</a></div>`
        : '<p>Homestead could not read this cluster\'s Harvester version - use the ISO matching the version on the Cluster page.</p>'}
      <p class="dim xs">Write it to a USB stick with Rufus (in DD mode) or balenaEtcher, or mount it as virtual media from the
        server's BMC (iDRAC, iLO, IPMI). Boot from it and choose <b>Harvester Installer</b>.</p></section>
    <section class="guide-step"><h4><span>2</span>Read the cluster token</h4>
      <p>It is on every management host. Sign in as <span class="mono">rancher</span>, with the password set when that host was installed:</p>
      ${guideCopy(`ssh rancher@${g.token_host || "<management-node>"}`)}
      ${guideCopy(g.token_command)}
      <p class="dim xs">Copy the value after <span class="mono">token:</span>. Anyone holding it can join a machine to this cluster,
        so keep it out of chats and notes.${g.token_host ? "" : ` Management nodes: ${esc(g.nodes.filter(n => n.management).map(n => n.name).join(", ") || "none found")}.`}</p></section>
    <section class="guide-step"><h4><span>3</span>Answer the installer</h4>
      <p class="dim xs">In the order Harvester asks; some releases put the password or role a screen earlier.</p>
      ${guideScreens(g)}</section>
    <section class="guide-step"><h4><span>4</span>Watch it join</h4><div id="guideWatch"></div></section>`;
  const sections = [...document.querySelectorAll("#mbody .guide-step")];
  const steps = sections.map(section => ({title:section.querySelector("h4").textContent.replace(/^\d+/, "").trim(), html:section.innerHTML.replace(/<h4>[\s\S]*?<\/h4>/, "")}));
  $("#mbody").innerHTML = UI.lead("Use the Harvester installer on the new host to join this cluster.") + stepper("host_join", steps, UI.button("View hosts", "closeModal();go('nodes')", {kind:"pri"}));
  guideWatch();
};

async function guideWatch() {
  clearTimeout(ONBOARD.timer);
  const host = $("#guideWatch");
  if (!host || $("#modal").classList.contains("hidden")) return;
  let nodes = null, unread = "";
  try { nodes = await api("/api/nodes", { keep: true }); } catch (e) { unread = e.message; }
  if (!$("#guideWatch")) return;
  const fresh = (nodes || []).filter(n => !ONBOARD.known.has(n.name));
  // Unread hosts are not "waiting for a new host"; it keeps trying (#620).
  host.innerHTML = unread ? UI.callout("warn", "Could not read the hosts", `${esc(unread)} · trying again every 10 seconds`) : fresh.length ? fresh.map(n => `<div class="guide-joined ${n.status === "Ready" ? "ok" : ""}">
      <b>${esc(n.name)}</b><span>${n.status === "Ready" ? "joined and Ready" : "registered - finishing setup"}</span>
      <button class="btn sm" onclick="closeModal();go('nodes')">Hosts</button></div>`).join("")
    : '<div class="dim xs"><span class="spin2"></span> Waiting for a new host. It appears here as soon as Harvester registers it, about a minute after it reboots.</div>';
  ONBOARD.timer = setTimeout(guideWatch, 10000);
}

/* After a removal: what the cluster still holds of hosts that are gone, with
   the same buttons the Cluster page's cleanup card has. */
async function removalLeftovers() {
  const host = $("#rm_left");
  if (!host) return;
  const report = await api("/api/cluster/cleanup").catch(e => ({ unread: e.message }));
  if (!$("#rm_left")) return;
  // Unread is not "the cluster is clean" (#620).
  if (report.unread) {
    host.innerHTML = `${UI.callout("warn", "Could not read what is left over", esc(report.unread))}
      <button class="btn sm" style="margin-top:6px" onclick="removalLeftovers()">Try again</button>`;
    return;
  }
  const rows = cleanupRows(report, false);
  host.innerHTML = rows.length ? `<div class="card flat cleanup-card">${rows.join("")}</div>
      <div class="dim xs" style="margin-top:6px">Replica records can take a few minutes to rebuild elsewhere before Longhorn's record of the host can go.</div>`
    : '<div class="note good">Nothing is left over: the cluster is clean.</div>';
  if (window.applyRole) window.applyRole();
}
window.removalLeftovers = removalLeftovers;

/* ---------------- cleanup ---------------- */
/* What is left over after hosts come and go, shown on the Cluster page. */
function cleanupRows(report, withDead = true) {
  return [
    ...(withDead ? report.dead_nodes : []).map(n => `<div class="cleanup-row bad"><div><b>${esc(n.name)} is not ready</b>
      <span>${esc((n.roles || []).join(" · ") || "worker")}${n.since ? ` · since ${esc(fmtAt(n.since))}` : ""}</span></div>
      <button class="btn sm danger" data-need="admin" onclick="nodeRemoval(${jsq(n.name)})">Remove…</button></div>`),
    ...report.stale_machines.map(m => `<div class="cleanup-row"><div><b>Leftover machine ${esc(m.name)}</b>
      <span>${m.stuck ? `Being deleted, but waiting on ${esc(m.node || "its host")}, which will not answer`
        : `Cluster API still lists it${m.node ? ` for ${esc(m.node)}, which is gone` : ` (${esc(m.phase)})`}`}</span></div>
      <button class="btn sm ${m.stuck ? "danger" : ""}" data-need="admin" onclick="clusterCleanup('machine',${jsq(m.name)},${m.stuck})">${m.stuck ? "Force" : "Delete"}</button></div>`),
    ...report.stale_longhorn.map(n => `<div class="cleanup-row"><div><b>Longhorn still lists ${esc(n.name)}</b>
      <span>${n.replicas ? `${n.replicas} replica${n.replicas === 1 ? "" : "s"} still recorded there. They rebuild elsewhere on their own; force it if the host is gone for good` : "No replicas left; safe to delete"}</span></div>
      <button class="btn sm ${n.replicas ? "danger" : ""}" data-need="admin" onclick="clusterCleanup('longhorn',${jsq(n.name)},${!!n.replicas})">${n.replicas ? "Force" : "Delete"}</button></div>`),
    ...(report.passwords || []).map(s => `<div class="cleanup-row"><div><b>${esc(s.node)}'s node password</b>
      <span>A Secret k3s/RKE2 keeps for a host that is gone. While it is there, a rebuilt host called ${esc(s.node)} cannot join</span></div>
      <button class="btn sm" data-need="admin" onclick="clusterCleanup('password',${jsq(s.name)})">Delete</button></div>`),
    ...(report.pinned_workloads || []).map(w => `<div class="cleanup-row"><div><b>${esc(w.name)} is pinned to ${esc(w.node)}</b>
      <span>That host is gone, so ${esc(w.name)} waits for it. Unpinning lets it run on any host that suits it</span></div>
      <button class="btn sm" data-need="admin" onclick="clusterCleanup('pin',${jsq(`${w.namespace}/${w.name}`)})">Unpin</button></div>`),
    ...(report.pinned_volumes || []).map(v => `<div class="cleanup-row bad"><div><b>${esc(`${v.namespace}/${v.claim}`)} was kept on ${esc(v.node)}</b>
      <span>Its data went with that host${v.users?.length ? `, so ${esc(v.users.join(", "))} cannot start` : ""}. Making it again empty lets ${v.users?.length === 1 ? "it" : "them"} start elsewhere</span></div>
      <button class="btn sm danger" data-need="admin" onclick="clusterCleanup('pinned-volume',${jsq(`${v.namespace}/${v.claim}`)},true)">Make it empty</button></div>`),
    ...(report.attachments || []).map(a => `<div class="cleanup-row"><div><b>A volume is still attached to ${esc(a.node)}</b>
      <span>${esc(a.name)} - that host is gone, so the volume cannot attach anywhere else until it is released</span></div>
      <button class="btn sm" data-need="admin" onclick="clusterCleanup('attachment',${jsq(a.name)})">Release</button></div>`),
  ];
}

window.clusterCleanupPaint = async () => {
  const host = $("#clusterCleanup");
  if (!host) return;
  const report = await api("/api/cluster/cleanup").catch(e => ({ unread: e.message }));
  if (!$("#clusterCleanup")) return;
  // Unread is said; nothing is kept to be drawn first next time, neither the
  // last card read nor this failure (#620).
  if (report.unread) {
    STATE.data.cleanupHtml = "";
    host.innerHTML = `<div class="sec">Hosts coming and going</div>
      ${UI.callout("warn", "Could not read what hosts left behind", esc(report.unread))}`;
    return;
  }
  const rows = cleanupRows(report);
  // Kept, so the next refresh draws it straight away instead of the page
  // shrinking while it is fetched again and growing back when it arrives.
  STATE.data.cleanupHtml = `<div class="sec">Hosts coming and going ${tip("What a removed host can leave behind: a host that stopped reporting, a Cluster API machine with no host, a Longhorn node record with no host, a k3s/RKE2 node password, apps and volumes tied to a host that is gone, a volume still attached there.")}</div>
    <div class="card flat cleanup-card">${rows.join("") || '<div class="cleanup-row"><div><b>Nothing to tidy</b><span>Every host is ready and nothing is left over.</span></div></div>'}</div>`;
  host.innerHTML = STATE.data.cleanupHtml;
  if (window.applyRole) window.applyRole();
};

/* Each cleanup's title and button: what it does, in a few words. */
const SETUP_CLEANUP_TITLES = {
  password: ["Delete node password", "Delete password"],
  pin: ["Let it run anywhere", "Unpin"],
  attachment: ["Release the volume", "Release"],
  "pinned-volume": ["Make the volume again, empty", "Make it again"],
};
window.clusterCleanup = async (kind, name, force = false) => {
  const asks = {
    password: `Delete the node password ${name}?\n\nA host rebuilt under that name can then join the cluster.`,
    pin: `Let ${name} run on any host?\n\nIts pin to a host that is gone is taken off.`,
    attachment: `Release ${name}?\n\nIts volume can then attach on another host.`,
    "pinned-volume": `Make ${name} again, empty?\n\nIts data was on a host that is gone and cannot be read back. The claim is made again under the same name, empty, so its app can start on another host.`,
  };
  if (asks[kind]) {
    if (!(await ask(asks[kind], { title: SETUP_CLEANUP_TITLES[kind][0], ok: SETUP_CLEANUP_TITLES[kind][1], danger: kind !== "pin" }))) return;
  } else {
    const what = kind === "machine" ? `the leftover Cluster API machine ${name}` : `Longhorn's record of ${name}`;
    const forced = kind === "machine"
      ? "\n\nIts deletion is waiting on a host that will not answer. Forcing clears the finalizers holding it; only do this if the host is gone for good."
      : "\n\nThe replica records Longhorn keeps there are deleted too, and it rebuilds them from the remaining copies. Only do this if the host is gone for good.";
    if (!(await ask(`${force ? "Force-delete" : "Delete"} ${what}?${force ? forced : ""}`, { title: force ? "Force-delete" : "Delete", ok: force ? "Force-delete" : "Delete", danger: true }))) return;
  }
  try {
    const result = await api("/api/cluster/cleanup/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, name, force }) });
    toast(result.message || "Done", "ok");
    clusterCleanupPaint();
    if ($("#rm_left")) removalLeftovers();
  } catch (e) { toast(e.message, "bad"); }
};

/* Which host to remove: the failed ones first, each with how long it has
   been down. A host still running is shown, but has to be stopped first -
   a running node registers itself again. */
window.clusterRemovePick = async () => {
  modal("Remove a host", '<div class="empty"><span class="spin2"></span>reading the hosts</div>', true);
  let nodes;
  try { nodes = await api("/api/nodes"); }
  catch (e) {
    // Hosts that could not be read are not hosts that are all Ready (#620).
    $("#mbody").innerHTML = `<div class="empty"><b>Could not read the hosts</b><br><span class="dim small">${esc(e.message)}</span></div>
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}`;
    return;
  }
  const down = nodes.filter(n => n.status !== "Ready"), up = nodes.filter(n => n.status === "Ready");
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">Remove a retired or permanently failed host. Review quorum, volume replicas and pinned apps and VMs before confirming.</p>
    ${down.length ? `<div class="sec">Not ready</div><div class="cleanup-card card flat">${down.map(n => `<div class="cleanup-row bad"><div>
        <b>${esc(n.name)}</b><span>${esc((n.roles || []).join(" · ") || "worker")} · ${esc(n.status || "not ready")}</span></div>
        <button class="btn sm danger" onclick="nodeRemoval(${jsq(n.name)})">Check and remove…</button></div>`).join("")}</div>`
      : '<div class="note good">Every host is Ready: none has failed.</div>'}
    ${up.length ? `<div class="sec">Running ${tip("A running host re-registers itself, so it cannot simply be deleted. Drain it, stop Kubernetes on it (or uninstall it), power it off, and it shows here as Not ready.")}</div>
      <div class="cleanup-card card flat">${up.map(n => `<div class="cleanup-row"><div><b>${esc(n.name)}</b>
        <span>${esc((n.roles || []).join(" · ") || "worker")} · Ready - stop it first to retire it</span></div>
        <button class="btn sm" onclick="nodeRemoval(${jsq(n.name)})">What it takes…</button></div>`).join("")}</div>` : ""}
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}`;
};

/* Removing a node: what it costs, checked first, then Harvester's order. */
window.nodeRemoval = async name => {
  childModal(`Remove · ${name}`, '<div class="empty"><span class="spin2"></span>checking what removing it would do</div>', true);
  let plan;
  try { plan = await api(`/api/cluster/removal?node=${encodeURIComponent(name)}`); }
  catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Could not check</b><br><span class="dim small">${esc(e.message)}</span></div>`; return; }
  $("#mbody").innerHTML = `
    ${plan.blockers.length ? `${UI.callout("bad", { html: `Not now.` }, `<ul>${plan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`)}` : ""}
    ${plan.warnings.length ? `${UI.callout("warn", { html: `Worth knowing first.` }, `<ul>${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`)}` : ""}
    ${plan.ok && (plan.disk_jobs || []).length ? `${UI.callout("bad", { html: `Saved disk ${plan.disk_jobs.length === 1 ? "job" : "jobs"} on ${esc(name)}.` }, `${tip("These jobs hold this host's disks. Most can't be cancelled once started; a disk retire can be rolled back early. A host that may come back keeps them so they can resume; removing a host gone for good leaves them with nothing to finish on.")}
      <ul>${plan.disk_jobs.map(j => `<li>${esc(j.text)}</li>`).join("")}</ul><span id="rm_jobs_hint"></span>`)}` : ""}
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
    ${removalLosses(plan)}
    <div class="sec">What happens</div>
    <ol class="removal-steps" id="rm_steps">${plan.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>
    <p class="muted small">Homestead cannot reach a dead host to wipe it. If it comes back, reinstall it before it
      rejoins; a host that still has its old install will try to register again.</p>
    ${(plan.lost_volumes.length || (plan.pinned_volumes || []).length) && plan.ok ? `<label class="switch dependency-confirm"><input type="checkbox" id="rm_loss"
      onchange="nodeRemovalMode()"> <span id="rm_loss_text"></span></label>` : ""}
    ${(plan.disk_jobs || []).length && plan.ok ? `<label class="switch dependency-confirm"><input type="checkbox" id="rm_jobs"
      onchange="nodeRemovalMode()"> <span>Remove it anyway. ${esc(plan.disk_jobs_stay || "")}</span></label>` : ""}
    ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>
      ${plan.ok ? `<button class="btn danger" id="rm_go" data-need="admin"
        onclick="nodeRemove(${jsq(name)})">Remove ${esc(name)}</button>` : ""}`)}`;
  window.__removalPlan = plan;
  nodeRemovalMode();
  if (window.applyRole) window.applyRole();
};

/* What is lost, named the way a person knows it: the claim and the apps
   using it, not Longhorn's volume ID. */
function removalLosses(plan) {
  const who = users => users?.length ? ` <span class="dim xs">used by ${users.map(esc).join(", ")}</span>` : "";
  const lost = plan.lost_detail || [];
  const pinned = plan.pinned_volumes || [], apps = plan.pinned_workloads || [];
  if (!lost.length && !pinned.length && !apps.length) {
    return plan.ok ? '<div class="note good" style="margin-top:10px"><b>No data is lost.</b> Every volume it held has a healthy copy elsewhere, and nothing is tied to it.</div>' : "";
  }
  return `<div class="sec">What is lost</div><div class="removal-losses">
    ${lost.length ? `${UI.callout("bad", { html: `${lost.length} volume${lost.length === 1 ? "" : "s"} with no other copy.` }, `${tip("Longhorn kept every healthy copy of these on this host. Removing it gives up on that data; a backup is the way back - restore it under Data protection afterwards.")}
      <ul>${lost.map(v => `<li><span class="mono">${esc(v.claim ? `${v.namespace}/${v.claim}` : v.volume)}</span>${who(v.users)}</li>`).join("")}</ul>`)}` : ""}
    ${pinned.length ? `${UI.callout("bad", { html: `${pinned.length} volume${pinned.length === 1 ? " was" : "s were"} kept on the host itself.` }, `${tip("Volumes like k3s's local-path live on one host's disk, so their data went with it. Gone for good makes each again under the same name, empty, so its app can start on another host.")}
      <ul>${pinned.map(v => `<li><span class="mono">${esc(`${v.namespace}/${v.claim}`)}</span> <span class="dim xs">${esc(v.class)} · ${esc(v.size)}</span>${who(v.users)}</li>`).join("")}</ul>`)}` : ""}
    ${apps.length ? `${UI.callout("warn", { html: `${apps.length} app${apps.length === 1 ? " is" : "s are"} pinned to it and would wait for it` }, `
      ${tip("They were told to run only on this host - by a Pin in Edit, or for hardware it had. Gone for good takes the pin off, so they start on another host if one suits them.")}
      <ul>${apps.map(w => `<li>${esc(w.name)} <span class="dim xs">${esc(w.namespace)}</span></li>`).join("")}</ul>`)}` : ""}
  </div>`;
}

/* Gone for good adds its own steps ahead of the standard ones. */
window.nodeRemovalMode = () => {
  const plan = window.__removalPlan, list = $("#rm_steps");
  if (!plan || !list) return;
  const gone = $('input[name="rm_mode"]:checked')?.value === "gone";
  // In the order remove_node runs them: VMs, pods and attachments before the
  // node goes; the machine's finalizers and the replica records after.
  const [pods, vms, attachments, replicas, machine] = plan.gone_steps;
  // Then, off the old host: its pinned apps freed, its volumes made again.
  const after = (plan.gone_steps || []).slice(5).filter(step => !/^No (apps|volumes)/.test(step));
  const steps = gone ? [plan.steps[0], vms, pods, attachments, plan.steps[1], plan.steps[2], machine, replicas, plan.steps[3], ...after]
    : plan.steps;
  list.innerHTML = steps.map(s => `<li>${esc(s)}</li>`).join("");
  const button = $("#rm_go");
  if (button) button.textContent = gone ? `Remove ${plan.node} for good` : `Remove ${plan.node}`;
  // What has to be accepted: the volumes with no other copy, and - gone for
  // good - the ones kept on the host, which are made again empty.
  const lost = plan.lost_volumes.length, pinned = gone ? (plan.pinned_volumes || []).length : 0;
  const text = $("#rm_loss_text"), box = $("#rm_loss");
  if (text) {
    const parts = [lost ? `${lost} volume${lost === 1 ? "" : "s"} that only ${plan.node} held` : "",
      pinned ? `${pinned} volume${pinned === 1 ? "" : "s"} kept on it, made again empty` : ""].filter(Boolean);
    box.closest("label").hidden = !parts.length;
    text.textContent = `Remove it, losing ${parts.join(", and ")}`;
  }
  // A saved disk job: the standard way waits for it; gone for good leaves it,
  // once the person accepts that.
  const jobs = (plan.disk_jobs || []).length, jobBox = $("#rm_jobs"), hint = $("#rm_jobs_hint");
  if (jobBox) jobBox.closest("label").hidden = !gone;
  // What leaving them means depends on their kinds; the server words it (disk_jobs_stay).
  if (hint) hint.textContent = gone ? `${jobs === 1 ? "Gone for good leaves it as it is." : "Gone for good leaves them as they are."} ${plan.disk_jobs_stay || ""}`
    : "The standard way waits for them: open each job first, or choose It is gone for good.";
  if (button) button.disabled = !!(box && !box.closest("label").hidden && !box.checked)
    || !!jobs && !(gone && jobBox?.checked);
};

window.nodeRemove = async name => {
  const gone = $('input[name="rm_mode"]:checked')?.value === "gone";
  if (!(await ask(gone
    ? `Remove ${name} for good?\n\nIts pods and VMs are force-stopped so they start elsewhere, its volume attachments and replica records are let go, and its machine is deleted even if something still waits on it. This cannot be undone from Homestead.`
    : `Remove ${name} from the cluster?\n\nThis cannot be undone from Homestead.`, { title: "Remove host", ok: gone ? "Remove for good" : "Remove host", danger: true }))) return;
  const button = $("#rm_go");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spin2"></span> removing'; }
  try {
    const result = await api("/api/cluster/remove-node", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: name, accept_loss: !!$("#rm_loss")?.checked, gone,
        // Jobs it leaves, by id, as the review listed them: one started since still refuses.
        accept_jobs: gone && $("#rm_jobs")?.checked ? (window.__removalPlan?.disk_jobs || []).map(j => j.id) : [] }) });
    $("#mbody").innerHTML = `${UI.callout("ok", { html: `${esc(name)} is out of the cluster.` }, ``)}
      <ol class="removal-steps done">${result.log.map(l => `<li>${esc(l)}</li>`).join("")}</ol>
      <div class="sec">Anything left over</div><div id="rm_left"><div class="dim small"><span class="spin2"></span> checking</div></div>
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn pri" onclick="closeModal(); if (STATE.view === 'cluster') { resetPaint(); viewCluster(); }">Done</button>`)}`;
    removalLeftovers();
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = `Remove ${name}`; }
  }
};

/* Homestead views: Import: bringing apps and VMs in from another system. Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- import ---------------- */
/* rsync restarts its percentage for every folder, so the job announces each
   one and the bar shows how far through the whole set the copy is. */
function importProgressCell(job) {
  const done = job.state === "done", failed = job.state === "failed";
  if (job.percent == null && !done) {
    return `<div class="dim xs">${failed ? "stopped before reporting" : "starting…"}</div>`;
  }
  const percent = done ? 100 : Math.max(2, job.percent || 0);
  if (failed && job.error) {
    return `<div class="jobmeter"><span class="failed" style="width:${percent}%"></span></div>
      <div class="xs" style="color:#ffb4b8">${esc(job.error)}</div>
      ${job.error_detail ? `<div class="dim xs mono" data-tip="${esc(job.error_detail)}">${esc(job.error_detail.slice(0, 48))}${job.error_detail.length > 48 ? "…" : ""}</div>` : ""}`;
  }
  const label = done ? "copied"
    : job.steps > 1 ? `folder ${job.step || 1} of ${job.steps}${job.folder ? ` · ${job.folder}` : ""}`
    : job.folder ? esc(job.folder) : "copying";
  return `<div class="jobmeter"><span class="${failed ? "failed" : ""}" style="width:${percent}%"></span></div>
    <div class="dim xs mono">${done ? "100%" : `${job.percent ?? 0}%`}${job.rate ? ` · ${esc(job.rate)}` : ""}</div>
    <div class="dim xs">${esc(label)}</div>`;
}

async function viewImport() {
  // What could not be read is named on the page, and no class or namespace
  // list is invented for the dialogs that use these (#620).
  const missed = [];
  const part = (path, label, fallback) => api(path).catch(e => { missed.push(`${label}: ${e.message}`); return fallback; });
  // Unread hosts keep the last ones read; the page says they could not be read.
  const [, nodes] = await Promise.all([loadHardwareFeatures(), part("/api/nodes", "hosts", null)]);
  if (nodes?.length) STATE.data.nodes = nodes;
  const [srcs, jobs, namespaces, storageClasses] = await Promise.all([
    api("/api/sources"), part("/api/imports", "recent imports", []),
    part("/api/namespaces", "namespaces", null), part("/api/storageclasses", "storage classes", null),
  ]);
  const classFacts = await part("/api/storageclasses?facts=1", "storage class details", null);
  STATE.data.classFacts = classFacts ? classFacts.facts || {} : STATE.data.classFacts || {};
  STATE.data.srcs = srcs; STATE.data.importNamespaces = namespaces; STATE.data.importStorageClasses = storageClasses;
  paint(`${UI.pageHeader(`Import`, `Bring apps in from an Unraid or Docker server, or a Docker Compose file. VMs are imported under Virtual machines.`, `${menuButton("＋ Import", [{ label: "From an Unraid or Docker server", icon: "import", run: "srcAdd()", need: "admin" },
        { label: "From a Docker Compose file", icon: "box", run: "composeImport()", need: "operator" },
        // VM imports moved to their own page; this points there for a release or so.
        { label: "A VM, from Unraid or a disk image", icon: "vm", run: "go('vmimport')", tip: "Now under Virtual machines › Import" }])}`)}
    ${UI.notices(missed.map(text => ["warn", text]), "Parts of this page could not be read")}

    <div class="sec">Unraid and Docker servers ${tip("Containers on another server - Unraid, or any Linux host running Docker - with their settings and appdata. Homestead reaches the server over SSH.")}</div>
    ${srcs.length ? `<div class="grid g3">${srcs.map(importSourceCard).join("")}</div>
    <div class="dim small" style="margin-top:10px">Each imported container gets its image, ports, variables and devices, and its
      folders are copied into Longhorn volumes. It is created <b>stopped</b>: start it in Containers once the copy finishes.</div>`
      : importSourceSteps()}

    ${jobs.length ? `<div class="sec">Transfers</div>
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="imports" class="tbl stack"><thead><tr>
      <th>App</th><th>Job</th><th>State</th><th>Progress</th><th data-nosort>Started</th><th></th></tr></thead><tbody>
      ${jobs.map(j => `<tr><td><b>${esc(j.app || "—")}</b>${j.kind === "chown" ? ` ${UI.chip("ownership", "neutral")}` : ""}${j.namespace && j.namespace !== defaultNamespace() ? `<div class="dim xs mono">${esc(j.namespace)}</div>` : ""}</td>
        <td class="mono small dim">${esc(j.name)}</td>
        <td>${UI.chip(j.state, j.state === "done" ? "ok" : j.state === "failed" ? "bad" : "neutral")}</td>
        <td style="min-width:150px">${importProgressCell(j)}</td>
        <td class="small dim">${fmtWhen(j.start)}</td>
        <td data-actions>${actionBar([{ label: "Logs", run: `jobLogs(${jsq(j.namespace || defaultNamespace())},${jsq(j.name)})` },
          { label: j.state === "running" ? "Cancel" : "Remove", run: `importRemove(${jsq(j.name)},${jsq(j.state)},${jsq(j.namespace || defaultNamespace())})`, need: "admin", danger: j.state === "failed",
            tip: j.state === "running" ? "Stop this copy and remove its job" : "Remove this job; it keeps referencing the volume until it is gone" }])}</td></tr>`).join("")}
    </tbody></table></div></div>` : ""}

`);
  // A copy under way moves faster than the page's 15-second refresh.
  clearTimeout(window.__importFollow);
  if (jobs.some(j => !["done", "failed", "complete", "completed", "succeeded"].includes(String(j.state || "").toLowerCase())))
    window.__importFollow = setTimeout(() => { if (STATE.view === "imports") refresh(); }, 5000);
}

const SOURCE_KINDS = { unraid: "Unraid", proxmox: "Proxmox", ssh: "Docker host" };
/* Servers are shared by both Import tabs: after a change, draw the one that is open. */
const importRepaint = () => { resetPaint(); STATE.view === "vmimport" && window.viewVmImport ? viewVmImport() : viewImport(); };

/* A server to import from: what it is, and the one thing to do next -
   verify its SSH key, or pick its containers. */
function importSourceCard(s) {
  const trusted = !!s.ssh_trust;
  return `<div class="card flat importsource">
    <div class="between"><div><div class="ctitle">${esc(s.name)}</div>
      <div class="csub">${esc(SOURCE_KINDS[s.kind] || s.kind)} · ${esc(s.user)}@${esc(s.host)}</div></div>
      <button class="btn sm danger" data-need="admin" title="Remove this server" onclick="srcDel(${jsq(s.name)})">✕</button></div>
    <div class="drow"><div class="dl">Appdata</div><div class="dv mono small">${esc(s.base_path)}</div></div>
    <div class="drow"><div class="dl">SSH key</div><div class="dv">${trusted ? '<span class="ok-t">Verified</span>' : '<span class="med-t">Not verified yet</span>'}</div></div>
    ${trusted ? "" : '<div class="dim xs" style="margin-top:8px">Next: verify its SSH key, so Homestead knows it is talking to this server.</div>'}
    <div class="row" style="margin-top:12px">${trusted
      ? `<button class="btn pri" data-need="admin" onclick="srcBrowse(${jsq(s.name)})">Import containers</button>
         <button class="btn" data-need="admin" onclick="srcVerify(${jsq(s.name)})">Verify again</button>`
      : `<button class="btn pri" data-need="admin" onclick="srcVerify(${jsq(s.name)})">Verify SSH key</button>`}</div>
  </div>`;
}

/* No server yet: how importing from one goes, and where to start. */
function importSourceSteps() {
  const step = (n, title, text) => `<div class="importstep"><span class="importstep-n">${n}</span>
    <div><b>${title}</b><div class="dim small">${text}</div></div></div>`;
  return `<div class="card flat importsteps">
    <div class="importstep-row">
      ${step(1, "Add the server", "Your Unraid server, or any Linux host running Docker: its address, an SSH login and where appdata lives.")}
      ${step(2, "Verify its SSH key", "Compare the fingerprint with the server's own console. No password is sent until you trust it.")}
      ${step(3, "Pick containers", "Image, ports, variables and devices come across, and each folder is copied into a Longhorn volume.")}
    </div>
    <div class="row" style="margin-top:14px"><button class="btn pri" data-need="admin" onclick="srcAdd()">＋ Add an Unraid or Docker server</button>
      <span class="dim small">Imported apps start stopped, and run once their data has been copied.</span></div>
  </div>`;
}
window.importRemove = async (name, state, namespace = defaultNamespace()) => {
  const running = state === "running";
  const plan = await api("/api/imports/cleanup-plan", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, namespace }) }).catch(e => ({ unread: e.message }));
  // Without the plan there is nothing safe to offer: not "it created nothing" (#620).
  if (plan.unread) {
    modal(`Remove import · ${name}`, UI.callout("bad", "Could not check what this import created", esc(plan.unread)) + UI.actions(UI.cancel("Close")));
    return;
  }
  if (plan.journalled) {
    modal(`Remove import Job · ${name}`, UI.lead("Remove the copy Job after a verified successful import. The app, Services and all volumes stay in place.") +
      UI.callout("info", "Incomplete import?", "Open Recent jobs → Inspect import. An active or uncertain copy cannot be removed here.") +
      UI.actions(UI.cancel("Keep it") + UI.button("Remove completed Job", `importRemoveNow(${jsArg(name)},this,${jsArg(namespace)})`, {kind:"danger",id:"imr_go"})));
    return;
  }
  // An import can have filled several volumes; every one it created is offered.
  const claims = plan.volumes?.length ? plan.volumes
    : plan.volume ? [{ name: plan.volume, created: plan.volume_created }] : [];
  modal(running ? `Cancel import · ${name}` : `Remove import · ${name}`, `
    <div class="note">${running
      ? "Cancelling stops the copy where it is. Files already written stay on the volume."
      : "Removing the job clears it from Transfers. While it exists it keeps referencing the volume it copied into, which blocks deleting that volume."}</div>
    <div class="sec">Also remove what this import created</div>
    <div class="volume-delete-grid">
      <label class="volume-delete-option ${plan.workload ? "" : "disabled"}">
        <input type="checkbox" id="imr_workload" ${plan.workload ? "" : "disabled"}>
        <span><b>The app${plan.workload ? ` · ${esc(plan.workload)}` : ""}</b>
          <small>${plan.workload
            ? "Deletes the Deployment this import created, and every Service pointing at it."
            : "This import created no app, or it has already been deleted."}</small></span></label>
      ${claims.length ? claims.map(claim => `
      <label class="volume-delete-option danger ${claim.created ? "" : "disabled"}">
        <input type="checkbox" class="imr-volume" value="${esc(claim.name)}" ${claim.created ? "" : "disabled"}>
        <span><b>The volume · ${esc(claim.name)}</b>
          <small>${claim.created
            ? "Deletes the claim this import created, along with whatever was copied into it."
            : `${esc(claim.name)} existed before this import, so it is not this import's to delete. Remove it from Volumes if you mean to.`}</small></span></label>`).join("")
      : `<label class="volume-delete-option danger disabled"><input type="checkbox" disabled>
        <span><b>The volume</b><small>No volume is recorded for this import.</small></span></label>`}
    </div>
    ${plan.known === false ? '<div class="note">This import predates the record of what it created, so only the job is removed.</div>' : ""}
    ${UI.actions(`<button class="btn danger" id="imr_go" data-need="admin" onclick="importRemoveNow(${jsq(name)},this,${jsq(namespace)})">${running ? "Cancel import" : "Remove"}</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Keep it</button>`)}`);
  if (window.applyRole) window.applyRole();
};
window.importRemoveNow = async (name, button, namespace = defaultNamespace()) => {
  const volumes = $$(".imr-volume").filter(box => box.checked).map(box => box.value);
  const body = { name, namespace, remove_workload: !!$("#imr_workload")?.checked,
    remove_volume: volumes.length > 0, remove_volumes: volumes };
  if (volumes.length && !(await ask(`Delete ${volumes.length === 1 ? volumes[0] : volumes.join(" and ")} `
      + "and everything copied into " + (volumes.length === 1 ? "it" : "them") + "?\n\nThis cannot be undone.", { title: volumes.length === 1 ? "Delete volume" : "Delete volumes", ok: "Delete", danger: true }))) return;
  if (button) { button.disabled = true; button.textContent = "Removing…"; }
  try {
    const result = await api("/api/imports/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    closeModal(); toast(result.message || `${name} removed`, "ok"); resetPaint(); viewImport();
  } catch (e) {
    if (button) { button.disabled = false; button.textContent = "Remove"; }
    toast(e.message, "bad");
  }
};

window.vmDiskImport = () => {
  if (STATE.data.importStorageClasses === null || STATE.data.importNamespaces === null) {
    modal("Import VM disk", UI.callout("bad", `Could not read the ${STATE.data.importStorageClasses === null ? "storage classes" : "namespaces"}`,
      "Refresh the Import page and try again.") + UI.actions(UI.cancel("Close")));
    return;
  }
  const namespaces = STATE.data.importNamespaces || [defaultNamespace()];
  // /api/storageclasses lists the class Homestead puts new volumes on first (#1125);
  // with no list, the server picks that class for a disk that names none.
  const classes = STATE.data.importStorageClasses || [];
  modal("Import VM disk", `
    <div class="note"><b>CDI handles the conversion.</b> Provide a directly downloadable HTTP(S) qcow2, vmdk, raw, vdi, vhd or vhdx image. The source is streamed into a new Longhorn PVC; an existing disk is never overwritten.</div>
    <div class="f2" style="margin-top:16px">
      <div class="f"><label>Namespace ${tip("The imported DataVolume and its PVC are created here. A VM using the disk must be in the same namespace.")}</label><select id="vd_ns">${namespaces.map(n => `<option value="${esc(n)}" ${n === defaultNamespace() ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
      <div class="f"><label>Disk / PVC name ${tip("This becomes both the CDI DataVolume name and the resulting PVC name.")}</label><input id="vd_name" placeholder="ubuntu-server"></div>
    </div>
    <div class="f"><label>Image URL ${tip("HTTP and HTTPS only. URLs containing embedded usernames or passwords are rejected; use a Kubernetes Secret for authenticated endpoints.")}</label><input type="url" id="vd_url" placeholder="https://example.net/images/server.qcow2"></div>
    <div class="f2">
      <div class="f"><label>Capacity (GiB) ${tip("Must be at least as large as the image's virtual disk capacity. CDI expands the converted disk to fit this PVC.")}</label><input type="number" min="1" max="16384" id="vd_size" value="20"></div>
      <div class="f"><label>Storage class</label><select id="vd_sc">${classes.length ? classes.map((s, i) => `<option value="${esc(s)}" ${i === 0 ? "selected" : ""}>${esc(s)}</option>`).join("") : '<option value="" selected>Homestead\'s default class</option>'}</select></div>
    </div>
    <div class="f2">
      <div class="f"><label>Access mode ${tip("RWO is the normal choice for a VM boot disk. RWX allows several hosts to mount it but does not make concurrent VM writes safe.")}</label><select id="vd_mode"><option value="ReadWriteOnce">RWO · one node</option><option value="ReadWriteMany">RWX · many nodes</option></select></div>
      <div class="f"><label>Checksum (optional) ${tip("Recommended for downloaded images. Paste the publisher's SHA-256 or SHA-512 hexadecimal digest.")}</label><div class="row" style="gap:7px"><select id="vd_algo" aria-label="Checksum type" style="max-width:110px"><option value="sha256">SHA-256</option><option value="sha512">SHA-512</option></select><input id="vd_checksum" class="mono" aria-label="Checksum" placeholder="hex digest"></div></div>
    </div>
    <details><summary class="small">Authenticated or private CA source</summary><div class="f2" style="margin-top:12px">
      <div class="f"><label>Credential Secret ${tip("Optional Secret in the destination namespace containing CDI-compatible accessKeyId and secretKey fields.")}</label><input id="vd_secret" placeholder="image-download-credentials"></div>
      <div class="f"><label>CA ConfigMap ${tip("Optional ConfigMap in the destination namespace containing the endpoint's CA certificate.")}</label><input id="vd_ca" placeholder="private-ca"></div></div></details>
    ${UI.actions(`<button class="btn pri" onclick="doVmDiskImport()">Start import</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}
    <div class="dim xs" style="margin-top:12px">Progress continues under Jobs after this dialog closes. Source URLs are not copied into Homestead's operation history.</div>`, true);
};
window.doVmDiskImport = async () => {
  const digest = $("#vd_checksum").value.trim();
  const body = { namespace: $("#vd_ns").value, name: $("#vd_name").value.trim(),
    source_url: $("#vd_url").value.trim(), size_gb: +$("#vd_size").value,
    storage_class: $("#vd_sc").value, access_mode: $("#vd_mode").value,
    checksum: digest ? `${$("#vd_algo").value}:${digest}` : "",
    secret_ref: $("#vd_secret").value.trim(), cert_config_map: $("#vd_ca").value.trim() };
  if (!body.name || !body.source_url) return toast("Disk name and image URL are required", "bad");
  try {
    const plan = await api(`/api/vm-disks/import-plan?ns=${encodeURIComponent(body.namespace)}&name=${encodeURIComponent(body.name)}`);
    if (!plan.ready) return toast(plan.message, "bad");
    const result = await api("/api/vm-disks/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.message, "ok"); closeModal(); importRepaint();
  } catch (e) { toast(e.message, "bad"); }
};
/* What each type of server is for, in the dialog's lead. */
const SOURCE_LEADS = {
  unraid: "Homestead reaches the server over SSH, lists its Docker containers and VMs, and copies each one's data.",
  ssh: "Homestead reaches the host over SSH, lists its Docker containers, and copies each one's appdata.",
  proxmox: "Homestead reaches the Proxmox host over SSH (root, or a user that may run qm), lists its VMs, and copies each one's disks.",
};
/* Add a server: its type first, since it decides the rest. kind is the type to
   start on: Proxmox from the VM import page, Unraid elsewhere. */
window.srcAdd = (kind = STATE.view === "vmimport" ? "proxmox" : "unraid") => {
  const option = (value, label) => `<option value="${value}"${value === kind ? " selected" : ""}>${label}</option>`;
  modal("Add a server", `<p class="ui-lead" id="sc_lead">${esc(SOURCE_LEADS[kind] || SOURCE_LEADS.unraid)} Save it, then verify its SSH fingerprint: no login is attempted until you trust it.</p>` +
  UI.field("Type", `<select id="sc_kind" onchange="srcKindChanged()">${option("unraid", "Unraid")}${option("proxmox", "Proxmox VE")}${option("ssh", "Other Docker host")}</select>`) +
  UI.fields(UI.field("Name", `<input id="sc_name" placeholder="${kind === "proxmox" ? "proxmox" : "unraid"}">`), UI.field("Address", '<input id="sc_host" placeholder="192.0.2.10">'),
    UI.field("Username", '<input id="sc_user" value="root" autocomplete="off">'), UI.field("Password", '<input type="password" id="sc_pass" autocomplete="new-password">'),
    UI.field("SSH port", '<input id="sc_port" type="number" min="1" max="65535" value="22">')) +
  `<div id="sc_path_field"${kind === "proxmox" ? " hidden" : ""}>${UI.field("Appdata folder", '<input id="sc_path" value="/mnt/user/appdata">')}</div>` +
  UI.more("Credentials and source ownership", '<p>The password is stored in a Kubernetes Secret. Administrators with access to Secrets can read it. Removing a source does not revoke credentials already used by copy Jobs.</p><p>Unraid® is a registered trademark of Lime Technology, Inc. Homestead is not affiliated with or endorsed by Lime Technology, Inc.</p>') +
  UI.actions(UI.cancel() + UI.button("Save and verify", "doSrcAdd()", {kind:"pri",id:"sc_save"})), false, "operation-review");
};
/* The lead and the appdata folder follow the type: a Proxmox host has no appdata. */
window.srcKindChanged = () => {
  const kind = $("#sc_kind").value;
  $("#sc_lead").textContent = `${SOURCE_LEADS[kind] || SOURCE_LEADS.unraid} Save it, then verify its SSH fingerprint: no login is attempted until you trust it.`;
  $("#sc_path_field").hidden = kind === "proxmox";
  $("#sc_name").placeholder = kind === "proxmox" ? "proxmox" : "unraid";
};
window.doSrcAdd = async () => {
  const save = $("#sc_save"), password = $("#sc_pass");
  if (!save || save.disabled) return;
  // A name is lower case, as Kubernetes names are: Proxmox is saved as proxmox.
  const body = { name: $("#sc_name").value.trim().toLowerCase(), host: $("#sc_host").value.trim(),
    user: $("#sc_user").value.trim(), password: $("#sc_pass").value,
    kind: $("#sc_kind").value, base_path: $("#sc_kind").value === "proxmox" ? "/" : $("#sc_path").value.trim(), port: +$("#sc_port").value };
  if (!body.name || !body.host) return toast("Name and host are required", "bad");
  try {
    save.disabled = true;
    const result = await api("/api/sources", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    password.value = ""; body.password = "";
    STATE.data.srcs = result.sources;
    if ($("#sc_save") === save) await srcVerify(body.name);
  } catch (e) {
    // Refused (a bad name, a missing field): nothing was saved, so correct it
    // and save again. Anything else may have saved: refresh before retrying.
    if (e.status >= 400 && e.status < 500) { save.disabled = false; toast(e.message, "bad"); }
    else toast(e.message + " — refresh Sources before retrying an uncertain save.", "bad");
  }
};
/* Review, then trust a source's SSH host key (#909), on the shared review flow: it keeps the sequence, the
   busy flag and the one-shot send. This view keeps its words. */
const SOURCE_KEY = UI.reviewFlow({
  loading: name => modal(`Verify source · ${name}`, '<div id="sourceKeyLoading" class="empty"><span class="spin2"></span>Reading the SSH host key. No login or password is sent…</div>', false, "operation-review"),
  live: () => !!$("#sourceKeyLoading"),
  preview: async name => {
    const review = await api("/api/sources/scan", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
    if (!review.capacity_token || !review.key || !review.fingerprint || review.name !== name) throw new Error("Fingerprint review is incomplete. Nothing was trusted.");
    return review;
  },
  render: review => {
    const file = {"ssh-ed25519":"ed25519","ecdsa-sha2-nistp256":"ecdsa","ssh-rsa":"rsa"}[review.algorithm];
    $("#mbody").innerHTML = UI.lead("Compare this fingerprint with the source host's own console before allowing Homestead to log in.") +
      UI.callout(review.changed ? "bad" : "warn", review.changed ? "The source key changed" : "A scan is not proof of identity",
        review.changed ? "A reinstall or an unexpected host can cause this. Confirm the change independently; do not trust it just to clear an error." : "Use the host's physical console or an already trusted management session, not this new SSH connection.") +
      UI.facts([["Host",esc(review.connection?.host) + ":" + esc(review.connection?.port)],["Host key",esc(review.algorithm)]]) +
      UI.section("Compare fingerprint", `<div class="mono" style="overflow-wrap:anywhere">${esc(review.fingerprint)}</div>`) +
      UI.section("On the source console", `<code style="overflow-wrap:anywhere">ssh-keygen -lf /etc/ssh/ssh_host_${esc(file || "ed25519")}_key.pub</code>`) +
      UI.more("What trusting changes", `<p>New discovery and copy connections must match this key. A key change stops new connections; it is never accepted automatically. Existing helpers retain the key and credentials they started with.</p>${review.previous ? `<p>Previously pinned: <span class="mono" style="overflow-wrap:anywhere">${esc(review.previous)}</span></p>` : ""}`) +
      UI.ack("sourceKeyAck", "I compared this fingerprint with the source host's console.", {onchange:"sourceKeyReady()"}) +
      UI.actions(UI.cancel("Not now") + UI.button("Trust this source", "sourceKeyTrust()", {kind:"pri",id:"sourceKeyApply",disabled:true}));
  },
  failed: e => { $("#mbody").innerHTML = UI.callout("bad", "Source not verified", esc(e.message)) + UI.actions(UI.cancel("Close")); },
  ack: "sourceKeyAck", button: "sourceKeyApply",
  apply: async review => {
    const button = $("#sourceKeyApply");
    button.disabled = true;
    try {
      await api("/api/sources/trust", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
        name:review.name,key:review.key,capacity_token:review.capacity_token,confirm_fingerprint:true})});
      toast("Source key pinned", "ok");
      if ($("#sourceKeyApply") === button) { closeModal(); importRepaint(); }
    } catch(e) {
      if ($("#sourceKeyApply") === button) $("#mbody").innerHTML = UI.callout("bad", "Trust save not confirmed", esc(e.message) + " Refresh and scan again; nothing was retried.") + UI.actions(UI.cancel("Close"));
      else toast("Trust save not confirmed. Refresh Sources before retrying.", "bad");
    }
  },
});
window.srcVerify = name => SOURCE_KEY.open(name);
window.sourceKeyReady = () => SOURCE_KEY.ready();
window.sourceKeyTrust = async () => {
  if (!sourceKeyReady()) return;
  await SOURCE_KEY.apply();
};
window.srcDel = async name => {
  if (!(await ask(`Remove import source "${name}"? Existing copy Jobs and their credentials are retained; this does not stop or revoke them.`, { title: "Remove import source", ok: "Remove", danger: true }))) return;
  try { await api("/api/sources/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast("Removed", "ok"); importRepaint(); } catch (e) { toast(e.message, "bad"); }
};
window.srcBrowse = async name => {
  modal("Import from · " + name, `<div class="empty"><span class="spin2"></span>connecting to ${esc(name)} and listing its containers - this runs a
    one-shot pod, so it takes ~20s</div>`, true);
  try {
    const [r, dc] = await Promise.all([
      api("/api/sources/browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }),
      api("/api/sources/containers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }).catch(e => ({ containers: [], unread: e.message })),
    ]);
    const src = (STATE.data.srcs || []).find(s => s.name === name) || {};
    const entries = r.entries.filter(e => !e.startsWith("==") && !/^(WARNING|Permission|Warning)/i.test(e));
    $("#mbody").innerHTML = `${dc.containers.length ? `<div class="ctitle">Containers on ${esc(name)}</div>
      <p class="muted small">Choose a container to carry over its image, ports, environment variables, icon and appdata mount automatically.</p>
      <div class="apps importapps" style="margin-top:14px">${dc.containers.map(c => `<div class="card flat importapp">
        <div class="between"><b>${esc(c.name)}</b>${UI.chip(c.state || "unknown", c.state === "running" ? "ok" : "neutral")}</div>
        <div class="mono xs dim" style="margin-top:7px;word-break:break-all">${esc(c.image)}</div>
        <button class="btn pri wide sm" style="margin-top:10px" onclick="inspectImport(${jsq(name)},${jsq(c.name)})">Import container</button></div>`).join("")}</div>
      <div class="sec">Appdata folders</div>` : ""}` + (entries.length
      ? `<p class="muted small">${entries.length} directories under <span class="mono">${esc(src.base_path)}</span>. Use this when Docker metadata is unavailable.</p>
         <div class="apps" style="margin-top:14px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
         ${entries.map(e => `<div class="card flat" style="padding:13px">
           <div style="font-weight:660;word-break:break-all">${esc(e)}</div>
           <button class="btn wide sm" style="margin-top:10px"
             onclick="importSetup(${jsq(name)},${jsq(e)})">Import</button></div>`).join("")}</div>`
      : `<div class="empty">No appdata folders returned. Check the credentials and that
         <span class="mono">${esc(src.base_path)}</span> exists on ${esc(src.host)}.</div>`)
      + (dc.unread ? UI.callout("warn", `Could not list the Docker containers on ${name}`, `${esc(dc.unread)} Only its appdata folders are offered.`)
        : dc.containers.length ? "" : `<div class="note" style="margin-top:12px">No Docker containers were listed on ${esc(name)}, so only its
         appdata folders are offered. Check that Docker is running there and the SSH user may use it.</div>`);
  } catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `srcBrowse(${jsArg(name)})`, dialog: true }); }
};

window.imageCleanupReview = digest => {
  const image = (STATE.data.imageCache?.images || []).find(row => row.digest === digest);
  if (!image) return toast("Refresh Image Cache before cleaning this image", "bad");
  if (image.protected || image.system) return toast("This image is protected and cannot be cleaned", "bad");
  const perNode = bytesText(image.size_mb * 1024 ** 2);
  modal("Clean cached image", `<div class="update-review">
    ${UI.callout("bad", { html: `This deletes only the cached image layers, not an app.` }, `A future start may be slower while the registry image is downloaded again. Active, immediate rollback, and platform images are blocked server-side.`)}
    <div class="update-image"><div><span>Image</span><code>${esc(image.name)}</code></div>
      <div><span>Digest</span><code>${esc(digest)}</code></div><div><span>Per host</span><code>${esc(perNode)}</code></div></div>
    <div class="f"><label>Remove from hosts</label><div class="cleanup-nodes">${image.nodes.map(node =>
      `<label class="switch"><input class="cleanup-node" type="checkbox" value="${esc(node)}" checked onchange="imageCleanupGate()"> ${esc(node)}</label>`).join("")}</div></div>
    ${UI.typedConfirm({ id: "cleanupConfirm", expect: "CLEAN", oninput: "imageCleanupGate()" })}
    ${UI.actions(`<button id="cleanupGo" class="btn danger" data-need="admin" disabled onclick="imageCleanupApply(${jsq(digest)})">Remove cached image</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}</div>`, true);
  if (window.applyRole) window.applyRole();
};
window.imageCleanupGate = () => {
  const button = $("#cleanupGo");
  if (button) button.disabled = !typedConfirmMatches("cleanupConfirm") || !$(".cleanup-node:checked");
};
window.imageCleanupApply = async digest => {
  const nodes = $$(".cleanup-node:checked").map(input => input.value);
  try {
    const result = await api("/api/images/cleanup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ digest, nodes }) });
    toast(`Cleanup started on ${result.nodes.length} host${result.nodes.length === 1 ? "" : "s"}`, "ok");
    closeModal();
  } catch (error) { toast(error.message, "bad"); }
};
window.inspectImport = async (source, container) => {
  $("#mbody").innerHTML = '<div class="empty"><span class="spin2"></span>reading Docker configuration…</div>';
  try {
    const cfg = await api("/api/sources/inspect", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: source, container }) });
    importSetup(source, container, cfg);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty"><b>Could not inspect ${esc(container)}</b><br><span class="dim small">${esc(e.message)}</span></div>`; }
};
/* Every Docker mount under the source appdata path is importable; anything
   else on the host is offered but left unticked, because it is not appdata. */
function importMappingRows(cfg, src) {
  const base = (src.base_path || "/mnt/user/appdata").replace(/\/+$/, "");
  const seen = new Set();
  const rows = (cfg.mounts || [])
    .filter(mount => mount.path && (mount.source || mount.type === "tmpfs"))
    .map(mount => mount.type === "tmpfs"
      // tmpfs on the source is RAM, so there is nothing to copy and nowhere to
      // copy it from: it becomes a memory-backed scratch volume instead.
      ? { mount_path: mount.path, medium: "memory", size_mb: mount.size_mb || 1024, include: true }
      : { remote_path: mount.source, mount_path: mount.path,
          include: mount.source === cfg.remote_path || mount.source.startsWith(base + "/") });
  // cfg.remote_path is only worth adding when the host actually reported it.
  // A guessed one is a folder nobody has: it would be ticked, copied, and fail.
  // Kubernetes gives a pod 64 MiB of /dev/shm and no way to ask for more except
  // a memory-backed volume over the top, so a container Docker gave more than
  // that keeps it: without it Frigate's frames never reach the detectors.
  if (cfg.shm_mb && !rows.some(row => row.mount_path === "/dev/shm")) {
    rows.push({ mount_path: "/dev/shm", medium: "memory", size_mb: cfg.shm_mb, include: true });
  }
  if (cfg.remote_path && !cfg.guessed_path && !rows.some(row => row.remote_path === cfg.remote_path)) {
    rows.unshift({ remote_path: cfg.remote_path, mount_path: cfg.mount_path || "/config", include: true });
  }
  // Nothing mounted means nothing to copy. An empty list says that plainly;
  // a blank row invites someone to invent a path the container never had.
  if (!rows.length && cfg.remote_path && !cfg.guessed_path) {
    rows.push({ remote_path: cfg.remote_path, mount_path: cfg.mount_path || "/config", include: true });
  }
  return rows.filter(row => {
    const key = row.medium ? `ram:${row.mount_path}` : row.remote_path;
    return !seen.has(key) && seen.add(key);
  });
}

function importFolderName(remotePath, mountPath) {
  const tail = String(remotePath || mountPath || "data").replace(/\/+$/, "").split("/").pop();
  return tail.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "").slice(0, 60) || "data";
}

function importMappingRow(row = {}) {
  if (row.medium === "memory") {
    return `<div class="im-map im-scratch" data-medium="memory">
      <div><label>Source</label><input class="imm-remote" type="text" value="" disabled placeholder="RAM — nothing to copy"></div>
      <div><label>Kind</label><input type="text" value="Memory scratch" disabled></div>
      <div><label>Size MiB</label><input class="imm-ram" type="number" min="1" max="65536" value="${esc(row.size_mb || 1024)}"></div>
      <div><label>Path inside the container</label><input class="imm-mount" type="text" value="${esc(row.mount_path || "")}" placeholder="/tmp/cache"></div>
      <label class="switch"><input class="imm-on" type="checkbox" ${row.include === false ? "" : "checked"} onchange="imSyncMaps()">Create</label>
      <button class="iconbtn row-remove" type="button" title="Remove scratch volume" onclick="this.closest('.im-map').remove();imSyncMaps()">×</button>
      <div class="dim xs" style="grid-column:1/-1;margin-top:-4px">${row.mount_path === "/dev/shm"
        ? "Shared memory, as <code>--shm-size</code> gave it on the source. A pod gets 64 MiB otherwise, which is not enough for apps that pass frames or buffers between processes."
        : "This was tmpfs on the source: a RAM disk that starts empty every time. Kubernetes gives it the same thing, capped at this size and counted against the host's memory."}</div></div>`;
  }
  return `<div class="im-map" data-pvc="${esc(row.pvc || "")}">
    <div><label>Source folder on the host</label><input class="imm-remote" type="text" value="${esc(row.remote_path || "")}" placeholder="/mnt/user/appdata/app/config" oninput="imSyncMaps()"></div>
    <div><label>Goes to volume</label><select class="imm-pvc" onchange="imSyncMaps()"></select></div>
    <div><label>Subfolder</label><input class="imm-folder" type="text" value="${esc(row.folder || "")}" placeholder="${esc(importFolderName(row.remote_path, row.mount_path))}" oninput="imSyncMaps()"></div>
    <div><label>Path inside the container</label><input class="imm-mount" type="text" value="${esc(row.mount_path || "")}" placeholder="/config"></div>
    <div><label>Contents ${tip("Copy: the folder's files are copied into the volume. Mount empty: the volume is mounted at this path with nothing copied - new recordings, say, where the old ones stay behind. Leave out: not mounted at all.")}</label>
      <select class="imm-mode" onchange="imSyncMaps()">
        <option value="copy" ${row.include === false || row.copy === false ? "" : "selected"}>Copy</option>
        <option value="empty" ${row.copy === false && row.include !== false ? "selected" : ""}>Mount empty</option>
        <option value="skip" ${row.include === false ? "selected" : ""}>Leave out</option></select></div>
    <button class="iconbtn row-remove" type="button" title="Remove folder" onclick="this.closest('.im-map').remove();imSyncMaps()">×</button>
    <div class="dim xs imm-size" style="grid-column:1/-1;margin-top:-4px"></div></div>`;
}

window.imAddScratch = () => {
  $("#im_maps").insertAdjacentHTML("beforeend",
    importMappingRow({ medium: "memory", size_mb: 1024, mount_path: "/tmp/cache", include: true }));
  imSyncMaps();
};

/* A volume row is just a claim: a name, whether it is new, and how big.
   The mapping rows above pick from these by name. */
function importVolumeRow(volume = {}, index = 0) {
  const classes = STATE.data.importStorage?.storage_classes || [];
  const facts = STATE.data.importStorage?.storage_class_facts || STATE.data.classFacts || {};
  const claims = (STATE.data.importStorage?.pvcs || []);
  return `<div class="f4 im-volume">
    <div><label>Volume name</label><input class="imv-name" type="text" value="${esc(volume.name || "")}" list="im_pvc_list" oninput="imSyncVolumes()">
      <datalist id="im_pvc_list">${claims.map(claim => `<option value="${esc(claim.name)}">${esc(claim.size || "")} ${esc((claim.access_modes || []).join("/"))}</option>`).join("")}</datalist></div>
    <div><label>Source</label><select class="imv-kind" onchange="imSyncVolumes()">
      <option value="new-rwo" ${volume.access_mode === "ReadWriteMany" ? "" : "selected"}>New Longhorn volume · RWO</option>
      <option value="new-rwx" ${volume.access_mode === "ReadWriteMany" ? "selected" : ""}>New shared volume · RWX</option>
      <option value="existing" ${volume.create === false ? "selected" : ""}>Existing PVC · merge data</option></select></div>
    <div><label>Size GiB</label><input class="imv-size" type="number" min="1" max="16384" value="${esc(volume.size_gb || 10)}"></div>
    <div><label>Storage class</label><select class="imv-class">${storageClassOptions(classes, defaultStorageClass(classes, volume.storage_class, facts), facts)}</select></div>
    ${index === 0 ? "" : '<button class="iconbtn row-remove" type="button" title="Remove volume" onclick="this.closest(\'.im-volume\').remove();imSyncVolumes()">×</button>'}
  </div>`;
}

window.imAddVolume = () => {
  const rows = $$("#im_volumes .im-volume");
  const base = ($("#im_name")?.value.trim() || "app");
  $("#im_volumes").insertAdjacentHTML("beforeend",
    importVolumeRow({ name: `${base}-${rows.length === 0 ? "appdata" : "data" + (rows.length + 1)}`, size_gb: rows.length ? 100 : 10 }, rows.length));
  imSyncVolumes();
};

window.importVolumes = () => $$("#im_volumes .im-volume").map(row => {
  const kind = $(".imv-kind", row).value;
  return { name: $(".imv-name", row).value.trim(), create: kind !== "existing",
    size_gb: +$(".imv-size", row).value || 10, storage_class: $(".imv-class", row).value,
    access_mode: kind === "new-rwx" ? "ReadWriteMany" : "ReadWriteOnce" };
}).filter(volume => volume.name);

window.imSyncVolumes = () => {
  const volumes = importVolumes();
  $$("#im_volumes .im-volume").forEach(row => {
    const existing = $(".imv-kind", row).value === "existing";
    $(".imv-size", row).disabled = existing;
    $(".imv-class", row).disabled = existing;
  });
  // Every mapping keeps its choice if that volume still exists, else falls to the first.
  $$("#im_maps .im-map").forEach(row => {
    const select = $(".imm-pvc", row);
    if (!select) return;
    const wanted = select.value || row.dataset.pvc || "";
    select.innerHTML = volumes.map(volume => `<option ${volume.name === wanted ? "selected" : ""}>${esc(volume.name)}</option>`).join("")
      || '<option value="">add a volume first</option>';
    if (!volumes.some(volume => volume.name === wanted) && volumes.length) select.value = volumes[0].name;
    row.dataset.pvc = select.value;
  });
  imSyncMaps();
};

window.imAddMap = () => {
  $("#im_maps").insertAdjacentHTML("beforeend", importMappingRow({ include: true }));
  imSyncMaps();
};
/* The folders as a picture: each source, what happens to it, its volume
   and where it lands - redrawn as the rows change. */
function importMapPicture() {
  const host = $("#im_map_picture");
  if (!host || !window.Diagram) return;
  const rows = $$("#im_maps .im-map").map(row => row.dataset.medium === "memory"
    ? { how: $(".imm-on", row)?.checked === false ? "skip" : "ram", source: "RAM", volume: `${$(".imm-ram", row)?.value || 0} MiB in memory`, path: $(".imm-mount", row)?.value || "" }
    : { how: imMode(row), source: $(".imm-remote", row)?.value || "", volume: $(".imm-pvc", row)?.value || "", path: $(".imm-mount", row)?.value || "" });
  host.innerHTML = Diagram.mapping(rows, { from: "on the source server" });
}
window.importMapPicture = importMapPicture;
window.imSyncMaps = () => {
  setTimeout(importMapPicture, 0);
  const all = $$("#im_maps .im-map").filter(row => imMode(row) !== "skip");
  const scratch = all.filter(row => row.dataset.medium === "memory");
  const empty = all.filter(row => row.dataset.medium !== "memory" && imMode(row) === "empty");
  const rows = all.filter(row => row.dataset.medium !== "memory" && imMode(row) === "copy");
  const note = $("#im_maps_note");
  if (note) {
    const targets = new Set(rows.map(row => $(".imm-pvc", row)?.value).filter(Boolean));
    const ram = scratch.length ? ` ${scratch.length} RAM scratch volume${scratch.length === 1 ? "" : "s"} is created empty.` : "";
    const mounted = empty.length ? ` ${empty.length} more mounted empty, nothing copied.` : "";
    note.textContent = (!rows.length ? "Nothing selected to copy."
      : targets.size > 1 ? `${rows.length} folders across ${targets.size} volumes, each mounted back separately.`
      : rows.length > 1 ? `${rows.length} folders into one volume, each in its own subfolder.`
      : "One folder copied to the root of its volume.") + mounted + ram;
  }
  const sizes = STATE.data.importSizes || {};
  const everything = importSourcePaths();
  $$("#im_maps .im-map").forEach(row => {
    if (row.dataset.medium === "memory") return;
    $(".imm-folder", row).placeholder = importFolderName($(".imm-remote", row).value, $(".imm-mount", row).value);
    const path = $(".imm-remote", row).value.trim().replace(/\/+$/, ""), readout = $(".imm-size", row);
    if (!readout) return;
    // A folder mapped in its own right never travels inside its parent, so say
    // so on the parent: otherwise the recordings look like they went missing.
    const inside = path ? everything.filter(other => other.startsWith(path + "/")) : [];
    const skips = inside.length ? ` · leaves out ${inside.map(other => other.slice(path.length)).join(", ")}, mapped separately` : "";
    readout.classList.toggle("bad", sizes[path] === "missing");
    readout.textContent = (!(path in sizes) ? ""
      : sizes[path] === "missing" ? "this folder does not exist on the source"
      : sizes[path] === null ? "size unknown — du timed out on this folder"
      : `${importBytes(sizes[path])} to copy`) + (sizes[path] === "missing" ? "" : skips);
  });
};
const importBytes = value => Number(value) ? bytesText(value) : "";

/* du walks every inode, so a deep appdata tree can take a while. Each folder
   is measured under its own timeout on the host: a slow one costs its own
   answer, not the whole measurement. */
window.imMeasure = async source => {
  const rows = $$("#im_maps .im-map");
  const paths = rows.filter(row => row.dataset.medium !== "memory")
    .map(row => $(".imm-remote", row).value.trim()).filter(path => path.startsWith("/"));
  if (!paths.length) return toast("Add a source folder first", "bad");
  const button = $("#im_measure");
  if (button) { button.disabled = true; button.textContent = "Measuring…"; }
  try {
    const result = await api("/api/sources/measure", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: source, paths }) });
    STATE.data.importSizes = Object.fromEntries((result.paths || [])
      .map(row => [row.path, row.measured ? row.bytes : row.exists === false ? "missing" : null]));
    // Each volume is sized from the folders pointed at it, not the total.
    const mappings = importMappings();
    $$("#im_volumes .im-volume").forEach(row => {
      const name = $(".imv-name", row).value.trim();
      if ($(".imv-kind", row).value === "existing") return;
      const bytes = mappings.filter(map => !map.medium && (map.pvc || name) === name)
        .reduce((sum, map) => sum + ((STATE.data.importSizes || {})[map.remote_path] || 0), 0);
      if (bytes) $(".imv-size", row).value = Math.max(1, Math.round(bytes / 1024 ** 3 * 1.25) + 1);
    });
    imSyncMaps();
    const note = $("#im_maps_note");
    if (note) {
      const copying = new Set(importMappings().filter(map => !map.medium).map(map => map.remote_path));
      const missing = (result.missing || []).filter(path => copying.has(path));
      note.innerHTML = `${importBytes(result.total_bytes)} measured across ${paths.length - missing.length} folder${paths.length - missing.length === 1 ? "" : "s"}`
        + (missing.length ? `. <b>${missing.map(esc).join(", ")} ${missing.length === 1 ? "does" : "do"} not exist on the source</b> — fix the path or untick the folder, or the copy will fail.`
          : result.complete ? ". Volume sizes set from what is actually there, with room to grow."
          : `, but some folders timed out after ${result.timeout_seconds}s — the sizes cover only what was measured.`);
    }
  } catch (e) { toast(e.message, "bad"); }
  finally { if (button) { button.disabled = false; button.textContent = "Measure sizes"; } }
};

/* Every folder listed, ticked or not: an unticked subfolder still has to be
   kept out of its parent's copy, or leaving it out achieves nothing. */
window.importSourcePaths = () => $$("#im_maps .im-map")
  .filter(row => row.dataset.medium !== "memory")
  .map(row => $(".imm-remote", row).value.trim())
  .filter(path => path.startsWith("/"))
  .map(path => path.replace(/\/+$/, ""));

/* Whether a folder row is in the import: copied, mounted empty, or left out. */
const imMode = row => row.dataset.medium === "memory" ? ($(".imm-on", row).checked ? "copy" : "skip")
  : ($(".imm-mode", row)?.value || "copy");

/* Source data safety is asked on the Storage step, so it is answered there -
   not discovered missing two steps later at Review. */
window.imSafetyNote = show => {
  const note = $("#im_consistency_err"), select = $("#im_consistency");
  if (note) note.hidden = !show;
  if (select) select.toggleAttribute("aria-invalid", !!show);
  if (show && select) { select.scrollIntoView({ block: "center" }); select.focus(); }
};
const imSafetyMissing = () => !["stopped", "snapshot"].includes($("#im_consistency")?.value)
  && importMappings().some(row => !row.medium && row.copy !== false);
window.imStepGuard = step => {
  if (step !== 1 || !imSafetyMissing()) return true;
  imSafetyNote(true);
  return false;
};

/* Existing volumes live in a namespace: choosing another one offers its own. */
window.imNamespace = async ns => {
  const storage = await api(`/api/deploy/options?ns=${encodeURIComponent(ns)}`).catch(e => ({ unread: e.message }));
  if ($("#im_ns")?.value !== ns) return;
  // Unread volumes here are not the last namespace's: none is offered, and it says so (#620).
  if (storage.unread) {
    STATE.data.importStorage = { ...(STATE.data.importStorage || {}), pvcs: [] };
    $$("datalist#im_pvc_list").forEach(list => { list.innerHTML = ""; });
    toast(`Could not read the volumes in ${ns}: ${storage.unread}`, "warn");
    return;
  }
  STATE.data.importStorage = storage;
  const options = (storage.pvcs || []).map(claim => `<option value="${esc(claim.name)}">${esc(claim.size || "")} ${esc((claim.access_modes || []).join("/"))}</option>`).join("");
  $$("datalist#im_pvc_list").forEach(list => { list.innerHTML = options; });
};

window.importMappings = () => {
  const all = importSourcePaths();
  return $$("#im_maps .im-map")
    .filter(row => imMode(row) !== "skip")
    .map(row => {
      if (row.dataset.medium === "memory") {
        return { medium: "memory", mount_path: $(".imm-mount", row).value.trim() || "/tmp/cache",
          size_mb: +$(".imm-ram", row).value || 1024 };
      }
      const remote = $(".imm-remote", row).value.trim().replace(/\/+$/, "");
      const copy = imMode(row) === "copy";
      return { remote_path: remote, mount_path: $(".imm-mount", row).value.trim() || "/config",
        folder: $(".imm-folder", row).value.trim(), pvc: $(".imm-pvc", row)?.value || "", copy,
        exclude: copy ? all.filter(path => path.startsWith(remote + "/")).map(path => path.slice(remote.length)) : [],
        bytes: copy ? Number((STATE.data.importSizes || {})[remote]) || 0 : 0 };
    });
};

window.importSetup = async (source, dir, cfg = {}) => {
  const src = (STATE.data.srcs || []).find(s => s.name === source) || {};
  const name = (cfg.name || dir).toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 38);
  STATE.data.importCfg = cfg;
  // Live nodes, so hardware plugged in since the page loaded is offered.
  const [storage, liveNodes, importVips] = await Promise.all([
    api(`/api/deploy/options?ns=${encodeURIComponent(defaultNamespace())}`).catch(e => ({ unread: e.message })),
    api("/api/nodes").catch(e => ({ unread: e.message })),
    // ServiceLB puts it on every host's own address: there is no address to choose.
    nodeAddressesOnly() ? null : vipChoices().catch(e => ({ unread: e.message }))]);
  // An unread network is not "no addresses": the picker would offer only a new one (#620).
  if (importVips?.unread) {
    childModal("Import · " + dir, UI.error({ message: importVips.unread }, { title: "Could not open the import", dialog: true }));
    return;
  }
  // No class list is not "pick none": the server would choose one (#620).
  if (storage.unread) {
    childModal("Import · " + dir, UI.callout("bad", "Could not read the storage options", esc(storage.unread)) + UI.actions(UI.cancel("Close")));
    return;
  }
  if (Array.isArray(liveNodes) && liveNodes.length) STATE.data.nodes = liveNodes;
  STATE.data.importStorage = storage;
  // A container that mounts nothing needs no storage: the folders and volumes
  // stay out of the way, offered only if someone wants to add some anyway.
  const keeps = (cfg.mounts || []).some(m => m.source || m.type === "tmpfs") || !!cfg.shm_mb;
  const appStep = `
    <div class="f2"><div class="f"><label>App name</label><input type="text" id="im_name" value="${esc(name)}"></div>
      <div class="f"><label>Namespace ${tip("The app, its volumes and the copy are all created here. Existing volumes to merge into are offered from this namespace.")}</label>
        <select id="im_ns" onchange="imNamespace(this.value)">${[...new Set([defaultNamespace(), ...(STATE.data.importNamespaces || [])])].map(n =>
          `<option value="${esc(n)}" ${n === defaultNamespace() ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div></div>
    <div class="f"><label>Remote path</label>
      <input type="text" id="im_path" value="${esc(cfg.remote_path || "")}" placeholder="${esc((src.base_path || "") + "/" + dir)}"></div>
    <div class="f"><label>Docker image ${tip("Read from Docker on the source host. You can change the tag before importing.")}</label>
      <input type="text" id="im_image" value="${esc(cfg.image || "")}" placeholder="lscr.io/linuxserver/${esc(name)}:latest"></div>
    <div class="f"><label>Logo URL</label><input type="url" id="im_icon" value="${esc(cfg.icon || "")}" placeholder="https://…/logo.png or https://example.com"></div>
    ${liveNodes?.unread ? UI.callout("warn", "Could not read the hosts just now", `${esc(liveNodes.unread)} · the hardware offered is from the hosts last read`) : ""}
    <div class="sec">Hardware requirements ${tip("Docker device mappings are pre-selected. Add or remove features before import; placement will be limited to hosts that provide every selected feature.")}</div>
    <div class="hwchoices">${hardwareChoices("im_hw", cfg.hardware || [])}</div>
    <div class="sec">Privileges ${tip("Read from Docker on the source: privileged mode, added capabilities, and the /dev/net/tun device a VPN needs.")}</div>
    ${privilegeFields("im_pv", cfg)}`;
  const storageStep = `
    ${keeps ? "" : `<div class="note good" id="im_nothing">This container keeps nothing on disk, so there is nothing to copy and no
        volume to create. Import will bring across its image, ports and environment alone.
        <div style="margin-top:8px"><button class="btn sm" onclick="imStorageAnyway()">Add storage anyway</button></div></div>`}
    <div id="im_storage" ${keeps ? "" : "hidden"}>
    ${UI.field("Source data safety", '<select id="im_consistency" onchange="imSafetyNote(false)"><option value="">Choose before copying…</option><option value="stopped">All source writers are stopped</option><option value="snapshot">These paths are a consistent snapshot or backup</option></select>', {help: cfg.source_container_id ? "The original Docker container must stay stopped. Snapshot mode skips that check; verify the paths really point to the snapshot." : "Homestead does not stop source applications. Stop every writer or choose stable backup paths before copying."})}
    <div id="im_consistency_err" hidden>${UI.callout("bad", "Choose Source data safety", "How the source's data stays still while it is copied: every writer stopped, or a consistent snapshot or backup.")}</div>
    <div id="im_map_picture"></div>
    <div class="sec">Folders ${tip("Each folder the container mounts: copied into a volume, mounted empty, or left out. Several can share one volume, each in its own subfolder, mounted back where the container expects it.")}</div>
    ${keeps && cfg.guessed_path ? `${UI.callout("warn", "", `Nothing this container mounts is under <span class="mono">${esc(src.base_path || "/mnt/user/appdata")}</span>,
      so Homestead cannot tell which folder holds its configuration. Tick the ones to copy yourself.`)}` : ""}
    <div id="im_maps">${importMappingRows(cfg, src).map(importMappingRow).join("")}</div>
    <div class="row"><button class="btn sm" onclick="imAddMap()">＋ add folder</button>
      <button class="btn sm" onclick="imAddScratch()">＋ add RAM scratch</button>
      <button class="btn sm" id="im_measure" onclick="imMeasure(${jsq(source)})">Measure sizes</button></div>
    <div class="dim xs" id="im_maps_note" style="margin-top:8px"></div>
    <details class="import-advanced"><summary class="dim small">Override file ownership (rarely needed)</summary>
      <div class="note">The copy keeps the ownership the files already had on the source, so the app finds
        its appdata exactly as it left it. Set these only when the container should run as a different user
        here than it did there.</div>
      <div class="f2"><div class="f"><label>Owner UID</label>
        <input type="number" id="im_uid" min="0" max="65535" placeholder="keep what the source had"></div>
        <div class="f"><label>Owner GID</label><input type="number" id="im_gid" min="0" max="65535" placeholder="same as UID"></div></div>
    </details>
    <div class="sec">Volumes ${tip("An import can fill more than one volume: appdata on a small replicated claim, recordings on a large one - they rarely want the same one. Each folder above says which volume it goes to.")}</div>
    <div id="im_volumes"></div>
    <button class="btn sm" onclick="imAddVolume()">＋ add volume</button>
    </div>`;
  const networkStep = `
    <div class="sec">Network</div><div class="f2"><div class="f"><label>Docker network → Kubernetes</label><select id="im_net"><option value="loadbalancer">LAN access (VIP)</option><option value="internal">Cluster only</option><option value="host" ${cfg.network_mode === "host" ? "selected" : ""}>Host network (advanced)</option></select></div>
      <div class="f"><label>VIP allocation ${tip("Use the default workload VIP configured in Networking, another reserved address, or a specific VIP. Shared ports must be unique.")}</label><select id="im_vip" onchange="$('#im_vip_wrap').style.display = this.value === 'manual' ? '' : 'none'">${nodeAddressesOnly() ? nodeAddressOption() : `${importVips.shared ? `<option value="shared">Default workload VIP · ${esc(importVips.shared)}</option>` : ""}${nodeAddressChoice(!importVips.shared)}<option value="auto">New automatic VIP</option><option value="manual">Specific VIP</option>`}</select></div></div>
    <div class="f" id="im_vip_wrap" style="display:none"><label>Specific VIP</label><div id="im_vip_pick"><span class="dim xs"><span class="spin2"></span></span></div></div>
    <div class="sec">Port mappings ${tip("Container port is what the app listens on. LAN port is what you open from another device. TCP and UDP mappings are kept separately.")}</div>
    <div id="im_ports">${(cfg.ports || []).map(p => `<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number" value="${p.container}"></div><div><label>LAN</label><input class="iph" type="number" value="${p.host}"></div><div><label>Protocol</label><select class="ipp"><option ${p.protocol === "TCP" ? "selected" : ""}>TCP</option><option ${p.protocol === "UDP" ? "selected" : ""}>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" ${p.expose !== false ? "checked" : ""}>Expose</label></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddPort()">＋ add port</button>
    <div class="sec">Environment variables ${tip("Copied from Docker inspect. Review secrets and host-specific paths before starting the imported app.")}</div>
    <div id="im_env">${Object.entries(cfg.env || {}).map(([k,v]) => `<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek" value="${esc(k)}"></div><div class="f"><label>Value</label><input class="iev" value="${esc(v)}"></div></div>`).join("")}</div>
    <button class="btn sm" onclick="imAddEnv()">＋ add variable</button>`;
  const reviewStep = `
    <div class="sec">Memory</div>
    <div class="f2"><div class="f"><label>Memory reserved ${tip('The RAM the scheduler keeps for this container. It is not a limit. 1 GiB = 1024 MiB.')}</label>${UI.quantity("memory", "im_memory", cfg.memory || "256Mi", { placeholder: "256", label: "Memory reserved" })}</div>
      <div class="f"><label>Memory max ${tip('The most memory this container may use: past it, it is stopped and restarted. Leave empty for no limit; it cannot be below Memory reserved.')}</label>${UI.quantity("memory", "im_memory_limit", cfg.memory_limit || "", { placeholder: "No limit", label: "Memory max", reserved: "im_memory" })}</div></div>
    ${UI.more("How the import runs", `<p>The copy runs as a Job: you can close this and watch it on the Import page, folder by folder; large appdata directories take a while. Its helper reserves 128 MiB and is limited to 512 MiB.</p>
      <p>The application is created stopped. Once the copy is done, Start in Containers checks capacity again before it runs.</p>`)}`;
  childModal("Import · " + dir, stepper("im_steps", [
    { title: "App", html: appStep }, { title: "Storage", html: storageStep },
    { title: "Network", html: networkStep }, { title: "Memory and review", html: reviewStep }],
    `<button class="btn pri" onclick="doImport(${jsq(source)})">Review import</button>`), true);
  $("#im_steps").dataset.guard = "imStepGuard";
  if (keeps) $("#im_volumes").innerHTML = importVolumeRow({ name: `${name}-appdata`, size_gb: 10 }, 0);
  $("#im_storage")?.addEventListener("input", importMapPicture);
  if ($("#im_vip_pick")) $("#im_vip_pick").innerHTML = importVips ? vipPicker("im", "", importVips) : "";
  imSyncVolumes();
};
/* Another Homestead on the network. The destination pulls, so these
   credentials are this cluster reaching out, not the far one reaching in. */
/* One cluster a move can come from: whether the two releases can move
   workloads, what a move still needs over there, and its workloads. Shown
   under Settings → Linked clusters. */
function clusterCardHtml(c) {
  return `<div class="card flat clcard">
    <div class="clhead"><div class="ctitle">${esc(c.label || c.name)}</div>
      <div class="csub mono clurl" title="${esc(c.url)}">${c.fleet ? "linked" : "added before linking"} · ${esc(c.url)}</div></div>
    <div class="clver"><span class="dim xs">Version</span>
      <span id="clver_${esc(c.name)}"><span class="dim xs"><span class="spin2"></span> checking…</span></span>
      <button class="iconbtn clrecheck" data-tip="Check the version again" onclick="clusterCheck(${jsq(c.name)})">${icon("refresh")}</button></div>
    <div id="clvermsg_${esc(c.name)}"></div>
    <div class="clsteps" id="clready_${esc(c.name)}"></div>
    <div class="row" style="margin-top:12px"><button class="btn sm" onclick="clusterBrowse(${jsq(c.name)})">Browse apps</button></div>
    <div class="dim xs" id="cluster_${esc(c.name)}" style="margin-top:10px"></div>
  </div>`;
}
window.clusterCardHtml = clusterCardHtml;

/* Wherever the moves are shown, show them afresh. */
window.movesRepaint = () => {
  if ($("#fleetMovesCard") && window.fleetMovesPaint) fleetMovesPaint();
  else watchMoves();
};

/* Whether the two Homesteads can move workloads between them, and if not,
   which one to update. Same protocol on different releases still works. */
const CLUSTER_VERSION_TAGS = {
  same: ["ok", v => `v${v} · matches`],
  differs: ["info", v => `v${v} · differs`],
  behind: ["bad", v => `v${v || "?"} · too old`],
  ahead: ["bad", v => `v${v || "?"} · newer`],
  unreachable: ["warn", () => "no answer"],
  refused: ["bad", () => "unknown"],
};

window.clusterCheck = async name => {
  const host = $("#clver_" + name), note = $("#clvermsg_" + name);
  if (!host) return;
  host.innerHTML = '<span class="dim xs"><span class="spin2"></span> checking…</span>';
  if (note) note.innerHTML = "";
  let check;
  try {
    check = await api("/api/move/clusters/check", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
  } catch (e) { check = { state: "refused", message: e.message }; }
  const [tone, label] = CLUSTER_VERSION_TAGS[check.state] || CLUSTER_VERSION_TAGS.refused;
  host.innerHTML = UI.chip(label(check.version), tone, { tip: check.message || "" });
  // A reason to act gets a note; a harmless difference gets a quiet line.
  const trouble = check.compatible === false || ["unreachable", "refused"].includes(check.state);
  if (!note) return;
  if (trouble) note.innerHTML = `${UI.callout(tone === "warn" ? "warn" : "bad", "", `${esc(check.message || "")}`, { className: "clvernote" })}`;
  else if (check.state === "differs") note.innerHTML = `<div class="dim xs clverdim">This cluster runs v${esc(HOMESTEAD_VERSION)}. Moves work between the two.</div>`;
  if (!trouble) clusterReady(name);
};

/* What a move from this cluster still needs, as steps with the fix beside
   each: the connection, then backup storage over there that this cluster
   can reach. */
window.clusterReady = async name => {
  const host = $("#clready_" + name);
  if (!host) return;
  host.innerHTML = '<span class="dim xs"><span class="spin2"></span> checking what a move needs…</span>';
  let r;
  try {
    r = await api("/api/move/clusters/readiness", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
  } catch (e) { host.innerHTML = `<div class="dim xs">${esc(e.message)}</div>`; return; }
  const target = r.target || {}, store = r.storage || {};
  (window.__clusterReady ||= {})[name] = r;
  const step = (ok, text, action = "") => `<div class="clstep ${ok ? "done" : "todo"}"><span>${ok ? "✓" : "•"}</span><div>${text}${action}</div></div>`;
  host.innerHTML = step(true, "Connected")
    + (["pending", "applying"].includes(store.longhorn?.state)
      ? step(false, `Backup target on ${esc(name)} is switching`, `<div class="dim xs">${esc(store.longhorn.detail)}. Homestead retries automatically.</div>`)
      : store.longhorn?.state === "failed" || (store.ready && store.longhorn?.pointed === false)
      ? step(false, `Longhorn on ${esc(name)} is not writing to its backup storage`,
          `<div class="dim xs">${esc(store.longhorn.detail || `Current endpoint: ${store.longhorn.endpoint || "none"}`)}</div>
           <div><button class="btn sm" data-need="admin" onclick="clusterPointTarget(${jsq(name)})">Retry target switch</button></div>`)
      : store.stopped
      ? step(false, `Migration from ${esc(name)} is off: its backup storage is stopped`,
          `<div><button class="btn sm pri" data-need="admin" onclick="clusterTransfersOn(${jsq(name)})">Enable it</button></div>`)
      : target.configured && target.reachable_off_cluster && !target.answers
      ? step(false, `Backup storage on ${esc(name)} is at <span class="mono">${esc(target.endpoint || "")}</span>, but this cluster cannot reach it`,
          `<div class="dim xs">It may still be starting. Otherwise the address is taken by something else or firewalled - give it another.</div>
           <div><button class="btn sm" data-need="admin" onclick="clusterStorage(${jsq(name)},true)">Give it another address</button></div>`)
      : r.update_first
      ? step(false, `${esc(name)} runs Homestead v${esc(r.version?.version || "?")}, which sets up backup storage with MinIO - whose images can no longer be downloaded`,
          `<div class="dim xs">Update it to 2.8.111 or later, then set it up from here:</div><div class="mono xs">kubectl -n lab set image deployment/homestead homestead=ghcr.io/homestead-lab/homestead:2.8.111</div>`)
      : target.configured && target.reachable_off_cluster
      ? step(true, `Backup storage on ${esc(name)}`, `<div class="dim xs mono">${esc(target.url || "")} · ${esc(target.endpoint || "")}</div>`)
      : target.configured
        ? step(false, `Backup storage on ${esc(name)} has no LAN address, so this cluster cannot read it${target.endpoint ? ` - it is at <span class="mono">${esc(target.endpoint)}</span>, inside that cluster` : ""}`,
            `<div><button class="btn sm" data-need="admin" onclick="clusterStorage(${jsq(name)},true)">Give it an address</button></div>`)
        : store.deployed && !store.ready
          ? step(false, `Backup storage on ${esc(name)} is starting`, '<div class="dim xs">Its first start downloads the S3 server; this checks again every 15 seconds.</div>')
        : store.deployed
          ? step(false, `Backup storage is running on ${esc(name)}, but its Longhorn is not pointed at it yet`,
              `<div><button class="btn sm pri" data-need="admin" onclick="clusterStorage(${jsq(name)})">Finish setting it up</button></div>`)
        : step(false, `${esc(name)} has no backup storage yet`,
            `<div><button class="btn sm pri" data-need="admin" onclick="clusterStorage(${jsq(name)})">Set it up on ${esc(name)}</button></div>`))
    + step(r.ready, "Browse its apps and move them here");
  if (window.applyRole) applyRole();
  clearTimeout(window["__clready_" + name]);
  if (store.deployed && (!store.ready || ["pending", "applying"].includes(store.longhorn?.state)))
    window["__clready_" + name] = setTimeout(() => { if ($("#clready_" + name)) clusterReady(name); }, 15000);
};

window.clusterPointTarget = async (name, after = null) => {
  try {
    const r = await api("/api/move/clusters/target", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(r.detail, "ok");
    if (after) after(); else clusterReady(name);
  } catch (e) { toast(e.message, "bad"); }
};

window.clusterTransfersOn = async name => {
  try {
    const r = await api("/api/move/clusters/transfers", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, allow: true }) });
    toast(`${name}: ${r.detail}`, "ok");
    clusterReady(name);
    if (window.fleetTransfersPaint) fleetTransfersPaint();
  } catch (e) { toast(e.message, "bad"); }
};

/* Backup storage on the other cluster, made from here with the account
   Homestead already holds for it - what its own Data protection page does. */
window.clusterStorage = (name, addressOnly = false, after = null) => {
  window.__clusterStorageAfter = after;
  window.__clusterStorageAddressOnly = addressOnly;
  const ready = window.__clusterReady?.[name] || {};
  const free = ready.free_vips || [];
  const shared = ready.shared_vip || "";
  // Its shared address first: the store answers on its own port there, beside
  // the apps that share it, so no address of its own is needed.
  childModal(`Backup storage · ${name}`, `<div class="ui-stack">
    ${UI.lead(addressOnly
      ? `The backup storage on <b>${esc(name)}</b> runs, but only inside that cluster. Give it an address on your LAN and this cluster reads the backups from there.`
      : `Puts an S3 store (RustFS) on a Longhorn volume on <b>${esc(name)}</b> and points its Longhorn backups at it. A move backs each volume up there, then restores it here.`)}
    ${UI.fields(
      addressOnly ? "" : UI.field("Size (GiB)", '<input id="cs_size" type="number" min="5" value="100">'),
      UI.field("Address", `<select id="cs_pick" onchange="$('#cs_ip').hidden = this.value !== '__typed'">
          <option value="__shared" selected>${shared ? `Its shared address · ${esc(shared)}` : "Its hosts' own addresses"}</option>
          ${free.some(v => v.from === "vips") ? `<optgroup label="An address of its own: ${esc(name)}'s VIPs">${free.filter(v => v.from === "vips").map(v =>
            `<option value="${esc(v.ip)}">${esc(v.ip)}${v.label ? ` · ${esc(v.label)}` : ""}</option>`).join("")}</optgroup>` : ""}
          ${free.some(v => v.from !== "vips") ? `<optgroup label="An address of its own: free in ${esc(name)}'s IP pools">${free.filter(v => v.from !== "vips").map(v =>
            `<option value="${esc(v.ip)}">${esc(v.ip)}</option>`).join("")}</optgroup>` : ""}
          <option value="__typed">Type an address…</option></select>
        <input id="cs_ip" class="mono" placeholder="192.0.2.243" hidden data-ipam>`,
        { help: `${shared ? `${esc(name)}'s shared address is the one its apps share; the store answers on its own port there.` : `${esc(name)} puts Services on its hosts' own addresses; the store answers on its own port there.`} Choose an address of its own only to keep its traffic apart.` }),
      addressOnly ? "" : UI.field("Port", '<input id="cs_port" type="number" min="1" max="65534" placeholder="9000" class="mono">',
        { help: "Blank keeps the existing port or uses 9000. The console uses the next port; both must be available." }))}
    ${UI.more("Whose address this is", `<p>This address serves ${esc(name)}'s backup store. The destination reads backups from it without taking the address. The store shares the source cluster's disks; keep an independent backup.</p>`)}
    ${UI.actions(UI.button("Cancel", "modalBack()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button(addressOnly ? "Set the address" : "Set it up", `clusterStorageGo(${jsArg(name)})`, { kind: "pri", id: "cs_go" }))}
  </div>`);
};
window.clusterStorageGo = async name => {
  const pick = $("#cs_pick")?.value || "__shared";
  const address = pick === "__typed" ? $("#cs_ip").value.trim() : pick === "__shared" ? "" : pick;
  if (pick === "__typed" && !address) return toast("Type the address this cluster should reach it at", "bad");
  const button = $("#cs_go");
  if (button) { button.disabled = true; button.textContent = "Working…"; }
  try {
    const r = await api("/api/move/clusters/storage", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: +($("#cs_size")?.value || 100), lb_ip: address,
        vip_mode: pick === "__shared" ? "shared" : "", port: +($("#cs_port")?.value || 0) || undefined }) });
    toast(r.detail, "ok");
    modalBack();
    const after = window.__clusterStorageAfter;
    setTimeout(() => { clusterReady(name); if (after) after(); }, 1500);
  } catch (e) {
    toast(e.message, "bad");
    if (button) { button.disabled = false; button.textContent = "Set it up"; }
  }
};

window.clusterBrowse = async name => {
  const host = $("#cluster_" + name);
  if (host) host.innerHTML = '<span class="spin2"></span> asking…';
  try {
    const report = await api("/api/move/remote", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    STATE.data.remoteInventory = report;
    clusterInventory(report);
    const volumeCount = (report.volumes || []).length;
    if (host) host.textContent = `${report.workloads.length} app${report.workloads.length === 1 ? "" : "s"}`
      + ` · ${report.movable} movable` + (volumeCount ? ` · ${volumeCount} volume${volumeCount === 1 ? "" : "s"}` : "");
  } catch (e) {
    if (host) host.innerHTML = `<span class="bad">${esc(e.message)}</span>`;
    toast(e.message, "bad");
  }
};

/* What the far cluster has, and plainly what of it cannot come. */
window.clusterInventory = report => {
  const rows = [...(report.workloads || []), ...(report.vms || [])];
  const theirs = report.version || "";
  modal(`Apps · ${report.cluster}`, `
  <div class="dim xs" style="margin-bottom:10px">${esc(report.cluster)} runs Homestead
    ${theirs ? `<b class="mono">v${esc(theirs)}</b>` : "an older release that does not say which"};
    this one runs <b class="mono">v${esc(HOMESTEAD_VERSION)}</b>.</div>
  ${rows.length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr>
    <th>App</th><th>Runs</th><th>Volumes</th><th>State</th><th></th></tr></thead><tbody>
    ${rows.map(w => `<tr>
      <td><b>${esc(w.name)}</b> ${w.kind === "vm" ? UI.chip("VM", "neutral") : ""}
        <div class="dim xs mono">${esc(w.namespace)}</div></td>
      <td class="mono small" style="word-break:break-all">${w.kind === "vm"
        ? `${esc(w.cores || "?")} vCPUs · ${esc(w.memory || "?")}` : esc(w.image)}</td>
      <td class="small">${w.volumes.length
        ? w.volumes.map(v => `<div class="mono xs">${esc(v.claim)} · ${v.size_gb} GiB${v.path ? ` → ${esc(v.path)}` : ""}</div>`).join("")
        : '<span class="dim">none</span>'}</td>
      <td>${UI.chip(w.running ? "running" : "stopped", w.running ? "ok" : "neutral")}</td>
      <td>${w.movable
        ? actionBar([{ label: "Copy to this cluster", run: `moveReview(${jsq(report.cluster)},${jsq(w.kind)},${jsq(w.name)},'copy',${jsq(w.namespace || "")})`, need: "admin" },
                     { label: "Move to this cluster", run: `moveReview(${jsq(report.cluster)},${jsq(w.kind)},${jsq(w.name)},'move',${jsq(w.namespace || "")})`, need: "admin" }])
          + ((w.warnings || []).length ? `<div class="dim xs" style="max-width:240px;margin-top:4px">${w.warnings.map(esc).join("; ")}</div>` : "")
        : `${UI.chip("cannot move", "bad")}<div class="dim xs" style="max-width:240px">${w.blockers.map(esc).join("; ")}</div>`}</td>
    </tr>`).join("")}</tbody></table></div>`
    : '<div class="empty">That cluster is running nothing Homestead can see.</div>'}
  ${clusterVolumesHtml(report)}
  ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Close</button>`)}`);
};

/* Volumes that can move on their own. One a running app or VM uses is
   listed but refused, naming what uses it: moving that brings the volume. */
function clusterVolumesHtml(report) {
  const volumes = report.volumes || [];
  if (!volumes.length) return "";
  return `<h3 style="margin:18px 0 8px">Volumes</h3>
  <div class="dim xs" style="margin-bottom:8px">A volume moves on its own when nothing is using it. Moving an app or VM brings its volumes with it.</div>
  <div class="tblwrap"><table class="tbl stack dense"><thead><tr>
    <th>Volume</th><th>Size</th><th>Used by</th><th></th></tr></thead><tbody>
    ${volumes.map(v => `<tr>
      <td><b class="mono">${esc(v.name)}</b>${v.volume_mode === "Block" ? ` ${UI.chip("disk", "neutral")}` : ""}
        <div class="dim xs mono">${esc(v.namespace)}${v.storage_class ? ` · ${esc(v.storage_class)}` : ""}</div></td>
      <td class="mono small">${v.size_gb} GiB</td>
      <td class="small">${v.used_by.length ? v.used_by.map(esc).join(", ") : '<span class="dim">nothing</span>'}</td>
      <td>${v.movable
        ? actionBar([{ label: "Move to this cluster", run: `moveReview(${jsq(report.cluster)},'volume',${jsq(v.name)})`, need: "admin" }])
          + ((v.warnings || []).length ? `<div class="dim xs" style="max-width:240px;margin-top:4px">${v.warnings.map(esc).join("; ")}</div>` : "")
        : `${UI.chip("cannot move", "bad")}<div class="dim xs" style="max-width:240px">${v.blockers.map(esc).join("; ")}</div>`}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

/* Before anything stops: where it lands, what address it gets, and every
   reason it would fail or surprise someone - asked of both clusters. */
/* The linked-cluster move or copy review (#909), on the shared review flow: it keeps the sequence and the
   one-shot send. A plan is read again whenever a field changes, so most of the page invalidates it. */
let MOVE_PLAN_EDITS = 0;
const invalidateMovePlan = () => { ++MOVE_PLAN_EDITS; MOVE_PLAN.back(); };
/* Each volume's choice in the move being reviewed: move (the default),
   blank or skip, and its storage class here. Kept across re-checks. */
let MOVE_VOLUMES = {};
let MOVE_TRANSFER_MODE = "move";
let MOVE_SOURCE_NAMESPACE = "";
let MOVE_DEVICES = {}, MOVE_ROM_NAMES = {};
// Whether the last review copies a VM while it keeps running (plan.live),
// and whether the person chose to stop it for the copy instead.
let MOVE_LIVE = false, MOVE_STOP_SOURCE = false;
// The transfer whose dialog is open now. A send that settles re-plans this, not what it sent: a person can go
// Back during a send and open another, and the flow reads no plan until that send has settled.
let MOVE_TARGET = null;
window.moveReview = (cluster, kind, name, transferMode = "move", sourceNamespace = "") => {
  invalidateMovePlan();
  MOVE_TARGET = { cluster, kind, name };
  MOVE_VOLUMES = {};
  MOVE_DEVICES = {}; MOVE_ROM_NAMES = {};
  MOVE_TRANSFER_MODE = transferMode === "copy" && kind !== "volume" ? "copy" : "move";
  MOVE_SOURCE_NAMESPACE = sourceNamespace;
  MOVE_STOP_SOURCE = false;
  const copy = MOVE_TRANSFER_MODE === "copy", verb = copy ? "Copy" : "Move";
  // A review of what moves and where, with the choices that shape it above the plan.
  childModal(`${verb} from ${cluster} · ${name}${sourceNamespace ? ` · ${sourceNamespace}` : ""}`, UI.dialog({ shape: "review",
    subject: { kind: kind === "vm" ? "VM" : kind === "volume" ? "Volume" : "App", name, where: `${cluster}${sourceNamespace ? ` · ${sourceNamespace}` : ""}` },
    lead: `${kind === "volume"
    ? `Holds ${esc(name)} on ${esc(cluster)} while nothing uses it, backs it up to the shared backup storage,
      and restores it here. The original stays on ${esc(cluster)} until you remove it, so nothing is lost if
      you change your mind.`
    : copy ? `Creates a stopped copy here from backups of the source's volumes. ${kind === "vm"
      ? "A VM keeps running while its disks are snapshotted, when its cluster's Homestead can do that; the review says."
      : "The source pauses for its volume backups, then resumes its previous running state."}
      Review the copy's network settings before starting it.`
    : `Stops ${esc(name)} on ${esc(cluster)}, backs up its volumes to the shared backup
      storage, restores them here, and starts it here. The original stays on ${esc(cluster)}, stopped,
      until you remove it, so it can be put back at any point before then.`}`,
    body: `
  <div class="f2"><div class="f"><label>Namespace here</label><input id="mv_ns" value="${esc(defaultNamespace())}"></div>
    ${kind !== "container" ? "" : `<div class="f"><label>Address ${tip("The LAN address its Services get on this cluster. Shared uses this Homestead's own address; automatic takes a free one from the Harvester IP pool.")}</label>
      <select id="mv_mode" onchange="moveAddressMode(this.value)">
        <option value="shared">This cluster's shared address</option>
        <option value="automatic">Next free pool address</option>
        <option value="manual">A specific address</option></select></div>`}</div>
  <div class="f" id="mv_ip_wrap" hidden><label>Specific address</label><input id="mv_ip" class="mono" placeholder="192.0.2.245" data-ipam></div>
  <div class="f"><label>Destination storage class</label><select id="mv_sc" disabled><option value="">Loading destination classes…</option></select>
    <div class="dim xs">Restored volumes inherit this class's replica count, disk tags and other storage settings.</div></div>
  ${copy && kind === "vm" ? `<div class="f" id="mv_stop_row" hidden><label class="switch"><input type="checkbox" id="mv_stop"
      onchange="moveStopSource(${jsq(cluster)},${jsq(kind)},${jsq(name)},this.checked)"> Stop the VM while its disks are backed up</label>
    <div class="dim xs">It starts again on ${esc(cluster)} once the backups are done. Stopping gives the copy the disks of a cleanly shut-down guest, without needing its guest agent.</div></div>` : ""}
  <div id="mv_plan"></div>
`,
    actions: { dismiss: { label: "Cancel", onclick: "modalBack()" },
      start: `<button class="btn" onclick="movePlan(${jsq(cluster)},${jsq(kind)},${jsq(name)})">Check again</button>`,
      primary: { label: `Start ${copy ? "copy" : "move"}`, onclick: `moveStart(${jsArg(cluster)},${jsArg(kind)},${jsArg(name)})`, id: "mv_go", disabled: true, attrs: 'data-need="admin"' } } }),
    false, "operation-review");
  movePlan(cluster, kind, name);
  for (const id of ["#mv_ns", "#mv_ip"]) {
    const field = $(id);
    if (field) field.oninput = () => { invalidateMovePlan(); $("#mv_go").disabled = true; };
  }
};

window.moveAddressMode = value => {
  invalidateMovePlan();
  if ($("#mv_go")) $("#mv_go").disabled = true;
  const wrap = $("#mv_ip_wrap");
  if (wrap) wrap.hidden = value !== "manual";
};

const moveBody = (cluster, kind, name) => ({
  cluster, kind, name, namespace: $("#mv_ns")?.value.trim() || defaultNamespace(),
  address_mode: $("#mv_mode")?.value || "shared", address: $("#mv_ip")?.value.trim() || "",
  storage_class: $("#mv_sc")?.value || "", volumes: MOVE_VOLUMES, transfer_mode: MOVE_TRANSFER_MODE,
  source_namespace: MOVE_SOURCE_NAMESPACE, host_devices: MOVE_DEVICES, stop_source: MOVE_STOP_SOURCE });

window.moveStopSource = (cluster, kind, name, stop) => {
  MOVE_STOP_SOURCE = stop === true;
  if ($("#mv_go")) $("#mv_go").disabled = true;
  movePlan(cluster, kind, name);
};
window.moveDeviceSet = (cluster, kind, name, device, field, value) => {
  MOVE_DEVICES[device] = { ...(MOVE_DEVICES[device] || {}), [field]: value };
  if (field === "rom") delete MOVE_ROM_NAMES[device];
  if ($("#mv_go")) $("#mv_go").disabled = true;
  movePlan(cluster, kind, name);
};
window.moveDeviceRom = async (cluster, kind, name, device, input) => {
  const file = input.files?.[0];
  if (!file) return;
  invalidateMovePlan();
  const edit = MOVE_PLAN_EDITS;
  if ($("#mv_go")) $("#mv_go").disabled = true;
  try {
    const data = await vmReadRom(file);
    if (edit !== MOVE_PLAN_EDITS) return;
    MOVE_DEVICES[device] = { ...(MOVE_DEVICES[device] || {}), rom: data };
    MOVE_ROM_NAMES[device] = file.name;
    movePlan(cluster, kind, name);
  } catch (error) { toast(error.message, "bad"); }
};
function moveDevicesTable(plan, cluster, kind, name) {
  if (!plan.host_devices?.length) return "";
  const args = [cluster, kind, name].map(jsq).join(",");
  const rows = plan.host_devices.map(d => {
    const choice = MOVE_DEVICES[d.name] || {}, resources = (plan.device_resources || []).filter(r => !d.gpu || r.kind === "pci");
    const handler = `${args},${jsq(d.name)}`;
    const uploaded = choice.rom && !["source", ""].includes(choice.rom);
    return `<tr><td data-label="Source device"><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.resource)}</div></td>
      <td data-label="Destination device" data-wide><select aria-label="Destination device for ${esc(d.name)}" onchange="moveDeviceSet(${handler},'resource',this.value)">
        <option value="" disabled ${!("resource" in choice) ? "selected" : ""}>Choose a device</option>
        <option value="" ${choice.resource === "" ? "selected" : ""}>Leave out</option>
        ${resources.map(r => `<option value="${esc(r.resource)}" ${r.resource === choice.resource ? "selected" : ""}>${esc(r.label || r.resource)} · ${esc(r.nodes?.join(", ") || "unavailable")}</option>`).join("")}</select></td>
      <td data-label="vBIOS" data-wide><select aria-label="vBIOS for ${esc(d.name)}" onchange="moveDeviceSet(${handler},'rom',this.value)" ${choice.resource === "" ? "disabled" : ""}>
        ${d.rom ? `<option value="source" ${!("rom" in choice) || choice.rom === "source" ? "selected" : ""}>Keep source vBIOS</option>` : ""}
        <option value="" ${choice.rom === "" || !d.rom && !uploaded ? "selected" : ""}>Use device default ROM</option>
        ${uploaded ? `<option selected disabled>${esc(MOVE_ROM_NAMES[d.name] || "Supplied vBIOS")}</option>` : ""}</select>
        <input type="file" class="pd_rom" aria-label="Upload destination vBIOS for ${esc(d.name)}" accept=".rom,.bin,application/octet-stream" onchange="moveDeviceRom(${handler},this)" ${choice.resource === "" ? "disabled" : ""}></td></tr>`;
  }).join("");
  return `<div class="f"><label>Passthrough on this cluster</label><p class="dim small">Map each source device to hardware here, or leave it out. Prepare IOMMU and devices under Hosts → Hardware first. A different GPU may need a different vBIOS.</p>
    <div class="tblwrap"><table class="tbl stack dense vm-passthrough-table"><colgroup><col style="width:25%"><col style="width:40%"><col style="width:35%"></colgroup><thead><tr><th>Source device</th><th>Destination device</th><th>vBIOS</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${plan.device_hosts?.length ? `<p class="dim small">Hosts offering all selected devices: ${plan.device_hosts.map(esc).join(", ")}.</p>` : ""}</div>`;
}

/* One volume's choice changed: remembered, and the move checked again. */
window.moveVolumeSet = (cluster, kind, name, claim, field, value) => {
  MOVE_VOLUMES[claim] = { ...(MOVE_VOLUMES[claim] || {}), [field]: value };
  if (field === "action") { delete MOVE_VOLUMES[claim].storage_class; delete MOVE_VOLUMES[claim].size_gb; }
  if ($("#mv_go")) $("#mv_go").disabled = true;
  movePlan(cluster, kind, name);
};

const MOVE_VOLUME_ACTIONS = { move: "Move its data", blank: "Create blank", skip: "Skip (use one already here)" };
function moveVolumesTable(plan, cluster, kind, name) {
  const volumes = plan.claims || [];
  if (!volumes.length) return "";
  if (kind === "volume") {
    return `<div class="drow"><div class="dl">Volume</div><div class="dv mono xs">${volumes.map(c =>
      `${esc(c.claim)} · ${c.size_gb} GiB${c.volume_mode === "Block" ? " · disk" : ""}`).join("<br>")}<br>Storage class: ${esc(plan.storage_class || "cluster default")}</div></div>`;
  }
  const arg = v => esc(JSON.stringify(v));
  const rows = volumes.map(c => {
    const classes = c.action === "move" ? plan.storage_classes || [] : plan.all_storage_classes || [];
    const pick = (MOVE_VOLUMES[c.claim] || {}).storage_class || c.storage_class || plan.storage_class || "";
    return `<tr><td><b class="mono">${esc(c.claim)}</b><div class="dim xs">${c.size_gb} GiB${c.volume_mode === "Block" ? " · disk" : ""}${c.source_class ? ` · ${esc(c.source_class)} on ${esc(cluster)}` : ""}</div></td>
      <td><select id="mv_vol_${esc(c.claim)}" onchange="moveVolumeSet(${arg(cluster)},${arg(kind)},${arg(name)},${arg(c.claim)},'action',this.value)">
        ${Object.entries(MOVE_VOLUME_ACTIONS).map(([value, label]) => `<option value="${value}" ${c.action === value ? "selected" : ""}>${value === "move" && MOVE_TRANSFER_MODE === "copy" ? "Copy its data" : label}</option>`).join("")}</select></td>
      <td>${c.action === "skip" ? '<span class="dim xs">the one here keeps its own</span>'
        : `<select id="mv_volsc_${esc(c.claim)}" aria-label="${esc(`Storage class for ${c.claim}`)}" onchange="moveVolumeSet(${arg(cluster)},${arg(kind)},${arg(name)},${arg(c.claim)},'storage_class',this.value)">
          ${classes.map(k => `<option value="${esc(k)}" ${k === pick ? "selected" : ""}>${esc(k)}</option>`).join("")}</select>`}</td>
      <td>${c.action === "blank"
        ? `<input id="mv_volsize_${esc(c.claim)}" type="number" min="1" max="16384" style="width:90px" value="${esc((MOVE_VOLUMES[c.claim] || {}).size_gb || c.target_size_gb || c.size_gb)}"
            onchange="moveVolumeSet(${arg(cluster)},${arg(kind)},${arg(name)},${arg(c.claim)},'size_gb',+this.value)"> GiB`
        : `<span class="dim xs">${c.action === "move" ? `${c.size_gb} GiB, as it is` : "as it is here"}</span>`}</td></tr>`;
  }).join("");
  return `<div class="f"><label>Volumes ${tip("Move its data backs the volume up there and restores it here. Create blank makes an empty volume, the size you choose - for a cache, say. Skip uses a volume of the same name that is already here. A moved volume needs a Longhorn class; a blank one can use any.")}</label>
    <div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Volume</th><th>Bring</th><th>Storage class here</th><th>Size here</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

const moveLoading = ({ host, go }) => {
  host.innerHTML = '<div class="empty"><span class="spin2"></span> asking both clusters…</div>';
  if (go) go.disabled = true;
};
const MOVE_PLAN = UI.reviewFlow({
  loading: moveLoading,
  live: ({ host }) => $("#mv_plan") === host,
  preview: ({ cluster, kind, name }) => api("/api/move/plan", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(moveBody(cluster, kind, name)) }),
  render: (plan, { cluster, kind, name, host, go }) => {
    MOVE_LIVE = plan.live === true;
    const stopChoice = $("#mv_stop_row");
    if (stopChoice) stopChoice.hidden = plan.can_live !== true;
    if (MOVE_TRANSFER_MODE === "copy" && plan.transfer_mode !== "copy") {
      plan.unusable = true;
      host.innerHTML = UI.callout("bad", "Update Homestead here before copying", "This destination only supports moves.");
      return;
    }
    // A plan without each volume's action is from before there was a choice: every volume moves.
    const volumes = (plan.claims || []).map(c => ({ ...c, action: c.action || "move" }));
    plan.claims = volumes;
    const classes = $("#mv_sc");
    if (classes && plan.storage_classes) {
      const selected = classes.value || plan.storage_class;
      classes.innerHTML = plan.storage_classes.map(c => `<option value="${esc(c)}" ${c === selected ? "selected" : ""}>${esc(c)}</option>`).join("");
      classes.disabled = !plan.storage_classes.length;
      classes.onchange = () => { if (go) go.disabled = true; window.movePlan(cluster, kind, name); };
      // A cluster without Homestead's old default can still choose another
      // valid class. Review the actual selection before enabling Start.
      if (classes.value && classes.value !== plan.storage_class) return window.movePlan(cluster, kind, name);
    }
    const fix = (plan.fixes || [])[0];
    host.innerHTML = `
      ${UI.notices([...(plan.blockers || []).map(b => ["bad", b]), ...(plan.warnings || []).map(w => ["warn", w])],
        plan.blockers?.length ? `This ${MOVE_TRANSFER_MODE === "copy" ? "copy" : "move"} cannot start` : "Before you start",
        { actions: plan.blockers?.length && fix ? `<button class="btn sm pri" data-need="admin" onclick="clusterStorage(${jsq(cluster)},${fix.kind === "source-address"},() => movePlan(${jsq(cluster)},${jsq(kind)},${jsq(name)}))">${fix.kind === "source-address"
          ? `Give ${esc(cluster)}'s backup storage an address` : `Set up backup storage on ${esc(cluster)}`}</button>` : "" })}
      ${plan.ok ? `${(plan.warnings?.length
        // Ready with warnings: the one notice is the warnings, and this is said plainly.
        ? (t, b) => `<p class="ui-lead"><b>${t}</b> ${b}</p>` : (t, b) => UI.callout("ok", { html: t }, b))(`Ready to ${MOVE_TRANSFER_MODE === "copy" ? "copy" : "move"}.`, `${kind === "volume" ? `${esc(name)} (${plan.total_gb} GiB) goes through ${plan.joined ? "the backup storage both clusters already share" : `the backup storage on ${esc(cluster)}, which this cluster will be pointed at`}.`
          : volumes.some(c => c.action === "move") ? (() => { const moving = volumes.filter(c => c.action === "move");
            return `${moving.length === 1 ? "One volume" : `${moving.length} volumes`} (${plan.total_gb} GiB) ${moving.length === 1 ? "goes" : "go"}
          through ${plan.joined ? "the backup storage both clusters already share" : `the backup storage on ${esc(cluster)}, which this cluster will be pointed at`}.`; })()
          : volumes.length ? "No volume's data is moved, so only its definition travels."
          : "It has no volumes, so only its definition travels."}
        ${plan.addresses?.length ? `<br>Reachable here at ${plan.addresses.map(esc).join(", ")}.` : ""}
        ${MOVE_TRANSFER_MODE === "copy" || plan.will_run || kind === "volume" ? "" : `<br>It is stopped on ${esc(cluster)}, and will arrive stopped.`}`)}` : ""}
      ${moveVolumesTable(plan, cluster, kind, name)}${moveDevicesTable(plan, cluster, kind, name)}`;
    MOVE_PLAN.ready();
  },
  failed: (e, { host }) => { host.innerHTML = `${UI.callout("bad", "", `${esc(e.message)}`)}`; },
  // Start follows the plan: enabled only when it says the move can go ahead.
  blocked: plan => !plan.ok || !!plan.unusable, button: "mv_go",
  apply: async () => {
    const { body, copy, cluster, name } = MOVE_STARTING;
    try {
      await api("/api/move/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      toast(`${copy ? "Copying" : "Moving"} ${name} from ${cluster}; follow progress under Linked clusters or in Activity`, "ok");
      closeModal(); movesRepaint();
    } catch (e) {
      toast(e.message, "bad");
      MOVE_REPLAN = true;   // read the plan again once this send has settled, so Start can be tried again
    }
  },
  after: () => {
    const again = MOVE_REPLAN && MOVE_TARGET; MOVE_REPLAN = false;
    if (again) window.movePlan(again.cluster, again.kind, again.name);   // nothing happens if no such dialog is open
  },
});
let MOVE_STARTING = null, MOVE_REPLAN = false;
window.movePlan = (cluster, kind, name) => {
  const host = $("#mv_plan"), go = $("#mv_go");
  if (!host) return;
  ++MOVE_PLAN_EDITS;
  // Nothing is read while a start is being sent. The dialog waits on the spinner: a refused start's after() reads
  // its plan, and an accepted one closes it.
  if (MOVE_PLAN.busy) return moveLoading({ host, go });
  return MOVE_PLAN.open({ cluster, kind, name, host, go });
};

window.moveStart = async (cluster, kind, name) => {
  const review = MOVE_PLAN.review;
  const body = JSON.parse(JSON.stringify(moveBody(cluster, kind, name)));
  const copy = body.transfer_mode === "copy";
  const question = copy ? `Copy ${name} from ${cluster}?` + String.fromCharCode(10, 10)
      + (MOVE_LIVE ? "It keeps running while its disks are snapshotted and backed up. The destination copy stays stopped."
        : MOVE_STOP_SOURCE ? "It stops while its disks are backed up, then starts again as it was. The destination copy stays stopped."
        : "The source stops while its volumes are backed up, then returns to its original running state. The destination copy stays stopped.")
    : kind === "volume"
    ? `Bring the volume ${name} here from ${cluster}?` + String.fromCharCode(10, 10)
      + `Nothing on ${cluster} can use it until the move finishes or is put back.`
    : `Stop ${name} on ${cluster} and bring it here?` + String.fromCharCode(10, 10)
      + "It is unavailable from the moment it stops there until it starts here.";
  if (!(await ask(question, { title: copy ? "Copy here" : "Move here", ok: copy ? "Copy" : kind === "volume" ? "Move" : "Stop and move", danger: !copy && kind !== "volume" }))) return;
  if (!review || MOVE_PLAN.review !== review) return toast("The transfer settings changed; review them again", "bad");
  MOVE_STARTING = { body, copy, cluster, name };
  await MOVE_PLAN.apply();
};

const MOVE_PHASE_WORDS = { joining: "Share storage", quiescing: "Stop there", "backing-up": "Back up",
  "releasing-source": "Resume source", syncing: "See backups", restoring: "Restore here", creating: "Create here", starting: "Start here" };

function movesHtml(moves) {
  return (moves || []).map(m => {
    const copy = m.transfer_mode === "copy";
    const tone = m.status === "succeeded" ? "ok" : m.status === "failed" ? "bad"
      : m.status === "cancelled" ? "neutral" : "warn";
    const steps = m.phases.filter(p => p !== "done").map((phase, index) => {
      const state = m.status === "succeeded" || index < m.phase_index ? "done"
        : index === m.phase_index ? (m.status === "failed" ? "failed" : m.status === "running" ? "active" : "")
        : "";
      return `<div class="${state}"><i></i><span><b>${esc(copy && phase === "starting" ? "Keep copy stopped" : MOVE_PHASE_WORDS[phase] || phase)}</b></span></div>`;
    }).join("");
    const actions = [
      m.status === "failed" || m.resource_cleanup_pending ? `<button class="btn sm" data-need="admin" onclick="moveAct('retry',${jsq(m.id)})">${m.cleanup_pending || m.resource_cleanup_pending ? "Retry cleanup" : m.phase === "backing-up" ? "Retry failed backups" : "Retry"}</button>` : "",
      m.status === "succeeded" && !m.source_removed && !copy
        ? `<button class="btn sm" data-need="admin" onclick="moveFinish(${jsq(m.id)},${jsq(m.name)},${jsq(m.cluster)},${jsq(m.kind)})">Remove from ${esc(m.cluster)}</button>` : "",
      ["running", "failed", "succeeded"].includes(m.status) && !m.source_removed
        ? `<button class="btn sm danger" data-need="admin" onclick="moveBack(${jsq(m.id)},${jsq(m.name)},${jsq(m.cluster)},${jsq(m.status)},${m.source_stopped === false ? "false" : "true"},${jsq(copy ? "copy" : "move")})">${copy ? m.status === "succeeded" ? "Remove copy" : "Cancel copy" : m.source_stopped === false ? "Cancel" : "Put back"}</button>` : "",
      ["succeeded", "cancelled", "failed"].includes(m.status) && !(copy && m.source_stopped) && m.can_dismiss !== false
        ? `<button class="btn sm" data-need="admin" data-tip="${m.status === "failed" ? `Clear it from this list, touching neither cluster` : m.status === "succeeded" && !m.source_removed && !copy ? `Clear it from this list. ${esc(m.cluster)} keeps its stopped copy until you remove it there.` : "Clear it from this list"}"
            onclick="moveDismiss(${jsq(m.id)})">Dismiss</button>` : "",
    ].join("");
    const started = Math.max(0, (Date.now() - Date.parse(m.created_at)) / 1000);
    return `<div class="card flat moveitem">
      <div class="between"><div><div class="ctitle">${esc(m.name)} <span class="dim">from ${esc(m.cluster)}</span>
        ${m.kind === "vm" ? UI.chip("VM", "neutral") : ""}${copy ? ` ${UI.chip("Copy", "neutral")}` : ""}</div>
        <div class="csub">into ${esc(m.namespace)} · started ${esc(started < 90 ? "just now" : fmtAgo(started))}</div></div>
        ${UI.chip(m.status, tone)}</div>
      <div class="rollout-meter"><span style="width:${Math.max(2, m.progress)}%"></span></div>
      <div class="rollout-steps movesteps">${steps}</div>
      ${m.status === "failed" && m.message
        // A failure is an error notice: its summary, the rest when opened (design.md, Notices).
        ? `${UI.callout("bad", "", esc(m.message))}<div class="row" style="justify-content:flex-end">${actions}</div>`
        : `<div class="between"><span class="dim xs">${esc(m.message || "")}</span><div class="row">${actions}</div></div>`}
      ${m.cleanup_message ? `<div class="dim xs" style="margin-top:6px">${m.resource_cleanup_pending ? '<span class="spin2"></span> ' : ""}${esc(m.cleanup_message)}</div>` : ""}
    </div>`;
  }).join("");
}

/* A failed read of the moves is not "no moves": say so, with Retry. */
function movesUnreadHtml(error) {
  return `${UI.callout("bad", "", `The moves could not be read: ${esc(error)}
    <button class="btn sm" onclick="movesRepaint()">Retry</button>`)}`;
}
window.movesUnreadHtml = movesUnreadHtml;

/* Only the moves section repaints while one runs, so a form half-filled
   elsewhere on the page is left alone. */
async function watchMoves() {
  clearTimeout(window.__moveTimer);
  if (!$("#movesList")) return;
  const moves = await api("/api/move/moves").catch(e => ({ error: e.message }));
  const host = $("#movesList");
  if (host) host.innerHTML = moves.error ? movesUnreadHtml(moves.error) : movesHtml(moves);
  if (moves.error) return;
  if (window.applyRole) window.applyRole();
  if ((moves || []).some(m => m.status === "running" || m.resource_cleanup_pending || m.backup_target_cleanup_pending)) window.__moveTimer = setTimeout(watchMoves, 4000);
}
window.watchMoves = watchMoves;

window.moveAct = async (action, id) => {
  try {
    await api(`/api/move/moves/${action}`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast(action === "retry" ? "Retry requested" : "done", "ok");
    watchMoves();
  } catch (e) { toast(e.message, "bad"); }
};

window.moveBack = async (id, name, cluster, status, stopped = true, transferMode = "move") => {
  if (transferMode === "copy") {
    if (!(await ask(`${status === "succeeded" ? "Remove the copy of" : "Cancel copying"} ${name}?` + String.fromCharCode(10, 10)
      + `Removes the objects this copy created here, including their volume data. The original on ${cluster} is kept${stopped ? " and its previous running state is restored" : " as it is"}.`, { title: status === "succeeded" ? "Remove copy" : "Cancel copying", ok: status === "succeeded" ? "Remove copy" : "Cancel copying", danger: true }))) return;
    return moveAct("abandon", id);
  }
  // Nothing has stopped on the source yet: undoing it only cancels the move.
  if (!stopped) {
    if (!(await ask(`Cancel moving ${name}?` + String.fromCharCode(10, 10)
        + `Nothing has stopped on ${cluster}; anything this move set up here is removed.`, { title: "Cancel move", ok: "Cancel move", danger: true }))) return;
    return moveAct("abandon", id);
  }
  const landed = status === "succeeded";
  if (!(await ask(`Put ${name} back on ${cluster}?` + String.fromCharCode(10, 10)
      + "It starts again there, as it was, and what this move created here is removed"
      + (landed ? ", including anything written to it here since it arrived." : "."), { title: "Put it back", ok: "Put back" }))) return;
  moveAct("abandon", id);
};

window.moveFinish = (id, name, cluster, kind) => modal(`Remove from ${cluster} · ${name}`, kind === "volume" ? `
  <p>${esc(name)} is here now. Removing the original from ${esc(cluster)} deletes that copy and makes the move
    permanent: after this it cannot be put back.</p>
  <div class="note">Its backup stays in the backup storage.</div>` : `
  <p>${esc(name)} is running here. Removing the stopped original from ${esc(cluster)} makes the move
    permanent: after this it cannot be put back.</p>
  <label class="switch"><input type="checkbox" id="mv_vols"> Also delete the volumes it moved on ${esc(cluster)}</label>
  <div class="dim xs">Volumes it skipped or made blank here stay on ${esc(cluster)}: they are the only copy of that data.</div>
  <div class="note">Leaving the volumes costs space on ${esc(cluster)} but keeps a copy of the data as it
    was at the moment of the move. Their backups stay in the backup storage either way.</div>
  ${UI.actions(`<button class="btn danger" data-need="admin" onclick="moveFinishNow(${jsq(id)})">Remove from ${esc(cluster)}</button>
    <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Not yet</button>`)}`);

window.moveFinishNow = async id => {
  try {
    const result = await api("/api/move/moves/finish", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, volumes: !!$("#mv_vols")?.checked }) });
    toast(result.message || "source removed", "ok");
    closeModal(); watchMoves();
  } catch (e) { toast(e.message, "bad"); }
};

window.imAddPort = () => { $("#im_ports").insertAdjacentHTML("beforeend", '<div class="f4 im-port"><div><label>Container</label><input class="ipc" type="number"></div><div><label>LAN</label><input class="iph" type="number"></div><div><label>Protocol</label><select class="ipp"><option>TCP</option><option>UDP</option></select></div><label class="switch"><input class="ipe" type="checkbox" checked>Expose</label></div>'); };
window.imAddEnv = () => { $("#im_env").insertAdjacentHTML("beforeend", '<div class="f2 im-env"><div class="f"><label>Variable</label><input class="iek"></div><div class="f"><label>Value</label><input class="iev"></div></div>'); };
window.doImport = async source => {
  const env = {}; $$(".im-env").forEach(r => { const k = $(".iek", r).value.trim(); if (k) env[k] = $(".iev", r).value; });
  const cfg = STATE.data.importCfg || {};
  const volumes = importVolumes();
  const mappings = importMappings();
  const first = volumes[0];
  // A container that keeps nothing imports as just the workload. Demanding a
  // volume for it would mean creating storage nobody asked for.
  const keepsNothing = !mappings.some(row => !row.medium && (row.remote_path || row.copy === false));
  if (!first && !keepsNothing) return toast("Add at least one volume", "bad");
  const existing = !!first && !first.create;
  try { await logoReady("#im_icon"); } catch (e) { return toast(e.message, "bad"); }
  const body = { source, name: $("#im_name").value.trim(), namespace: $("#im_ns")?.value || defaultNamespace(), remote_path: $("#im_path").value.trim(),
    source_consistency: $("#im_consistency")?.value || "", source_container_id: cfg.source_container_id || "",
    image: $("#im_image").value.trim(), icon: $("#im_icon").value.trim(),
    mappings, volumes: keepsNothing ? [] : volumes,
    pvc_name: first?.name || "", size_gb: first?.size_gb || 0, reuse_existing: existing,
    storage_class: first?.storage_class || "", access_mode: first?.access_mode || "",
    start_after_copy: true, memory: $("#im_memory").value.trim(), memory_limit: $("#im_memory_limit").value.trim(),
    ports: $$(".im-port").map(r => ({ container: +$(".ipc", r).value, host: +$(".iph", r).value || +$(".ipc", r).value, protocol: $(".ipp", r).value, expose: $(".ipe", r).checked })).filter(p => p.container),
    env, hardware: selectedHardware("im_hw"), ...(readPrivileges("im_pv") || {}), network_mode: $("#im_net").value, vip_mode: $("#im_vip").value, lb_ip: ($("#im_lb_ip")?.value || "").trim(),
    uid: $("#im_uid").value.trim(), gid: $("#im_gid").value.trim() };
  if (!body.name || !body.image) return toast("App name and image are required", "bad");
  for (const volume of volumes) {
    const measured = body.mappings.filter(row => row.pvc === volume.name)
      .reduce((sum, row) => sum + (row.bytes || 0), 0);
    if (!measured) continue;
    const claim = volume.create ? null : (STATE.data.vols || []).find(vol => vol.pvc_name === volume.name);
    const capacityGb = volume.create ? volume.size_gb : (claim?.size_gb || 0);
    // Longhorn actual_gb includes snapshot/replica allocation, not filesystem
    // usage. Never subtract it to invent the free space of an existing claim.
    const neededGb = measured / 1024 ** 3;
    if (capacityGb && neededGb > capacityGb) {
      return toast(`${volume.name}: ${neededGb.toFixed(1)} GiB to copy exceeds its ${capacityGb.toFixed(1)} GiB total capacity — grow it or raise its size`, "bad");
    }
  }
  // A RAM scratch mapping has no source by design, so only copied folders are
  // asked for one.
  const copied = body.mappings.filter(row => !row.medium && row.copy !== false);
  if (copied.length && !["stopped", "snapshot"].includes(body.source_consistency)) {
    stepGo("im_steps", 1);
    return imSafetyNote(true);
  }
  const bad = body.mappings.find(row => row.medium || row.copy === false
    ? !String(row.mount_path || "").startsWith("/")
    : !String(row.remote_path || "").startsWith("/") || !String(row.mount_path || "").startsWith("/"));
  if (bad) {
    return toast(bad.medium
      ? `a RAM scratch volume needs an absolute path inside the container (${esc(bad.mount_path || "blank")})`
      : `every folder needs an absolute source and container path (${esc(bad.remote_path || "blank")})`, "bad");
  }
  const reused = keepsNothing ? [] : volumes.filter(volume => !volume.create).map(volume => volume.name);
  const absent = copied.find(row => (STATE.data.importSizes || {})[row.remote_path] === "missing");
  if (absent) return toast(`${absent.remote_path} does not exist on the source — fix the path or untick it`, "bad");
  body.remote_path = copied[0]?.remote_path || "";
  body.mount_path = copied[0]?.mount_path || "/config";
  if (reused.length && !(await ask(`Import into existing volume${reused.length === 1 ? "" : "s"} ${reused.join(", ")}?` +
      "\n\nThe current data is kept, but imported files with the same names may be replaced.", { title: "Import into existing volumes", ok: "Import" }))) return;
  await importReview(body);
};

/* Review, then import (#909), on the shared review flow: it keeps the sequence, the busy flag and the
   one-shot send. This view keeps its words. */
const IMPORT_REVIEW = UI.reviewFlow({
  preview: async config => {
    const result = await api("/api/import/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(config)});
    if (!result.capacity || !result.capacity_token || !Array.isArray(result.phases)) throw new Error("Import review unavailable; refresh before continuing.");
    return {config, ...result};
  },
  render: (result, config) => {
    const phaseWarnings = new Map();
    for (const phase of result.phases) {
      const warnings = capacityNotes(phase.capacity).concerns;
      if (phase.capacity.blocked && !warnings.length) warnings.push("Capacity checks block this phase.");
      for (const warning of warnings) phaseWarnings.set(warning, [...(phaseWarnings.get(warning) || []), phase.title]);
    }
    const warningItems = [...new Set(result.capacity.warnings || [])].map(w => `<li>${esc(w)}</li>`);
    for (const [warning, phases] of phaseWarnings) {
      if (!(result.capacity.warnings || []).includes(warning)) warningItems.push(`<li><b>${phases.map(esc).join(", ")}:</b> ${esc(warning)}</li>`);
    }

    childModal("Review import", `<div class="update-review">
      <div class="reviewbox"><b>${esc(config.name)}</b><p>${esc(config.image)}</p>
        ${config.source_consistency ? `<p>${config.source_consistency === "snapshot" ? "Copy from a consistent snapshot or backup. Source-container checks are skipped; verify the selected paths." : "All source writers must stay stopped." + (config.source_container_id ? " The original Docker container is checked before and after copying each folder." : " Homestead cannot verify other writers.")}</p>` : ""}
        ${(result.volumes || []).map(v => `<div class="dependency-row"><span>${v.create ? 'Create' : 'Reuse'} volume</span><b>${esc(v.name)} · ${esc(v.access_mode)} · ${esc(v.storage_class)}</b></div>`).join('')}
      </div>${warningItems.length ? UI.callout(result.capacity.blocked ? "bad" : "warn", result.capacity.blocked ? "Import blocked" : "Review before importing", `<ul class="ui-list">${warningItems.join("")}</ul>`) : ""}
      ${(config.mappings || []).some(m => m.copy !== false && m.medium !== 'memory') || config.remote_path ? UI.more('Copy safety checks', 'Free space is checked on the mounted destination against measured source sizes. Missing measurements are reported in the job log, not treated as zero. Estimates do not reserve space. Unsafe destination paths stop the copy; failed transfers keep both copies.') : ''}
      ${result.phases.map(p => UI.more(`${p.title}: capacity`, deployCapacityHtml(p.capacity))).join('')}
      ${!result.capacity.blocked ? '<label class="switch"><input type="checkbox" id="importConfirm"> Import it anyway, replacing files</label>' : ''}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Back to import</button><button class="btn pri" id="importGo" ${result.capacity.blocked ? 'disabled' : ''} onclick="confirmImport()">Import</button>`)}</div>`, true);
  },
  failed: e => toast(e.message, "bad"),
  blocked: review => review.capacity.blocked, ack: "importConfirm",
  apply: async review => {
    $("#importGo").disabled = true;
    try {
      const result = await api("/api/import", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({...review.config, capacity_token:review.capacity_token, confirm_capacity:true})});
      if (result.operation) window.noteOperation?.(result.operation);
      toast(result.job ? "Copy started; the application stays stopped" : "Import configuration saved", "ok");
      closeModal(); resetPaint(); viewImport();
    } catch (e) {
      // An unknown outcome is never an automatic retry: the flow consumed the review.
      toast(e.message + " — inspect Recent jobs before trying again; any created volumes are kept.", "bad");
      $("#importGo").textContent = "Review again";
      $("#importGo").disabled = false;
      $("#importGo").onclick = () => importReview(review.config);
    }
  },
  after: () => window.refreshOperations?.(true),
});
window.importReview = body => IMPORT_REVIEW.open(JSON.parse(JSON.stringify(body)));
window.confirmImport = async () => {
  if (!IMPORT_REVIEW.ready()) return toast("Review and approve the import first", "bad");
  await IMPORT_REVIEW.apply();
};

/* Finished moves off the list, leaving the source's stopped copy where it is. */
window.moveDismiss = async (id = "") => {
  let all;
  try { all = await api("/api/move/moves"); }
  catch (e) { return toast(`The moves could not be read, so nothing was cleared: ${e.message}`, "bad"); }
  const failed = id ? all.find(m => m.id === id && m.status === "failed") : null;
  const rows = all.filter(m => (!id || m.id === id) && m.status === "succeeded" && !m.source_removed);
  if (failed && !(await ask(`Clear the failed move of ${failed.name}?` + String.fromCharCode(10, 10)
      + "Nothing is changed on either cluster. "
      + (failed.source_stopped !== false ? `${failed.name} stays stopped on ${failed.cluster}: start it there from its page if you still want it running. ` : "")
      + "Anything the move made here stays too; remove it here if you do not want it. Retry and Put back are no longer offered.", { title: "Clear failed move", ok: "Clear" }))) return;
  if (!failed && rows.length && !(await ask(`Clear ${id ? "this move" : "finished moves"} from the list?` + String.fromCharCode(10, 10)
      + `${[...new Set(rows.map(m => m.cluster))].join(", ")} keeps the stopped original of ${rows.map(m => m.name).join(", ")} - `
      + "nothing is removed there, and Put back is no longer offered here.", { title: "Clear finished moves", ok: "Clear" }))) return;
  try {
    const r = await api("/api/move/moves/dismiss", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
    toast(r.detail, "ok");
    const host = $("#movesList");
    if (host) host.innerHTML = await api("/api/move/moves").then(movesHtml, e => movesUnreadHtml(e.message));
    if (window.applyRole) applyRole();
  } catch (e) { toast(e.message, "bad"); }
};

/* What transfers from other clusters hold on this one. The other cluster
   ends a hold when its move or copy finishes or is cancelled; one whose
   record is gone there never will, so a person ends it here. */
const HOLD_KINDS = { vm: "VM", container: "App", volume: "Volume" };

function moveHoldsHtml(h) {
  if (h.error) return UI.callout("bad", "Could not read what transfers hold here", esc(h.error));
  const holds = h.holds || [], left = h.leftovers || [];
  window.__moveHolds = { holds, left };
  const since = row => row.since ? ` · since ${esc(fmtAgo(Math.max(0, Date.now() / 1000 - row.since)))}` : "";
  const holdRow = (row, i) => `<li class="between">
      <div><b>${esc(row.name)}</b> ${UI.chip(HOLD_KINDS[row.kind] || row.kind, "neutral")}${row.copy ? ` ${UI.chip("Copy", "neutral")}` : ""}
        <div class="dim xs">${esc(row.namespace)} · ${row.kept_running ? "kept running" : "stopped for the transfer"}${row.backups ? ` · ${row.backups} backup${row.backups === 1 ? "" : "s"}` : ""}${since(row)}${row.transfer_id ? ` · transfer ${esc(row.transfer_id)}` : ""}</div></div>
      <button class="btn sm danger" data-need="admin" onclick="moveHoldRelease(${i})">Release</button></li>`;
  const leftRow = (row, i) => `<li class="between">
      <div><b>${esc(row.name || "Something no longer on this cluster")}</b>${row.kind ? ` ${UI.chip(HOLD_KINDS[row.kind] || row.kind, "neutral")}` : ""}
        <div class="dim xs">${[row.backups && `${row.backups} backup${row.backups === 1 ? "" : "s"}`, row.snapshots && `${row.snapshots} snapshot${row.snapshots === 1 ? "" : "s"}`].filter(Boolean).join(", ")}
          ${row.deleting ? ` · ${row.deleting} deleting` : ""} · transfer ${esc(row.transfer_id)}</div></div>
      <button class="btn sm" data-need="admin" onclick="moveLeftoverTidy(${i})">Remove</button></li>`;
  return `${holds.length ? `<h3 style="margin:18px 0 8px">Held here for transfers</h3>
      <p class="dim xs">Moves and copies to other clusters hold these. The other cluster lets go when its transfer finishes or is cancelled; release one only if that transfer is no longer listed there.</p>
      <ul class="fleet-list">${holds.map(holdRow).join("")}</ul>` : ""}
    ${left.length ? `<h3 style="margin:18px 0 8px">Leftovers from transfers</h3>
      <p class="dim xs">Backups and snapshots made for transfers that no longer hold anything here. They use space here and in backup storage.</p>
      <ul class="fleet-list">${left.map(leftRow).join("")}</ul>` : ""}`;
}
window.moveHoldsHtml = moveHoldsHtml;

window.moveHoldRelease = async i => {
  const row = window.__moveHolds?.holds?.[i];
  if (!row) return;
  const nl = String.fromCharCode(10, 10);
  const effect = row.kept_running
    ? `${row.name} keeps running as it is.`
    : `${row.name} is put back as it was before the transfer, so it starts again if it was running then.`
      + (row.copy ? "" : ` If that move finished, ${row.name} already runs on the other cluster, and both would run.`);
  if (!(await ask(`Release ${row.name}?` + nl + effect + nl
      + "The transfer's backups and snapshots here are deleted. A transfer still running on the other cluster fails at its next step; cancel it there instead if it is listed.",
      { title: "Release hold", ok: "Release", danger: !row.kept_running }))) return;
  try {
    const r = await api("/api/move/holds/release", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: row.kind, namespace: row.namespace, name: row.name, transfer_id: row.transfer_id, uid: row.uid }) });
    toast(r.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  movesRepaint();
};

window.moveLeftoverTidy = async i => {
  const row = window.__moveHolds?.left?.[i];
  if (!row) return;
  if (!(await ask(`Remove what transfer ${row.transfer_id} left here?` + String.fromCharCode(10, 10)
      + `Deletes its ${[row.backups && `${row.backups} backup${row.backups === 1 ? "" : "s"}`, row.snapshots && `${row.snapshots} snapshot${row.snapshots === 1 ? "" : "s"}`].filter(Boolean).join(" and ")}`
      + `${row.name ? ` of ${row.name}` : ""}, and its folder in backup storage once nothing else is kept there. ${row.name ? `${row.name} itself is not touched.` : ""}`,
      { title: "Remove leftovers", ok: "Remove", danger: true }))) return;
  try {
    const r = await api("/api/move/holds/tidy", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transfer_id: row.transfer_id, source_uid: row.source_uid }) });
    toast(r.detail, r.complete ? "ok" : "warn");
  } catch (e) { toast(e.message, "bad"); }
  movesRepaint();
};

/* Storage for an import that has none on the source: a volume and a folder
   row to fill in, only because someone asked. */
window.imStorageAnyway = () => {
  $("#im_storage").hidden = false;
  $("#im_nothing")?.remove();
  if (!$$("#im_volumes .im-volume").length) {
    const base = $("#im_name")?.value.trim() || "app";
    $("#im_volumes").innerHTML = importVolumeRow({ name: `${base}-appdata`, size_gb: 10 }, 0);
  }
  if (!$$("#im_maps .im-map").length) imAddMap();
  imSyncVolumes();
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  importProgressCell, viewImport, SOURCE_KINDS, importRepaint, importSourceCard, importSourceSteps,
  SOURCE_LEADS, SOURCE_KEY, importMappingRows, importFolderName, importMappingRow, importVolumeRow,
  importMapPicture, importBytes, imMode, imSafetyMissing, clusterCardHtml, CLUSTER_VERSION_TAGS,
  clusterVolumesHtml, MOVE_PLAN_EDITS, invalidateMovePlan, MOVE_VOLUMES, MOVE_TRANSFER_MODE,
  MOVE_SOURCE_NAMESPACE, MOVE_DEVICES, MOVE_LIVE, MOVE_TARGET, moveBody, moveDevicesTable, MOVE_VOLUME_ACTIONS,
  moveVolumesTable, moveLoading, MOVE_PLAN, MOVE_STARTING, MOVE_PHASE_WORDS, movesHtml, movesUnreadHtml, watchMoves, IMPORT_REVIEW, HOLD_KINDS, moveHoldsHtml
};

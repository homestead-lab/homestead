/* Virtual machines: each one's real state and the actions that fit it, a
   page per VM (disks, network, guest, events), editing and deleting. */

const VM_TONE = { Running: "ok", Stopped: "low", Paused: "info", Migrating: "info", Starting: "med", Stopping: "med", Deleting: "med",
  Provisioning: "med", WaitingForVolumeBinding: "med" };
const VM_ACTIONS = {
  // Shut down asks the guest to power off, as its own power button would;
  // Force off cuts it at once, like pulling the plug. Each has an icon of
  // its own - they were all a square, and Pause with them.
  start: ["Start", "play", "Boot the VM"], stop: ["Shut down", "power", "Ask the guest to power off, as its own power button would, then stop the VM"],
  restart: ["Restart", "restart", "Restart the VM"], pause: ["Pause", "pause", "Freeze the VM where it is, keeping its memory"],
  unpause: ["Resume", "play", "Carry on from where it was paused"], "force-stop": ["Force off", "plug", "Cut the power at once, without asking the guest - like pulling the plug; unsaved work in it is lost"],
};
const vmTone = status => VM_TONE[status] || (/Error|Fail|Crash|BackOff/i.test(status) ? "crit" : "med");
/* A disk CDI is still filling: what a Provisioning VM is waiting for. */
const VM_FILL_WORDS = { ImageDownloading: "Harvester is downloading the image for", ImportInProgress: "Downloading", CloneInProgress: "Copying", ImportScheduled: "Waiting to start the download",
  CloneScheduled: "Waiting to start the copy", Pending: "Waiting for its volume", WaitForFirstConsumer: "Waiting for the VM to be placed",
  PendingPopulation: "Waiting for its volume", Failed: "Failed" };
const vmWaited = s => s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min` : s >= 60 ? `${Math.floor(s / 60)} min` : `${s} s`;
const vmFilling = v => (v.filling || []).map(f => `<div class="vm-filling ${f.stuck ? "stuck" : ""}"><span>${esc(VM_FILL_WORDS[f.phase] || f.phase)} <b class="mono">${esc(f.claim)}</b>
    ${f.seconds ? `<span class="dim xs">· ${esc(vmWaited(f.seconds))}</span>` : ""}</span>
  ${f.progress != null ? `<span class="mono">${f.progress.toFixed(1)}%</span>` : ""}
  <div class="rollout-meter"><span style="width:${Math.max(2, f.progress || 0)}%"></span></div>
  ${f.why?.length ? `<div class="note ${f.stuck ? "warn" : ""} vm-why">${f.stuck ? "<b>Stuck.</b> " : ""}${f.why.map(w => esc(w)).join("<br>")}</div>`
    : f.stuck ? '<div class="note warn vm-why"><b>Stuck</b>, and CDI says nothing about why. The events of its importer pod, on the Resources page, may.</div>' : ""}</div>`).join("");

async function viewVMs() {
  if (platformLacks("kubevirt", "Virtual machines")) return;
  const [vms] = await Promise.all([api("/api/vms"), loadUptime()]);
  STATE.data.vms = vms;
  const q = STATE.q.toLowerCase();
  const rows = vms.filter(v => !q || [v.name, v.ns, v.os, v.ip, v.node, v.description].join(" ").toLowerCase().includes(q));
  const running = vms.filter(v => v.status === "Running").length;
  const layout = viewLayout("vms");
  const items = [{ label: "Image store", icon: "store", run: "vmStore()", tip: "Cloud images from their publishers - Ubuntu, Debian, Fedora, Rocky and more - to start VMs from" },
    { label: "ISO library", icon: "disk", run: "vmIsoLibrary()", tip: "ISO images from folders on your Network Shares, for VMs' CD-ROM drives" },
    { label: "New k3s cluster", icon: "plus", run: "k3sCluster()", need: "operator", tip: "A k3s cluster made of VMs here, each with an address of its own" }];
  const create = '<button class="btn pri" data-need="operator" onclick="vmNew()">＋ New VM</button>';
  paint(`<div class="vms-page collection-page" data-collection="vms">${UI.pageHeader(`Virtual machines`, `${vms.length} VM${vms.length === 1 ? "" : "s"} · ${running} running · ${STATE.platform?.harvester === false ? `KubeVirt on ${esc(platformName(STATE.platform))}${STATE.platform.cdi ? "" : " · no CDI"}` : "KubeVirt on Harvester"}`, `${layoutSwitch("vms", "viewVMs")}
      ${moreMenu(items)}${create}`)}
    ${UI.collectionHeader(`<span class="collection-mobile-scope">All VMs</span>${vmListOptions(layout, items)}${create}`, `<span>${rows.length} VM${rows.length === 1 ? "" : "s"} · ${rows.filter(v => v.status === "Running").length} running</span>
        ${STATE.platform?.harvester === false && !STATE.platform.cdi ? '<span class="pill slim warn">No CDI</span>' : ""}`)}
    ${!rows.length ? `<div class="empty">${q ? "Nothing matches that search." : "No virtual machines yet — create one to get started."}</div>`
      : layout === "rows" ? vmTable(rows) : `<div class="vm-grid">${rows.map(vmCard).join("")}</div>`}</div>`);
}

function vmListOptions(layout, items) {
  const controls = `${layout === "rows" ? '<div class="sortbar" data-sort-controls="vms"></div>' : ""}
    <label class="list-option-setting">Layout <select aria-label="VM layout" onchange="vmListLayout(this)">
      <option value="rows"${layout === "rows" ? " selected" : ""}>Rows</option><option value="cards"${layout === "cards" ? " selected" : ""}>Cards</option></select></label>`;
  return listOptions(controls, items);
}
window.vmListLayout = select => {
  const layout = select.value;
  closeActionMenu(select.closest("details"));
  document.querySelector(".vms-page .list-options>summary")?.focus({preventScroll:true});
  setViewLayout("vms", "viewVMs", layout, {preservePaint:true});
};

/* ---------------- the image store ----------------
   Cloud images from the people who make them. On Harvester one is kept as a
   Harvester image and can keep itself current; elsewhere a VM always starts
   from the publisher's newest build. */
window.vmStore = async (check = false) => {
  if (!$("#mbody") || $("#modal").classList.contains("hidden")) modal("Image store", '<div class="empty"><span class="spin2"></span>asking the publishers</div>', true);
  let s;
  try { s = await api("/api/vm/store" + (check ? "?check=1" : "")); } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  STATE.data.vmStore = s;
  const mb = bytes => bytes ? (bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${Math.round(bytes / 1024 ** 2)} MB`) : "—";
  const day = t => t ? new Date(t * 1000).toLocaleDateString() : "";
  const state = r => {
    if (!s.harvester) return "";
    if (!r.kept) return '<span class="dim xs">not here</span>';
    const v = r.versions[r.versions.length - 1];
    if (!v) return '<span class="pill slim med">starting</span>';
    if (v.failed) return '<span class="pill slim crit">download failed</span>';
    if (!v.ready) return `<span class="pill slim med">downloading ${Math.round(v.progress || 0)}%</span>`;
    return `<span class="pill slim ok" data-tip="${esc(v.display || v.image)}">here · ${esc(day(v.added))}</span>`
      + (r.versions.length > 1 ? ` <span class="dim xs" data-tip="Older builds stay while a disk was made from them">+${r.versions.length - 1} older</span>` : "")
      + (r.update ? ' <span class="pill slim warn" data-tip="The publisher has a newer build; it downloads on the next check if this keeps itself current">newer build out</span>' : "");
  };
  const actions = r => `<div class="row nowrap" style="gap:6px;justify-content:flex-end">
      ${s.harvester && !r.kept ? `<button class="btn sm" data-need="admin" onclick="vmStoreKeep(${jsq(r.id)})" data-tip="Download it now as a Harvester image, so VMs start from a local copy">Keep</button>` : ""}
      ${s.harvester && r.kept ? `<label class="switch xs" data-tip="Check twice a day for a newer build, download it, and let go of older builds no disk came from"><input type="checkbox" ${r.auto ? "checked" : ""} data-need="admin" onchange="vmStoreAuto(${jsq(r.id)}, this.checked)"> current</label>` : ""}
      <button class="btn sm pri" data-need="operator" onclick="vmStoreNew(${jsq("store:" + r.id)})">New VM</button>
      ${s.harvester && r.kept ? `<button class="btn sm danger" data-need="admin" title="Stop keeping it; builds a disk came from stay" onclick="vmStoreForget(${jsq(r.id)})">${icon("trash")}</button>` : ""}</div>`;
  const rows = s.images.filter(r => r.available);
  const distros = [...new Set(rows.map(r => r.distro))];
  const filter = (STATE.vmStoreFilter || "").toLowerCase();
  const shown = rows.filter(r => !filter || `${r.distro} ${r.name} ${r.variant} ${r.about}`.toLowerCase().includes(filter));
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">Publisher images for <b>${esc(s.arch)}</b> hosts. ${tip("Cloud-init sets the password for the listed user and expands the image to fill the VM disk.")}</p>
    ${s.harvester ? `${UI.more("Image caching and updates", "Kept images are downloaded once and copied for each VM. Automatic updates check twice daily. Older builds are removed only when no disks depend on them.")}` : `<div class="note">${esc(s.note)}</div>`}
    ${(s.own || []).length ? `<div class="sec">Your images on this cluster</div>
      <div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr><th>Image</th><th>From</th><th>Size</th><th></th></tr></thead><tbody>
      ${s.own.map(o => `<tr><td><b>${esc(o.display)}</b><div class="dim xs mono">${esc(o.image)}</div></td>
        <td class="small" data-label="From">${esc(o.from)}${o.created ? `<div class="dim xs">${esc(day(o.created))}</div>` : ""}</td>
        <td class="mono small" data-label="Size">${o.ready ? `${o.size_gb} GB` : o.failed ? '<span class="pill slim crit">failed</span>' : `<span class="pill slim med">${Math.round(o.progress || 0)}%</span>`}</td>
        <td>${actionBar([o.ready ? { label: "New VM", run: `vmStoreNew(${jsq("image:" + o.image)})`, need: "operator", pri: true } : null,
          { label: "Image cache", run: "closeModal();go('images')", tip: "Sizes, copies and deleting are on the Image cache page" }])}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}
    <div class="between" style="margin-top:14px;gap:10px;flex-wrap:wrap"><div class="sec" style="margin:0">From their publishers</div>
      <input id="vmStoreFilter" type="search" placeholder="Filter: minimal, debian, lvm..." value="${esc(STATE.vmStoreFilter || "")}"
        oninput="STATE.vmStoreFilter=this.value; clearTimeout(window.__vsf); window.__vsf=setTimeout(() => vmStore(), 250)" style="max-width:260px"></div>
    <div class="card flat pad0" style="margin-top:8px"><div class="tblwrap"><table class="tbl stack dense vmstore"><thead><tr>
      <th>Image</th><th>Variant</th><th>Download</th><th>Sign in as</th>${s.harvester ? "<th>Here</th>" : ""}<th></th></tr></thead>
      ${distros.map(d => {
        const group = shown.filter(r => r.distro === d);
        return group.length ? `<tbody><tr class="vmstore-distro"><td colspan="${s.harvester ? 6 : 5}">${esc(d)}</td></tr>
          ${group.map(r => `<tr><td><b>${esc(r.name)}</b>${r.error ? `<div class="dim xs badtext">${esc(r.error)}</div>` : ""}</td>
            <td data-label="Variant"><span class="tag" data-tip="${esc(r.about)}">${esc(r.variant)}</span></td>
            <td class="mono small" data-label="Download">${mb(r.size)}</td>
            <td class="mono small" data-label="Sign in as">${esc(r.user)}</td>
            ${s.harvester ? `<td data-label="Here">${state(r)}</td>` : ""}<td>${actions(r)}</td></tr>`).join("")}</tbody>` : "";
      }).join("") || `<tbody><tr><td colspan="6" class="empty">Nothing matches that filter.</td></tr></tbody>`}</table></div></div>
    <div class="row between" style="margin-top:12px;flex-wrap:wrap;gap:8px">
      <span class="dim xs">Import your own disk image under <a class="linkish" onclick="closeModal();go('import')">Import</a>.</span>
      ${s.harvester ? `<button class="btn sm" data-need="admin" onclick="vmStoreRefresh()">${icon("refresh")}Check for newer builds</button>` : ""}</div>`;
  if (window.applyRole) applyRole();
  const box = $("#vmStoreFilter");
  if (box && filter) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  // Downloads under way: follow them.
  if ([...s.images.flatMap(r => r.versions), ...(s.own || [])].some(v => !v.ready && !v.failed)) {
    clearTimeout(window.__vmStoreTimer);
    window.__vmStoreTimer = setTimeout(() => { if ($("#mtitle")?.textContent === "Image store") vmStore(); }, 5000);
  }
};
const vmStorePost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
window.vmStoreKeep = async id => {
  try { toast((await vmStorePost("/api/vm/store/keep", { id, auto: true })).detail, "ok"); vmStore(); }
  catch (e) { toast(e.message, "bad"); }
};
window.vmStoreAuto = async (id, auto) => {
  try { await vmStorePost("/api/vm/store/auto", { id, auto }); toast(auto ? "keeps itself current" : "stays on the build it has", "ok"); }
  catch (e) { toast(e.message, "bad"); vmStore(); }
};
window.vmStoreForget = async id => {
  const row = (STATE.data.vmStore?.images || []).find(r => r.id === id);
  if (!(await ask(`Stop keeping ${row?.name || id}?\n\nIts builds no disk was made from are deleted; the ones a disk came from stay until that disk is gone.`))) return;
  try { toast((await vmStorePost("/api/vm/store/forget", { id })).detail, "ok"); vmStore(); }
  catch (e) { toast(e.message, "bad"); }
};
window.vmStoreRefresh = async () => {
  try {
    const r = await vmStorePost("/api/vm/store/refresh", {});
    toast(r.updated.length ? `downloading newer builds of ${r.updated.join(", ")}` : "every kept image is the newest build", "ok");
    vmStore(true);
  } catch (e) { toast(e.message, "bad"); }
};
window.vmStoreNew = value => {
  window.__vmPreset = value;
  closeModal();
  vmNew();
};

/* What a VM is, in one line: its size, its disk and where it runs. */
function vmSpecs(v) {
  const disks = v.disks.filter(d => d.kind === "disk");
  const size = disks.map(d => d.size).filter(Boolean).join(" + ");
  return [`${v.cores} core${v.cores === 1 ? "" : "s"}`, v.memory || "",
    disks.length ? (size ? `${size} disk${disks.length > 1 ? "s" : ""}` : `${disks.length} disk${disks.length > 1 ? "s" : ""}`) : "no disk"]
    .filter(Boolean);
}
/* Its addresses, whole: the first, and how many more. Stopped, it has none. */
function vmAddress(v, withNetwork = true) {
  const ips = v.ips?.length ? v.ips : v.ip ? [v.ip] : [];
  if (!ips.length) return `<span class="dim">${v.running ? "no address reported yet" : "no address while stopped"}</span>`;
  return `<span class="mono vm-ip">${esc(ips[0])}</span>
    <button class="iconbtn vm-copy" type="button" title="Copy ${esc(ips[0])}" onclick="event.stopPropagation();ipamCopy(${jsq(ips[0])})">${icon("copy")}</button>
    ${ips.length > 1 ? `<span class="tag" data-tip="${esc(ips.slice(1).join(", "))}">+${ips.length - 1}</span>` : ""}
    ${withNetwork && v.network ? `<span class="dim xs vm-net" title="${esc(v.network)}">on ${esc(v.network)}</span>` : ""}`;
}
/* What a running VM is using. CPU and memory are its launcher pod's, from
   the metrics API; disk traffic is KubeVirt's own count, as a rate over the
   last half minute. Anything not measured is left out, not guessed. */
const vmBytes = b => b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(1)} GiB` : b >= 1024 ** 2 ? `${Math.round(b / 1024 ** 2)} MiB` : `${Math.round(b / 1024)} KiB`;
const vmRate = b => b >= 1024 ** 2 ? `${(b / 1024 ** 2).toFixed(1)} MB/s` : b >= 1024 ? `${Math.round(b / 1024)} KB/s` : `${Math.round(b || 0)} B/s`;
function vmIo(u) {
  if (!u || u.read_bps == null) return `<span class="dim" data-tip="${esc(u?.io_note || "Measured from the second reading, half a minute after Homestead starts")}">—</span>`;
  return `<span class="vm-io"><span class="mono" title="Read from its disks">↓ ${vmRate(u.read_bps)}</span><span class="mono" title="Written to its disks">↑ ${vmRate(u.write_bps)}</span></span>`;
}
function vmUsage(v) {
  const u = v.usage;
  if (!v.running) return "";
  if (!u) return '<div class="dim xs vm-usage-none">Usage appears once the metrics API reports it</div>';
  const cell = (label, pct, text, metric) => `<div><div class="between"><span>${label}</span><b class="mono">${text}</b></div>
    ${pct != null ? meter(pct, "", metric) : '<div class="meter"><span style="width:0"></span></div>'}</div>`;
  return `<div class="vm-usage">
    ${cell("CPU", u.cpu_pct, u.cpu_pct != null ? `${u.cpu_pct}%` : "—", "cpu")}
    ${cell("RAM", u.mem_pct, u.mem != null ? vmBytes(u.mem) : "—", "memory")}
    <div><span>DISK</span>${vmIo(u)}</div></div>`;
}
const vmClusterTag = v => v.cluster ? `<span class="tag info" data-tip="A node of the k3s cluster ${esc(v.cluster)}, made here">${esc(v.cluster)}${v.cluster_role ? ` · ${esc(v.cluster_role)}` : ""}</span>` : "";

function vmActions(v, compact = false) {
  const main = v.actions.filter(a => ["start", "stop", "restart", "unpause"].includes(a));
  // One power action beside Console, as a container card has Logs; the
  // others are the first entries of ⋯.
  const shown = main.slice(0, 1);
  const later = [];
  return `${shown.map((a, i) => vmActionButton(v, a, i === 0 && a === "start", compact, i > 0 ? "sm-more" : "")).join("")}
      ${v.actions.includes("console") ? `<button class="btn sm ${compact ? "vm-iconbtn" : ""}" data-need="operator" title="Console" aria-label="Console" onclick="vmConsole(${jsq(v.ns)},${jsq(v.name)})">${icon("console")}${compact ? "" : "Console"}</button>` : ""}
      <details class="actionmenu"><summary class="btn sm" title="More actions">⋯</summary><div class="actionmenu-pop">
        ${later.map(a => `<button class="sm-only" data-need="operator" onclick="this.closest('details').open=false;vmPower(${jsq(v.ns)},${jsq(v.name)},${jsq(a)})">${icon(VM_ACTIONS[a][1])}${VM_ACTIONS[a][0]}</button>`).join("")}
        <button onclick="this.closest('details').open=false;vmOpen(${jsq(v.ns)},${jsq(v.name)})">${icon("list")}Details</button>
        ${main.filter(a => !shown.includes(a)).map(a => `<button data-need="operator" onclick="this.closest('details').open=false;vmPower(${jsq(v.ns)},${jsq(v.name)},${jsq(a)})">${icon(VM_ACTIONS[a][1])}${VM_ACTIONS[a][0]}</button>`).join("")}
        <button data-need="operator" onclick="this.closest('details').open=false;vmEdit(${jsq(v.ns)},${jsq(v.name)})">${icon("edit")}Edit</button>
        <button data-need="operator" onclick="this.closest('details').open=false;vmLogo(${jsq(v.ns)},${jsq(v.name)})">${icon("logo")}Logo</button>
        <button onclick="this.closest('details').open=false;vmMonitoring(${jsq(v.ns)},${jsq(v.name)})">${icon("pulse")}Monitoring</button>
        ${v.actions.includes("pause") ? `<button data-need="operator" onclick="this.closest('details').open=false;vmPower(${jsq(v.ns)},${jsq(v.name)},'pause')">${icon("pause")}Pause</button>` : ""}
        ${v.actions.includes("migrate") ? `<button data-need="operator" onclick="this.closest('details').open=false;vmMove(${jsq(v.ns)},${jsq(v.name)})">${icon("move")}Move host</button>` : ""}
        ${FLEET.view?.linked ? `<button data-need="admin" title="Move it to another linked cluster, disks and all" onclick="this.closest('details').open=false;moveToCluster('vm',${jsq(v.name)},${jsq(v.site?.handle || "")},'move',${jsq(v.ns)})">${icon("move")}Move to cluster</button>` : ""}
        ${FLEET.view?.linked ? `<button data-need="admin" title="Keep the source VM and create a stopped copy on another linked cluster" onclick="this.closest('details').open=false;moveToCluster('vm',${jsq(v.name)},${jsq(v.site?.handle || "")},'copy',${jsq(v.ns)})">${icon("copy")}Copy to cluster</button>` : ""}
        ${v.actions.includes("force-stop") ? `<button class="danger" data-need="operator" onclick="this.closest('details').open=false;vmPower(${jsq(v.ns)},${jsq(v.name)},'force-stop')" title="${esc(VM_ACTIONS["force-stop"][2])}">${icon("plug")}Force off</button>` : ""}
        <button class="danger" data-need="admin" onclick="this.closest('details').open=false;vmDelete(${jsq(v.ns)},${jsq(v.name)})">${icon("trash")}Delete</button>
      </div></details>`;
}

/* The same VMs as rows: everything a card says, one VM a line. */
/* A VM's logo: one set for it, else its OS's, else its initials. */
function vmAvatar(v, cls = "") {
  if (v.icon) return appAvatar(v.name, v.icon, cls);
  if (v.os_logo) return appAvatar(v.name, `/assets/os-${v.os_logo}.svg`, `os-logo ${cls}`);
  return `<div class="av n3 ${cls}">${esc(String(v.name || "?").slice(0, 2).toUpperCase())}</div>`;
}

function vmTable(rows) {
  return `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack compact vm-table" data-sort="vms" data-sort-controls="vms"><thead><tr>
    <th>VM</th><th>Status</th><th>Address</th><th>CPU</th><th>RAM</th><th>Disk IO</th><th data-nosort></th></tr></thead><tbody>
    ${rows.map(v => `<tr class="clickable"${clusterAttr(v)} onclick="if(!event.target.closest('button,details,a'))vmOpen(${jsq(v.ns)},${jsq(v.name)})">
      <td class="cell-name" data-sort="${esc(v.name)}"><div class="vm-name-cell">${vmAvatar(v)}<div class="vm-name-text"><b>${esc(v.name)}</b> ${clusterTag(v)}${vmClusterTag(v)}
        <div class="dim xs vm-sub">${esc([v.ns, v.os, v.node ? `on ${v.node}` : ""].filter(Boolean).join(" · "))}</div></div></div></td>
      <td data-label="Status" data-status data-sort="${esc(v.status)}"><span class="pill ${vmTone(v.status)}" data-tip="${esc([v.status, v.problem].filter(Boolean).join(": "))}">${esc(v.status)}</span>${answerTag(v, true)}
        ${v.restart_required ? '<div class="dim xs">restart to apply changes</div>' : ""}
        ${(v.filling || []).length ? `<div class="dim xs">${esc(VM_FILL_WORDS[v.filling[0].phase] || v.filling[0].phase)}${v.filling[0].progress != null ? ` · ${v.filling[0].progress.toFixed(0)}%` : ""}</div>` : ""}</td>
      <td data-label="Address" class="nowrap" data-sort="${esc((v.ips || [])[0] || "")}"><div class="vm-addr">${vmAddress(v, false)}</div>
        ${v.network ? `<div class="dim xs">${esc(v.network)}</div>` : ""}</td>
      <td data-label="CPU" class="nowrap vm-cell-use" data-sort="${v.usage?.cpu_pct ?? -1}">${v.usage?.cpu_pct != null ? `${meter(v.usage.cpu_pct, "", "cpu")}<div class="dim xs mono">${v.usage.cpu_pct}% of ${v.cores}</div>` : `<span class="dim xs">${v.cores} core${v.cores === 1 ? "" : "s"}</span>`}</td>
      <td data-label="RAM" class="nowrap vm-cell-use" data-sort="${v.usage?.mem_pct ?? -1}">${v.usage?.mem_pct != null ? `${meter(v.usage.mem_pct, "", "memory")}<div class="dim xs mono">${vmBytes(v.usage.mem)} of ${esc(v.memory)}</div>` : `<span class="dim xs">${esc(v.memory || "—")}</span>`}</td>
      <td data-label="Disk IO" data-sm-hide class="nowrap small" data-sort="${(v.usage?.read_bps || 0) + (v.usage?.write_bps || 0)}">${v.running ? vmIo(v.usage) : '<span class="dim">—</span>'}</td>
      <td class="nowrap" data-actions><div class="row vm-actions" style="justify-content:flex-end">${vmActions(v, true)}</div></td></tr>`).join("")}
    </tbody></table></div></div>`;
}
window.viewVMs = viewVMs;

function vmActionButton(v, action, primary = false, iconOnly = false, extra = "") {
  const [label, iconName, title] = action === "stop" && v.stop_retries
    ? ["Stop retries", "power", "Stop automatic boot retries and keep the VM off until you start it"]
    : VM_ACTIONS[action];
  return `<button class="btn sm ${primary ? "pri" : ""} ${iconOnly ? "vm-iconbtn" : ""} ${extra}" data-need="operator" title="${esc(iconOnly ? `${label}: ${title}` : title)}"
    aria-label="${esc(label)}" onclick="vmPower(${jsq(v.ns)},${jsq(v.name)},${jsq(action)})">${icon(iconName)}${iconOnly ? "" : label}</button>`;
}

/* A VM at a glance. Its address has a line of its own, whole - it is what
   people come here for, and three narrow columns cut it off - and its size
   reads as one line rather than five labelled boxes. */
function vmCard(v) {
  // The container card's shape: name and state, one row of facts, what it
  // is, and one action with the rest in ⋯. The rest is in its details.
  const ips = v.ips?.length ? v.ips : v.ip ? [v.ip] : [];
  const u = v.running ? v.usage : null;
  const sub = [v.ns, v.os, v.cluster ? `${v.cluster}${v.cluster_role ? ` ${v.cluster_role}` : ""}` : ""].filter(Boolean).join(" · ");
  return `<div class="card flat vm-card vm-${vmTone(v.status)}"${clusterAttr(v)}>
    <div class="between vm-head">
      <a class="vm-title" onclick="vmOpen(${jsq(v.ns)},${jsq(v.name)})">${vmAvatar(v)}
        <div class="vm-name"><b title="${esc(v.description || v.name)}">${esc(v.name)}</b><div class="dim xs" title="${esc(sub)}">${esc(sub)}</div></div></a>
      <span class="row nowrap" style="gap:6px">${clusterTag(v)}<span class="pill ${vmTone(v.status)}" ${v.problem ? `data-tip="${esc(v.problem)}"` : ""}>${esc(v.status)}</span></span></div>
    ${v.problem ? `<div class="note bad vm-problem">${esc(v.problem)}</div>` : ""}
    ${vmFilling(v)}
    ${v.restart_required ? '<div class="dim xs vm-restart">Changes are waiting for a restart</div>' : ""}
    ${answerCardRow(v, true)}
    <div class="wmeta vm-meta">
      <div><div class="dim xs">ADDRESS</div><div class="mono small">${ips[0] ? `${esc(ips[0])}<button class="iconbtn vm-copy" type="button" title="Copy ${esc(ips[0])}" onclick="event.stopPropagation();ipamCopy(${jsq(ips[0])})">${icon("copy")}</button>${ips.length > 1 ? `<span class="dim" data-tip="${esc(ips.slice(1).join(", "))}">+${ips.length - 1}</span>` : ""}` : '<span class="dim">—</span>'}</div></div>
      <div><div class="dim xs">CPU</div><div class="mono small">${u?.cpu_pct != null ? `${u.cpu_pct}%` : "—"}</div></div>
      <div><div class="dim xs">HOST</div><div class="mono small">${esc(v.node || "—")}</div></div>
      <div><div class="dim xs">RAM</div><div class="mono small">${u?.mem != null ? vmBytes(u.mem) : "—"}</div></div>
    </div>
    <div class="dim xs mono wimg">${esc(vmSpecs(v).join(" · "))}</div>
    <div class="row vm-actions">${vmActions(v)}</div></div>`;
}

window.vmStateInitHtml = (plan, prefix, changed) => {
  const state = plan.vm?.state_initialization;
  if (!state || plan.blocked) return "";
  return `<div class="reviewbox vm-state-confirm"><b>Initialize fresh VM state?</b>
    <div class="note warn">${esc(state.reason)} Cancel and inspect the original state volume and backups if this VM has run before.</div>
    <label class="check"><input type="checkbox" id="${prefix}StateAck" onchange="${changed}()"> I intend to initialize fresh state, not recover the original TPM/EFI or backup state</label>
    <div class="f"><label for="${prefix}StateName">Type ${esc(state.name)} to confirm</label><input id="${prefix}StateName" autocomplete="off" spellcheck="false" oninput="${changed}()"></div></div>`;
};
window.vmStateInitReady = (plan, prefix) => !plan.vm?.state_initialization ||
  ($(`#${prefix}StateAck`)?.checked === true && $(`#${prefix}StateName`)?.value === plan.vm.state_initialization.name);
window.vmStateInitBody = (plan, prefix) => plan.vm?.state_initialization ? {
  ack_state_initialization: $(`#${prefix}StateAck`)?.checked === true,
  confirm_state_name: $(`#${prefix}StateName`)?.value || ""
} : {};
let VM_POWER_REVIEW = null, VM_POWER_SEQUENCE = 0, VM_POWER_BUSY = false;
window.vmPower = async (ns, name, action) => {
  if (VM_POWER_BUSY && ["start", "restart", "unpause"].includes(action)) return toast("A VM power request is being sent; wait for its result", "bad");
  if (["start", "restart", "unpause"].includes(action)) return vmPowerReview({ ns, name, action });
  if (action === "force-stop" && !(await ask(`Force off ${name}? The power is cut at once - the guest is not asked to shut down, so unsaved work in it is lost. Shut down asks it first.`))) return;
  try {
    const r = await api("/api/vm/power", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, action }) });
    toast(r.detail, "ok"); setTimeout(() => refresh(true), 1200);
  } catch (e) { toast(e.message, "bad"); }
};

window.vmPowerReview = async config => {
  if (VM_POWER_BUSY) return;
  VM_POWER_REVIEW = null;
  const sequence = ++VM_POWER_SEQUENCE, frozen = { ...config };
  const open = window.childModal && !$("#modal").classList.contains("hidden") ? childModal : modal;
  const title = `${VM_ACTIONS[frozen.action]?.[0] || "Power"} · ${frozen.name}`;
  open(title, '<div id="vmPowerLoading" class="empty"><span class="spin2"></span>Checking VM, storage and host capacity…</div>', true);
  try {
    const review = await api("/api/vm/power/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(frozen)});
    if (sequence !== VM_POWER_SEQUENCE || !$("#vmPowerLoading")) return;
    if (!review.capacity || typeof review.capacity.blocked !== "boolean" || !review.capacity_token)
      throw new Error("VM capacity review unavailable. Nothing was sent; refresh before continuing.");
    VM_POWER_REVIEW = { ...review, config:frozen };
    const plan = review.capacity, facts = plan.vm || {};
    const policy = facts.policy_after && facts.policy_after !== facts.policy_before
      ? ` Its restart policy changes from ${esc(facts.policy_before || "unknown")} to <b>${esc(facts.policy_after)}</b>.` : "";
    const what = frozen.action === "restart" ? "Restarts" : frozen.action === "unpause" ? "Resumes" : "Starts";
    const downtime = frozen.action === "restart" ? " The guest stops and starts again." : "";
    const guest = facts.guest_memory_gb != null ? ` <span class="ui-help">${esc(facts.guest_memory_gb)} GiB guest memory.</span>` : "";
    $("#mbody").innerHTML = startReview(plan, { what, name: frozen.name, extra: `${downtime}${policy}${guest}`,
        ackId: "vmPowerApprove", onAck: "vmPowerReviewReady()",
        details: facts.request_is_lower_bound ? `<p class="ui-help">${facts.cpu_request_is_estimate
          ? "CPU uses a conservative IO-thread allowance because this renderer version is unverified. RAM requests are lower bounds; the RAM estimate is not a configured memory limit."
          : "Scheduler requests are lower bounds; the launcher can need additional overhead. The RAM estimate is not a configured memory limit."}</p>` : "" })
      + vmStateInitHtml(plan, "vmPower", "vmPowerReviewReady")
      + UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>${plan.blocked ? ""
        : `<button class="btn pri" id="vmPowerApply" onclick="vmPowerReviewedApply()">${esc(VM_ACTIONS[frozen.action]?.[0] || "Apply")}</button>`}`);
    vmPowerReviewReady();
  } catch (error) {
    if (sequence !== VM_POWER_SEQUENCE || !$("#vmPowerLoading")) return;
    VM_POWER_REVIEW = null;
    $("#mbody").innerHTML = `<div class="note bad">${esc(error.message)}</div>${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Close</button>`)}`;
  }
};
window.vmPowerReviewReady = () => {
  // The tickbox is there only when there is a warning to accept.
  const ready = !!(VM_POWER_REVIEW && !VM_POWER_BUSY && !VM_POWER_REVIEW.capacity.blocked
    && (!$("#vmPowerApprove") || $("#vmPowerApprove").checked) && vmStateInitReady(VM_POWER_REVIEW.capacity, "vmPower"));
  if ($("#vmPowerApply")) $("#vmPowerApply").disabled = !ready;
  return ready;
};
window.vmPowerReviewedApply = async () => {
  if (!vmPowerReviewReady()) return toast("Tick Start it anyway to accept the warning", "bad");
  const review = VM_POWER_REVIEW, button = $("#vmPowerApply");
  VM_POWER_REVIEW = null; // one shot, including errors and lost responses
  VM_POWER_BUSY = true;
  button.disabled = true;
  button.textContent = "Sending…";
  try {
    const result = await api("/api/vm/power", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...review.config, ...vmStateInitBody(review.capacity, "vmPower"), capacity_token:review.capacity_token, confirm_capacity:true})});
    if (result.operation && window.noteOperation) noteOperation(result.operation);
    toast(result.detail, "ok");
    modalBack(); setTimeout(() => refresh(true), 1200);
  } catch (error) {
    // No retry button: the server may have accepted power before contact was
    // lost. Inspect the current VM state before initiating a fresh review.
    if ($("#vmPowerApply") === button) {
      button.textContent = "Inspect VM before retrying";
      $("#mbody").insertAdjacentHTML("afterbegin", `<div class="note bad">${esc(error.message)}. The request was not repeated. Its outcome may be uncertain: inspect the VM and its Recent jobs entry before trying again. Stop and Force stop remain available.</div>`);
      if ($(".modalbox")) $(".modalbox").scrollTop = 0;
    }
    toast(error.message, "bad");
  } finally { VM_POWER_BUSY = false; if (window.startOperationChecks) startOperationChecks(); }
};

window.vmOpen = async (ns, name) => {
  modal(`VM · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  let v;
  try { v = await api(`/api/vm?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`); }
  catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const tab = (id, label) => `<button class="${id === "overview" ? "on" : ""}" onclick="vmTab(this,${jsq(id)})">${label}</button>`;
  $("#mbody").innerHTML = `<div class="between"><div><span class="pill ${vmTone(v.status)}">${esc(v.status)}</span>
      <span class="dim xs"> run strategy ${esc(v.run_strategy)}${v.node ? ` · on ${esc(v.node)}` : ""}</span></div>
      <div class="row">${v.actions.filter(a => VM_ACTIONS[a]).map(a => vmActionButton(v, a, a === "start")).join("")}
        <button class="btn sm" data-need="operator" onclick="vmEdit(${jsq(ns)},${jsq(name)})">${icon("edit")}Edit</button></div></div>
    ${v.problem ? `<div class="note bad" style="margin-top:10px">${esc(v.problem)}</div>` : ""}
    ${vmFilling(v)}
    <div class="seg" style="margin:12px 0">${tab("overview", "Overview")}${tab("disks", `Disks · ${v.disks.length}`)}${tab("network", `Network · ${v.isolated ? "isolated" : v.implicit_network ? "automatic" : v.nics.length}`)}${tab("events", "Events")}</div>
    <div class="vm-pane" data-pane="overview">
      <div class="vm-facts wide">
        <div><span>CPU</span><b>${v.cores == null ? "Unavailable" : `${esc(v.cores)} cores`}</b></div><div><span>Memory</span><b>${esc(v.memory || "—")}</b></div>
        <div><span>Guest OS</span><b>${esc(v.guest?.prettyName || v.os || "unknown")}</b></div>
        <div><span>Kernel</span><b class="mono xs">${esc(v.guest?.kernelRelease || "—")}</b></div>
        <div><span>Live migration</span><b>${v.migratable ? "possible" : "not possible"}</b></div>
        <div><span>Created</span><b>${esc((v.created || "").slice(0, 10))}</b></div></div>
      ${v.profile_error ? `<div class="note warn">${esc(v.profile_error)}</div>` : ""}
      ${v.description ? `<p class="small" style="margin-top:10px">${esc(v.description)}</p>` : ""}
      ${v.guest?.prettyName ? "" : '<p class="dim xs" style="margin-top:8px">The guest reports its OS and addresses through the QEMU guest agent, when it runs one.</p>'}
      <table class="tbl dense stack" style="margin-top:10px"><thead><tr><th>Condition</th><th>Status</th><th>Detail</th></tr></thead><tbody>
        ${v.conditions.map(c => `<tr><td>${esc(c.type)}</td><td><span class="pill slim ${c.status === "True" ? "ok" : "neutral"}">${esc(c.status)}</span></td><td class="small">${esc(c.message || c.reason)}</td></tr>`).join("")}</tbody></table></div>
    <div class="vm-pane" data-pane="disks" hidden><table class="tbl dense stack"><thead><tr><th>Disk</th><th>Kind</th><th>Volume</th><th>Size</th><th>Boot</th></tr></thead><tbody>
      ${v.disks.map(d => `<tr><td><b>${esc(d.name)}</b><div class="dim xs">${esc(d.bus)}</div></td><td>${esc(d.kind)}</td>
        <td class="mono small">${esc(d.claim || "—")}${d.storage_class ? `<div class="dim xs">${esc(d.storage_class)}</div>` : ""}</td>
        <td class="mono">${esc(d.size || "—")}</td><td>${d.boot ? `#${d.boot}` : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="vm-pane" data-pane="network" hidden><div class="note small">Pod-network VMs can use a default or custom Service VIP with port mappings. Bridged interfaces use DHCP or a static guest address.
      <button class="btn sm" data-need="operator" onclick="networkManage(${jsq(v.namespace)},${jsq(v.name)},'VirtualMachine')">Configure VIP / ports</button></div>
      <table class="tbl dense stack"><thead><tr><th>Interface</th><th>Network</th><th>MAC</th><th>Addresses</th></tr></thead><tbody>
      ${v.nics.map(n => `<tr><td><b>${esc(n.name)}</b><div class="dim xs">${esc(n.model)}</div></td><td>${esc(n.network || "—")}</td>
        <td class="mono xs">${esc(n.mac || "—")}</td><td class="mono small">${esc(n.ips.join(", ") || "—")}</td></tr>`).join("")}</tbody></table></div>
    <div class="vm-pane" data-pane="events" hidden>${v.events.length ? `<table class="tbl dense stack"><thead><tr><th>Type</th><th>Reason</th><th>Message</th><th>Last</th></tr></thead><tbody>
      ${v.events.map(e => `<tr><td><span class="pill slim ${e.type === "Warning" ? "med" : "neutral"}">${esc(e.type)}</span></td><td class="small">${esc(e.reason)}</td>
        <td class="small">${esc(e.message)}</td><td class="small dim">${esc((e.last || "").replace("T", " ").slice(0, 16))}</td></tr>`).join("")}</tbody></table>` : '<div class="dim small">No recent events.</div>'}</div>`;
  if (window.applyRole) applyRole();
};
window.vmTab = (button, pane) => {
  $$("#mbody .seg button").forEach(b => b.classList.toggle("on", b === button));
  $$("#mbody .vm-pane").forEach(p => { p.hidden = p.dataset.pane !== pane; });
};

/* ---- editing: everything the VM is made of ---- */
const VM_BUSES = ["virtio", "sata", "scsi"], VM_MODELS = ["virtio", "e1000", "e1000e", "rtl8139"];
const vmNet = n => n.network === "pod network" ? "pod" : n.network;
const vmOpt = (value, label, chosen) => `<option value="${esc(value)}" ${value === chosen ? "selected" : ""}>${esc(label)}</option>`;
/* Networks a VM can join: not a macvlan one, which carries containers only. */
const vmJoinable = o => (o.networks || ["pod"]).filter(x => (o.network_details || []).find(d => d.name === x)?.vms !== false);
const vmNetLabel = (o, x) => x === "pod" ? "pod network (NAT)"
  : (o.network_details || []).find(d => d.name === x)?.vms === false ? `${x} (macvlan - containers only)` : x;

/* Where a disk's contents come from: blank, a download, or a Harvester image. */
function vmSourceSelect(cls, o, current = "", cdrom = false) {
  const isos = cdrom ? (o.isos || []) : [];
  return `<select class="${cls}" onchange="vmSourceChanged(this)">
    ${isos.length ? `<optgroup label="ISO library">${isos.map(i => vmOpt(`iso:${i.name}`, i.file, current)).join("")}</optgroup>` : ""}
    ${cdrom ? "" : vmOpt("blank", "blank disk", current)}
    ${o.cdi || o.harvester ? vmOpt("url", o.harvester ? "download from a URL (as a Harvester image)" : "download from a URL", current) : ""}
    ${(o.images || []).filter(i => i.storage_class).map(i => vmOpt(`image:${i.namespace}/${i.name}`, `Harvester image · ${i.display}`, current)).join("")}</select>`;
}
window.vmSourceChanged = select => {
  const box = select.closest("[data-disk],.vd-add");
  const url = box?.querySelector(".vd_url,.va_url");
  if (url) url.hidden = select.value !== "url";
  // An ISO from the library is its own shared volume: no size or class to give.
  box?.querySelectorAll(".va-sized").forEach(el => { el.hidden = select.value.startsWith("iso:"); });
};

function vmDiskRow(d, o) {
  const cdrom = d.kind === "cd-rom", unmade = !d.made && d.template;
  const current = d.source?.url !== undefined ? "url" : d.source?.image ? `image:${d.source.image}` : "blank";
  const buses = cdrom ? VM_BUSES.filter(b => b !== "virtio") : VM_BUSES;
  return `<tr data-disk="${esc(d.name)}" data-size="${esc(d.size || d.template_size || "")}" data-source="${esc(current)}" data-url="${esc(d.source?.url || "")}">
    <td><b>${esc(d.name)}</b><div class="dim xs">${esc(d.kind)}${d.claim ? ` · <span class="mono">${esc(d.claim)}</span>` : ""}</div>
      ${d.storage_class || d.template_class ? `<div class="dim xs">${esc(d.storage_class || d.template_class)}</div>` : ""}
      ${unmade ? `<span class="pill slim crit" data-tip="This disk has not been made${d.phase ? ` (${esc(d.phase)})` : ""}, so its source can still be changed">not made</span>` : ""}</td>
    <td><input class="vd_boot mono" type="number" min="1" max="64" value="${d.boot || ""}" placeholder="—" style="width:64px"></td>
    <td><select class="vd_bus">${buses.map(b => vmOpt(b, b, d.bus)).join("")}</select></td>
    <td>${d.claim ? `<input class="vd_size mono" value="${esc(d.size || d.template_size || "")}" style="width:84px" data-tip="${unmade ? "Its size when it is made" : "Grow the disk; it cannot shrink"}">` : "—"}</td>
    <td>${unmade ? `${vmSourceSelect("vd_src", o, current)}<input class="vd_url mono" type="url" placeholder="https://…/image.img" value="${esc(d.source?.url || "")}" ${current === "url" ? "" : "hidden"} style="margin-top:6px">`
      : `<span class="dim xs">${d.source?.image ? esc(d.source.image) : d.source?.url ? "downloaded" : d.claim ? "made" : "—"}</span>`}</td>
    <td><label class="switch" data-tip="Detach it from the VM; the volume is kept"><input type="checkbox" class="vd_rm"> detach</label></td></tr>`;
}

function vmNicRow(n, o) {
  const nets = [...new Set([...vmJoinable(o), vmNet(n)].filter(Boolean))];
  return `<tr data-nic="${esc(n.name)}" data-model="${esc(n.model)}" data-net="${esc(vmNet(n))}" data-mac="${esc(n.mac || "")}">
    <td><b>${esc(n.name)}</b><div class="dim xs mono">${esc(n.ips.join(", "))}</div></td>
    <td><select class="vn_model">${VM_MODELS.map(m => vmOpt(m, m, n.model)).join("")}</select></td>
    <td><select class="vn_net">${nets.map(x => vmOpt(x, vmNetLabel(o, x), vmNet(n))).join("")}</select></td>
    <td><input class="vn_mac mono" value="${esc(n.mac || "")}" placeholder="automatic" style="width:150px"></td>
    <td><label class="switch"><input type="checkbox" class="vn_rm"> remove</label></td></tr>`;
}

function vmEditResourceFields(v) {
  const profile = v.resource_profile || {}, locked = !!profile.name || !!v.profile_error;
  const explanation = v.profile_error || (profile.name
    ? `CPU and memory are managed by instance type ${profile.name}. This form keeps that profile; it does not replace it with manual values.`
    : v.preference_profile?.name ? `Defaults come from preference ${v.preference_profile.name}. Unchanged CPU and memory are left as they are.` : "");
  return `${explanation ? `<div class="note ${v.profile_error ? "warn" : "small"}">${esc(explanation)}</div>` : ""}
    <div class="f2"><div class="f"><label>CPU cores</label>${locked
      ? `<div class="mono">${esc(v.cores ?? "Unavailable")}</div>`
      : `<input id="ve_cores" type="number" min="1" max="128" value="${esc(v.cores)}">`}</div>
      <div class="f"><label>Memory</label>${locked
        ? `<div class="mono">${esc(v.memory || "Unavailable")}</div>`
        : `<input id="ve_mem" class="mono" value="${esc(v.memory)}" placeholder="4Gi">`}</div></div>`;
}

window.vmEdit = async (ns, name) => {
  modal(`Edit · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true, "vm-config");
  let v, o, res;
  try {
    [v, o, res] = await Promise.all([api(`/api/vm?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`),
      api("/api/vm/create-options").catch(() => ({ cdi: true, images: [], storage_classes: [], networks: ["pod"], nodes: [] })),
      api("/api/passthrough/resources").catch(error => ({ resources: [], error: error.message }))]);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  window.__vmEdit = { ns, name, v, o };
  const disks = v.disks.filter(d => d.kind === "disk" || d.kind === "cd-rom");
  const ci = v.cloud_init || {};
  $("#mbody").innerHTML = UI.sectionForm("ve", [
    {key:"general", title:"General", html:`
      ${vmEditResourceFields(v)}
      <div class="f2"><div class="f"><label>Run strategy ${tip("RerunOnFailure (Harvester's default): runs, and starts again if the guest crashes, but not after you stop it. Always: kept running whatever happens. Manual: runs only when started, never restarted. Halted: kept off.")}</label>
        <select id="ve_strategy">${["RerunOnFailure", "Always", "Manual", "Halted"].map(x => vmOpt(x, x, v.run_strategy)).join("")}</select></div>
        <div class="f"><label>Host ${tip("Keep the VM on one host, or let Kubernetes choose. A VM on a disk only one host can reach stays there anyway.")}</label>
        <select id="ve_node">${vmOpt("", "any host", v.node_selector || "")}${(o.nodes || []).map(n => vmOpt(n, n, v.node_selector || "")).join("")}</select></div></div>
      <div class="f"><label>Description</label><input id="ve_desc" value="${esc(v.description || "")}" maxlength="300"></div>
      <div class="f"><label>Logo</label><div class="row vm-edit-logo">${vmAvatar((STATE.data.vms || []).find(x => x.ns === ns && x.name === name) || v)}
        <button class="btn sm" type="button" data-need="operator" onclick="vmLogo(${jsq(ns)},${jsq(name)},() => vmEdit(${jsq(ns)},${jsq(name)}))">Change…</button>
        <span class="dim xs">Saved on its own, at once; the VM keeps running. Other changes here are not saved by it.</span></div></div>
      <div class="f"><label>Monitoring</label><div class="row vm-edit-logo">${(() => { const a = answerOf({ ns, name }, true); return a ? `<span class="answer-state"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span><span class="dim xs">${esc(answerDetail(a))}</span>` : '<span class="dim xs">Not checked yet</span>'; })()}
        <button class="btn sm" type="button" data-need="operator" onclick="vmMonitoring(${jsq(ns)},${jsq(name)},() => vmEdit(${jsq(ns)},${jsq(name)}))">Change…</button></div></div>
      ${UI.more("Advanced editing", UI.button("Edit YAML", `vmYaml(${jsq(ns)},${jsq(name)})`, { attrs: 'data-need="admin"' }))}`},
    v.hardware && {key:"hardware", title:"Hardware", html:vmHardwareFields(v.hardware, o, !!v.resource_profile?.name)},
    {key:"disks", title:`Disks · ${disks.length}`, html:`
      <div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Disk</th><th>Boot</th><th>Bus</th><th>Size</th><th>Source</th><th></th></tr></thead>
        <tbody>${disks.map(d => vmDiskRow(d, o)).join("")}</tbody></table></div>
      <div id="ve_adds"></div>
      <div class="row" style="margin-top:10px"><button class="btn sm" onclick="vmAddDisk('disk')">＋ Disk</button><button class="btn sm" onclick="vmAddDisk('cd-rom')">＋ CD-ROM</button></div>
      <div class="dim xs" style="margin-top:8px">Boot order: the lowest number boots first. Detached disks are kept as volumes.</div>`},
    {key:"network", title:`Network · ${v.isolated ? "isolated" : v.implicit_network ? "automatic" : v.nics.length}`, html:`
      <label class="check"><input id="ve_isolated" type="checkbox" ${v.isolated ? "checked" : ""} onchange="vmIsolationChanged()"> Isolated VM</label>
      <p class="dim small">Removes virtual network cards at the next start. Clear this option to add a card. Passthrough may still provide a physical network device.</p>
      ${v.implicit_network ? '<div class="note warn">No network card is saved, but KubeVirt currently adds its default pod-network card at boot. Select Isolated VM to disable it, or add an explicit interface.</div>' : ""}
      <div id="ve_network_controls">
      <div class="note small"><b>Service VIP (default or selected address)</b><p>Uses a masquerade pod-network interface and forwards the ports you select. It is not the guest's own IP or MAC. Configure this separately from NIC changes; save those first if you are adding a pod interface.</p>
        <button class="btn" data-need="operator" onclick="networkManage(${jsq(ns)},${jsq(name)},'VirtualMachine')">Configure default / selected VIP</button></div>
      <div class="tblwrap"><table class="tbl dense stack ve-table"><thead><tr><th>Interface</th><th>Model</th><th>Network</th><th>MAC</th><th></th></tr></thead>
        <tbody id="ve_nics">${v.nics.map(n => vmNicRow(n, o)).join("")}</tbody></table></div>
      <div class="row" style="margin-top:10px"><button class="btn sm" onclick="vmAddNic()">＋ Interface</button></div>
      ${UI.more("Direct LAN addressing", "<p>A bridge/VLAN interface gets its IP from LAN DHCP or the guest OS. Reserve its MAC in DHCP for a stable address. Changing the MAC does not set an IP. Cloud-init may not rerun on an existing VM.</p>")}</div>`},
    {key:"devices", title:`Passthrough · ${(v.host_devices || []).length}`, html:window.vmDevicesPane ? window.vmDevicesPane(v, res) : ""},
    {key:"cloud", title:"Cloud-init", html:`
      ${v.sensitive_hidden ? `<div class="note">An administrator can view and edit cloud-init. It is preserved when you save other changes.</div>` : ci.source === "unreadable" ? `<div class="note bad">This VM's cloud-init is in a secret Homestead cannot read, so it is left as it is.</div>` : `
      ${ci.source === "secret" ? '<div class="dim xs" style="margin-bottom:8px">Kept in the VM\'s own secret, as Harvester does.</div>' : ""}
      <div class="f"><label>User data</label><textarea id="ve_user" class="mono helm-values" spellcheck="false" placeholder="#cloud-config">${esc(ci.user_data || "")}</textarea></div>
      <div class="f"><label>Network data</label><textarea id="ve_netdata" class="mono helm-values" spellcheck="false" style="min-height:90px" placeholder="optional">${esc(ci.network_data || "")}</textarea></div>
      <div class="dim xs">Cloud-init runs when the guest first boots; most images read it only once.</div>`}`}
  ], UI.button("Review changes", "vmEditSave()", {kind:"pri"}), {always:true,
    noticeHtml:v.status === "Running" ? `<label class="switch"><input type="checkbox" id="ve_restart"> Review a restart after saving</label>
      <div class="dim xs">Some changes can apply live through KubeVirt; a restart is a separate reviewed action.</div>` : ""});
  if (v.hardware) vmHardwareChanged();
  if (window.applyRole) applyRole();
  vmIsolationChanged();
};
window.vmIsolationChanged = () => {
  const isolated = !!$("#ve_isolated")?.checked;
  $$("#ve_network_controls input, #ve_network_controls select, #ve_network_controls button").forEach(el => {
    if (isolated && !el.disabled) { el.dataset.isolationDisabled = "true"; el.disabled = true; }
    else if (!isolated && el.dataset.isolationDisabled) { el.disabled = false; delete el.dataset.isolationDisabled; }
  });
};
window.vmEditTab = (button, pane) => UI.selectSection("ve", pane);
window.vmYaml = (ns, name) => {
  RES.pick = { group: "kubevirt.io", version: "v1", resource: "virtualmachines", kind: "VirtualMachine", namespaced: true };
  resOpen(ns, name);
};
window.vmAddDisk = kind => {
  const { o } = window.__vmEdit, cdrom = kind === "cd-rom";
  const classes = o.storage_classes || [];
  $("#ve_adds").insertAdjacentHTML("beforeend", `<div class="vd-add card flat" data-kind="${kind}">
    <div class="between"><b>New ${cdrom ? "CD-ROM" : "disk"}</b><button class="btn sm" onclick="this.closest('.vd-add').remove()">✕</button></div>
    <div class="f2"><div class="f"><label>Contents</label>${vmSourceSelect("va_src", o, cdrom ? ((o.isos || [])[0] ? `iso:${o.isos[0].name}` : (o.images || [])[0] ? `image:${o.images[0].namespace}/${o.images[0].name}` : "url") : "blank", cdrom)}
        <input class="va_url mono" type="url" placeholder="https://…/image.iso" ${cdrom && !(o.images || []).length && !(o.isos || []).length ? "" : "hidden"} style="margin-top:6px">
        ${cdrom && !(o.isos || []).length ? '<div class="dim xs" style="margin-top:6px">ISOs from your shares appear here once made ready in the <a class="linkish" onclick="vmIsoLibrary()">ISO library</a>.</div>' : ""}</div>
      <div class="f va-sized" ${cdrom && (o.isos || []).length ? "hidden" : ""}><label>Size</label><input class="va_size mono" value="${cdrom ? "10Gi" : "20Gi"}"></div></div>
    <div class="f2">${classes.length ? `<div class="f va-sized" ${cdrom && (o.isos || []).length ? "hidden" : ""}><label>Storage class</label><select class="va_class">${classes.map(c => vmOpt(c, c, o.default_class)).join("")}</select></div>` : ""}
      <div class="f"><label>Bus · boot order</label><div class="row" style="flex-wrap:nowrap"><select class="va_bus">${(cdrom ? ["sata", "scsi"] : VM_BUSES).map(b => vmOpt(b, b, cdrom ? "sata" : "virtio")).join("")}</select>
        <input class="va_boot mono" type="number" min="1" max="64" placeholder="boot #" style="width:80px"></div></div></div></div>`);
};
window.vmAddNic = () => {
  if ($("#ve_isolated")?.checked) return toast("Clear Isolated VM before adding a network card", "bad");
  const { o } = window.__vmEdit;
  $("#ve_nics").insertAdjacentHTML("beforeend", `<tr class="vn-add"><td><b>new</b></td>
    <td><select class="vn_model">${VM_MODELS.map(m => vmOpt(m, m, "virtio")).join("")}</select></td>
    <td><select class="vn_net">${vmJoinable(o).map(x => vmOpt(x, vmNetLabel(o, x), vmJoinable(o)[1] || "pod")).join("")}</select></td>
    <td class="dim xs">automatic</td><td><button class="btn sm" data-form-row aria-label="Remove this row" onclick="this.closest('tr').remove()">✕</button></td></tr>`);
};
window.vmEditSave = async () => {
  const { ns, name, v } = window.__vmEdit;
  const badUrl = $$("#mbody tr[data-disk], #mbody .vd-add").some(box => box.querySelector(".vd_src,.va_src")?.value === "url"
    && !/^https?:\/\/[^/\s]+/i.test(box.querySelector(".vd_url,.va_url").value.trim()));
  if (badUrl) return toast("a disk's URL must start with http:// or https://", "bad");
  const sourceOf = (select, url) => select.value === "url" ? { url: url.value.trim() }
    : select.value.startsWith("image:") ? { image: select.value.slice(6) }
    : select.value.startsWith("iso:") ? { iso: select.value.slice(4) } : {};
  const disks = $$("#mbody tr[data-disk]").map(row => {
    const edit = { name: row.dataset.disk, boot: row.querySelector(".vd_boot").value, bus: row.querySelector(".vd_bus").value,
      remove: row.querySelector(".vd_rm").checked };
    const size = row.querySelector(".vd_size");
    if (size && size.value.trim() && size.value.trim() !== row.dataset.size) edit.size = size.value.trim();
    const src = row.querySelector(".vd_src"), url = row.querySelector(".vd_url");
    if (src && (src.value !== row.dataset.source || (src.value === "url" && url.value.trim() !== row.dataset.url))) {
      edit.source = sourceOf(src, url);
    }
    return edit;
  });
  const add_disks = $$("#mbody .vd-add").map(box => ({ kind: box.dataset.kind, size: box.querySelector(".va_size").value.trim(),
    storage_class: box.querySelector(".va_class")?.value || "", bus: box.querySelector(".va_bus").value,
    boot: box.querySelector(".va_boot").value || "", ...sourceOf(box.querySelector(".va_src"), box.querySelector(".va_url")) }));
  const isolated = !!$("#ve_isolated")?.checked;
  const nics = isolated ? [] : $$("#mbody tr[data-nic]").map(row => ({ name: row.dataset.nic, model: row.querySelector(".vn_model").value,
    network: row.querySelector(".vn_net").value, mac: row.querySelector(".vn_mac").value.trim(), remove: row.querySelector(".vn_rm").checked }));
  const add_nics = isolated ? [] : $$("#mbody tr.vn-add").map(row => ({ model: row.querySelector(".vn_model").value, network: row.querySelector(".vn_net").value }));
  const body = { ns, name, run_strategy: $("#ve_strategy").value,
    description: $("#ve_desc").value, node: $("#ve_node").value, restart: false,
    disks, add_disks, nics, add_nics };
  if ($("#ve_isolated")) body.isolated = isolated;
  // Profile-controlled fields have no inputs. Unchanged preference defaults
  // must not become explicit overrides just because the form was opened.
  if ($("#ve_cores") && +$("#ve_cores").value !== v.cores) body.cores = +$("#ve_cores").value;
  if ($("#ve_mem") && $("#ve_mem").value.trim() !== v.memory) body.memory = $("#ve_mem").value.trim();
  if ($("#ve_user")) body.cloud_init = { user_data: $("#ve_user").value, network_data: $("#ve_netdata").value };
  try {
    const devices = window.vmDevicesChanges ? await window.vmDevicesChanges() : null;
    if (devices) body.host_devices = devices;
  } catch (e) { return toast(`the ROM file could not be read: ${e.message}`, "bad"); }
  const hardware = v.hardware ? vmHardwareChanges(v.hardware) : null;
  if (hardware) {
    body.hardware = hardware;
    // The topology sets the count: the General tab's figure is not sent too.
    if (hardware.cpu && ["sockets", "cores", "threads"].some(k => k in hardware.cpu)) delete body.cores;
  }
  return vmEditReview(body, !!$("#ve_restart")?.checked);
};

let VM_EDIT_REVIEW = null, VM_EDIT_SEQUENCE = 0, VM_EDIT_BUSY = false;
window.vmEditReview = async (config, restartAfter = false) => {
  if (VM_EDIT_BUSY) return;
  VM_EDIT_REVIEW = null;
  const sequence = ++VM_EDIT_SEQUENCE, frozen = JSON.parse(JSON.stringify(config));
  childModal(`Review changes · ${frozen.name}`, '<div id="vmEditLoading" class="empty"><span class="spin2"></span>Checking proposed VM, disks and capacity…</div>', true);
  try {
    const review = await api("/api/vm/edit/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(frozen)});
    if (sequence !== VM_EDIT_SEQUENCE || !$("#vmEditLoading")) return;
    if (!review.capacity || typeof review.capacity.blocked !== "boolean" || !review.capacity_token)
      throw new Error("VM edit review unavailable. Nothing was saved; go back and review again.");
    VM_EDIT_REVIEW = {...review, config:frozen, restartAfter};
    const plan = review.capacity, facts = plan.vm || {};
    $("#mbody").innerHTML = `<div class="update-review">
      <div class="reviewbox"><b>Save ${esc(frozen.name)}</b><p class="small">${esc(frozen.cores ?? "Unchanged")} CPU cores · ${esc(frozen.memory || "unchanged memory")}</p>
        ${frozen.isolated ? '<p class="small"><b>Isolated VM</b> - removes all virtual network cards and disables automatic attachment at next start.</p>' : frozen.isolated === false ? '<p class="small">Virtual network cards are allowed. No automatic card is added when the last interface is removed.</p>' : ""}
        <p class="small">Restart policy: ${esc(facts.policy_before || "unknown")} → ${esc(facts.policy_after || "unknown")}</p>
        <p class="small muted">${facts.admission_needed ? "Resource and policy changes may take effect immediately through KubeVirt. Host RAM estimates include launcher overhead but are not a configured memory limit." : "No new launcher capacity is needed for this metadata or stop/manual-policy edit."}</p></div>
      ${plan.blockers?.length ? `<div class="note bad">${plan.blockers.map(esc).join(" · ")}</div>` : ""}
      ${facts.admission_needed ? deployCapacityHtml(plan) : `<div class="note warn">${(plan.warnings || []).map(esc).join(" ")}</div>`}
      ${vmStateInitHtml(plan, "vmEdit", "vmEditReviewReady")}
      ${review.volumes?.length ? `<div class="reviewbox"><b>New disks</b>${review.volumes.map(v => `<p class="small"><span class="mono">${esc(v.name)}</span> · ${esc(v.size)} · ${esc(v.storage_class)} · ${esc(v.access_mode)}</p>`).join("")}</div>` : ""}
      <p class="small muted">${restartAfter ? "After saving, a separate restart review checks the saved VM and current host capacity. Saving does not automatically send Restart." : "Save sends no Restart request. If needed, restart the VM through its power controls afterward."}</p>
      ${!plan.blocked ? '<label class="check"><input type="checkbox" id="vmEditApprove" onchange="vmEditReviewReady()"> Save these exact changes and accept the displayed memory, policy and partial-save risks</label>' : ""}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="vmEditReviewBack()">Back to edit</button><button class="btn pri" id="vmEditApply" disabled onclick="vmEditReviewedApply()">Save reviewed changes</button>`)}</div>`;
  } catch (error) {
    if (sequence !== VM_EDIT_SEQUENCE || !$("#vmEditLoading")) return;
    VM_EDIT_REVIEW = null;
    $("#mbody").innerHTML = `<div class="note bad">${esc(error.message)}</div><button class="btn" onclick="vmEditReviewBack()">Back to edit</button>`;
  }
};
window.vmEditReviewBack = () => { VM_EDIT_REVIEW = null; ++VM_EDIT_SEQUENCE; modalBack(); };
window.vmEditReviewReady = () => {
  const ready = !!(VM_EDIT_REVIEW && !VM_EDIT_BUSY && !VM_EDIT_REVIEW.capacity.blocked && $("#vmEditApprove")?.checked && vmStateInitReady(VM_EDIT_REVIEW.capacity, "vmEdit"));
  if ($("#vmEditApply")) $("#vmEditApply").disabled = !ready;
  return ready;
};
window.vmEditReviewedApply = async () => {
  if (!vmEditReviewReady()) return toast("Review and acknowledge these VM changes first", "bad");
  const review = VM_EDIT_REVIEW, button = $("#vmEditApply");
  VM_EDIT_REVIEW = null;
  VM_EDIT_BUSY = true;
  button.disabled = true; button.textContent = "Saving reviewed changes…";
  let saved = false;
  try {
    const result = await api("/api/vm/edit", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...review.config, ...vmStateInitBody(review.capacity, "vmEdit"), capacity_token:review.capacity_token, confirm_capacity:true})});
    if (result.operation && window.noteOperation) noteOperation(result.operation);
    saved = true;
    toast(result.detail, "ok"); closeModal(); refresh(true);
  } catch (error) {
    if ($("#vmEditApply") === button) {
      button.textContent = "Inspect VM before retrying";
      $("#mbody").insertAdjacentHTML("afterbegin", `<div class="note bad">${esc(error.message)}. Some VM, disk or Secret changes may already be saved. No request was repeated and no Restart was sent. Close this review and inspect the VM before editing again.</div>`);
    }
    toast(error.message, "bad");
  } finally { VM_EDIT_BUSY = false; if (window.refreshOperations) refreshOperations(true); }
  if (saved && review.restartAfter) await vmPowerReview({ns:review.config.ns, name:review.config.name, action:"restart"});
};

window.vmDelete = (ns, name) => {
  const v = (STATE.data.vms || []).find(x => x.ns === ns && x.name === name) || { disks: [] };
  const filling = v.filling || [], unfinished = new Set(filling.map(f => f.claim));
  const disks = v.disks.filter(d => (d.kind === "disk" || d.kind === "cd-rom") && d.claim && !unfinished.has(d.claim));
  modal(`Delete · ${name}`, `<p>The VM is deleted${v.status === "Running" ? ", and stopped first" : ""}. It shows as Deleting until everything it owns is gone.</p>
    ${filling.length ? `<div class="note warn">${filling.map(f => `<b class="mono">${esc(f.claim)}</b> is still being filled${f.progress != null ? ` (${f.progress.toFixed(0)}%)` : ""}`).join("; ")}.
      Deleting the VM stops that and removes the unfinished disk.</div>` : ""}
    ${disks.length ? `<label class="switch"><input type="checkbox" id="vd_disks"> Delete its disks too: ${disks.map(d => `<span class="mono">${esc(d.claim)}</span>`).join(", ")}</label>
      <div class="dim xs">Left unticked, the disks are kept and can be attached to another VM or deleted from Volumes later.</div>` : ""}
    <div class="f" style="margin-top:12px"><label>Type the VM's name to delete it</label><input id="vd_confirm" autocomplete="off"></div>
    ${UI.actions(`<button class="btn danger" onclick="vmDeleteGo(${jsq(ns)},${jsq(name)})">Delete</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.vmDeleteGo = async (ns, name) => {
  if ($("#vd_confirm").value.trim() !== name) return toast("type the VM's name exactly", "bad");
  try { const r = await api("/api/vm/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, disks: !!$("#vd_disks")?.checked }) });
    toast(r.detail, "ok"); closeModal(); refresh(true); } catch (e) { toast(e.message, "bad"); }
};


/* ---------------- a k3s cluster of VMs ----------------
   Servers and workers as VMs on a LAN network, each with an address of its
   own, the first a server the rest join with a token made here. The review
   says what goes where - and anything already at an address - before a
   single VM is made. */
const K3S_SETUPS = { homestead: "k3s, Longhorn and Homestead - what a new install gets",
  local: "k3s and Homestead, on local-path storage", k3s: "k3s alone" };
const K3S_UBUNTU = "https://cloud-images.ubuntu.com/minimal/releases/resolute/release-20260827/ubuntu-26.04-minimal-cloudimg-amd64.img";

window.k3sCluster = async () => {
  modal("New k3s cluster", '<div class="empty"><span class="spin2"></span></div>', true);
  window.__vmNetworkReopen = "k3s";
  const opts = await api("/api/vm/create-options").catch(() => ({}));
  window.__vmCreateOptions = opts;
  const lan = vmLanNetworks(opts, true);
  const images = (opts.images || []).filter(i => i.storage_class);
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">Create a k3s cluster from VMs, each with its own LAN address. The first VM is the server; the others join it.</p>
    ${lan.length ? "" : vmNetworkNote(opts, true)}
    <div class="f2"><div class="f"><label>Name ${tip("Starts each VM's name: k3s-demo-server-1, k3s-demo-agent-1 and so on.")}</label><input id="k_name" value="k3s-demo"></div>
      <div class="f"><label>Install ${tip("What each node sets up. k3s, Longhorn and Homestead is what a new install from our bootstrap script gets; local-path skips Longhorn and keeps each volume on one node; k3s alone installs nothing else.")}</label><select id="k_setup" onchange="k3sSetupChanged()">${Object.entries(K3S_SETUPS).map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select></div></div>
    <label class="switch" style="margin:0 0 14px"><input type="checkbox" id="k_kubevirt" onchange="k3sCountChanged()">
      <span>Include KubeVirt, for VMs inside the cluster
      ${tip("Installs KubeVirt and CDI on the new cluster, so its Homestead can run VMs. The nodes get this host's CPU as it is, so VMs inside run with hardware virtualisation where this host allows nesting; where it does not, they are emulated - slower, but they run. Give the nodes more memory for this.")}</span></label>
    <div class="f2"><div class="f"><label>Servers ${tip("One is enough to try things. Three keep the cluster running if one fails.")}</label>
        <select id="k_servers" onchange="k3sCountChanged()"><option value="1">1</option><option value="3">3</option></select></div>
      <div class="f"><label>Workers ${tip("Nodes that run apps but not the cluster's control plane. They join the first server. Zero is fine: a server runs apps too.")}</label><input id="k_agents" type="number" min="0" max="6" value="2" oninput="k3sCountChanged()"></div></div>
    <div class="f2"><div class="f"><label>Cores each ${tip("CPU cores for every node. Two is enough to try things; k3s itself needs little.")}</label><input id="k_cores" type="number" min="1" max="16" value="2"></div>
      <div class="f"><label>Memory each ${tip("Memory for every node, like 4Gi. Longhorn and Homestead inside want at least 4Gi on the server.")}</label><input id="k_mem" value="4Gi"></div></div>
    <div class="f2"><div class="f"><label>Disk each (GB) ${tip("Longhorn inside the cluster keeps its volumes here, so leave room for your apps.")}</label><input id="k_disk" type="number" min="20" value="40"></div>
      <div class="f"><label>Login password ${tip("For the ubuntu user on every node, at the console or over SSH.")}</label><input id="k_pass" type="password" autocomplete="new-password"></div></div>
    <div class="f"><label>Image ${tip("The operating system every node starts from. An Ubuntu cloud image works best: it runs cloud-init, which sets up the address, login and k3s.")}</label><select id="k_image">
      ${images.map(i => `<option value="image:${esc(i.namespace)}/${esc(i.name)}">Harvester image · ${esc(i.display)}</option>`).join("")}
      <option value="url" selected>Ubuntu 26.04.1 LTS minimal cloud image (downloaded${opts.harvester ? " as a Harvester image" : ""})</option></select></div>
    ${(opts.storage_classes || []).length ? `<div class="f"><label>Storage class ${tip("The storage class for each node's disk on this cluster.")}</label><select id="k_sc">${opts.storage_classes.map(c => `<option ${c === opts.default_class ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div>` : ""}
    <div class="sec">Network</div>
    <div class="f"><label>LAN network ${tip("The network bridged to your LAN the nodes join, so each has an address of its own there.")}</label><select id="k_net">${lan.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}</option>`).join("") || '<option value="">none reaches the LAN</option>'}</select></div>
    ${vmAddressFields("k", opts, 3)}
    <div id="k_review"></div>
    ${UI.actions(`<button class="btn" onclick="k3sReview()" ${lan.length ? "" : "disabled"}>Review</button>
      <button class="btn pri" id="k_go" data-need="operator" onclick="k3sCreate()" disabled>Create cluster</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`;
  k3sCountChanged();
  $$("#mbody input, #mbody select").forEach(field => {
    field.addEventListener("input", k3sInvalidateReview);
    field.addEventListener("change", k3sInvalidateReview);
  });
  if (window.applyRole) applyRole();
};
window.k3sSetupChanged = () => {
  const bare = $("#k_setup").value === "k3s", box = $("#k_kubevirt");
  box.disabled = bare;
  if (bare) box.checked = false;
  box.closest("label").title = bare ? "k3s alone installs nothing more; choose a setup with Homestead" : "";
  k3sCountChanged();
};
window.k3sCountChanged = () => {
  const count = +$("#k_servers").value + Math.max(0, +$("#k_agents").value || 0);
  vmSubnetPicked("k", count);
  k3sInvalidateReview();
};
function k3sBody() {
  const image = $("#k_image").value;
  return Object.assign(vmReadAddress("k"), {
    name: $("#k_name").value.trim(), setup: $("#k_setup").value, kubevirt: !!$("#k_kubevirt")?.checked,
    servers: +$("#k_servers").value, agents: +$("#k_agents").value || 0,
    cores: +$("#k_cores").value, memory: $("#k_mem").value.trim(), disk_gb: +$("#k_disk").value,
    password: $("#k_pass").value, network: $("#k_net").value, storage_class: $("#k_sc")?.value || "",
    image_id: image.startsWith("image:") ? image.slice(6) : "", image_url: image === "url" ? K3S_UBUNTU : "",
    addresses: $("#k_ip").value.split(",").map(x => x.trim()).filter(Boolean) });
}
let K3S_REVIEW = null, K3S_REVIEW_SEQUENCE = 0, K3S_CREATE_BUSY = false;
window.k3sInvalidateReview = () => {
  K3S_REVIEW = null; ++K3S_REVIEW_SEQUENCE;
  if ($("#k_go")) { $("#k_go").disabled = true; $("#k_go").textContent = "Create reviewed cluster"; }
  if ($("#k_review")) $("#k_review").innerHTML = "";
};
window.k3sReviewReady = () => {
  const ready = !!(K3S_REVIEW && !K3S_CREATE_BUSY && !K3S_REVIEW.capacity.blocked && $("#k_capacity_confirm")?.checked);
  if ($("#k_go")) $("#k_go").disabled = !ready;
  return ready;
};
window.k3sReview = async () => {
  if (K3S_CREATE_BUSY) return;
  k3sInvalidateReview();
  const sequence = K3S_REVIEW_SEQUENCE, input = JSON.stringify(k3sBody());
  try {
    const plan = await api("/api/vm/k3s-cluster/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: input });
    if (sequence !== K3S_REVIEW_SEQUENCE || !$("#k_go")) return;
    if (!plan.config || !plan.capacity_token || typeof plan.capacity?.blocked !== "boolean" || !Array.isArray(plan.nodes))
      throw new Error("Complete VM batch review unavailable; nothing can be created yet.");
    K3S_REVIEW = {...plan, input};
    $("#k_review").innerHTML = `<div class="sec">What it makes</div>
      <table class="tbl dense stack"><thead><tr><th>VM</th><th>Role</th><th>Address</th></tr></thead><tbody>
      ${plan.nodes.map(n => `<tr><td><b>${esc(n.name)}</b><div class="mono xs dim">${esc(plan.config.macs?.[n.name] || "")}</div></td><td data-label="Role">${n.role === "server" ? '<span class="tag info">server</span>' : '<span class="tag">worker</span>'}</td>
        <td data-label="Address" class="mono">${esc(n.address)}${n.problem ? `<div class="badtext xs">${esc(n.problem)}</div>` : ""}</td></tr>`).join("")}</tbody></table>
      <div class="note ${plan.ok ? "" : "bad"}" style="margin-top:10px">${plan.ok
        ? `Each address is recorded under its VM in IP addresses. Allow 10-15 minutes: the VMs start, install ${esc(K3S_SETUPS[plan.setup])}${plan.kubevirt ? " and KubeVirt" : ""}, and join.
           ${plan.url ? `Its own Homestead then answers at <span class="mono">${esc(plan.url)}</span>.` : ""} The job tray follows it.`
        : "The batch cannot proceed under the checked constraints. Resolve the issues below and review again."}</div>
      <div class="reviewbox"><b>Whole-batch capacity · ${plan.capacity.vm_count ?? plan.nodes.length} VMs</b>
        <p class="small">Each VM: ${esc(plan.config.cores || 2)} CPU cores · ${esc(plan.config.memory || "4Gi")} memory · ${esc(plan.config.disk_gb || 40)} GiB disk.</p>
        <p class="small muted">All VMs share one capacity budget, including new VMs waiting for launcher pods. The example is not an enforced reservation.</p>
        <div class="dependency-list">${(plan.capacity.nodes || []).map(n => `<div class="drow"><div class="dl mono">${esc(n.name)}</div><div class="dv">${n.metrics_available ? `${esc(n.baseline_gb)} → up to ${esc(n.upper_gb)} GiB (${esc(n.upper_percent)}%)` : "Live RAM unavailable"}</div></div>`).join("")}</div>
        ${(plan.capacity.blockers || []).concat(plan.capacity.reasons || []).length ? `<div class="note bad">${(plan.capacity.blockers || []).concat(plan.capacity.reasons || []).map(esc).join(" · ")}</div>` : ""}
        <div class="note warn">${(plan.capacity.warnings || []).map(esc).join(" ")}</div>
        ${(plan.capacity.example || []).length ? `<p class="small muted">Example placement: ${plan.capacity.example.map(p => `${esc(p.service)} → ${esc(p.host)}`).join("; ")}</p>` : ""}</div>
      ${!plan.capacity.blocked ? '<div class="note">Memory warnings can be overridden, even above 100%, but that may cause OOM restarts or downtime. Hardware, storage and checked scheduling blockers cannot be overridden.</div><label class="check"><input type="checkbox" id="k_capacity_confirm" onchange="k3sReviewReady()"> Create this exact batch and accept the capacity and partial-creation risks. If a step fails, keep all resources for inspection.</label>' : ""}`;
    k3sReviewReady();
  } catch (e) {
    if (sequence !== K3S_REVIEW_SEQUENCE || !$("#k_go")) return;
    K3S_REVIEW = null;
    $("#k_review").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; $("#k_go").disabled = true;
  }
};
window.k3sCreate = async () => {
  const button = $("#k_go");
  if (K3S_CREATE_BUSY || !button || !k3sReviewReady()) return;
  if (JSON.stringify(k3sBody()) !== K3S_REVIEW.input) { k3sInvalidateReview(); return toast("Configuration changed; review the VM batch again", "bad"); }
  const review = K3S_REVIEW;
  K3S_REVIEW = null;
  K3S_CREATE_BUSY = true;
  button.disabled = true; button.textContent = "Creating VMs…";
  try {
    await api("/api/vm/k3s-cluster", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({...review.config, capacity_token:review.capacity_token, confirm_capacity:true}) });
    toast("Cluster VMs created - the job tray follows them coming up", "ok");
    closeModal(); if (window.refreshOperations) refreshOperations(true); go("vms");
  } catch (e) {
    toast(e.message, "bad"); button.disabled = true; button.textContent = "Inspect batch before retrying";
    $("#k_review")?.insertAdjacentHTML("afterbegin", `<div class="note bad">${esc(e.message)}. Some VMs or their disks and Secrets may already exist. No request was repeated. Inspect the job tray and Virtual machines before a new review.</div>`);
    if (window.refreshOperations) refreshOperations(true);
  } finally { K3S_CREATE_BUSY = false; }
};

/* ---------------- importing from Unraid ----------------
   Virtual machines › Import: each Unraid server's VMs as Unraid runs them,
   one copied across with its settings mapped - the same servers, and the
   same verified SSH login, the container import uses. Disk images from a web
   address live here too. A VM running on Unraid is shut down first (its power
   button), so its disk is copied as it was left. */
const uvmSize = bytes => bytes >= 1e9 ? `${(bytes / 1e9).toFixed(bytes >= 1e11 ? 0 : 1)} GB` : `${Math.max(1, Math.round((bytes || 0) / 1e6))} MB`;
const uvmId = name => "uvm-" + String(name).replace(/[^a-z0-9-]/gi, "-");
const uvmPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function viewVmImport() {
  if (platformLacks("kubevirt", "Virtual machines")) return;
  const [srcs, disks, namespaces, storageClasses] = await Promise.all([
    api("/api/sources").catch(() => []), api("/api/vm-disks").catch(() => []),
    api("/api/namespaces").catch(() => ["lab"]), api("/api/storageclasses").catch(() => ["longhorn-r2"]),
  ]);
  STATE.data.srcs = srcs; STATE.data.importNamespaces = namespaces; STATE.data.importStorageClasses = storageClasses;
  STATE.data.uvms = STATE.data.uvms || {};
  const servers = srcs.filter(s => s.kind !== "proxmox");
  paint(`${UI.pageHeader(`Virtual machines`, `Bring VMs across from an Unraid server, or a disk image from a web address`, `${menuButton("＋ Import", [{ label: "Add an Unraid server", icon: "import", run: "srcAdd()", need: "admin" },
        { label: "A disk image from a URL", icon: "disk", run: "vmDiskImport()", need: "admin" }])}`)}
    ${servers.length ? servers.map(s => `<div class="sec">${esc(s.name)} ${tip(`${SOURCE_KINDS[s.kind] || s.kind} · ${s.user}@${s.host}`)}</div>
      <div class="card flat" id="${uvmId(s.name)}">${s.ssh_trust
        ? '<div class="empty"><span class="spin2"></span>asking it for its VMs</div>'
        : serviceRow("SSH key not verified", "", "Homestead only logs in to a server whose key you have checked.",
            `<button class="btn pri" data-need="admin" onclick="srcVerify(${jsq(s.name)})">Verify SSH key</button>`)}</div>`).join("")
      : `<div class="card flat">${serviceRow("No Unraid server yet", "", "Add it once - its address and an SSH login - and its VMs and containers can both be imported.",
          '<button class="btn pri" data-need="admin" onclick="srcAdd()">＋ Add an Unraid server</button>')}</div>`}
    <div id="uvmCopies"></div>
    <div class="sec">Disk images ${tip("Disks brought in from a web address or from Unraid. A finished one no VM uses yet can become a VM.")}</div>
    ${disks.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr>
      <th>Disk</th><th>State</th><th>Size</th><th>Attached to</th><th></th></tr></thead><tbody>${disks.map(d => {
      const done = d.phase === "Succeeded", failed = ["Failed", "Error", "Unknown"].includes(d.phase);
      return `<tr><td><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.namespace)}</div></td>
        <td data-label="State"><span class="pill ${done ? "ok" : failed ? "crit" : "med"}">${esc(done ? "ready" : String(d.phase || "").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase())}</span>${failed && d.message ? `<div class="dim xs">${esc(d.message)}</div>` : ""}</td>
        <td class="mono" data-label="Size">${esc(d.capacity || "—")}</td>
        <td data-label="Attached to">${d.in_use ? esc((d.used_by || []).join(", ")) : '<span class="dim">not attached</span>'}</td>
        <td>${done && !d.in_use ? actionBar([{ label: "Create VM", run: `vmNew(${jsq(d.name)},${jsq(d.namespace)})`, need: "operator" }]) : ""}</td></tr>`;
    }).join("")}</tbody></table></div></div>` : '<div class="dim small">None yet. ＋ Import brings one from a web address: qcow2, vmdk, raw, vdi, vhd or vhdx.</div>'}`);
  uvmCopiesPaint();
  servers.filter(s => s.ssh_trust).forEach(s => uvmLoad(s.name));
}
window.viewVmImport = viewVmImport;

/* Copies in progress, from the jobs the bell follows too. */
function uvmCopiesPaint() {
  const host = $("#uvmCopies");
  if (!host) return;
  const ops = (STATE.data.operations || []).filter(op => op.kind === "unraid-vm-import").slice(0, 10);
  host.innerHTML = ops.length ? `<div class="sec">Recent imports</div><div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr>
    <th>Import</th><th>Progress</th><th></th></tr></thead><tbody>${ops.map(op => `<tr><td><b>${esc(op.title)}</b><div><span class="pill ${operationTone(op.status)}">${esc(op.status)}</span></div></td>
      <td data-label="Progress" style="min-width:200px"><div class="small">${esc(op.message || "")}</div>
        <span class="jobmeter" style="display:block;margin-top:6px"><span style="width:${Math.max(2, Math.min(100, op.progress || 0))}%"></span></span></td>
      <td>${actionBar([{ label: "Job details", icon: "log", run: `operationLog(${jsq(op.id)})` },
        ...(!operationActive(op) && op.dismissible !== false ? [{ label: "Dismiss", need: "operator",
          run: `dismissOperation(${jsq(op.id)})`, tip: "Remove this job from history. VMs and disk data are kept." }] : [])])}</td></tr>`).join("")}</tbody></table></div></div>` : "";
  if (window.applyRole) applyRole();
}
window.uvmCopiesPaint = uvmCopiesPaint;

window.uvmLoad = async name => {
  const host = document.getElementById(uvmId(name));
  if (!host) return;
  let found;
  try {
    found = await uvmPost("/api/sources/vms", { name });
  } catch (e) {
    host.innerHTML = serviceRow("Could not list its VMs", "", esc(e.message), `<button class="btn" onclick="uvmLoad(${jsq(name)})">Try again</button>`);
    return;
  }
  STATE.data.uvms[name] = found;
  if (!found.virsh) { host.innerHTML = '<div class="dim small">This server runs no VMs: its VM manager (libvirt) is not there.</div>'; return; }
  // A list of like things: a stacked table, each row's buttons an actionBar.
  host.innerHTML = found.vms.length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr>
      <th>VM</th><th>State</th><th>Machine</th><th>Disk</th><th></th></tr></thead><tbody>${found.vms.map(vm => {
    const off = vm.shut_off, disk = vm.disks[0];
    const machine = [`${vm.cores} cores`, vm.memory.replace("Gi", " GiB").replace("Mi", " MiB"), vm.firmware === "uefi" ? "UEFI" : "BIOS"].map(esc).join(" · ");
    const action = !vm.ready ? `<span class="dim small">${esc(vm.problem)}</span>`
      : actionBar([off ? { label: "Import", run: `uvmImport(${jsq(name)},${jsq(vm.name)})`, need: "admin", pri: true }
        : { label: "Shut down", run: `uvmShutdown(${jsq(name)},${jsq(vm.name)},this)`, need: "admin", tip: "Shut it down on Unraid, so its disk is copied as it was left" }]);
    return `<tr><td><b>${esc(vm.name)}</b>${vm.os ? `<div class="dim xs">${esc(vm.os)}</div>` : ""}</td>
      <td data-label="State"><span class="pill ${off ? "" : "ok"}">${esc(vm.state || "unknown")}</span>${off || !vm.ready ? "" : '<div class="med-t xs">shut it down to import it</div>'}</td>
      <td data-label="Machine" class="small">${machine}</td>
      <td data-label="Disk" class="small mono">${disk ? `${esc(disk.path.split("/").pop())} · ${esc(disk.size_gb)} GB ${esc(disk.format)}${vm.disks.length > 1 ? ` + ${vm.disks.length - 1} more` : ""}` : "—"}</td>
      <td>${action}</td></tr>`;
  }).join("")}</tbody></table></div>` : '<div class="dim small">No VMs on this server.</div>';
  if (window.applyRole) applyRole();
};

window.uvmShutdown = async (source, vm, button) => {
  button.disabled = true; button.textContent = "Shutting down…";
  try {
    toast((await uvmPost("/api/sources/vms/shutdown", { source, vm })).message, "ok");
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.textContent = "Shut down"; return; }
  // A guest takes a while to stop; look again until it has, for two minutes.
  for (let i = 0; i < 8 && STATE.view === "vmimport"; i++) {
    await new Promise(r => setTimeout(r, 15000));
    await uvmLoad(source);
    if ((STATE.data.uvms[source]?.vms || []).find(v => v.name === vm)?.shut_off) return;
  }
};

/* The import: three steps, with the picture of what maps across kept live. */
window.uvmImport = async (source, name) => {
  const vm = (STATE.data.uvms[source]?.vms || []).find(v => v.name === name);
  if (!vm) return;
  const opts = await api("/api/vm/create-options").catch(() => ({ cdi: true, storage_classes: [], network_details: [] }));
  if (opts.cdi === false) return modal(`Import ${name}`, UI.lead("VM disks arrive through CDI, KubeVirt's disk importer, and this cluster does not have it. Install CDI from kubevirt.io, then import again.") + UI.actions(UI.cancel("Close")));
  window.__uvm = { source, vm };
  const namespaces = STATE.data.importNamespaces || ["lab"];
  const classes = opts.storage_classes?.length ? opts.storage_classes : (STATE.data.importStorageClasses || ["longhorn-r2"]);
  const facts = opts.storage_class_facts || {};
  const hw = [vm.firmware === "uefi" ? "UEFI" : "BIOS", vm.secure_boot ? "Secure Boot" : "", vm.tpm ? "TPM" : "", vm.hyperv ? "Hyper-V enlightenments" : ""].filter(Boolean).join(" · ");
  const settings = `
    ${UI.fields(UI.field("Name", `<input id="uvm_name" value="${esc(vm.slug)}" oninput="uvmPicture()">`, { tipHtml: tip("The VM's name here: lowercase letters, numbers and dashes. Its disks are named after it.") }),
      UI.field("Namespace", `<select id="uvm_ns">${namespaces.map(n => `<option ${n === "lab" ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`),
      UI.field("Cores", `<input id="uvm_cores" type="number" min="1" max="128" value="${vm.cores}" oninput="uvmPicture()">`),
      UI.field("Memory", `<input id="uvm_mem" value="${esc(vm.memory)}" oninput="uvmPicture()">`))}
    ${settingRow("Firmware", "As on Unraid; change it later under the VM's Hardware.", `<span class="mono small">${esc(hw)}</span>`)}`;
  const disks = `
    ${UI.field("Storage class", `<select id="uvm_sc">${classes.map(c => `<option value="${esc(c)}" ${facts[c]?.default ? "selected" : ""}>${esc(c)}${facts[c]?.default ? " (default)" : ""}</option>`).join("")}</select>`,
      { tipHtml: tip("The storage class for the disks. Longhorn keeps copies on several nodes, so the VM can run on any of them.") })}
    ${vm.disks.map((d, i) => settingRow(`${i ? `Disk ${i + 1}` : "Boot disk"} · ${esc(d.path.split("/").pop())}`,
      `${esc(d.size_gb)} GB ${esc(d.format)}, ${esc(uvmSize(d.used))} in use · ${esc(d.bus.toUpperCase())} bus${d.unraid_bus !== d.bus ? ` (was ${esc(d.unraid_bus)})` : ""}`,
      i ? `<label class="toggle"><input type="checkbox" class="uvm-disk" data-i="${d.index}" checked onchange="uvmPicture()"><span></span></label>` : '<span class="dim small">always</span>')).join("")}
    <p class="ui-help">The whole disk crosses the network, empty space too: ${esc(uvmSize(vm.disks.reduce((t, d) => t + d.size, 0)))} at about 100 MB/s on gigabit.</p>`;
  const lan = (opts.network_details || []).filter(n => n.vms !== false);
  const network = `
    ${UI.field("Network", `<select id="uvm_net" onchange="uvmPicture()">${lan.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.lan ? " · LAN" : ""}</option>`).join("")}<option value="pod">Pod network - reached through a Service</option></select>`,
      { tipHtml: tip("br0 on Unraid is the LAN: pick the LAN network here, and the VM is a machine on it like before. The pod network reaches it through a Service instead.") })}
    ${vm.nic.mac ? settingRow("Keep its MAC address", `${esc(vm.nic.mac)} - so your router's DHCP reservation still gives it the same address.`,
      '<label class="toggle"><input type="checkbox" id="uvm_mac" checked onchange="uvmPicture()"><span></span></label>') : ""}
    ${UI.field("Network card", `<select id="uvm_nic" onchange="uvmPicture()">${["virtio", "e1000e", "e1000", "rtl8139"].map(m => `<option ${m === vm.nic.nic_model ? "selected" : ""}>${m}</option>`).join("")}</select>`,
      { tipHtml: tip("As on Unraid. VirtIO needs its driver in the guest; Windows has it if it used VirtIO on Unraid.") })}`;
  const behind = [...vm.dropped.map(d => `<p><b>${esc(d.what)}</b> ${esc(d.detail)}: ${esc(d.reason)}.</p>`), ...vm.notes.map(n => `<p>${esc(n)}.</p>`)];
  modal(`Import ${name}`, UI.lead(`From ${esc(source)}. The VM here is made stopped once its disks have arrived; ${esc(name)} on Unraid is left as it is.`)
    + '<div id="uvm_picture"></div>'
    + stepper("uvm_steps", [{ title: "Settings", html: settings }, { title: "Disks", html: disks }, { title: "Network", html: network }],
      '<button class="btn pri" data-need="admin" id="uvm_go" onclick="uvmStart()">Import</button>')
    + (behind.length ? UI.more("What stays behind", behind.join("")) : ""), true);
  uvmPicture();
};

window.uvmPicture = () => {
  const host = $("#uvm_picture"), state = window.__uvm;
  if (!host || !state || !window.Diagram) return;
  const vm = state.vm, name = $("#uvm_name")?.value.trim() || vm.slug;
  const kept = new Set([0, ...$$(".uvm-disk").filter(c => c.checked).map(c => +c.dataset.i)]);
  const net = $("#uvm_net")?.value || "pod";
  let n = 0;
  const diskRow = d => {
    if (!kept.has(d.index)) return { what: "Disk", from: d.path.split("/").pop(), drop: true };
    const dv = n ? `${name}-disk-${n + 1}` : `${name}-disk`;
    n += 1;
    return { what: n > 1 ? `Disk ${n}` : "Disk", from: `${d.path.split("/").pop()} · ${d.size_gb} GB`, to: `${dv} · ${d.size_gb} GiB` };
  };
  const rows = [
    { what: "CPU", from: `${vm.cores} vCPUs`, to: `${$("#uvm_cores")?.value || vm.cores} cores` },
    { what: "Memory", from: vm.memory, to: $("#uvm_mem")?.value || vm.memory },
    { what: "Firmware", from: vm.firmware === "uefi" ? `OVMF${vm.tpm ? " · TPM" : ""}` : "SeaBIOS", to: vm.firmware === "uefi" ? `EFI · q35${vm.tpm ? " · TPM" : ""}` : "BIOS · q35" },
    ...vm.disks.map(diskRow),
    { what: "Network", from: `${vm.nic.bridge || "—"} · ${vm.nic.model || "virtio"}`, to: `${net === "pod" ? "pod network" : net} · ${$("#uvm_nic")?.value || vm.nic.nic_model}${vm.nic.mac && $("#uvm_mac")?.checked !== false ? " · same MAC" : ""}` },
    ...vm.dropped.map(d => ({ what: d.what.replace("GPU or PCI device", "PCI device"), from: d.detail, drop: true })),
  ];
  host.innerHTML = Diagram.vmImport(rows, { from: `${vm.name} on ${state.source}`, to: `${name} here` });
};

window.uvmStart = async () => {
  const state = window.__uvm, go = $("#uvm_go");
  if (!state || !go || go.disabled) return;
  const body = { source: state.source, vm: state.vm.name, name: $("#uvm_name").value.trim(), namespace: $("#uvm_ns").value,
    cores: +$("#uvm_cores").value, memory: $("#uvm_mem").value.trim(), storage_class: $("#uvm_sc").value,
    network: $("#uvm_net").value, keep_mac: $("#uvm_mac") ? $("#uvm_mac").checked : false, nic_model: $("#uvm_nic").value,
    skip_disks: $$(".uvm-disk").filter(c => !c.checked).map(c => +c.dataset.i) };
  go.disabled = true; go.textContent = "Starting…";
  try {
    await uvmPost("/api/vms/import-unraid", body);
    toast(`Copying ${state.vm.name}: follow it in the bell`, "ok");
    closeModal();
    if (window.refreshOperations) await refreshOperations(true);
    resetPaint(); viewVmImport();
  } catch (e) { toast(e.message, "bad"); go.disabled = false; go.textContent = "Import"; }
};

/* Virtual machines: each one's real state and the actions that fit it, a
   page per VM (disks, network, guest, events), editing and deleting. */

const VM_TONE = { Running: "ok", Stopped: "neutral", Paused: "neutral", Migrating: "neutral", Starting: "neutral", Stopping: "neutral",
  Deleting: "neutral", Provisioning: "neutral", WaitingForVolumeBinding: "neutral" };
const VM_ACTIONS = {
  // Shut down asks the guest to power off, as its own power button would;
  // Force off cuts it at once, like pulling the plug. Each has an icon of
  // its own - they were all a square, and Pause with them.
  start: ["Start", "play", "Boot the VM"], stop: ["Shut down", "power", "Ask the guest to power off, as its own power button would, then stop the VM"],
  restart: ["Restart", "restart", "Restart the VM"], pause: ["Pause", "pause", "Freeze the VM where it is, keeping its memory"],
  unpause: ["Resume", "play", "Carry on from where it was paused"], "force-stop": ["Force off", "plug", "Cut the power at once, without asking the guest - like pulling the plug; unsaved work in it is lost"],
};
const vmTone = status => VM_TONE[status] || (/Error|Fail|Crash|BackOff/i.test(status) ? "bad" : "warn");
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
    { label: "New k3s cluster", icon: "plus", run: "k3sCluster()", need: "operator", tip: "A k3s cluster made of VMs here, each with an address of its own" },
    { label: "Power schedules", icon: "clock", run: "schedulesOverview()", tip: "Apps and VMs that stop and start on their own" }];
  const create = '<button class="btn pri" data-need="operator" onclick="vmNew()">＋ New VM</button>';
  paint(`<div class="vms-page collection-page" data-collection="vms">${UI.pageHeader(`Virtual machines`, `${vms.length} VM${vms.length === 1 ? "" : "s"} · ${running} running · ${STATE.platform?.harvester === false ? `KubeVirt on ${esc(platformName(STATE.platform))}${STATE.platform.cdi ? "" : " · no CDI"}` : "KubeVirt on Harvester"}${metricsNotice(vms)}`, `${layoutSwitch("vms", "viewVMs")}
      ${moreMenu(items)}${create}`)}
    ${UI.collectionHeader(`<span class="collection-mobile-scope">All VMs</span>${vmListOptions(layout, items)}${create}`, `<span>${rows.length} VM${rows.length === 1 ? "" : "s"} · ${rows.filter(v => v.status === "Running").length} running</span>
        ${STATE.platform?.harvester === false && !STATE.platform.cdi ? UI.chip("No CDI", "warn", { size: "slim" }) : ""}`)}
    ${uptimeUnreadNote()}
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
  try { s = await api("/api/vm/store" + (check ? "?check=1" : "")); } catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `vmStore(${check ? "true" : "false"})`, dialog: true }); return; }
  STATE.data.vmStore = s;
  const mb = bytes => bytes ? (bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GiB` : `${Math.round(bytes / 1024 ** 2)} MiB`) : "—";
  const day = t => t ? fmtDay(t) : "";
  const state = r => {
    if (!s.harvester) return "";
    if (!r.kept) return '<span class="dim xs">not here</span>';
    const v = r.versions[r.versions.length - 1];
    if (!v) return UI.chip("starting", "warn", { size: "slim" });
    if (v.failed) return UI.chip("download failed", "bad", { size: "slim" });
    if (!v.ready) return UI.chip(`downloading ${Math.round(v.progress || 0)}%`, "warn", { size: "slim" });
    return UI.chip(`here · ${day(v.added)}`, "ok", { size: "slim", tip: v.display || v.image })
      + (r.versions.length > 1 ? ` <span class="dim xs" data-tip="Older builds stay while a disk was made from them">+${r.versions.length - 1} older</span>` : "")
      + (r.update ? ` ${UI.chip("newer build out", "warn", { size: "slim", tip: "The publisher has a newer build; it downloads on the next check if this keeps itself current" })}` : "");
  };
  // Keeping current is a setting of a kept image, so it sits with its state.
  const current = r => s.harvester && r.kept ? ` <label class="switch xs" data-tip="Check twice a day for a newer build, download it, and let go of older builds no disk came from"><input type="checkbox" ${r.auto ? "checked" : ""} data-need="admin" onchange="vmStoreAuto(${jsq(r.id)}, this.checked)"> current</label>` : "";
  const actions = r => actionBar([
    s.harvester && !r.kept ? { label: "Keep", run: `vmStoreKeep(${jsq(r.id)})`, need: "admin", tip: "Download it now as a Harvester image, so VMs start from a local copy" } : null,
    { label: "New VM", run: `vmStoreNew(${jsq("store:" + r.id)})`, need: "operator", pri: true },
    s.harvester && r.kept ? { label: "Stop keeping", icon: "trash", run: `vmStoreForget(${jsq(r.id)})`, need: "admin", danger: true, tip: "Stop keeping it; builds a disk came from stay" } : null,
  ], { shown: s.harvester && r.kept ? 1 : 2, label: `Actions for ${r.name}` });
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
        <td class="mono small" data-label="Size">${o.ready ? `${o.size_gb} GiB` : o.failed ? UI.chip("failed", "bad", { size: "slim" }) : UI.chip(`${Math.round(o.progress || 0)}%`, "warn", { size: "slim" })}</td>
        <td data-actions>${actionBar([o.ready ? { label: "New VM", run: `vmStoreNew(${jsq("image:" + o.image)})`, need: "operator", pri: true } : null,
          { label: "Image cache", run: "closeModal();go('images')", tip: "Sizes, copies and deleting are on the Image cache page" }])}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}
    <div class="between" style="margin-top:14px;gap:10px;flex-wrap:wrap"><div class="sec" style="margin:0">From their publishers</div>
      <input id="vmStoreFilter" type="search" aria-label="Filter images" placeholder="Filter: minimal, debian, lvm..." value="${esc(STATE.vmStoreFilter || "")}"
        oninput="STATE.vmStoreFilter=this.value; clearTimeout(window.__vsf); window.__vsf=setTimeout(() => vmStore(), 250)" style="max-width:260px"></div>
    <div class="card flat pad0" style="margin-top:8px"><div class="tblwrap"><table class="tbl stack dense vmstore"><thead><tr>
      <th>Image</th><th>Variant</th><th>Download</th><th>Sign in as</th>${s.harvester ? "<th>Here</th>" : ""}<th></th></tr></thead>
      ${distros.map(d => {
        const group = shown.filter(r => r.distro === d);
        return group.length ? `<tbody><tr class="vmstore-distro"><td colspan="${s.harvester ? 6 : 5}">${esc(d)}</td></tr>
          ${group.map(r => `<tr><td><b>${esc(r.name)}</b>${r.error ? `<div class="dim xs badtext">${esc(r.error)}</div>` : ""}</td>
            <td data-label="Variant">${UI.chip(r.variant, "neutral", { tip: r.about })}</td>
            <td class="mono small" data-label="Download">${mb(r.size)}</td>
            <td class="mono small" data-label="Sign in as">${esc(r.user)}</td>
            ${s.harvester ? `<td data-label="Here">${state(r)}${current(r)}</td>` : ""}<td data-actions>${actions(r)}</td></tr>`).join("")}</tbody>` : "";
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
  if (!(await ask(`Stop keeping ${row?.name || id}?\n\nIts builds no disk was made from are deleted; the ones a disk came from stay until that disk is gone.`, { title: "Stop keeping the image", ok: "Stop keeping it", danger: true }))) return;
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
  return [`${v.cores} vCPU${v.cores === 1 ? "" : "s"}`, v.memory || "",
    disks.length ? (size ? `${size} disk${disks.length > 1 ? "s" : ""}` : `${disks.length} disk${disks.length > 1 ? "s" : ""}`) : "no disk"]
    .filter(Boolean);
}
/* Its addresses, whole: the first, and how many more. Stopped, it has none. */
function vmAddress(v, withNetwork = true) {
  const ips = v.ips?.length ? v.ips : v.ip ? [v.ip] : [];
  if (!ips.length) return `<span class="dim">${v.running ? "no address reported yet" : "no address while stopped"}</span>`;
  return `<span class="mono vm-ip">${esc(ips[0])}</span>
    <button class="iconbtn vm-copy" type="button" title="Copy ${esc(ips[0])}" onclick="event.stopPropagation();ipamCopy(${jsq(ips[0])})">${icon("copy")}</button>
    ${ips.length > 1 ? UI.chip(`+${ips.length - 1}`, "neutral", { tip: ips.slice(1).join(", ") }) : ""}
    ${withNetwork && v.network ? `<span class="dim xs vm-net" title="${esc(v.network)}">on ${esc(v.network)}</span>` : ""}`;
}
/* What a running VM is using. CPU and memory are its launcher pod's, from
   the metrics API; disk traffic is KubeVirt's own count, as a rate over the
   last half minute. Anything not measured is left out, not guessed. */
const vmBytes = b => b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(1)} GiB` : b >= 1024 ** 2 ? `${Math.round(b / 1024 ** 2)} MiB` : `${Math.round(b / 1024)} KiB`;
const vmRate = b => b >= 1024 ** 2 ? `${(b / 1024 ** 2).toFixed(1)} MiB/s` : b >= 1024 ? `${Math.round(b / 1024)} KiB/s` : `${Math.round(b || 0)} B/s`;
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
const vmClusterTag = v => v.cluster ? `<span class="tag cluster-tag" data-tip="A VM of the k3s cluster ${esc(v.cluster)}, made here">${esc(v.cluster)}${v.cluster_role ? ` · ${esc(v.cluster_role)}` : ""}</span>` : "";

function vmActions(v, compact = false) {
  // One power action beside Console, as a container card has Logs; the rest
  // are in ⋯, the shared action bar, named for this VM (#487).
  const ns = jsq(v.ns), name = jsq(v.name), run = a => `vmPower(${ns},${name},${jsq(a)})`;
  const main = v.actions.filter(a => ["start", "stop", "restart", "unpause"].includes(a));
  const words = a => a === "stop" && v.stop_retries ? ["Stop retries", "power", "Stop automatic boot retries and keep the VM off until you start it"] : VM_ACTIONS[a];
  const power = a => ({ label: words(a)[0], icon: words(a)[1], tip: words(a)[2], run: run(a), need: "operator", pri: a === "start" });
  const console = v.actions.includes("console");
  return actionBar([
    main[0] && power(main[0]),
    console && { label: "Console", icon: "console", run: `vmConsole(${ns},${name})`, need: "operator", tip: "Console" },
    { label: "Details", icon: "list", run: `vmOpen(${ns},${name})` },
    ...main.slice(1).map(power),
    { label: "Edit", icon: "edit", run: `vmEdit(${ns},${name})`, need: "operator" },
    { label: "Logo", icon: "logo", run: `vmLogo(${ns},${name})`, need: "operator" },
    { label: "Monitoring", icon: "pulse", run: `vmMonitoring(${ns},${name})` },
    { label: "Schedule", icon: "clock", run: `vmSchedule(${ns},${name})`, need: "operator" },
    v.actions.includes("pause") && { label: "Pause", icon: "pause", run: run("pause"), need: "operator" },
    v.actions.includes("migrate") && { label: "Move to another host", icon: "move", run: `vmMove(${ns},${name})`, need: "operator" },
    FLEET.view?.linked && { label: "Move to cluster", icon: "move", run: `moveToCluster('vm',${name},${jsq(v.site?.handle || "")},'move',${ns})`, need: "admin", tip: "Move it to another linked cluster, disks and all" },
    FLEET.view?.linked && { label: "Copy to cluster", icon: "copy", run: `moveToCluster('vm',${name},${jsq(v.site?.handle || "")},'copy',${ns})`, need: "admin", tip: "Keep the source VM and create a stopped copy on another linked cluster" },
    v.actions.includes("force-stop") && { label: "Force off", icon: "plug", run: run("force-stop"), need: "operator", danger: true, tip: VM_ACTIONS["force-stop"][2] },
    { label: "Delete", icon: "trash", run: `vmDelete(${ns},${name})`, need: "admin", danger: true },
  ], { shown: (main.length ? 1 : 0) + (console ? 1 : 0), label: `More actions for ${v.name}`, iconOnly: compact });
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
      <td class="cell-name" data-sort="${esc(v.name)}"><div class="vm-name-cell">${vmAvatar(v)}<div class="vm-name-text"><b title="${esc(v.name)}">${esc(v.name)}</b>
        ${clusterTag(v) || vmClusterTag(v) ? `<div class="row vm-tags">${clusterTag(v)}${vmClusterTag(v)}</div>` : ""}
        <div class="dim xs vm-sub">${esc([v.ns, v.os, v.node ? `on ${v.node}` : ""].filter(Boolean).join(" · "))}</div></div></div></td>
      <td data-label="Status" data-status data-sort="${esc(v.status)}">${UI.chip(v.status, vmTone(v.status), { tip: v.problem || "" })}${answerTag(v, true)}${scheduleTag(v)}
        ${v.restart_required ? '<div class="dim xs">restart to apply changes</div>' : ""}
        ${(v.filling || []).length ? `<div class="dim xs">${esc(VM_FILL_WORDS[v.filling[0].phase] || v.filling[0].phase)}${v.filling[0].progress != null ? ` · ${v.filling[0].progress.toFixed(0)}%` : ""}</div>` : ""}</td>
      <td data-label="Address" class="nowrap" data-sort="${esc((v.ips || [])[0] || "")}"><div class="vm-addr">${vmAddress(v, false)}</div>
        ${v.network ? `<div class="dim xs">${esc(v.network)}</div>` : ""}</td>
      <td data-label="CPU" class="nowrap vm-cell-use" data-sort="${v.usage?.cpu_pct ?? -1}">${v.usage?.cpu_pct != null ? `${meter(v.usage.cpu_pct, "", "cpu")}<div class="dim xs mono">${v.usage.cpu_pct}% of ${v.cores}</div>` : `<span class="dim xs">${v.cores} vCPU${v.cores === 1 ? "" : "s"}</span>`}</td>
      <td data-label="RAM" class="nowrap vm-cell-use" data-sort="${v.usage?.mem_pct ?? -1}">${v.usage?.mem_pct != null ? `${meter(v.usage.mem_pct, "", "memory")}<div class="dim xs mono">${vmBytes(v.usage.mem)} of ${esc(v.memory)}</div>` : `<span class="dim xs">${esc(v.memory || "—")}</span>`}</td>
      <td data-label="Disk IO" data-sm-hide class="nowrap small" data-sort="${(v.usage?.read_bps || 0) + (v.usage?.write_bps || 0)}">${v.running ? vmIo(v.usage) : '<span class="dim">—</span>'}</td>
      <td class="nowrap" data-actions><div class="row vm-actions" style="justify-content:flex-end">${vmActions(v, true)}</div></td></tr>`).join("")}
    </tbody></table></div></div>`;
}
window.viewVMs = viewVMs;

/* The VM dialog's actions (#504): its state's main power action and Console
   as buttons; the rest - other power actions, with Force off as danger, Edit,
   Schedule and Delete - in ⋯, so a phone shows one row. */
function vmDialogActions(v) {
  const run = action => `vmPower(${jsq(v.ns)},${jsq(v.name)},${jsq(action)})`;
  const power = v.actions.filter(a => VM_ACTIONS[a]);
  const main = power.find(a => ["start", "unpause", "stop", "restart"].includes(a));
  const words = a => a === "stop" && v.stop_retries ? ["Stop retries", "power", "Stop automatic boot retries and keep the VM off until you start it"] : VM_ACTIONS[a];
  const item = a => ({ label: words(a)[0], icon: words(a)[1], tip: words(a)[2], run: run(a), need: "operator", pri: a === "start", danger: a === "force-stop" });
  const console = v.actions.includes("console");
  return actionBar([
    main && item(main),
    console && { label: "Console", icon: "console", run: `vmConsole(${jsq(v.ns)},${jsq(v.name)})`, need: "operator" },
    ...power.filter(a => a !== main).map(item),
    { label: "Edit", icon: "edit", run: `vmEdit(${jsq(v.ns)},${jsq(v.name)})`, need: "operator" },
    { label: "Schedule", icon: "clock", run: `vmSchedule(${jsq(v.ns)},${jsq(v.name)})`, need: "operator" },
    { label: "Delete", icon: "trash", run: `vmDelete(${jsq(v.ns)},${jsq(v.name)})`, need: "operator", danger: true },
  ], { shown: (main ? 1 : 0) + (console ? 1 : 0), label: `More actions for ${v.name}` });
}

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
      <span class="row nowrap" style="gap:6px">${clusterTag(v)}${UI.chip(v.status, vmTone(v.status), { tip: v.problem || "" })}</span></div>
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
    ${UI.typedConfirm({ id: `${prefix}StateName`, expect: state.name, oninput: `${changed}()` })}</div>`;
};
window.vmStateInitReady = (plan, prefix) => !plan.vm?.state_initialization ||
  ($(`#${prefix}StateAck`)?.checked === true && typedConfirmMatches(`${prefix}StateName`));
window.vmStateInitBody = (plan, prefix) => plan.vm?.state_initialization ? {
  ack_state_initialization: $(`#${prefix}StateAck`)?.checked === true,
  confirm_state_name: ($(`#${prefix}StateName`)?.value || "").trim()
} : {};
window.vmPower = async (ns, name, action) => {
  if (VM_POWER.busy && ["start", "restart", "unpause"].includes(action)) return toast("A VM power request is being sent; wait for its result", "bad");
  if (["start", "restart", "unpause"].includes(action)) return vmPowerReview({ ns, name, action });
  // Stopping is asked first, as starting is reviewed: the guest goes away.
  if (action === "stop" && !(await ask(`Shut down ${name}? The guest is asked to shut down, as pressing its power button does; it stays off until you start it.`, { title: "Shut down", ok: "Shut down" }))) return;
  if (action === "force-stop" && !(await ask(`Force off ${name}? The power is cut at once - the guest is not asked to shut down, so unsaved work in it is lost. Shut down asks it first.`, { title: "Force off", ok: "Force off", danger: true }))) return;
  try {
    const r = await api("/api/vm/power", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, action }) });
    toast(r.detail, "ok"); setTimeout(() => refresh(true), 1200);
  } catch (e) { toast(e.message, "bad"); }
};

const VM_POWER_WHAT = { start: "Starts", restart: "Restarts", unpause: "Resumes" };
window.vmPowerReview = config => VM_POWER.open({ ...config });
// Start, Restart and Resume are reviewed first; Shut down, Force off and
// Pause stay available while one is being sent.
const VM_POWER = UI.reviewFlow({
  loading: frozen => {
    const open = window.childModal && !$("#modal").classList.contains("hidden") ? childModal : modal;
    open(`${VM_ACTIONS[frozen.action]?.[0] || "Power"} · ${frozen.name}`, '<div id="vmPowerLoading" class="empty"><span class="spin2"></span>Checking VM, storage and host capacity…</div>', true);
  },
  live: () => !!$("#vmPowerLoading"),
  preview: async frozen => {
    const review = await api("/api/vm/power/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(frozen)});
    if (!review.capacity || typeof review.capacity.blocked !== "boolean" || !review.capacity_token)
      throw new Error("VM capacity review unavailable. Nothing was sent; refresh before continuing.");
    return { ...review, config:frozen };
  },
  render: (review, frozen) => {
    const plan = review.capacity, facts = plan.vm || {};
    const policy = facts.policy_after && facts.policy_after !== facts.policy_before
      ? ` Its restart policy changes from ${esc(facts.policy_before || "unknown")} to <b>${esc(facts.policy_after)}</b>.` : "";
    const what = VM_POWER_WHAT[frozen.action];
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
  },
  failed: error => {
    $("#mbody").innerHTML = `<div class="note bad">${esc(error.message)}</div>${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Close</button>`)}`;
  },
  // The tickbox is there only when there is a warning to accept.
  blocked: review => review.capacity.blocked, ack: "vmPowerApprove", button: "vmPowerApply",
  check: review => vmStateInitReady(review.capacity, "vmPower"),
  apply: async review => {
    const button = $("#vmPowerApply");
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
        $("#mbody").insertAdjacentHTML("afterbegin", `<div class="note bad">${esc(error.message)}. The request was not repeated. Its outcome may be uncertain: inspect the VM and its Recent jobs entry before trying again. Shut down and Force off remain available.</div>`);
        if ($(".modalbox")) $(".modalbox").scrollTop = 0;
      }
      toast(error.message, "bad");
    }
  },
  after: () => { if (window.startOperationChecks) startOperationChecks(); },
});
window.vmPowerReviewReady = () => VM_POWER.ready();
window.vmPowerReviewedApply = async () => {
  if (!VM_POWER.ready()) return toast(`Tick ${reviewAck(VM_POWER_WHAT[VM_POWER.review?.config?.action])} to accept the warning`, "bad");
  await VM_POWER.apply();
};

window.vmOpen = async (ns, name) => {
  modal(`VM · ${name}`, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  let v;
  try { v = await api(`/api/vm?ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`); }
  catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `vmOpen(${jsArg(ns)},${jsArg(name)})`, dialog: true }); return; }
  // Where it is first, then its figures; the rest in the shared section tabs (#506).
  const ips = v.ips?.length ? v.ips : v.ip ? [v.ip] : [];
  const overview = `${UI.facts([
      ["Address", ips.length ? ips.map(ip => `<span class="mono">${esc(ip)}</span>`).join(", ") : '<span class="dim">none yet</span>'],
      ["Host", v.node ? esc(v.node) : '<span class="dim">not running</span>'],
      ["CPU", v.cores == null ? "Unavailable" : `${esc(v.cores)} vCPUs`], ["Memory", esc(v.memory || "—")],
      ["Guest OS", esc(v.guest?.prettyName || v.os || "unknown")], ["Kernel", `<span class="mono xs">${esc(v.guest?.kernelRelease || "—")}</span>`],
      ["Live migration", v.migratable ? "possible" : "not possible"], ["Run strategy", esc(v.run_strategy || "—")],
      ["Created", esc((v.created || "").slice(0, 10) || "—")]])}
      ${v.profile_error ? UI.callout("warn", "", `<p>${esc(v.profile_error)}</p>`) : ""}
      ${v.description ? `<p class="small" style="margin-top:10px">${esc(v.description)}</p>` : ""}
      ${v.guest?.prettyName ? "" : '<p class="dim xs" style="margin-top:8px">The guest reports its OS and addresses through the QEMU guest agent, when it runs one.</p>'}
      ${v.conditions.length ? UI.more("Conditions", `<table class="tbl dense stack"><thead><tr><th>Condition</th><th>Status</th><th>Detail</th></tr></thead><tbody>
        ${v.conditions.map(c => `<tr><td>${esc(c.type)}</td><td>${UI.chip(c.status, c.status === "True" ? "ok" : "neutral", { size: "slim" })}</td><td class="small">${esc(c.message || c.reason)}</td></tr>`).join("")}</tbody></table>`) : ""}`;
  const disks = `<table class="tbl dense stack"><thead><tr><th>Disk</th><th>Kind</th><th>Volume</th><th>Size</th><th>Boot</th></tr></thead><tbody>
      ${v.disks.map(d => `<tr><td><b>${esc(d.name)}</b><div class="dim xs">${esc(d.bus)}</div></td><td>${esc(d.kind)}</td>
        <td class="mono small">${esc(d.claim || "—")}${d.storage_class ? `<div class="dim xs">${esc(d.storage_class)}</div>` : ""}</td>
        <td class="mono">${esc(d.size || "—")}</td><td>${d.boot ? `#${d.boot}` : ""}</td></tr>`).join("")}</tbody></table>`;
  // The tab's one action is its own, above the table, not part of a note (#559).
  const network = `<div class="between vm-net-head">${UI.lead("Pod-network VMs can use a default or custom Service VIP with port mappings. Bridged interfaces use DHCP or a static guest address.")}
      <button class="btn sm" data-need="operator" onclick="networkManage(${jsq(v.ns)},${jsq(v.name)},'VirtualMachine')">Configure VIP / ports</button></div>
      <table class="tbl dense stack"><thead><tr><th>Interface</th><th>Network</th><th>MAC</th><th>Addresses</th></tr></thead><tbody>
      ${v.nics.map(n => `<tr><td><b>${esc(n.name)}</b><div class="dim xs">${esc(n.model)}</div></td><td>${esc(n.network || "—")}</td>
        <td class="mono xs">${esc(n.mac || "—")}</td><td class="mono small">${esc(n.ips.join(", ") || "—")}</td></tr>`).join("")}</tbody></table>`;
  // What Edit changes, shown here too: hardware, display and passthrough (#vm-page).
  const hw = v.hardware, change = key => `<div class="row" style="margin-top:10px">${UI.button("Change", `vmEditAt(${jsArg(v.ns)},${jsArg(v.name)},${jsArg(key)})`, { attrs: 'data-need="operator"' })}</div>`;
  const yes = on => on ? "on" : '<span class="dim">off</span>';
  const hardware = hw ? `${UI.facts([
      ["CPU", `${esc((hw.cpu.sockets || 1) * (hw.cpu.cores || 1) * (hw.cpu.threads || 1))} vCPUs · ${esc(hw.cpu.model || "cluster default model")}${hw.cpu.dedicated ? " · dedicated" : ""}`],
      ["Firmware", hw.firmware === "uefi" ? `UEFI${hw.secure_boot ? " · Secure Boot" : ""}${hw.efi_persistent ? " · kept variables" : ""}` : "BIOS"],
      ["TPM", hw.tpm === "off" ? '<span class="dim">off</span>' : hw.tpm === "persistent" ? "on, kept" : "on"],
      ["Machine", esc(hw.machine || "default")], ["Hyper-V", yes(hw.hyperv)], ["Clock", esc(hw.timezone || "UTC")],
      ["Hugepages", esc(hw.hugepages || "normal pages")]])}${change("hardware")}` : "";
  const displayWord = { console: "Virtual display (the console's Screen)", gpu: "Passed-through GPU only - no virtual display", serial: "No display - serial console only" };
  const display = hw ? `${UI.facts([
      ["Display", esc(displayWord[hw.boot_output] || displayWord[hw.graphics ? "console" : "serial"])],
      ["Serial console", yes(hw.serial)], ["Tablet pointer", yes(hw.tablet)], ["Random-number device", yes(hw.rng)],
      ["Memory balloon", yes(hw.balloon)], ["Sound card", yes(hw.sound)]])}${change("display")}` : "";
  const devices = `${(v.host_devices || []).length ? `<table class="tbl dense stack"><thead><tr><th>Device</th><th>Resource</th><th>ROM (vBIOS)</th></tr></thead><tbody>
      ${v.host_devices.map(d => `<tr><td><b>${esc(d.name)}</b>${d.gpu ? ` ${UI.chip("GPU", "info")}` : ""}</td><td class="mono small">${esc(d.resource)}</td><td>${d.rom ? "its own" : '<span class="dim">default</span>'}</td></tr>`).join("")}</tbody></table>`
      : '<div class="dim small">No PCI or USB device is passed through.</div>'}${change("devices")}`;
  const events = v.events.length ? `<table class="tbl dense stack"><thead><tr><th>Type</th><th>Reason</th><th>Message</th><th>Last</th></tr></thead><tbody>
      ${v.events.map(e => `<tr><td>${UI.chip(e.type, e.type === "Warning" ? "warn" : "neutral", { size: "slim" })}</td><td class="small">${esc(e.reason)}</td>
        <td class="small">${esc(e.message)}</td><td class="small dim">${fmtWhen(e.last)}</td></tr>`).join("")}</tbody></table>` : '<div class="dim small">No recent events.</div>';
  $("#mbody").innerHTML = `<div class="between vm-head"><div>${UI.chip(v.status, vmTone(v.status))}</div>
      ${vmDialogActions(v)}</div>
    ${v.problem ? UI.callout("bad", "", `<p>${esc(v.problem)}</p>`) : ""}
    ${vmFilling(v)}
    ${UI.sectionForm("vm_detail", [
      { key: "overview", title: "Overview", html: overview },
      ...(hw ? [{ key: "hardware", title: "Hardware", html: hardware }, { key: "display", title: "Display and devices", html: display }] : []),
      { key: "disks", title: `Disks · ${v.disks.length}`, html: disks },
      { key: "network", title: `Network · ${v.isolated ? "isolated" : v.implicit_network ? "automatic" : v.nics.length}`, html: network },
      { key: "devices", title: `Passthrough · ${(v.host_devices || []).length}`, html: devices },
      { key: "events", title: "Events", html: events }], "", { always: true, cancelHtml: UI.cancel("Close") })}`;
  if (window.applyRole) applyRole();
};

/* Edit, opened at one of its sections - from the VM page's Change. */
window.vmEditAt = async (ns, name, key) => {
  await vmEdit(ns, name);
  if ($("#mbody")) UI.selectSection("ve", key);
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
      ${unmade ? UI.chip("not made", "bad", { size: "slim", tip: `This disk has not been made${d.phase ? ` (${d.phase})` : ""}, so its source can still be changed` }) : ""}</td>
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
    <div class="f2"><div class="f"><label>vCPUs</label>${locked
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
      // Hosts, images, classes and networks are not guessed (#620).
      mustRead("/api/vm/create-options", "what this VM can use"),
      api("/api/passthrough/resources").catch(error => ({ resources: [], error: error.message }))]);
  } catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: `vmEdit(${jsArg(ns)},${jsArg(name)})`, dialog: true }); return; }
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
      ${UI.more("Advanced editing", UI.button("Edit YAML", `vmYaml(${jsArg(ns)},${jsArg(name)})`, { attrs: 'data-need="admin"' }))}`},
    v.hardware && {key:"hardware", title:"Hardware", html:vmHardwareFields(v.hardware, o, !!v.resource_profile?.name, false, true)},
    v.hardware && {key:"display", title:"Display and devices", html:vmDisplayFields(v.hardware)},
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
  window.__vmEdit.initial = JSON.stringify(vmEditBody());
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
/* What the edit form would send, without passthrough and hardware, which
   report their own changes: compared with the form as it opened, it says
   whether anything changed. */
function vmEditBody() {
  const { ns, name, v } = window.__vmEdit;
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
  return body;
}
window.vmEditSave = async () => {
  const badUrl = $$("#mbody tr[data-disk], #mbody .vd-add").some(box => box.querySelector(".vd_src,.va_src")?.value === "url"
    && !/^https?:\/\/[^/\s]+/i.test(box.querySelector(".vd_url,.va_url").value.trim()));
  if (badUrl) return toast("A disk's URL must start with http:// or https://", "bad");
  const body = vmEditBody(), v = window.__vmEdit.v;
  let devices = null;
  try {
    devices = window.vmDevicesChanges ? await window.vmDevicesChanges() : null;
    if (devices) body.host_devices = devices;
  } catch (e) { return toast(`The ROM file could not be read: ${e.message}`, "bad"); }
  const hardware = v.hardware ? vmHardwareChanges(v.hardware) : null;
  if (!devices && !hardware && JSON.stringify(vmEditBody()) === window.__vmEdit.initial) return toast("No changes to save.", "ok");
  if (hardware) {
    body.hardware = hardware;
    // The topology sets the count: the General tab's figure is not sent too.
    if (hardware.cpu && ["sockets", "cores", "threads"].some(k => k in hardware.cpu)) delete body.cores;
  }
  return vmEditReview(body, !!$("#ve_restart")?.checked);
};

window.vmEditReview = (config, restartAfter = false) => VM_EDIT.open({ frozen: JSON.parse(JSON.stringify(config)), restartAfter });
// A save may be followed by a restart, which is a review of its own.
const VM_EDIT = UI.reviewFlow({
  loading: ({ frozen }) => childModal(`Save changes · ${frozen.name}`, '<div id="vmEditLoading" class="empty"><span class="spin2"></span>Checking proposed VM, disks and capacity…</div>', true),
  live: () => !!$("#vmEditLoading"),
  preview: async ({ frozen, restartAfter }) => {
    const review = await api("/api/vm/edit/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(frozen)});
    if (!review.capacity || typeof review.capacity.blocked !== "boolean" || !review.capacity_token)
      throw new Error("VM edit review unavailable. Nothing was saved; go back and review again.");
    return {...review, config:frozen, restartAfter};
  },
  render: (review, { frozen, restartAfter }) => {
    const plan = review.capacity, facts = plan.vm || {};
    const changes = UI.facts([
      frozen.cores != null || frozen.memory ? ["Size", `${esc(frozen.cores ?? "unchanged")} vCPUs · ${esc(frozen.memory || "unchanged memory")}`] : null,
      frozen.isolated ? ["Network", "Isolated: all virtual network cards are removed at the next start"]
        : frozen.isolated === false ? ["Network", "Virtual network cards are allowed"] : null,
      facts.policy_after && facts.policy_after !== facts.policy_before ? ["Restart policy", `${esc(facts.policy_before || "unknown")} → ${esc(facts.policy_after)}`] : null,
      review.volumes?.length ? ["New disks", review.volumes.map(v => `<span class="mono">${esc(v.name)}</span> · ${esc(v.size)} · ${esc(v.storage_class)}`).join("<br>")] : null]);
    const after = `<p class="ui-help">${restartAfter ? "After saving, a separate restart review checks the saved VM and host capacity; saving sends no restart." : "Saving sends no restart. Restart the VM from its power controls if a change needs it."}</p>`;
    const warnings = plan.warnings || [];
    $("#mbody").innerHTML = (facts.admission_needed
      ? startReview(plan, { what: "Saves", name: frozen.name, objects: changes + vmStateInitHtml(plan, "vmEdit", "vmEditReviewReady"), warnings,
          ackId: "vmEditApprove", onAck: "vmEditReviewReady()",
          details: `${after}<p class="ui-help">Resource and policy changes can take effect at once through KubeVirt. Host RAM estimates include launcher overhead; they are not a memory limit.</p>` })
      : [UI.lead(`Saves <b>${esc(frozen.name)}</b>. It needs no new room on a host.`),
        UI.notices([...(plan.blockers || []).map(b => ["bad", b]), ...warnings.map(w => ["warn", w])], plan.blockers?.length ? "Can't save" : "Check first"),
        changes, vmStateInitHtml(plan, "vmEdit", "vmEditReviewReady"), UI.more("Details", after),
        !plan.blocked && warnings.length ? UI.ack("vmEditApprove", reviewAck("Saves"), { onchange: "vmEditReviewReady()" }) : ""].join(""))
      + UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="vmEditReviewBack()">Back to edit</button><button class="btn pri" id="vmEditApply" disabled onclick="vmEditReviewedApply()">Save changes</button>`);
    vmEditReviewReady();
  },
  failed: error => {
    $("#mbody").innerHTML = `<div class="note bad">${esc(error.message)}</div><button class="btn" data-dialog-dismiss="true" onclick="vmEditReviewBack()">Back to edit</button>`;
  },
  // The tickbox is there only when there is something to accept.
  blocked: review => review.capacity.blocked, ack: "vmEditApprove", button: "vmEditApply",
  check: review => vmStateInitReady(review.capacity, "vmEdit"),
  apply: async review => {
    const button = $("#vmEditApply");
    button.disabled = true; button.textContent = "Saving changes…";
    try {
      const result = await api("/api/vm/edit", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({...review.config, ...vmStateInitBody(review.capacity, "vmEdit"), capacity_token:review.capacity_token, confirm_capacity:true})});
      if (result.operation && window.noteOperation) noteOperation(result.operation);
      toast(result.detail, "ok"); closeModal(); refresh(true);
      return true;
    } catch (error) {
      if (error?.status === 409) throw error; // conflict() below
      if ($("#vmEditApply") === button) {
        button.textContent = "Inspect VM before retrying";
        $("#mbody").insertAdjacentHTML("afterbegin", `<div class="note bad">${esc(error.message)}. Some VM, disk or Secret changes may already be saved. No request was repeated and no Restart was sent. Close this review and inspect the VM before editing again.</div>`);
      }
      toast(error.message, "bad");
      return false;
    }
  },
  after: () => { if (window.refreshOperations) refreshOperations(true); },
  // The server says the review is out of date: offer a fresh one of the same change.
  conflict: (error, review) => {
    VM_EDIT_AGAIN = review;
    toast(error.message, "bad");
    if ($("#mbody")) $("#mbody").innerHTML = UI.callout("bad", "Not saved as reviewed", `${esc(error.message)}. The review was out of date. Some VM, disk or Secret changes may already be saved; no request was repeated and no Restart was sent. Review again to see the VM as it is now.`)
      + UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="vmEditReviewBack()">Back to edit</button><button class="btn pri" onclick="vmEditReviewAgain()">Review again</button>`);
    $("#mbody")?.querySelector(".ui-actions-end .btn.pri")?.focus();
    return false;
  },
});
let VM_EDIT_AGAIN = null;
window.vmEditReviewAgain = () => VM_EDIT_AGAIN && vmEditReview(VM_EDIT_AGAIN.config, VM_EDIT_AGAIN.restartAfter);
window.vmEditReviewBack = () => { VM_EDIT.back(); modalBack(); };
window.vmEditReviewReady = () => VM_EDIT.ready();
window.vmEditReviewedApply = async () => {
  if (!VM_EDIT.ready()) return toast(`Tick ${reviewAck("Saves")} to accept the warnings first`, "bad");
  const review = VM_EDIT.review;
  let saved = false;
  // A 409 for a review no longer on screen comes back here: said, nothing drawn.
  try { saved = await VM_EDIT.apply(); } catch (error) { toast(error.message, "bad"); }
  if (saved && review.restartAfter) await vmPowerReview({ns:review.config.ns, name:review.config.name, action:"restart"});
};

window.vmDelete = (ns, name) => {
  const v = (STATE.data.vms || []).find(x => x.ns === ns && x.name === name) || { disks: [] };
  const filling = v.filling || [], unfinished = new Set(filling.map(f => f.claim));
  const disks = v.disks.filter(d => (d.kind === "disk" || d.kind === "cd-rom") && d.claim && !unfinished.has(d.claim));
  modal(`Delete VM · ${name}`, `${UI.lead(`Deletes <b>${esc(name)}</b>${v.status === "Running" ? ", stopping it first" : ""}. It shows as Deleting until everything it owns is gone.`)}
    ${filling.length ? UI.callout("warn", "A disk is still being filled", `${filling.map(f => `<b class="mono">${esc(f.claim)}</b>${f.progress != null ? ` (${f.progress.toFixed(0)}%)` : ""}`).join(", ")}: deleting the VM stops that and removes the unfinished disk.`) : ""}
    ${disks.length ? `<label class="switch"><input type="checkbox" id="vd_disks"> Delete its disks too: ${disks.map(d => `<span class="mono">${esc(d.claim)}</span>`).join(", ")}</label>
      <div class="dim xs">Left unticked, the disks are kept and can be attached to another VM or deleted from Volumes later.</div>` : ""}
    ${UI.typedConfirm({ id: "vd_confirm", expect: name, button: "vd_go" })}
    ${UI.actions(UI.cancel() + `<button class="btn danger" id="vd_go" disabled onclick="vmDeleteGo(${jsq(ns)},${jsq(name)})">Delete VM</button>`)}`);
};
window.vmDeleteGo = async (ns, name) => {
  if (!typedConfirmMatches("vd_confirm")) return;
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
  let opts;
  // Not guessed, and a failed read is not kept as the options (#620).
  try { opts = await mustRead("/api/vm/create-options", "what a new k3s cluster can use"); }
  catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: "k3sCluster()", dialog: true }); return; }
  window.__vmCreateOptions = opts;
  const lan = vmLanNetworks(opts, true);
  const images = (opts.images || []).filter(i => i.storage_class);
  $("#mbody").innerHTML = `
    <p class="small" style="margin-top:0">Create a k3s cluster from VMs, each with its own LAN address. The first VM is the server; the others join it.</p>
    ${lan.length ? "" : vmNetworkNote(opts, true)}
    <div class="f2"><div class="f"><label>Name ${tip("Starts each VM's name: k3s-demo-server-1, k3s-demo-agent-1 and so on.")}</label><input id="k_name" value="k3s-demo"></div>
      <div class="f"><label>Install ${tip("What each VM sets up. k3s, Longhorn and Homestead is what a new install from our bootstrap script gets; local-path skips Longhorn and keeps each volume on one VM; k3s alone installs nothing else.")}</label><select id="k_setup" onchange="k3sSetupChanged()">${Object.entries(K3S_SETUPS).map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select></div></div>
    <label class="switch" style="margin:0 0 14px"><input type="checkbox" id="k_kubevirt" onchange="k3sCountChanged()">
      <span>Include KubeVirt, for VMs inside the cluster
      ${tip("Installs KubeVirt and CDI on the new cluster, so its Homestead can run VMs. The VMs get this host's CPU as it is, so VMs inside run with hardware virtualisation where this host allows nesting; where it does not, they are emulated - slower, but they run. Give the VMs more memory for this.")}</span></label>
    <div class="f2"><div class="f"><label>Servers ${tip("One is enough to try things. Three keep the cluster running if one fails.")}</label>
        <select id="k_servers" onchange="k3sCountChanged()"><option value="1">1</option><option value="3">3</option></select></div>
      <div class="f"><label>Workers ${tip("VMs that run apps but not the cluster's control plane. They join the first server. Zero is fine: a server runs apps too.")}</label><input id="k_agents" type="number" min="0" max="6" value="2" oninput="k3sCountChanged()"></div></div>
    <div class="f2"><div class="f"><label>vCPUs each ${tip("vCPUs for every VM. Two is enough to try things; k3s itself needs little.")}</label><input id="k_cores" type="number" min="1" max="16" value="2"></div>
      <div class="f"><label>Memory each ${tip("Memory for every VM, like 4Gi. Longhorn and Homestead inside want at least 4Gi on the server.")}</label><input id="k_mem" value="4Gi"></div></div>
    <div class="f2"><div class="f"><label>Disk each (GiB) ${tip("Longhorn inside the cluster keeps its volumes here, so leave room for your apps.")}</label><input id="k_disk" type="number" min="20" value="40"></div>
      <div class="f"><label>Login password ${tip("For the ubuntu user on every VM, at the console or over SSH.")}</label><input id="k_pass" type="password" autocomplete="new-password"></div></div>
    <div class="f"><label>Image ${tip("The operating system every VM starts from. An Ubuntu cloud image works best: it runs cloud-init, which sets up the address, login and k3s.")}</label><select id="k_image">
      ${images.map(i => `<option value="image:${esc(i.namespace)}/${esc(i.name)}">Harvester image · ${esc(i.display)}</option>`).join("")}
      <option value="url" selected>Ubuntu 26.04.1 LTS minimal cloud image (downloaded${opts.harvester ? " as a Harvester image" : ""})</option></select></div>
    ${(opts.storage_classes || []).length ? `<div class="f"><label>Storage class ${tip("The storage class for each VM's disk on this cluster.")}</label><select id="k_sc">${opts.storage_classes.map(c => `<option ${c === opts.default_class ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></div>` : ""}
    <div class="sec">Network</div>
    <div class="f"><label>LAN network ${tip("The network bridged to your LAN the VMs join, so each has an address of its own there.")}</label><select id="k_net">${lan.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.vlan ? ` (VLAN ${esc(n.vlan)})` : ""}</option>`).join("") || '<option value="">none reaches the LAN</option>'}</select></div>
    ${vmAddressFields("k", opts, 3)}
    ${UI.actions(UI.cancel() + `<button class="btn pri" data-need="operator" onclick="k3sReview()" ${lan.length ? "" : "disabled"}>Review</button>`)}`;
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
// The review is of the form as it was sent: any change to the form drops it.
window.k3sInvalidateReview = () => { K3S.back(); };
window.k3sReviewReady = () => K3S.ready();
window.k3sReview = () => { if (!K3S.busy) return K3S.open(JSON.stringify(k3sBody())); };
let K3S_AGAIN = null;
window.k3sReviewAgain = () => {
  if (!K3S_AGAIN || K3S.busy) return;
  // From the conflict screen nothing else shows the plan is being read.
  if ($("#mbody")) $("#mbody").innerHTML = '<div id="k3sAgainLoading" class="empty"><span class="spin2"></span>Checking the VMs, hosts and capacity again…</div>';
  return K3S.open(K3S_AGAIN.input);
};
const K3S = UI.reviewFlow({
  preview: async input => {
    const plan = await api("/api/vm/k3s-cluster/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: input });
    if (!plan.config || !plan.capacity_token || typeof plan.capacity?.blocked !== "boolean" || !Array.isArray(plan.nodes))
      throw new Error("Complete VM batch review unavailable; nothing can be created yet.");
    return {...plan, input};
  },
  render: (plan, input) => {
    const cap = plan.capacity, name = JSON.parse(input).name || "k3s";
    const blockers = [...(cap.blockers || []), ...(cap.reasons || []), ...plan.nodes.filter(n => n.problem).map(n => `${n.name}: ${n.problem}`)];
    const warnings = cap.warnings || [];
    const stopped = cap.blocked || plan.ok === false;
    childModal(`Create cluster · ${name}`, [
      UI.lead(stopped ? `<b>${esc(name)}</b> can't be created until what is below is resolved.`
        : `Creates ${plan.nodes.length} VMs for <b>${esc(name)}</b>, which install ${esc(String(K3S_SETUPS[plan.setup] || plan.setup).split(" - ")[0])}${plan.kubevirt ? " and KubeVirt" : ""} and join in 10-15 minutes.${plan.url ? ` Its own Homestead then answers at <span class="mono">${esc(plan.url)}</span>.` : ""}`),
      UI.notices([...blockers.map(b => ["bad", b]), ...(stopped && !blockers.length ? [["bad", "The batch can't go ahead under the checked constraints."]] : []),
        ...warnings.map(w => ["warn", w])], stopped ? "Can't create" : "Check first"),
      UI.table([{ label: "VM" }, { label: "Role" }, { label: "Address" }], plan.nodes.map(n => [
        `<b>${esc(n.name)}</b><div class="mono xs dim">${esc(plan.config.macs?.[n.name] || "")}</div>`,
        n.role === "server" ? UI.chip("server") : UI.chip("worker"), `<span class="mono">${esc(n.address)}</span>`])),
      UI.facts([["Each VM", `${esc(plan.config.cores || 2)} vCPUs · ${esc(plan.config.memory || "4Gi")} memory · ${esc(plan.config.disk_gb || 40)} GiB disk`]]),
      (cap.nodes || []).length ? UI.section("Hosts", `<div class="dependency-list">${cap.nodes.map(n => `<div class="drow"><div class="dl mono">${esc(n.name)}</div><div class="dv">${n.metrics_available ? `${esc(n.baseline_gb)} GiB now → up to ${esc(n.upper_gb)} GiB` : "memory use unknown"}</div></div>`).join("")}</div>`) : "",
      UI.more("Details", `<p class="ui-help">Each address is recorded under its VM in IP addresses, and Jobs follows the VMs coming up.
        All the VMs share one capacity budget, new VMs waiting for launcher pods included; the placement below is an example, not a reservation.</p>
        ${(cap.example || []).length ? `<p class="ui-help">Example placement: ${cap.example.map(p => `${esc(p.service)} → ${esc(p.host)}`).join("; ")}</p>` : ""}
        <p class="ui-help">Memory warnings can be accepted, even above 100%, at the risk of out-of-memory restarts. Hardware, storage and scheduling blockers can't.</p>`),
      !stopped && warnings.length ? UI.ack("k_capacity_confirm", reviewAck("Creates"), { onchange: "k3sReviewReady()" }) : "",
      UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="k3sReviewBack()">Back to configuration</button><button class="btn pri" id="k_go" data-need="operator" onclick="k3sCreate()" disabled>Create cluster</button>`),
    ].join(""), true);
    k3sReviewReady();
  },
  failed: error => {
    toast(error.message, "bad");
    // From Review again the form is not behind it: say so where it was loading.
    if ($("#k3sAgainLoading")) $("#mbody").innerHTML = UI.error(error, { retry: "k3sReviewAgain()", dialog: true });
  },
  // The tickbox is there only when there is a warning to accept.
  blocked: plan => plan.ok === false || plan.capacity.blocked, ack: "k_capacity_confirm", button: "k_go",
  apply: async review => {
    const button = $("#k_go");
    button.disabled = true; button.textContent = "Creating VMs…";
    try {
      await api("/api/vm/k3s-cluster", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({...review.config, capacity_token:review.capacity_token, confirm_capacity:true}) });
      toast("Cluster VMs created - follow them coming up under Jobs", "ok");
      closeModal(); if (window.refreshOperations) refreshOperations(true); go("vms");
    } catch (e) {
      if (window.refreshOperations) refreshOperations(true);
      if (e?.status === 409) throw e; // conflict() below
      toast(e.message, "bad"); button.disabled = true; button.textContent = "Inspect batch before retrying";
      $("#mbody")?.insertAdjacentHTML("afterbegin", `<div class="note bad">${esc(e.message)}. Some VMs or their disks and Secrets may already exist. No request was repeated. Check Jobs and Virtual Machines before a new review.</div>`);
    }
  },
  // The server says the review is out of date: offer a fresh one of the same batch.
  conflict: (error, review) => {
    K3S_AGAIN = review;
    toast(error.message, "bad");
    if ($("#mbody")) $("#mbody").innerHTML = UI.callout("bad", "Not created as reviewed", `${esc(error.message)}. The review was out of date. Some VMs or their disks and Secrets may already exist; no request was repeated. Review again to see the batch as it is now.`)
      + UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="k3sReviewBack()">Back to configuration</button><button class="btn pri" onclick="k3sReviewAgain()">Review again</button>`);
    $("#mbody")?.querySelector(".ui-actions-end .btn.pri")?.focus();
  },
});
window.k3sReviewBack = () => { if (K3S.back()) modalBack(); };
// Without its button there is no review to send.
// A 409 for a review no longer on screen comes back here: said, nothing drawn.
window.k3sCreate = async () => { if ($("#k_go")) await K3S.apply().catch(error => toast(error.message, "bad")); };

/* ---------------- importing from Unraid or Proxmox ----------------
   Virtual machines › Import: each Unraid server's or Proxmox host's VMs as it runs them,
   one copied across with its settings mapped - the same servers, and the
   same verified SSH login, the container import uses. Disk images from a web
   address live here too. A VM still running there is shut down first (its power
   button), so its disk is copied as it was left. A VM is picked by its id: its
   name on Unraid, its VMID on Proxmox. */
const uvmSize = bytes => bytesText(bytes);
const uvmId = name => "uvm-" + String(name).replace(/[^a-z0-9-]/gi, "-");
/* What a server is called in a sentence: "Proxmox" or "Unraid". */
const uvmKind = source => (STATE.data.srcs || []).find(s => s.name === source)?.kind === "proxmox" ? "Proxmox" : "Unraid";
const uvmPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function viewVmImport() {
  if (platformLacks("kubevirt", "Virtual machines")) return;
  // What could not be read is named on the page, and no class or namespace
  // list is invented for the imports that use these (#620).
  const missed = [];
  const part = (path, label, fallback) => api(path).catch(e => { missed.push(`${label}: ${e.message}`); return fallback; });
  const [srcs, disks, namespaces, storageClasses] = await Promise.all([
    part("/api/sources", "import servers", []), part("/api/vm-disks", "VM disks", []),
    part("/api/namespaces", "namespaces", null), part("/api/storageclasses", "storage classes", null),
  ]);
  STATE.data.srcs = srcs; STATE.data.importNamespaces = namespaces; STATE.data.importStorageClasses = storageClasses;
  STATE.data.uvms = STATE.data.uvms || {};
  const servers = srcs;
  paint(`${UI.pageHeader(`Virtual machines`, `Bring VMs across from an Unraid server or a Proxmox host, or a disk image from a web address`, `${menuButton("＋ Import", [{ label: "Add a server", icon: "import", run: "srcAdd()", need: "admin" },
        { label: "A disk image from a URL", icon: "disk", run: "vmDiskImport()", need: "admin" }])}`)}
    ${UI.notices(missed.map(text => ["warn", text]), "Parts of this page could not be read")}
    ${servers.length ? servers.map(s => `<div class="sec between"><span>${esc(s.name)} ${tip(`${SOURCE_KINDS[s.kind] || s.kind} · ${s.user}@${s.host}`)}</span>
      ${actionBar([{ label: "Remove server", icon: "trash", run: `srcDel(${jsq(s.name)})`, need: "admin", danger: true,
        tip: "Remove it from Homestead's import servers. Nothing on it changes." }], { shown: 0, label: `More for ${s.name}` })}</div>
      <div class="card flat" id="${uvmId(s.name)}">${s.ssh_trust
        ? '<div class="empty"><span class="spin2"></span>asking it for its VMs</div>'
        : serviceRow("SSH key not verified", "", "Homestead only logs in to a server whose key you have checked.",
            `<button class="btn pri" data-need="admin" onclick="srcVerify(${jsq(s.name)})">Verify SSH key</button>`)}</div>`).join("")
      : `<div class="card flat">${serviceRow("No server yet", "", "Add an Unraid server or a Proxmox host once - its address and an SSH login - and its VMs can be imported.",
          '<button class="btn pri" data-need="admin" onclick="srcAdd()">＋ Add a server</button>')}</div>`}
    <div id="uvmCopies"></div>
    <div class="sec">Disk images ${tip("Disks brought in from a web address, Unraid or Proxmox. A finished one no VM uses yet can become a VM.")}</div>
    ${disks.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack dense"><thead><tr>
      <th>Disk</th><th>State</th><th>Size</th><th>Attached to</th><th></th></tr></thead><tbody>${disks.map(d => {
      const done = d.phase === "Succeeded", failed = ["Failed", "Error", "Unknown"].includes(d.phase);
      return `<tr><td><b>${esc(d.name)}</b><div class="dim xs mono">${esc(d.namespace)}</div></td>
        <td data-label="State">${UI.chip(done ? "ready" : String(d.phase || "").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase(), done ? "ok" : failed ? "bad" : "warn")}${failed && d.message ? `<div class="dim xs">${esc(d.message)}</div>` : ""}</td>
        <td class="mono" data-label="Size">${esc(d.capacity || "—")}</td>
        <td data-label="Attached to">${d.in_use ? esc((d.used_by || []).join(", ")) : '<span class="dim">not attached</span>'}</td>
        <td data-actions>${done && !d.in_use ? actionBar([{ label: "Create VM", run: `vmNew(${jsq(d.name)},${jsq(d.namespace)})`, need: "operator" }]) : ""}</td></tr>`;
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
    <th>Import</th><th>Progress</th><th></th></tr></thead><tbody>${ops.map(op => `<tr><td><b>${esc(op.title)}</b><div>${UI.chip(op.status, operationTone(op.status))}</div></td>
      <td data-label="Progress" style="min-width:200px"><div class="small">${esc(op.message || "")}</div>
        <span class="jobmeter" style="display:block;margin-top:6px"><span style="width:${Math.max(2, Math.min(100, op.progress || 0))}%"></span></span></td>
      <td data-actions>${actionBar([{ label: "Job details", icon: "log", run: `operationLog(${jsq(op.id)})` },
        // A cancel is reviewed first: what it removes, and that the source VM is kept.
        ...(operationActive(op) && op.cancellable ? [{ label: "Cancel", need: "operator", danger: true,
          run: `cancelOperation(${jsq(op.id)})`, tip: "Stop the import and remove the disks it was making. The source VM is unchanged." }] : []),
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
  if (!found.virsh && found.proxmox) {
    // A Proxmox host saved as Unraid or a Docker host: its type can't be changed, so it is added again.
    host.innerHTML = serviceRow("This is a Proxmox host", UI.chip("wrong type", "warn"),
      `${esc(name)} was added as ${esc(SOURCE_KINDS[STATE.data.srcs?.find(s => s.name === name)?.kind] || "Unraid")}. Remove it, then add it again with the type Proxmox VE to import its VMs.`,
      actionBar([{ label: "Remove server", run: `srcDel(${jsq(name)})`, need: "admin", danger: true },
        { label: "Add as Proxmox", run: "srcAdd('proxmox')", need: "admin", pri: true }]));
    if (window.applyRole) applyRole();
    return;
  }
  if (!found.virsh) { host.innerHTML = `<div class="dim small">This server runs no VMs: its VM manager (${found.kind === "proxmox" ? "qm" : "libvirt"}) is not there.</div>`; return; }
  // A list of like things: a stacked table, each row's buttons an actionBar.
  host.innerHTML = found.vms.length ? `<div class="tblwrap"><table class="tbl stack dense"><thead><tr>
      <th>VM</th><th>State</th><th>Machine</th><th>Disk</th><th></th></tr></thead><tbody>${found.vms.map(vm => {
    const off = vm.shut_off, disk = vm.disks[0];
    const machine = [`${vm.cores} vCPUs`, vm.memory.replace("Gi", " GiB").replace("Mi", " MiB"), vm.firmware === "uefi" ? "UEFI" : "BIOS"].map(esc).join(" · ");
    const action = !vm.ready ? `<span class="dim small">${esc(vm.problem)}</span>`
      : actionBar([off ? { label: "Import", run: `uvmImport(${jsq(name)},${jsq(vm.id)})`, need: "admin", pri: true }
        : { label: "Shut down", run: `uvmShutdown(${jsq(name)},${jsq(vm.id)},this)`, need: "admin", tip: `Shut it down on ${uvmKind(name)}, so its disk is copied as it was left` }]);
    return `<tr><td><b>${esc(vm.name)}</b>${found.kind === "proxmox" ? ` <span class="dim xs mono">${esc(vm.id)}</span>` : ""}${vm.os ? `<div class="dim xs">${esc(vm.os)}</div>` : ""}</td>
      <td data-label="State">${UI.chip(vm.state || "unknown", off ? "neutral" : "ok")}${off || !vm.ready ? "" : '<div class="med-t xs">shut it down to import it</div>'}</td>
      <td data-label="Machine" class="small">${machine}</td>
      <td data-label="Disk" class="small mono">${disk ? `${esc(disk.path.split("/").pop())} · ${esc(disk.size_gb)} GiB ${esc(disk.format)}${vm.disks.length > 1 ? ` + ${vm.disks.length - 1} more` : ""}` : "—"}</td>
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
    if ((STATE.data.uvms[source]?.vms || []).find(v => v.id === vm)?.shut_off) return;
  }
};

/* The import: three steps, with the picture of what maps across kept live. */
window.uvmImport = async (source, id) => {
  const vm = (STATE.data.uvms[source]?.vms || []).find(v => v.id === id);
  if (!vm) return;
  const name = vm.name, kind = uvmKind(source);
  const opts = await api("/api/vm/create-options").catch(e => ({ unread: e.message }));
  // Neither the VM's options nor a storage class are guessed (#620).
  const unread = opts.unread || (!opts.storage_classes?.length && STATE.data.importStorageClasses === null
    ? "the storage classes could not be read; refresh this page and try again" : "");
  if (unread) return modal(`Import ${name}`, UI.callout("bad", "Could not read what this import needs", esc(unread)) + UI.actions(UI.cancel("Close")));
  if (opts.cdi === false) return modal(`Import ${name}`, UI.lead("VM disks arrive through CDI, KubeVirt's disk importer, and this cluster does not have it. Install CDI from kubevirt.io, then import again.") + UI.actions(UI.cancel("Close")));
  window.__uvm = { source, vm };
  const namespaces = STATE.data.importNamespaces || [defaultNamespace()];
  const classes = opts.storage_classes?.length ? opts.storage_classes : (STATE.data.importStorageClasses || ["longhorn-r2"]);
  const facts = opts.storage_class_facts || {};
  const hw = [vm.firmware === "uefi" ? "UEFI" : "BIOS", vm.secure_boot ? "Secure Boot" : "", vm.tpm ? "TPM" : "", vm.hyperv ? "Hyper-V enlightenments" : ""].filter(Boolean).join(" · ");
  const settings = `
    ${UI.fields(UI.field("Name", `<input id="uvm_name" value="${esc(vm.slug)}" oninput="uvmPicture()">`, { tipHtml: tip("The VM's name here: lowercase letters, numbers and dashes. Its disks are named after it.") }),
      UI.field("Namespace", `<select id="uvm_ns">${namespaces.map(n => `<option ${n === defaultNamespace() ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`),
      UI.field("Cores", `<input id="uvm_cores" type="number" min="1" max="128" value="${vm.cores}" oninput="uvmPicture()">`),
      UI.field("Memory", `<input id="uvm_mem" value="${esc(vm.memory)}" oninput="uvmPicture()">`))}
    ${settingRow("Firmware", `As on ${kind}; change it later under the VM's Hardware.`, `<span class="mono small">${esc(hw)}</span>`)}`;
  const disks = `
    ${UI.field("Storage class", `<select id="uvm_sc" onchange="uvmRoom()">${classes.map(c => `<option value="${esc(c)}" ${facts[c]?.default ? "selected" : ""}>${esc(c)}${facts[c]?.default ? " (default)" : ""}</option>`).join("")}</select>`,
      { tipHtml: tip("The storage class for the disks. Longhorn keeps copies on several nodes, so the VM can run on any of them.") })}
    ${vm.disks.map((d, i) => settingRow(`${i ? `Disk ${i + 1}` : "Boot disk"} · ${esc(d.path.split("/").pop())}`,
      `${esc(d.size_gb)} GiB ${esc(d.format)}, ${esc(uvmSize(d.used))} in use · ${esc(d.bus.toUpperCase())} bus${d.unraid_bus !== d.bus ? ` (was ${esc(d.unraid_bus)})` : ""}`,
      i ? `<label class="toggle"><input type="checkbox" class="uvm-disk" data-i="${d.index}" checked onchange="uvmPicture();uvmRoom()"><span></span></label>` : '<span class="dim small">always</span>')).join("")}
    <p class="ui-help">The whole disk crosses the network, empty space too: ${esc(uvmSize(vm.disks.reduce((t, d) => t + d.size, 0)))} at about 110 MiB/s on gigabit.</p>`;
  const lan = (opts.network_details || []).filter(n => n.vms !== false);
  const network = `
    ${UI.field("Network", `<select id="uvm_net" onchange="uvmPicture()">${lan.map(n => `<option value="${esc(n.name)}">${esc(n.name)}${n.lan ? " · LAN" : ""}</option>`).join("")}<option value="pod">Pod network - reached through a Service</option></select>`,
      { tipHtml: tip(`${kind === "Proxmox" ? "vmbr0 on Proxmox" : "br0 on Unraid"} is the LAN: pick the LAN network here, and the VM is a machine on it like before. The pod network reaches it through a Service instead.`) })}
    ${vm.nic.mac ? settingRow("Keep its MAC address", `${esc(vm.nic.mac)} - so your router's DHCP reservation still gives it the same address.`,
      '<label class="toggle"><input type="checkbox" id="uvm_mac" checked onchange="uvmPicture()"><span></span></label>') : ""}
    ${UI.field("Network card", `<select id="uvm_nic" onchange="uvmPicture()">${["virtio", "e1000e", "e1000", "rtl8139"].map(m => `<option ${m === vm.nic.nic_model ? "selected" : ""}>${m}</option>`).join("")}</select>`,
      { tipHtml: tip(`As on ${kind}. VirtIO needs its driver in the guest; Windows has it if it used VirtIO on ${kind}.`) })}`;
  const behind = [...vm.dropped.map(d => `<p><b>${esc(d.what)}</b> ${esc(d.detail)}: ${esc(d.reason)}.</p>`), ...vm.notes.map(n => `<p>${esc(n)}.</p>`)];
  modal(`Import ${name}`, UI.lead(`From ${esc(source)}. The VM here is made stopped once its disks have arrived; ${esc(name)} on ${kind} is left as it is.`)
    + '<div id="uvm_picture"></div><div id="uvm_room"></div>'
    + stepper("uvm_steps", [{ title: "Settings", html: settings }, { title: "Disks", html: disks }, { title: "Network", html: network }],
      '<button class="btn pri" data-need="admin" id="uvm_go" onclick="uvmStart()">Import</button>')
    + (behind.length ? UI.more("What stays behind", behind.join("")) : ""), true);
  uvmPicture();
  uvmRoom();
};

/* Whether the chosen storage class can still place the disks, checked as the
   class and disks change: too little blocks the import, tight asks first. */
/* The flow keeps the newest check for an open dialog. The import itself is
   not sent through it: the server checks the room again, so a check is not
   used up, and Import stays open while none has answered. */
const UVM_ROOM = UI.reviewFlow({
  loading: ({ host }) => { host.innerHTML = '<p class="ui-help"><span class="spin2"></span>Checking the storage class has room for the disks…</p>'; },
  live: () => !!$("#uvm_room"),
  preview: ({ state, kept }) => uvmPost("/api/vms/import-unraid/room", { storage_class: $("#uvm_sc")?.value || "",
      disks: state.vm.disks.filter(d => kept.has(d.index)).map(d => ({ virtual: d.virtual, size_gb: d.size_gb })) })
    .catch(e => ({ level: "unknown", blockers: [], warnings: [`The room on the storage class could not be checked: ${e.message}`] })),
  render: (room, { host }) => {
    const blockers = room.blockers || [], warnings = room.warnings || [];
    host.innerHTML = blockers.length || warnings.length
      ? UI.notices([...blockers.map(b => ["bad", b]), ...warnings.map(w => ["warn", w])], blockers.length ? "Can't import" : "Check first")
        + (!blockers.length ? UI.ack("uvm_room_ok", reviewAck("Imports"), { onchange: "uvmRoomReady()" }) : "")
      : `<p class="ui-help">${esc(room.storage_class)} has room: these disks take ${esc(room.needed_gb)} of the ${esc(room.free_gb)} GiB it can still place.</p>`;
    uvmRoomReady();
  },
});
window.uvmRoom = async () => {
  const state = window.__uvm, host = $("#uvm_room");
  if (!state || !host) return;
  const kept = new Set([0, ...$$(".uvm-disk").filter(c => c.checked).map(c => +c.dataset.i)]);
  await UVM_ROOM.open({ state, host, kept });
};
window.uvmRoomReady = () => {
  const go = $("#uvm_go"), room = UVM_ROOM.review;
  // Until the check answers, starting is left to the server's own check.
  const ready = !room || (!(room.blockers || []).length && (!$("#uvm_room_ok") || $("#uvm_room_ok").checked));
  if (go && go.textContent === "Import") go.disabled = !ready;
  return ready;
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
    return { what: n > 1 ? `Disk ${n}` : "Disk", from: `${d.path.split("/").pop()} · ${d.size_gb} GiB`, to: `${dv} · ${d.size_gb} GiB` };
  };
  const rows = [
    { what: "CPU", from: `${vm.cores} vCPUs`, to: `${$("#uvm_cores")?.value || vm.cores} vCPUs` },
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
  if (!uvmRoomReady()) return toast(UVM_ROOM.review?.blockers?.[0] || `Tick ${reviewAck("Imports")} to accept the warning first`, "bad");
  const body = { source: state.source, vm: state.vm.id, name: $("#uvm_name").value.trim(), namespace: $("#uvm_ns").value,
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
  } catch (e) { toast(e.message, "bad"); go.textContent = "Import"; go.disabled = false; uvmRoomReady(); }
};

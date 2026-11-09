/* Homestead views: a host's disks. Split from views-storage.js (#911); a classic script. */
/* ---------------- node disks ----------------
   Every disk on a node, what it is for, and the Longhorn disks on it - with
   adding a disk to Longhorn and taking one out. */
const DISK_ROLE = { longhorn: ["Longhorn", "ok"], system: ["system", "neutral"], provisioning: ["being added", "warn"],
  "in use": ["in use", "neutral"], unused: ["unused", "info"] };

function diskRowsHtml(node, disks, harvester) {
  return disks.map(d => {
    const [word, tone] = DISK_ROLE[d.role] || [d.role, "neutral"];
    const lh = d.longhorn.map(x => {
      const pct = x.size_gb ? Math.round(x.used_gb / x.size_gb * 100) : 0;
      return `<div class="disk-lh">
        <div class="between"><span class="mono xs">${esc(x.path)}${x.type === "block" ? ` ${UI.chip("V2", "info")}` : ""}</span>
          <span class="mono xs">${esc(sizePair(x.used_gb, x.size_gb))} used · ${esc(sizeText(x.allocated_gb))} allocated · ${x.replicas} cop${x.replicas === 1 ? "y" : "ies"}</span></div>
        ${meter(pct, "", "disk")}
        ${x.reserved_gb > 0 ? UI.more('Capacity breakdown',UI.facts([
          [x.type==='block'?'Store capacity':'Filesystem capacity',esc(sizeText(x.capacity_gb))],
          ['Reserved by Longhorn',esc(sizeText(x.reserved_gb))],
          ['Capacity after reserve',esc(sizeText(x.size_gb))]
        ])+`<p class="ui-help">The usage bar does not include Longhorn’s reserve.${x.type==='block'?'':' The filesystem capacity is smaller than the device because the filesystem uses some space itself.'} Making the system partition larger does not make this store larger.</p>`) : ''}
        ${!x.ready ? `<div class="badtext xs disk-why">${esc(x.missing || x.problem || "Longhorn reports that this disk is not ready")}</div>` : ""}
        <div class="row disk-tags">${(x.tags || []).map(t => UI.chip(t, "info")).join("")
          || '<span class="dim xs">No tags</span>'}
          <button class="linkish xs" data-need="admin" data-tags="${esc(JSON.stringify(x.tags || []))}"
            onclick="diskTags(${jsq(node)},${jsq(x.id)},JSON.parse(this.dataset.tags))">${(x.tags || []).length ? "Edit tags" : "Add tags"}</button></div>
        <div class="row disk-lh-acts">
          ${x.evicting ? UI.chip("moving copies off", "warn") : !x.scheduling ? UI.chip("no new copies", "neutral") : ''}
          ${d.v2_preparation ? UI.button('V2 preparation',`diskV2Watch(${jsArg(d.v2_preparation.id)})`,{kind:'pri',attrs:'data-need="admin"'}) : actionBar([
            !x.ready && {label:'Replace failed disk',need:'admin',run:`diskRetire(${jsq(node)},${jsq(x.id)})`},
            !harvester && (x.path?.replace(/\/+$/,'')==='/var/lib/longhorn'||x.path?.startsWith('/mnt/homestead-retired-')) && {label:'Move Longhorn runtime',need:'admin',run:`runtimeMoveOpen(${jsq(node)},${jsq(x.id)})`},
            {label:x.scheduling?'Stop new copies':'Allow new copies',need:'admin',run:`diskAction('scheduling',${jsq(node)},${jsq(x.id)},${!x.scheduling})`},
            x.ready && x.replicas && !x.evicting && {label:'Move copies off',need:'admin',run:`diskAction('evict',${jsq(node)},${jsq(x.id)},true)`},
            x.evicting && {label:'Stop moving copies',need:'admin',run:`diskAction('evict',${jsq(node)},${jsq(x.id)},false)`},
            !harvester && x.ready && x.type!=='block' && {label:'Prepare for V2',need:'admin',run:`diskV2Open(${jsq(node)},${jsq(x.id)})`},
            !x.replicas && !x.scheduling && {label:'Remove from Longhorn',need:'admin',kind:'danger',run:`diskAction('remove',${jsq(node)},${jsq(x.id)})`}
          ],{shown:1,label:'Disk actions'})}</div></div>`;
    }).join("");
    return `<div class="disk-card">
      <div class="between"><div><b class="mono">${esc(d.device || "Longhorn")}</b> <span class="dim xs">${esc(d.model || "")}</span>
          <div class="dim xs">${esc(sizeText(d.size_gb))}${d.kind ? ` · ${esc(d.kind)}` : ""}${d.mounts.length ? ` · ${esc(d.mounts.slice(0, 3).join(", "))}` : ""}</div></div>
        <div class="row">${d.system ? UI.chip("system", "neutral") : ""}${d.system && d.role === "system" ? "" : UI.chip(word, tone)}
          ${d.can_add && !d.v2_preparation ? `<button class="btn sm pri" data-need="admin" onclick="diskAdd(${jsq(node)},${jsq(d.blockdevice.name)},${jsq(d.path)},${d.needs_wipe})">Add to Longhorn</button>` : ""}
          ${!harvester && !d.v2_preparation && d.role === "unused" && d.device ? `<button class="btn sm pri" data-need="admin" onclick="diskSetup(${jsq(node)},${jsq("/dev/" + d.device)})">Add to Longhorn</button>` : ""}
          ${!harvester && d.system ? actionBar([{label:"Adjust allocation",need:"admin",run:`diskAllocationOpen(${jsq(node)})`},{label:"Use free space",need:"admin",run:`diskOsSpace(${jsq(node)})`}],{shown:1,label:"Storage allocation"}) : ""}</div></div>
      ${lh}${d.v2_preparation && !d.longhorn.length ? UI.actions(UI.button('V2 preparation',`diskV2Watch(${jsArg(d.v2_preparation.id)})`,{kind:'pri',attrs:'data-need="admin"'})) : ''}</div>`;
  }).join("") + (harvester ? "" : `<button class="btn sm" data-need="admin" style="margin-top:8px" onclick="diskAdd(${jsq(node)})">＋ Add a disk to Longhorn</button>`)
    + (harvester && !disks.some(d => d.can_add) ? '<div class="dim xs" style="margin-top:8px">Every disk Harvester found on this host is in use. A new disk appears after it is connected and Harvester has scanned it.</div>' : "");
}

/* A node's own tags, for classes that keep replicas on some nodes. */
function nodeTagsLine(node, inv) {
  const tags = (inv.node_tags || {})[node];
  if (!tags) return "";
  return `<div class="row disk-tags node-tags"><span class="dim xs">Host tags</span>
    ${tags.map(t => UI.chip(t, "neutral")).join("") || '<span class="dim xs">None</span>'}
    <button class="linkish xs" data-need="admin" data-tags="${esc(JSON.stringify(tags))}"
      onclick="nodeTags(${jsq(node)},JSON.parse(this.dataset.tags))">${tags.length ? "Edit" : "Add"}</button></div>`;
}

/* Tags are words Longhorn matches a storage class against: a class that
   asks for "ssd" puts its replicas only on disks tagged ssd. */
const TAG_IDEAS = ["ssd", "nvme", "hdd", "fast", "bulk"];
function tagEditor(title, about, current, known, save, unread = "") {
  const ideas = [...new Set([...known, ...TAG_IDEAS])].filter(t => !current.includes(t));
  modal(title, `<p class="muted small" style="margin-top:0">${about}</p>
    ${unread ? `<div class="dim small" style="margin-bottom:8px">The tags already in use could not be read. ${esc(String(unread).replace(/\.\s*$/, ""))}. Only the common tags are suggested.</div>` : ""}
    <div class="f"><label>Tags</label>
      <input type="text" id="tg_text" value="${esc(current.join(", "))}" placeholder="ssd, fast" autocomplete="off"></div>
    ${ideas.length ? `<div class="row" style="gap:6px;flex-wrap:wrap;margin:-4px 0 14px"><span class="dim xs">Add</span>${ideas.map(t =>
      `<button class="tag linkish" onclick="tagAdd(${jsq(t)})">＋ ${esc(t)}</button>`).join("")}</div>` : ""}
    ${UI.actions(`<button class="btn pri" id="tg_save">Save tags</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  $("#tg_save").onclick = () => save($("#tg_text").value.split(/[\s,]+/).filter(Boolean));
  $("#tg_text").focus();
}
window.tagAdd = tag => {
  const input = $("#tg_text"), tags = input.value.split(/[\s,]+/).filter(Boolean);
  if (!tags.includes(tag)) tags.push(tag);
  input.value = tags.join(", ");
};
async function tagsSave(path, body, node, reopen) {
  try {
    const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); closeModal(); STATE.data.disks = null;
    if (reopen) lhDisks(); else setTimeout(() => disksRepaint(node), 500);
  } catch (e) { toast(e.message, "bad"); }
}
// The tags in use are only suggestions: unread, the editor still opens and says so (#620).
window.diskTags = async (node, disk, current) => {
  const inv = await loadDisks().catch(e => ({ unread: e.message }));
  // Opened from the Disks list (a dialog), it goes back there; from a node's page, it stays.
  const reopen = !$("#modal").classList.contains("hidden");
  tagEditor(`Tags · ${disk}`, `A storage class can keep its volume copies on disks with a tag. For example, tag the fast disks
      <b>ssd</b> and create a class that asks for ssd.${inv.harvester ? " Harvester stores a disk's tags with the disk, as its own dashboard does." : ""}`,
    current, inv.disk_tags || [], tags => tagsSave("/api/disks/tags", { node, disk, tags }, node, reopen), inv.unread);
};
window.nodeTags = async (node, current) => {
  const inv = await loadDisks().catch(e => ({ unread: e.message }));
  const reopen = !$("#modal").classList.contains("hidden");
  tagEditor(`Host tags · ${node}`, "A storage class can keep its volume copies on hosts with a tag, as well as or instead of disks with a tag.",
    current, inv.all_node_tags || [], tags => tagsSave("/api/disks/node-tags", { node, tags }, node, reopen), inv.unread);
};

async function loadDisks(force = false) {
  if (!force && STATE.data.disks && Date.now() - STATE.data.disksAt < 8000) return STATE.data.disks;
  STATE.data.disks = await api("/api/disks");
  STATE.data.disksAt = Date.now();
  return STATE.data.disks;
}

/* The node page's disk card. */
window.nodeDisksPaint = async node => {
  const host = $("#nodeDisks");
  if (!host) return;
  try {
    const inv = await loadDisks(true);
    const disks = inv.nodes[node] || [];
    host.innerHTML = nodeTagsLine(node, inv) + (disks.length ? diskRowsHtml(node, disks, inv.harvester)
      : '<div class="dim small">No disks have been reported yet. The node probe reports which disks this host has.</div>');
    // Each disk's partition table, where the host's OS has been read.
    if (window.nodePartitionsPaint) nodePartitionsPaint(node);
    if (window.applyRole) applyRole();
  } catch (e) { host.innerHTML = `<div class="dim small">${esc(e.message)}</div>`; }
};

/* Every node's disks in one place, from Volumes or Settings. */
window.lhDisks = async () => {
  modal("Disks", '<div class="empty"><span class="spin2"></span>Reading every host</div>', true);
  try {
    const inv = await loadDisks(true);
    $("#mbody").innerHTML = `<p class="dim small" style="margin-top:0">${inv.harvester
      ? "Harvester lists each host's disks. Adding a disk lets Harvester format it and give it to Longhorn, as its own dashboard does."
      : "Longhorn stores data in folders where a disk is mounted, or on a raw device for the V2 engine."}</p>
      ${Object.entries(inv.nodes).map(([node, disks]) => `<div class="sec">${esc(node)}</div>${nodeTagsLine(node, inv)}${diskRowsHtml(node, disks, inv.harvester)}`).join("")
        || '<div class="empty small">No host has reported its disks yet.</div>'}`;
    window.__disksModal = true;
    if (window.applyRole) applyRole();
  } catch (e) { $("#mbody").innerHTML = UI.error(e, { retry: "lhDisks()", dialog: true }); }
};

function disksRepaint(node) {
  if ($("#nodeDisks")) nodeDisksPaint(node);
  else if (window.__disksModal && !$("#modal").classList.contains("hidden")) lhDisks();
}

window.diskAction = async (action, node, disk, value) => {
  if (action === "remove" && !(await ask(`Remove ${disk} on ${node} from Longhorn? It holds no volume copies, and its files are left in place.`, { title: "Remove disk", ok: "Remove disk", danger: true }))) return;
  if (action === "evict" && value && !(await ask(`Move every volume copy off ${disk}? Longhorn first rebuilds each copy on another disk, which copies its data.`, { title: "Move volume copies", ok: "Move copies" }))) return;
  const body = { node, disk };
  if (action === "scheduling") body.allow = value;
  if (action === "evict") body.on = value;
  try {
    const r = await api(`/api/disks/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); STATE.data.disks = null; setTimeout(() => disksRepaint(node), 800);
  } catch (e) { toast(e.message, "bad"); }
};

window.diskAdd = (node, blockdevice = "", path = "", needsWipe = false) => {
  const v2 = STATE.data.lhcap?.v2?.enabled;
  const back = $("#nodeDisks") ? null : true;
  childModal(`Add a disk · ${node}`, `
    ${blockdevice ? `<p>Harvester formats <b class="mono">${esc(path)}</b> and gives it to Longhorn, which starts placing volume copies on it.</p>`
      : `${diskMountGuide(path)}
        <div class="f"><label>Disk location ${tip("For the V1 engine, the folder where a formatted disk is mounted on the host, such as /mnt/disk2. For V2, the raw device, such as /dev/sdb.")}</label>
        <input id="da_path" class="mono" placeholder="/mnt/disk2" value="${path ? `/mnt/${esc(path.replace("/dev/", ""))}` : ""}"></div>`}
    <div class="f"><label>Engine</label><select id="da_engine">
      <option value="v1">V1 — ${blockdevice ? "formatted and mounted" : "a mounted folder"}</option>
      ${v2 ? `<option value="v2">V2 (SPDK) — the raw device</option>` : ""}</select>
      ${v2 ? "" : '<div class="dim xs">The V2 engine is off. Turn it on under Settings › Hardware and storage to add a V2 disk.</div>'}</div>
    ${blockdevice ? `<label class="switch"><input type="checkbox" id="da_wipe" ${needsWipe ? "" : "disabled"}> Erase it first
      ${needsWipe ? '<span class="badtext xs">· It already has a filesystem or partitions, which are destroyed.</span>' : '<span class="dim xs">· It is blank.</span>'}</label>` : ""}
    ${UI.actions(`<button class="btn pri" onclick="diskAddGo(${jsq(node)},${jsq(blockdevice)})">Add disk</button>
      <button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}`);
};
/* Off Harvester: set a whole disk up for Longhorn from here. Homestead looks
   at it on its host first (homestead_disk_setup), then offers only what is
   safe for what it found: format a blank disk, keep one that already holds
   Longhorn data, or - typed to confirm - erase one holding something else.
   The system disk, and a mounted one, are refused. */
window.diskSetup = async (node, device) => {
  const v2 = STATE.data.lhcap?.v2?.enabled;
  childModal(`Add ${device} · ${node}`, '<div class="empty"><span class="spin2"></span> Checking the disk on its host…</div>');
  let f;
  try {
    f = await api("/api/disks/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node, device }) });
  } catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The disk could not be checked.", esc(e.message)) + UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' })); return; }
  STATE.data.diskFacts = f;
  const what = {
    blank: "It is blank.",
    longhorn: `It already holds Longhorn data${f.replicas ? `, with ${f.replicas} volume cop${f.replicas === 1 ? "y" : "ies"}` : ""}.`,
    data: `It holds a ${esc(f.fstype)} filesystem with ${f.entries} item${f.entries === 1 ? "" : "s"} at the top level.`,
    partitioned: `It has ${f.partitions.length} partition${f.partitions.length === 1 ? "" : "s"} (${esc(f.partitions.map(p => `${p.name}${p.fstype ? ` ${p.fstype}` : ""}`).join(", "))}).`,
    system: `It is this host's system disk (${esc(f.mounts.join(", "))}).`,
    mounted: `It is mounted at ${esc(f.mounts.join(", "))}.`,
    "longhorn-v2": `Longhorn's V2 engine stores data on it (${esc((f.longhorn_block || []).join(", "))}). Move its volume copies off and remove it from Longhorn before setting it up again.`,
    held: `It cannot be erased:<ul class="small">${(f.reasons || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul>`,
    lvm: `It holds LVM from an earlier installation that nothing on this host uses: ${esc((f.lvm || []).map(g =>
      `volume group ${g.vg || g.uuid}${g.lvs.length ? ` (${g.lvs.map(l => l.name).join(", ")})` : ""}`).concat((f.orphan_pvs || []).map(p => `an LVM label on ${p}`)).join("; "))}${
      f.partitions.length ? `, on ${f.partitions.length} partition${f.partitions.length === 1 ? "" : "s"}` : ""}.`,
    missing: esc(f.error || "The disk was not found."),
  }[f.state];
  const choice = (value, label, detail, checked) => `<label class="disk-choice"><input type="radio" name="ds_mode" value="${value}" ${checked ? "checked" : ""} onchange="diskSetupChanged()">
    <span><b>${label}</b><small>${detail}</small></span></label>`;
  const modes = f.choices || [];
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.callout(["system", "mounted", "missing", "held", "longhorn-v2"].includes(f.state) ? "bad" : "info",
      { html: `<span class="mono">${esc(device)}</span> · ${f.size_gb} GiB${f.by_id ? ` · <span class="mono dim xs">${esc(f.by_id.replace("/dev/disk/by-id/", ""))}</span>` : ""}` },
      `${/<ul/.test(what) ? `<div>${what}</div>` : `<p>${what}</p>`}${(f.multipath || []).length ? `<p class="small">multipathd is using it (${esc(f.multipath.map(m => m.name).join(", "))}). Setting it up releases it and adds its WWID to the blacklist in /etc/multipath.conf, so that multipathd ignores this disk. Nothing else changes.</p>` : ""}`)}
    ${modes.length ? `
      ${v2 ? `<div class="f"><label>Engine</label><select id="ds_engine" onchange="diskSetupChanged()"><option value="v1">V1 · formatted and mounted</option><option value="v2">V2 (SPDK) · the raw device</option></select></div>` : ""}
      <div class="disk-choices" id="ds_modes">
        ${modes.includes("format") ? choice("format", "Format the disk", `As a filesystem mounted at <span class="mono">${esc(f.mount_point)}</span>, for Longhorn.`, true) : ""}
        ${modes.includes("import") ? choice("import", "Keep its Longhorn data", `Mounted unchanged at <span class="mono">${esc(f.mount_point)}</span>. Volume copies from another cluster appear in Longhorn as orphaned data. Volumes are restored from their backups.`, true) : ""}
        ${modes.includes("wipe") ? choice("wipe", "Erase and prepare the disk", "Turns off and removes its old volume groups, erases the LVM labels, filesystem signatures and partition table, makes the host read the disk again, checks that it is blank, then formats it. Everything on it is destroyed.", true) : ""}
        ${modes.includes("erase") ? choice("erase", "Erase and format the disk", "Everything on it is destroyed.", !modes.includes("import")) : ""}</div>
      <div class="f" id="ds_fs_row"><label>Filesystem</label><select id="ds_fs"><option value="ext4">ext4</option><option value="xfs">XFS</option></select></div>
      <div id="ds_confirm_row">${UI.typedConfirm({ id: "ds_confirm", expect: device, oninput: "diskSetupChanged()", help: "This erases the disk." })}</div>
      <p class="dim xs">Mounted safely. The empty folder is locked, so nothing is written to the system disk if the drive fails. fstab refers to it
        by UUID with <span class="mono">nofail</span>, so the host still starts without it, and a copy of fstab is saved first. The mount is checked
        before Longhorn uses the disk. New disks are tagged <span class="mono">ssd</span> or <span class="mono">hdd</span>.</p>
      ${UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Set up disk", `diskSetupGo(${jsArg(node)},${jsArg(device)})`, { kind: "pri", id: "ds_go", attrs: 'data-need="admin"' }))}`
      : UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' }))}</div>`;
  diskSetupChanged();
  if (window.applyRole) applyRole();
};

/* The OS drive's unused LVM space, as a Longhorn disk of its own. */
/* Space nothing uses, given to Longhorn without touching what is there: a
   logical volume in the OS drive's volume group, or a new partition in a
   disk's unallocated space - made live, the table saved first. V1 gets a
   filesystem of its own; V2 the raw volume or partition. */
window.diskOsSpace = async node => {
  childModal(`Free space · ${node}`, '<div class="empty"><span class="spin2"></span> Checking the host\'s disks…</div>');
  let f;
  try {
    f = await api("/api/disks/os-space", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
  } catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The disks could not be checked.", esc(e.message)) + UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' })); return; }
  STATE.data.osSpace = f;
  const v2 = !!STATE.data.lhcap?.v2?.enabled;
  const lvmFree = f.vg && !f.error && f.usable_gb >= 5;
  const sources = [
    ...(lvmFree && (!f.taken?.v1 || (v2 && !f.taken?.v2))
      ? [["lvm", `${f.vg} · ${f.usable_gb} GiB free, ${f.reserve_gb} GiB reserved for the system`, f.usable_gb]] : []),
    ...(f.regions || []).map(r => [`part:${r.disk}:${r.start}`, `/dev/${r.disk} · ${r.size_gb} GiB unallocated · a new partition`, r.size_gb]),
  ];
  if (f.problem || !sources.length) {
    $("#mbody").innerHTML = `<div class="ui-stack">${UI.callout("warn", "No free space to use",
      `${esc(((f.problem || f.lvm_problems?.v1 || "no free space") + "").replace(/^./, c => c.toUpperCase()))}.`)}
      ${UI.more("What Homestead does and does not do", `<p class="small">It uses only space that nothing else uses: free space in the
        system's LVM volume group, or space after the last partition of a GPT disk. It never shrinks or moves a partition or
        filesystem of a running system. ext4 cannot shrink while mounted, and XFS cannot shrink at all. That needs a rescue start-up.</p>`)}
      ${UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' }))}</div>`;
    return;
  }
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.facts([["Root filesystem free", `${f.root_free_gb} GiB`], f.vg ? ["Volume group", `<span class="mono">${esc(f.vg)}</span> · ${f.size_gb} GiB`] : null])}
    <div class="f"><label>Space</label><select id="os_source" onchange="diskOsSpaceChanged(true)">${sources.map(([value, label]) =>
      `<option value="${esc(value)}">${esc(label)}</option>`).join("")}</select></div>
    ${v2 ? `<div class="f"><label>Engine</label><select id="os_engine" onchange="diskOsSpaceChanged()">
      <option value="v1">V1 · its own filesystem, mounted</option><option value="v2">V2 (SPDK) · the raw volume or partition</option></select></div>` : ""}
    <div class="f"><label>Size (GiB)</label><input id="os_size" type="number" min="5"></div>
    <p class="small" id="os_what"></p>
    <div id="os_confirm_row" hidden></div>
    <p class="dim xs">This space is on the system drive. A volume copy here and another copy elsewhere on the same drive do not protect against a drive failure.</p>
    ${UI.actions(UI.button("Back", "modalBack()", { attrs: 'data-dialog-dismiss="true"' }) + UI.button("Create and add disk", `diskOsSpaceGo(${jsArg(node)})`, { kind: "pri", id: "os_go", attrs: 'data-need="admin"' }))}</div>`;
  window.__osSources = Object.fromEntries(sources.map(([value, , max]) => [value, max]));
  diskOsSpaceChanged(true);
  if (window.applyRole) applyRole();
};
window.diskOsSpaceChanged = (first = false) => {
  const f = STATE.data.osSpace || {}, source = $("#os_source").value, engine = $("#os_engine")?.value || "v1";
  const max = (window.__osSources || {})[source] || 5, size = $("#os_size");
  size.max = max;
  if (first || +size.value > max || !+size.value) size.value = max;
  const part = source.startsWith("part:"), disk = part ? source.split(":")[1] : "";
  const taken = !part && f.taken?.[engine];
  $("#os_what").innerHTML = taken ? `<b>${esc(f.vg)}/${engine === "v2" ? "longhorn-v2" : "longhorn"}</b> already exists. Choose the other engine or unallocated space.`
    : part ? `A new GPT partition after the last one on <span class="mono">/dev/${esc(disk)}</span>, created while the system runs. The
      partition table is saved to <span class="mono">/var/lib/homestead</span>, the space is checked again, and only the new
      partition is reported to the kernel. ${engine === "v2" ? "It is given to the V2 engine as a raw device, by its PARTUUID." : `It is formatted as ext4 and mounted safely at <span class="mono">/mnt/${esc(disk)}-longhorn</span>.`}`
    : `A separate logical volume, <span class="mono">${esc(f.vg)}/${engine === "v2" ? "longhorn-v2" : "longhorn"}</span>, ${engine === "v2"
      ? "given to the V2 engine as a raw device" : `formatted as ext4 and mounted safely at <span class="mono">${esc(f.mount_point)}</span>`}. Nothing that exists is resized.`;
  $("#os_confirm_row").hidden = !part;
  // Drawn again only when the disk changes, so a half-typed name stays.
  if (part && $("#os_confirm")?.dataset.typedExpect !== `/dev/${disk}`)
    $("#os_confirm_row").innerHTML = UI.typedConfirm({ id: "os_confirm", expect: `/dev/${disk}`, oninput: "diskOsSpaceGate()", help: "A new entry is added to its partition table." });
  window.__osTaken = !!taken;
  diskOsSpaceGate();
};
window.diskOsSpaceGate = () => {
  const part = !$("#os_confirm_row")?.hidden;
  if ($("#os_go")) $("#os_go").disabled = !!window.__osTaken || (part && !typedConfirmMatches("os_confirm"));
};
window.diskOsSpaceGo = async node => {
  const go = $("#os_go");
  if (go) { go.disabled = true; go.textContent = "Creating…"; }
  try {
    const r = await api("/api/disks/os-space/use", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, size_gb: +$("#os_size").value, source: $("#os_source").value,
        engine: $("#os_engine")?.value || "v1", confirm: ($("#os_confirm")?.value || "").trim() }) });
    toast(r.detail, "ok"); STATE.data.disks = null; modalBack(); setTimeout(() => disksRepaint(node), 800);
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Create and add disk"; }
  }
};

window.diskSetupChanged = () => {
  const f = STATE.data.diskFacts || {};
  const v2 = $("#ds_engine")?.value === "v2";
  const mode = $("input[name=ds_mode]:checked")?.value || "";
  if ($("#ds_modes")) $("#ds_modes").hidden = v2;
  if ($("#ds_fs_row")) $("#ds_fs_row").hidden = v2 || mode === "import";
  // Formatting a blank disk still asks for the device: it is a format all the same.
  if ($("#ds_confirm_row")) $("#ds_confirm_row").hidden = v2 ? f.state === "blank" : mode === "import";
  // Erasing waits for the device's name; importing what is on it does not.
  if ($("#ds_go")) $("#ds_go").disabled = !$("#ds_confirm_row")?.hidden && !typedConfirmMatches("ds_confirm");
};

window.diskSetupGo = async (node, device) => {
  const v2 = $("#ds_engine")?.value === "v2";
  const body = { node, device, engine: v2 ? "v2" : "v1", mode: $("input[name=ds_mode]:checked")?.value || "",
    fstype: $("#ds_fs")?.value || "ext4", confirm: ($("#ds_confirm")?.value || "").trim() };
  if (!$("#ds_confirm_row")?.hidden && !typedConfirmMatches("ds_confirm")) return;
  const go = $("#ds_go");
  if (go) { go.disabled = true; go.textContent = "Setting up…"; }
  try {
    const r = await api("/api/disks/setup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); STATE.data.disks = null; modalBack(); setTimeout(() => disksRepaint(node), 800);
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Set up disk"; }
  }
};

/* Off Harvester, a disk is mounted on the host by hand before Longhorn is
   given the folder - and how it is mounted decides what a dead drive does at
   the next boot. A plain fstab line makes systemd wait for the drive and drop
   the host to an emergency shell when it never comes; nofail lets the host
   start without it. The folder is made immutable while empty, so when the
   drive is missing nothing can write into the folder on the system disk:
   Longhorn marks the disk failed instead of filling the system disk. */
function diskMountGuide(device) {
  const dev = device || "/dev/sdX";
  const dir = device ? `/mnt/${device.replace("/dev/", "")}` : "/mnt/disk2";
  const lines = [
    `sudo mkfs.ext4 ${dev}                # erases the disk`,
    `sudo mkdir -p ${dir} && sudo chattr +i ${dir}`,
    `echo "UUID=$(sudo blkid -s UUID -o value ${dev}) ${dir} ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2" | sudo tee -a /etc/fstab`,
    `sudo mount ${dir}`];
  return `<div class="note" style="margin-bottom:12px"><b>Mount the disk on the host first.</b> Run as root on ${device ? "that machine" : "the machine that has the disk"}:
    <pre class="mono xs" style="white-space:pre-wrap;margin:8px 0">${esc(lines.join("\n"))}</pre>
    <span class="dim xs"><b>nofail</b> lets the machine start when this drive has failed or is missing. Without it, start-up stops at an emergency
    shell. The empty folder is locked (<span class="mono">chattr +i</span>), so that nothing is written to the system disk when the drive
    is missing. Longhorn then marks the disk as failed, and Homestead offers to replace it.</span></div>`;
}

/* Replacing a failed disk: the review says what happens to every volume
   that had a copy on it before anything is let go of. */
window.diskRetire = async (node, disk) => {
  childModal(`Replace failed disk · ${node}`, '<div class="empty"><span class="spin2"></span>Reading its volume copies</div>');
  let p;
  try {
    p = await api("/api/disks/retire/plan", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, disk }) });
  } catch (e) { $("#mbody").innerHTML = `${UI.callout("bad", "", `${esc(e.message)}`)}`; return; }
  const word = { "only-copy": ["only copy", "bad"], waits: ["waits for the new disk", "warn"], elsewhere: ["rebuilt on another disk", "ok"] };
  $("#mbody").innerHTML = `
    <p style="margin-top:0"><b class="mono">${esc(p.path)}</b> on ${esc(node)}${p.problem ? ` · <span class="badtext">${esc(p.problem)}</span>` : ""}</p>
    <p class="small">Longhorn keeps a failed disk, and the volume copies on it, until told otherwise. This releases them, so
      each volume is rebuilt from a healthy copy. It then removes the disk from Longhorn${p.harvester_device ? " and Harvester" : ""},
      so that the new drive can be added in its place.</p>
    ${p.volumes.length ? `<div class="sec">${p.volumes.length} volume${p.volumes.length === 1 ? "" : "s"} had a copy on it</div>
      <div class="rc-uses">${p.volumes.map(v => `<div><b>${esc(v.claim)}</b> ${UI.chip(word[v.outcome][0], word[v.outcome][1])}
        <span class="dim xs">${esc(v.why)}</span></div>`).join("")}</div>` : '<div class="dim small">No volume copies were on it.</div>'}
    ${p.only_copies ? `${UI.callout("bad", { html: `${p.only_copies} volume${p.only_copies === 1 ? "" : "s"} had the only copy on this disk.` }, `If the drive is only disconnected, connect it again instead, and the data returns with it. Otherwise, restore
      ${p.only_copies === 1 ? "the volume" : "the volumes"} from a backup under Data protection. Continuing leaves ${p.only_copies === 1 ? "the volume" : "the volumes"} unchanged and keeps the disk,
      unless ${p.only_copies === 1 ? "it is" : "they are"} given up:
      <label class="switch" style="margin-top:8px"><input type="checkbox" id="dr_force" onchange="$('#dr_confirm_row').hidden=!this.checked;diskRetireGate()"> Give up ${p.only_copies === 1 ? "the volume" : "the volumes"} · the data is lost</label>
      <div id="dr_confirm_row" hidden>${UI.typedConfirm({ id: "dr_confirm", expect: disk, oninput: "diskRetireGate()" })}</div>`, { attrs: 'style="margin-top:12px"' })}` : ""}
    ${UI.actions(`<button class="btn pri" id="dr_go" data-need="admin" onclick="diskRetireGo(${jsq(node)},${jsq(disk)})">Replace disk</button>
      <button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>`)}`;
  if (window.applyRole) applyRole();
};
/* Giving up the only copies waits for the disk's name to be typed. */
window.diskRetireGate = () => {
  if ($("#dr_go")) $("#dr_go").disabled = !!$("#dr_force")?.checked && !typedConfirmMatches("dr_confirm");
};
window.diskRetireGo = async (node, disk) => {
  const force = !!$("#dr_force")?.checked;
  if (force && !typedConfirmMatches("dr_confirm")) return;
  try {
    await api("/api/disks/retire", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node, disk, force, confirm: force ? $("#dr_confirm").value.trim() : "" }) });
    toast("Replacing the failed disk. Progress is shown under Jobs.", "ok");
    STATE.data.disks = null; modalBack(); if (window.refreshOperations) refreshOperations(true);
    setTimeout(() => disksRepaint(node), 1500);
  } catch (e) { toast(e.message, "bad"); }
};

window.diskAddGo = async (node, blockdevice) => {
  const body = { node, engine: $("#da_engine").value };
  if (blockdevice) {
    body.blockdevice = blockdevice; body.wipe = !!$("#da_wipe")?.checked;
    if (body.wipe && !(await ask("Erase everything on this disk? This cannot be undone.", { title: "Erase disk", ok: "Erase disk", danger: true }))) return;
  } else body.path = $("#da_path").value.trim();
  try {
    const r = await api("/api/disks/add", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(r.detail, "ok"); STATE.data.disks = null; modalBack(); setTimeout(() => disksRepaint(node), 800);
  } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  DISK_ROLE, diskRowsHtml, nodeTagsLine, TAG_IDEAS, tagEditor, tagsSave, loadDisks, disksRepaint, diskMountGuide
};

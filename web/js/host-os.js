/* A k3s or RKE2 host's own operating system, on its node page.

   Harvester looks after its hosts itself; on k3s and RKE2 Homestead reads
   each host every six hours through the host helper (homestead_host_os.py):
   the distribution and kernel, updates waiting and which are security fixes,
   whether a restart is needed, failed services, the clock and the root
   filesystem, and each disk's partition table, which the Disks card draws
   to scale. Installing updates is the host's own package manager, run as a
   job; a restart afterwards is Host actions, with its drain review. */

const HOST_OS = { cache: {} };

async function hostOsLoad(node, force = false) {
  if (!force && HOST_OS.cache[node]) return HOST_OS.cache[node];
  const r = await api("/api/node/os?name=" + encodeURIComponent(node));
  HOST_OS.cache[node] = { applies: r.applies, facts: (r.hosts || {})[node] || null };
  return HOST_OS.cache[node];
}

function hostOsAge(at) {
  if (!at) return "never";
  const seconds = Math.max(0, Date.now() / 1000 - at);
  return seconds < 90 ? "just now" : fmtAgo(seconds);
}

function hostOsBody(node, f) {
  if (!f) return `${UI.lead("Not read yet: the leader reads each host within ten minutes of starting, then every six hours.")}
    ${UI.actions(UI.button("Check now", `hostOsCheck(${jsArg(node)})`, { id: "hosCheck", attrs: 'data-need="admin"' }))}`;
  const updates = f.updates || [], sec = f.security || 0;
  const updateText = f.upgrading ? UI.chip("installing now", "info")
    : updates.length ? `${updates.length} waiting ${sec ? UI.chip(`${sec} security`, "warn") : ""}`
    : f.package_manager ? "up to date" : "no package manager Homestead knows";
  const failed = f.failed_units || [];
  const facts = UI.facts([
    ["System", esc(f.os || "—")],
    ["Kernel", `<span class="mono">${esc(f.kernel || "—")}</span>`],
    ["Updates", updateText],
    ["Restart", f.reboot ? `${UI.chip("needed", "warn")} <span class="dim xs">${esc(f.reboot_for || "to finish an update")}</span>` : "not needed"],
    ["Services", failed.length ? `${UI.chip(`${failed.length} failed`, "bad")} <span class="mono xs">${esc(failed.join(", "))}</span>` : "all running"],
    ["Clock", f.ntp === false ? UI.chip("not synchronised", "warn") : f.ntp ? "synchronised" : "—"],
    ["Root filesystem", `${UI.meter({ now: f.root_used_pct || 0, warnAt: 90 })}<span class="dim xs">${f.root_used_pct || 0}% of ${sizeText(f.root_total_gb || 0)}</span>`],
  ]);
  const list = updates.length ? UI.more(`The ${updates.length} update${updates.length === 1 ? "" : "s"}`,
    `<div class="hos-updates">${updates.slice(0, 300).map(u => `<span class="tag ${u.security ? "warn" : ""}">${esc(u.name)}</span>`).join("")}
      ${updates.length > 300 ? `<span class="dim xs">and ${updates.length - 300} more</span>` : ""}</div>`) : "";
  const lists = f.lists_at ? ` · package lists from ${hostOsAge(f.lists_at)}` : "";
  const last = f.last_upgrade?.at ? ` · last update ${f.last_upgrade.ok ? "installed" : "failed"} ${hostOsAge(f.last_upgrade.at)}` : "";
  return `${facts}${list}
    <div class="dim xs" style="margin-top:8px">Read ${hostOsAge(f.at)}${lists}${last}</div>
    ${UI.actions([
      f.reboot ? UI.button("Restart…", `nodeActions(${jsArg(node)})`, { attrs: 'data-need="admin"' }) : "",
      UI.button("Check now", `hostOsCheck(${jsArg(node)})`, { id: "hosCheck", attrs: 'data-need="admin"' }),
      updates.length && !f.upgrading ? UI.button(`Install ${updates.length} update${updates.length === 1 ? "" : "s"}`,
        `hostOsUpgrade(${jsArg(node)})`, { kind: "pri", attrs: 'data-need="admin"' }) : "",
    ].join(""))}`;
}

window.nodeHostOsPaint = async (node, force = false) => {
  const card = $("#nodeHostOs");
  if (!card) return;
  try {
    const r = await hostOsLoad(node, force);
    // Harvester's hosts are Harvester's to update.
    if (!r.applies) { card.remove(); return; }
    card.querySelector(".hos-body").innerHTML = hostOsBody(node, r.facts);
    if (window.applyRole) applyRole();
    if ($("#nodeDisks") && r.facts) nodePartitionsPaint(node);
  } catch (e) { card.querySelector(".hos-body").innerHTML = `<div class="dim small">${esc(e.message)}</div>`; }
};

window.hostOsCheck = async node => {
  const button = $("#hosCheck");
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  try {
    const r = await api("/api/node/os/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    HOST_OS.cache[node] = { applies: true, facts: r.facts };
    toast(`${node}: ${r.facts.summary?.text || "read"}`, "ok");
  } catch (e) { toast(e.message, "bad"); }
  nodeHostOsPaint(node);
};

window.hostOsUpgrade = async node => {
  const f = HOST_OS.cache[node]?.facts || {};
  const count = (f.updates || []).length;
  const ok = await ask(`Install ${count} update${count === 1 ? "" : "s"} on ${node}? Its package manager (${f.package_manager || "the host's"}) refreshes its lists and upgrades everything waiting. Workloads keep running; ${f.reboot || (f.updates || []).some(u => /^linux-(image|modules)|^kernel/.test(u.name)) ? "a kernel update then needs a restart, from Host actions" : "a restart is only needed if an update asks for one"}.`,
    { title: "Install updates", ok: "Install updates" });
  if (!ok) return;
  try {
    const r = await api("/api/node/os/upgrade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node }) });
    if (r.operation && window.noteOperation) noteOperation(r.operation);
    toast(r.detail, "ok");
    HOST_OS.cache[node] = null;
    nodeHostOsPaint(node, true);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------- partition tables, to scale ---------- */
const HOS_SYSTEM = /^\/(boot(\/efi)?|usr|var|home)?$/;
function hostOsRole(p) {
  if (!p.name) return "free";
  const mounts = p.mounts || [];
  if (mounts.some(m => HOS_SYSTEM.test(m))) return "sys";
  if (mounts.some(m => /longhorn|^\/mnt\//.test(m))) return "data";
  if ((p.holds || []).includes("lvm") || /LVM|raid|crypto/i.test(p.fstype || "")) return "lvm";
  return p.fstype ? "other" : "free";
}
function hostOsBytes(bytes) { return sizeText((Number(bytes) || 0) / 1024 ** 3); }

function partitionMap(d) {
  const size = d.size || 1, segs = [];
  let at = 0;
  for (const p of d.partitions || []) {
    if (p.start - at > size * 0.004) segs.push({ size: p.start - at });
    segs.push(p);
    at = Math.max(at, p.start + p.size);
  }
  if (!(d.partitions || []).length) segs.push(d.fstype ? { name: d.name, size, fstype: d.fstype, mounts: d.mount ? [d.mount] : [] } : { size });
  else if (size - at > size * 0.004) segs.push({ size: size - at });
  const label = s => s.name ? `${s.name} · ${hostOsBytes(s.size)}${s.fstype ? ` · ${s.fstype}` : ""}${(s.mounts || []).length ? ` · ${s.mounts.join(", ")}` : (s.holds || []).length ? ` · holds ${s.holds.join(", ")}` : ""}`
    : `unallocated · ${hostOsBytes(s.size)}`;
  return `<div class="pmap-disk"><div class="between"><span class="small"><b class="mono">${esc(d.name)}</b>
      <span class="dim xs">${hostOsBytes(d.size)} · ${d.table ? esc(d.table.toUpperCase()) : (d.partitions || []).length ? "partitioned" : d.fstype ? "no partition table" : "blank"}</span></span></div>
    <div class="pmap" role="img" aria-label="${esc(`${d.name}: ${segs.map(label).join("; ")}`)}">${segs.map(s =>
      `<span class="pmap-seg ${hostOsRole(s)}" style="flex:${Math.max(s.size / size, 0.012)} 1 0" data-tip="${esc(label(s))}">
        ${s.size / size >= 0.08 ? `<span>${esc(s.name ? (s.mounts?.[0] || s.fstype || s.name) : "free")}</span>` : ""}</span>`).join("")}</div>
    ${segs.length > 1 ? `<div class="dim xs mono pmap-list">${segs.map(s => esc(label(s))).join("<br>")}</div>` : ""}</div>`;
}

window.nodePartitionsPaint = async node => {
  const host = $("#nodeDisks");
  if (!host) return;
  const f = HOST_OS.cache[node]?.facts;
  const disks = (f?.disks || []).filter(d => d.size > 0);
  host.querySelector(".pmaps")?.remove();
  if (!disks.length) return;
  host.insertAdjacentHTML("beforeend", `<div class="pmaps">${UI.more(`Partition tables · ${disks.length} disk${disks.length === 1 ? "" : "s"}`,
    `${disks.map(partitionMap).join("")}
     <div class="pmap-key dim xs"><span class="pmap-seg sys"></span>system <span class="pmap-seg data"></span>Longhorn and data
       <span class="pmap-seg lvm"></span>LVM or RAID <span class="pmap-seg other"></span>other <span class="pmap-seg free"></span>unallocated
       · as read ${hostOsAge(f.at)}</div>`, true)}</div>`);
};

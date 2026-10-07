/* Data protection — Longhorn recurring jobs, groups, snapshots, backups */

const TASK_ICON = {
  "snapshot": "◷", "snapshot-force-create": "◉", "snapshot-cleanup": "⌫",
  "snapshot-delete": "✕", "backup": "☁", "backup-force-create": "☁",
  "filesystem-trim": "⇅",
};

/* Ready-made protection: the jobs a sensible policy needs, made in one go.
   A plan's jobs are ordinary jobs afterwards, edited or deleted one by one. */
const PROTECT_PLANS = [
  { id: "snapshots", title: "Snapshots", blurb: "Hourly snapshots kept for a day, daily ones kept for a week. Stored on the volumes themselves: quick to roll back to, and no use if the volume itself is lost.",
    jobs: [{ name: "hourly-snapshot", task: "snapshot", cron: "0 * * * *", retain: 24, concurrency: 2 },
      { name: "daily-snapshot", task: "snapshot", cron: "0 2 * * *", retain: 7, concurrency: 2 }] },
  { id: "backups", title: "Snapshots and backups", blurb: "A daily snapshot kept for a week, a daily backup kept for two weeks and a weekly one kept for two months, uploaded to the backup target so they outlive the cluster.",
    needsTarget: true,
    jobs: [{ name: "daily-snapshot", task: "snapshot", cron: "0 2 * * *", retain: 7, concurrency: 2 },
      { name: "daily-backup", task: "backup", cron: "30 2 * * *", retain: 14, concurrency: 1 },
      { name: "weekly-backup", task: "backup", cron: "0 3 * * 0", retain: 8, concurrency: 1 }] },
  { id: "tidy", title: "Housekeeping", blurb: "A weekly trim, so space an app has freed is given back to the disks, and a weekly purge of the system snapshots Longhorn takes during rebuilds.",
    jobs: [{ name: "weekly-trim", task: "filesystem-trim", cron: "0 4 * * 6", retain: 0, concurrency: 1 },
      { name: "weekly-cleanup", task: "snapshot-cleanup", cron: "30 4 * * 6", retain: 0, concurrency: 1 }] },
];

/* When a time on the cluster's clock is, for the person reading. */
function localTime(date) {
  return date.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
}
function nextRun(cron) {
  const at = CRON.next(cron, new Date(), 1)[0];
  if (!at) return '<span class="dim">never</span>';
  const minutes = Math.max(1, Math.round((at - Date.now()) / 60000));
  const soon = minutes < 60 ? `in ${minutes} min` : minutes < 48 * 60 ? `in ${Math.round(minutes / 60)} h` : `in ${Math.round(minutes / 1440)} days`;
  return `<span class="small" data-tip="${esc(localTime(at))}, your time">${soon}</span>`;
}
function lastRunText(j) {
  if (j.running) return '<span class="pill slim med">running</span>';
  if (!j.last_run) return '<span class="dim">not yet</span>';
  const ago = fmtAgo(Math.max(0, (Date.now() - Date.parse(j.last_run)) / 1000));
  return j.last_failed ? `<span class="pill slim crit" data-tip="The last run did not finish; its pod's log in longhorn-system says why">failed</span> <span class="dim xs">${esc(ago)}</span>`
    : `<span class="small">${esc(ago)}</span>`;
}

/* A backup has to be written somewhere, and where decides what it is good for:
   a bucket inside this cluster moves workloads to another cluster, and is no
   use at all as the only copy of anything. Both facts belong on screen. */
function objectStoreCard(store, target) {
  if (!store.deployed) {
    return `<div class="note between" style="margin-bottom:18px"><span>
      <b>No backup storage.</b> Longhorn writes volume backups to an S3 bucket, and nothing here
      provides one — so backups, and moving a workload to another cluster, have nowhere to go.</span>
      <button class="btn sm pri" data-need="admin" onclick="objectStoreSetup()">Set up storage</button></div>`;
  }
  const pointed = store.longhorn?.pointed === true;
  const switching = ["pending", "applying"].includes(store.longhorn?.state);
  return `<div class="card flat" style="margin-bottom:18px">
    <div class="between node-section-head"><div><div class="ctitle">Backup storage</div>
      <div class="csub">An S3 server (RustFS) on a Longhorn volume, holding every backup this cluster writes</div></div>
      <div class="row"><span class="pill ${store.ready ? "ok" : "med"}">${store.ready ? "serving" : "starting"}</span>
        <button class="btn sm danger" data-need="admin" onclick="objectStoreRemove()">Remove</button></div></div>
    <div class="about-grid" style="margin-top:12px">
      <div><span>Endpoint</span><b class="mono">${esc(store.endpoint || "—")}</b></div>
      <div><span>Bucket</span><b class="mono">${esc(store.bucket)}</b></div>
      <div><span>Size</span><b>${store.size_gb ? store.size_gb + " GB" : "—"}</b></div>
      <div><span>Longhorn target</span><b>${pointed ? "pointed here" : switching ? "switching" : "not pointed here"}</b></div>
    </div>
    ${pointed ? "" : `<div class="note warn" style="margin-top:12px"><b>Longhorn is not writing here.</b>
      ${store.longhorn?.endpoint ? `Its current endpoint is <span class="mono">${esc(store.longhorn.endpoint)}</span>. ` : ""}
      ${esc(store.longhorn?.detail || "Backups will not reach this bucket until the target is changed.")}
      ${switching ? '<div class="dim xs">Homestead retries automatically for up to ten minutes, even with this page closed.</div>' : ""}
      <div class="row"><button class="btn sm" data-need="admin" onclick="objectStorePoint()">${switching || store.longhorn?.state === "failed" ? "Retry target switch" : "Point Longhorn at it"}</button>
        <button class="btn sm" data-need="admin" onclick="objectStoreSetup()">Storage settings</button></div></div>`}
    ${store.reachable_off_cluster ? "" : `<div class="note" style="margin-top:12px">
      <b>Only this cluster can read it.</b> The store has no LAN address, so another cluster cannot
      restore from these backups. Give its Service an address to migrate workloads elsewhere.</div>`}
    <div class="note" style="margin-top:12px"><b>This is not disaster recovery.</b> The bucket lives on
      the same Longhorn storage it protects, so it survives a lost workload or a bad upgrade, not a lost
      cluster. It exists so a workload can be rebuilt somewhere else.</div>
  </div>`;
}

window.objectStoreSetup = () => {
  const store = STATE.data.objectStore || {};
  modal(store.deployed ? "Backup storage settings" : "Set up backup storage", `
  <p>Runs an S3 server (RustFS) on a Longhorn volume and points Longhorn's backups at it. Volume backups,
    and moving a workload to another cluster, both read and write here.</p>
  <div class="f2"><div class="f"><label>Size (GB) ${tip("Holds every volume backup this cluster keeps. Longhorn backups are incremental, so this is usually far smaller than the volumes themselves.")}</label>
    <input id="os_size" type="number" min="5" max="16384" value="${store.size_gb || 100}" ${store.deployed ? "disabled" : ""}></div>
    <div class="f"><label>LAN address ${tip("Another cluster reads backups over this address. Left blank, the store shares Homestead's address and answers on its own port there; give it one of its own to keep its traffic apart.")}</label>
      <input id="os_ip" type="text" class="mono" value="${esc(store.reachable_off_cluster && store.endpoint ? new URL(store.endpoint).hostname : "")}" placeholder="Homestead's shared address" data-ipam></div>
    <div class="f"><label>Port ${tip("The port the store answers on. Pick another if an app on that address already uses 9000; the next port up is its console.")}</label>
      <input id="os_port" type="number" min="1" max="65534" value="${store.port || 9000}" class="mono"></div></div>
  <div class="note"><b>Keep an independent backup.</b> This store shares the cluster’s disks. Use it for migration staging, not your only copy.</div>
  ${UI.actions(`<button class="btn pri" data-need="admin" onclick="objectStoreDeploy()">${store.deployed ? "Save storage settings" : "Set up storage"}</button>
    <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};

window.objectStoreDeploy = async () => {
  const body = { size_gb: +$("#os_size").value || 100, lb_ip: $("#os_ip").value.trim(), port: +($("#os_port")?.value || 0) || undefined };
  try {
    const result = await api("/api/objectstore/deploy", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.longhorn?.detail || `backup storage is starting at ${result.endpoint}`, "ok");
    closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

window.objectStorePoint = async () => {
  try {
    const result = await api("/api/objectstore/longhorn", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(result.detail || "Backup target switch requested", result.pending ? "info" : "ok");
    resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

window.objectStoreRemove = async () => {
  if (!(await ask("Remove the backup storage?" + String.fromCharCode(10, 10)
      + "The volume holding the backups is kept, so this can be undone."))) return;
  try {
    const result = await api("/api/objectstore/remove", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep_data: true }) });
    toast(result.detail || "backup storage removed", "ok");
    resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

async function viewProtect() {
  if (platformLacks("longhorn", "Data protection")) return;
  const [d, objects, backupVolumes] = await Promise.all([
    api("/api/lh/overview"),
    api("/api/objectstore").catch(() => ({ deployed: false })),
    api("/api/lh/backupvolumes").catch(() => []),
  ]);
  STATE.data.lh = d;
  STATE.data.objectStore = objects;
  STATE.data.lhBackupVolumes = backupVolumes;
  const tgt = d.target || {};
  const cover = d.total ? Math.round(d.protected / d.total * 100) : 0;

  paint(`${UI.pageHeader(`Data protection`, `Longhorn recurring jobs, snapshot groups and backups across ${d.total} volume${d.total === 1 ? "" : "s"}`, `
        ${moreMenu([{ label: "Backup target", icon: "shield", run: "lhTarget()", need: "admin" }, { label: "Plans", icon: "clock", run: "lhPlans()", need: "operator" }])}
        <button class="btn pri" data-need="operator" onclick="lhJob()">＋ New job</button>
      `)}

  ${objects.deployed ? "" : objectStoreCard(objects, tgt)}
  ${summaryLine("protect", [
      `<span class="tag ${cover === 100 ? "ok" : cover ? "warn" : "bad"}">${cover}% covered</span> ${d.protected} of ${d.total} volumes`,
      objects.deployed ? `Backup storage <span class="tag ${objects.ready ? "ok" : "warn"}">${objects.ready ? "serving" : "starting"}</span>` : "",
      tgt.configured ? `Target <span class="tag ${tgt.available ? "ok" : "bad"}">${tgt.available ? "reachable" : "unavailable"}</span>` : '<span class="tag warn">no backup target</span>',
      `${d.backed_up ?? 0} backed up off the cluster`],
    `${objects.deployed ? objectStoreCard(objects, tgt) : ""}
  <div class="grid g3" style="margin-bottom:18px">
    <div class="card flat">
      <div class="ctitle">Coverage</div><div class="csub">Volumes a snapshot or backup job keeps copies of</div>
      <div class="row" style="margin-top:12px;gap:18px;align-items:flex-end">
        <div class="bignum">${cover}<span class="unit">%</span></div>
        <div class="csub" style="padding-bottom:6px">${d.protected} of ${d.total} protected ·
          ${d.backed_up ?? 0} backed up off the cluster</div>
      </div>
      ${meter(cover, 'style="margin-top:12px"')}
      ${d.unprotected.length ? `<div style="margin-top:12px">
        <div class="dim xs" style="margin-bottom:6px">NOT COVERED</div>
        ${d.unprotected.slice(0, 6).map(v => `<span class="tag warn">${esc(v)}</span>`).join("")}
        ${d.unprotected.length > 6 ? `<span class="tag">+${d.unprotected.length - 6}</span>` : ""}</div>` : ""}
    </div>

    <div class="card flat">
      <div class="ctitle">Backup target</div><div class="csub">Storage outside the cluster that receives backups</div>
      ${tgt.configured ? `
        <div class="drow"><div class="dl">URL</div><div class="dv mono small">${esc(tgt.url)}</div></div>
        <div class="drow"><div class="dl">Status</div><div class="dv">
          <span class="pill ${tgt.available ? "ok" : "crit"}">${tgt.available ? "reachable" : "unavailable"}</span></div></div>
        ${tgt.reason ? `<div class="dim xs" style="margin-top:8px">${esc(tgt.reason)}</div>` : ""}
        ${tgt.secret_missing ? `<div class="note warn" style="margin-top:10px"><b>Its keys are missing.</b>
          The Secret ${esc(tgt.secret)} is not in longhorn-system, so backups cannot sign in.
          <button class="btn sm" data-need="admin" onclick="lhTarget()">Enter the keys</button></div>` : ""}`
      : `<div class="note" style="margin-top:12px"><b>No backup target.</b> Snapshots work without one —
         they live on the volume. <b>Backups need somewhere to go</b>: an NFS share or S3 bucket.
         Until you set one, backup jobs will fail.</div>`}
    </div>

    <div class="card flat">
      <div class="between"><div><div class="ctitle">Groups</div><div class="csub">A job protects every volume in its groups</div></div>
        <button class="btn sm" data-need="operator" onclick="lhGroup()">＋ Group</button></div>
      <div style="margin-top:12px">${d.groups.map(g => {
        const n = d.volumes.filter(v => v.groups.includes(g)).length;
        return `<span class="tag ${g === "default" ? "info" : ""}" role="button" tabindex="0" style="cursor:pointer"
          data-tip="Edit ${esc(g)}" onclick="lhGroup(${jsq(g)})">${esc(g)} · ${n}</span>`;
      }).join("")}</div>
      <div class="dim xs" style="margin-top:12px">Longhorn puts every volume without a group in
        <span class="mono">default</span>. Give some volumes a plan of their own with a group.</div>
    </div>
  </div>
`)}

  <div id="restore-tests" class="restore-tests"><div class="sec">Restore tests</div><div class="empty small"><span class="spin2"></span> Finding each app's newest backups…</div></div>

  <div class="sec">Recurring jobs</div>
  ${d.jobs.length ? `<div class="cardlist">${d.jobs.map(j => `<div class="card flat wcard">
    <div class="between">
      <div class="row" style="gap:10px;min-width:0">
        <div class="av n3" style="font-size:15px">${TASK_ICON[j.task] || "◷"}</div>
        <div style="min-width:0"><div style="font-weight:680">${esc(j.name)}</div>
          <div class="dim xs">${esc(j.task)}</div></div>
      </div>
      <span class="pill ${j.covers ? "ok" : "med"}">${j.covers} vol</span>
    </div>
    <div class="wmeta">
      <div><div class="dim xs">SCHEDULE</div><div class="small" data-tip="cron ${esc(j.cron)} on the cluster's clock (UTC)">${esc(CRON.describe(j.cron))}</div></div>
      <div><div class="dim xs">NEXT</div><div>${nextRun(j.cron)}</div></div>
      <div><div class="dim xs">LAST</div><div>${lastRunText(j)}</div></div>
      <div><div class="dim xs">KEEPS</div><div class="mono small">${KEEPS(j.task) ? j.retain : "—"}</div></div>
    </div>
    <div class="dim xs">${esc(j.desc)} · ${j.groups.length ? `groups ${j.groups.map(g => `<span class="tag">${esc(g)}</span>`).join("")}` : "no groups"} · ${j.concurrency} at a time</div>
    <div class="row wacts">${actionBar([
      { label: "Run now", icon: "play", run: `lhRun(${jsq(j.name)})`, need: "operator", disabled: j.running },
      { label: "Edit", icon: "edit", run: `lhJob(${esc(JSON.stringify(j))})`, need: "operator" },
      { label: "Volumes it covers", icon: "disk", run: `lhCovered(${jsq(j.name)})` },
      { label: "Delete", icon: "trash", run: `lhJobDel(${jsq(j.name)})`, need: "admin", danger: true }], { label: `More actions for ${j.name}` })}
    </div></div>`).join("")}</div>`
  : `<div class="empty">No recurring jobs yet. A plan sets up a sensible policy in one go -
     <a onclick="lhPlans()" style="cursor:pointer;text-decoration:underline">choose one</a> - or a daily snapshot of the <span class="mono">default</span>
     group is the usual starting point: <a onclick="lhQuickStart()" style="cursor:pointer;text-decoration:underline">set that up</a>.</div>`}

  <div class="sec between"><span>Groups</span>
    <button class="btn sm" data-need="operator" onclick="lhGroup()">＋ New group</button></div>
  <div class="cardlist">${(d.group_rows || []).map(groupCard).join("")}</div>

  <div class="sec">Volumes</div>
  <div class="card flat pad0"><div class="tblwrap"><table data-sort="protect" class="tbl stack"><thead><tr>
    <th>Volume</th><th>Size</th><th>Health</th><th>Groups</th><th>Protected by</th><th data-nosort>Last backup</th><th></th>
  </tr></thead><tbody>
  ${d.volumes.map(v => `<tr>
    <td><b>${esc(v.pvc || v.name.slice(0, 16))}</b><div class="dim xs mono">${esc(v.namespace)}</div></td>
    <td class="mono">${v.size_gb} GB</td>
    <td><span class="pill ${v.robustness === "healthy" ? "ok" : v.robustness === "degraded" ? "med" : "crit"}">${esc(v.robustness || "?")}</span></td>
    <td>${v.groups.map(g => `<span class="tag ${g === "default" ? "info" : ""}">${esc(g)}</span>`).join("") || '<span class="dim">—</span>'}</td>
    <td>${(v.protected_by || []).map(j => `<span class="tag ok">${esc(j)}</span>`).join("")
      || '<span class="tag warn" data-tip="No snapshot or backup job covers it">nothing</span>'}</td>
    <td class="small dim">${v.last_backup_at ? esc(v.last_backup_at.replace("T", " ").replace("Z", "")) : "never"}</td>
    <td>${actionBar([{ label: "Snapshots", run: `lhSnaps(${jsq(v.name)},${jsq(v.pvc || v.name)})` },
      { label: "Protect", run: `lhAssign(${jsq(v.name)},${jsq(v.pvc || v.name)})`, need: "operator" }])}</td></tr>`).join("")}
  </tbody></table></div></div>

  <div class="sec">Backups</div>
  ${backupVolumes.length ? `<div class="card flat pad0"><div class="tblwrap"><table data-sort="backupvols" class="tbl stack"><thead><tr>
    <th>Volume</th><th>Backups</th><th data-nosort>Last backup</th><th>Size</th><th></th></tr></thead><tbody>
    ${backupVolumes.map(b => `<tr>
      <td><b>${esc(b.pvc || b.name)}</b>${b.exists ? "" : ' <span class="tag warn" data-tip="The volume is gone; its backups are all that is left of it">volume deleted</span>'}
        <div class="dim xs mono">${esc(b.name)}</div></td>
      <td class="mono">${b.count}</td>
      <td class="small dim">${b.last_backup_at ? esc(b.last_backup_at.replace("T", " ").replace("Z", "")) : "—"}</td>
      <td class="mono">${b.size_mb ? (b.size_mb >= 1024 ? `${(b.size_mb / 1024).toFixed(1)} GB` : `${b.size_mb} MB`) : "—"}</td>
      <td>${actionBar([{ label: "Backups", run: `lhBackupList(${jsq(b.name)},${jsq(b.pvc || b.name)})` }])}</td></tr>`).join("")}
  </tbody></table></div></div>`
  : `<div class="empty">${tgt.configured ? "No backups on the target yet." : "No backup target, so no backups."}</div>`}`);
  loadRestoreTests();
}

/* A group, what is in it and what protects it. */
function groupCard(g) {
  const d = STATE.data.lh || {}, jobs = (d.jobs || []).filter(j => g.jobs.includes(j.name));
  const vols = (d.volumes || []).filter(v => g.volumes.includes(v.name));
  const guards = jobs.filter(j => /^(snapshot|backup)(-force-create)?$/.test(j.task));
  return `<div class="card flat wcard">
    <div class="between"><div style="min-width:0"><div style="font-weight:680">${esc(g.name)}
      ${g.name === "default" ? '<span class="tag info">volumes with no other group</span>' : ""}</div>
      <div class="dim xs">${vols.length} volume${vols.length === 1 ? "" : "s"} · ${jobs.length} job${jobs.length === 1 ? "" : "s"}</div></div>
      <span class="pill ${guards.length ? "ok" : vols.length ? "warn" : ""}">${guards.length ? "protected" : vols.length ? "no snapshots" : "empty"}</span></div>
    <div class="small" style="margin-top:10px">${jobs.map(j => `<span class="tag" data-tip="${esc(CRON.describe(j.cron))}">${TASK_ICON[j.task] || "◷"} ${esc(j.name)}</span>`).join("")
      || '<span class="dim">no jobs cover it</span>'}</div>
    <div class="dim xs" style="margin-top:8px">${vols.slice(0, 8).map(v => esc(v.pvc || v.name)).join(", ")}${vols.length > 8 ? ` +${vols.length - 8}` : ""}</div>
    <div class="row wacts">
      <button class="btn sm" data-need="operator" onclick="lhGroup(${jsq(g.name)})">Edit</button>
      ${g.name === "default" ? "" : `<button class="btn sm danger" data-need="admin" onclick="lhGroupDel(${jsq(g.name)})">Delete</button>`}
    </div></div>`;
}

/* ---------------- job editor ---------------- */
window.lhJob = (j) => {
  const d = STATE.data.lh || { groups: ["default"], tasks: {} };
  j = j || { name: "", task: "snapshot", cron: "0 2 * * *", retain: 7, concurrency: 1, groups: ["default"] };
  modal(j.name ? "Edit job · " + j.name : "New recurring job", `
    <div class="f2">
      <div class="f"><label>Name</label><input type="text" id="lj_name" value="${esc(j.name)}"
        ${j.name ? "readonly" : ""} placeholder="daily-snapshot"></div>
      <div class="f"><label>Task</label><select id="lj_task" onchange="lhTaskChanged()">
        ${Object.entries(d.tasks || {}).map(([k, v]) =>
          `<option value="${esc(k)}" ${j.task === k ? "selected" : ""}>${esc(v)}</option>`).join("")}
      </select></div>
    </div>
    ${scheduleBuilder(j.cron)}
    <div class="f2">
      <div class="f" id="lj_retain_f"><label>Retain</label><input type="number" id="lj_retain" value="${j.retain || 7}" min="1" max="250">
        <div class="dim xs" style="margin-top:6px">How many to keep before the oldest is removed</div></div>
      <div class="f"><label>Run in parallel</label><input type="number" id="lj_conc" value="${j.concurrency}" min="1" max="10"></div>
    </div>
    <div class="f"><label>Groups this job protects</label>
      <div id="lj_groups">${(d.groups || ["default"]).map(g => `<label class="switch" style="margin:0 0 8px">
        <input type="checkbox" class="gk" value="${esc(g)}" ${j.groups.includes(g) ? "checked" : ""}>
        ${esc(g)}${g === "default" ? ' <span class="tag info">all volumes</span>' : ""}</label>`).join("")}</div>
      <input type="text" id="lj_newgroup" placeholder="…or type a new group name">
    </div>
    ${UI.actions(`<button class="btn pri" onclick="lhJobSave()">Save job</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}
    <div class="note" style="margin-top:14px">Snapshots are stored on the volume itself and are
    near-instant. Backups upload to the backup target and need one configured — without it a
    backup job fails on every run.</div>`, true);
  scheduleChanged();
  lhTaskChanged();
};
/* Trims and cleanups keep nothing, so they have nothing to count. */
const KEEPS = task => /^(snapshot|backup)(-force-create|-delete)?$/.test(task);
window.lhTaskChanged = () => { $("#lj_retain_f").hidden = !KEEPS($("#lj_task").value); };
/* ---------------- schedule builder ----------------
   Shapes people use - every few minutes or hours, daily, some weekdays,
   monthly - written to cron for Longhorn, with the next runs shown in the
   reader's own time. Custom keeps the raw expression. */
function scheduleBuilder(cron) {
  const plan = CRON.toPlan(cron);
  const time = (hour, minute) => `${String(hour ?? 2).padStart(2, "0")}:${String(minute ?? 0).padStart(2, "0")}`;
  const kinds = [["minutes", "Every few minutes"], ["hours", "Every few hours"], ["daily", "Every day"], ["weekly", "On chosen days"], ["monthly", "Every month"], ["custom", "Custom cron"]];
  return `<div class="f sched"><label>Schedule</label>
    <div class="sched-row"><select id="ls_kind" onchange="scheduleChanged()">${kinds.map(([value, label]) =>
      `<option value="${value}" ${plan.kind === value ? "selected" : ""}>${label}</option>`).join("")}</select>
      <select id="ls_minutes" data-for="minutes">${CRON.MINUTE_STEPS.map(n => `<option value="${n}" ${plan.every === n ? "selected" : ""}>every ${n} min</option>`).join("")}</select>
      <select id="ls_hours" data-for="hours">${CRON.HOUR_STEPS.map(n => `<option value="${n}" ${plan.kind === "hours" && plan.every === n ? "selected" : ""}>every ${n === 1 ? "hour" : `${n} hours`}</option>`).join("")}</select>
      <label class="inline" data-for="hours">at minute <input id="ls_minute" type="number" min="0" max="59" value="${plan.kind === "hours" ? plan.minute : 0}"></label>
      <select id="ls_day" data-for="monthly">${Array.from({ length: 28 }, (_, i) => i + 1).map(n => `<option value="${n}" ${plan.day === n ? "selected" : ""}>on day ${n}</option>`).join("")}</select>
      <label class="inline" data-for="daily weekly monthly">at <input id="ls_time" type="time" value="${time(plan.hour, plan.minute)}"> UTC</label></div>
    <div class="sched-days" data-for="weekly">${CRON.SHORT.map((day, i) => `<label class="daychip"><input type="checkbox" value="${i}" ${(plan.days || [0]).includes(i) ? "checked" : ""}><span>${day}</span></label>`).join("")}</div>
    <input type="text" id="lj_cron" class="mono" data-for="custom" value="${esc(cron)}" placeholder="min hour day month weekday">
    <div class="dim xs sched-next" id="ls_next"></div></div>`;
}
window.scheduleChanged = () => {
  const kind = $("#ls_kind").value;
  $$(".sched [data-for]").forEach(el => { el.hidden = !el.dataset.for.split(" ").includes(kind); });
  if (kind !== "custom") {
    const [hour, minute] = ($("#ls_time").value || "02:00").split(":").map(Number);
    $("#lj_cron").value = CRON.fromPlan({ kind, hour, minute,
      every: +(kind === "minutes" ? $("#ls_minutes").value : $("#ls_hours").value),
      ...(kind === "hours" ? { minute: Math.max(0, Math.min(59, +$("#ls_minute").value || 0)) } : {}),
      days: $$(".sched-days input:checked").map(box => +box.value), day: +$("#ls_day").value });
  }
  const cron = $("#lj_cron").value.trim(), runs = CRON.next(cron, new Date(), 3);
  $("#ls_next").innerHTML = runs.length
    ? `${esc(CRON.describe(cron))}. Next: ${runs.map(at => esc(localTime(at))).join(" · ")} <span class="dim">(your time)</span>`
    : `<span class="bad-text">That cron expression never runs, or is not five fields.</span>`;
};
document.addEventListener("input", event => { if (event.target.closest(".sched")) scheduleChanged(); });
document.addEventListener("change", event => { if (event.target.closest(".sched") && event.target.id !== "ls_kind") scheduleChanged(); });

window.lhRun = async name => {
  try {
    await api("/api/lh/job/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`${name} started; follow it in the job tray`, "ok");
    setTimeout(() => { resetPaint(); viewProtect(); }, 1500);
  } catch (e) { toast(e.message, "bad"); }
};

/* A plan's jobs for one group. Those for default keep their plain names;
   another group's are named after it, so a plan for it never takes over the
   jobs protecting everything else. */
function planJobName(group, job) {
  return group === "default" ? job.name : `${group}-${job.name}`.slice(0, 40).replace(/-+$/, "");
}
function planList(group, chosen) {
  const d = STATE.data.lh || { jobs: [] };
  const existing = new Set((d.jobs || []).map(j => j.name));
  return PROTECT_PLANS.map(plan => `<label class="plan-card card flat"><input type="radio" name="lp_plan" value="${plan.id}" ${plan.id === chosen ? "checked" : ""}>
      <div><b>${esc(plan.title)}</b>${plan.needsTarget && !(d.target || {}).configured ? ' <span class="pill slim warn">needs a backup target</span>' : ""}
        <div class="dim small">${esc(plan.blurb)}</div>
        <div class="plan-jobs">${plan.jobs.map(j => { const name = planJobName(group, j); return `<span class="tag${existing.has(name) ? " warn" : ""}" data-tip="${existing.has(name) ? "replaces the job of this name" : "new job"}">${esc(name)} · ${esc(CRON.describe(j.cron))}${j.retain ? ` · keeps ${j.retain}` : ""}</span>`; }).join("")}</div></div></label>`).join("");
}
window.lhPlans = (group = "default") => {
  const d = STATE.data.lh || { groups: ["default"], jobs: [] };
  modal("Protection plans", `<p class="muted small">A plan makes the jobs a sensible policy needs. They are ordinary jobs afterwards: edit or delete any of them.</p>
    <div class="f"><label>Volumes it protects</label><select id="lp_group" onchange="lhPlanGroup()">${(d.groups || ["default"]).map(g =>
      `<option value="${esc(g)}" ${g === group ? "selected" : ""}>${esc(g)}${g === "default" ? " - volumes with no other group" : ""}</option>`).join("")}</select>
      <div class="dim xs" style="margin-top:6px">A plan for a group of its own needs the group first:
        <a class="linkish" onclick="lhGroup()">make one</a>.</div></div>
    <div class="plan-list" id="lp_list">${planList(group, "snapshots")}</div>
    ${UI.actions(`<button class="btn pri" onclick="lhPlanApply()">Set up plan</button><button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`, true);
};
window.lhPlanGroup = () => {
  const chosen = $("input[name=lp_plan]:checked")?.value || "snapshots";
  $("#lp_list").innerHTML = planList($("#lp_group").value || "default", chosen);
};
async function planApply(plan, group) {
  const names = [];
  for (const job of plan.jobs) {
    const name = planJobName(group, job);
    await api("/api/lh/job", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...job, name, groups: [group] }) });
    names.push(name);
  }
  return names;
}
window.lhPlanApply = async () => {
  const plan = PROTECT_PLANS.find(p => p.id === $("input[name=lp_plan]:checked")?.value);
  if (!plan) return;
  const group = $("#lp_group").value || "default";
  try {
    await planApply(plan, group);
    toast(`${plan.title}: ${plan.jobs.length} jobs set up for ${group}`, "ok"); closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- groups ----------------
   A group is a label on volumes and a name in jobs, so it is made, renamed
   and emptied by changing both at once. */
window.lhGroup = (name = "") => {
  const d = STATE.data.lh || { volumes: [], jobs: [], group_rows: [] };
  const row = (d.group_rows || []).find(g => g.name === name) || { name: "", volumes: [], jobs: [] };
  const isDefault = name === "default";
  modal(name ? `Group · ${name}` : "New group", `
    <div class="f"><label>Name</label><input type="text" id="lg_name" value="${esc(name)}" ${isDefault ? "readonly" : ""}
      placeholder="media" autocomplete="off"></div>
    <div class="f"><label>Volumes in it</label>
      <input type="text" id="lg_filter" placeholder="Filter by name or namespace" oninput="lhGroupFilter()" autocomplete="off">
      <div class="lg-list" id="lg_vols">${(d.volumes || []).map(v => `<label class="lg-row" data-q="${esc(`${v.pvc} ${v.namespace} ${v.name}`.toLowerCase())}">
        <input type="checkbox" value="${esc(v.name)}" ${row.volumes.includes(v.name) ? "checked" : ""}>
        <span class="lg-name"><b>${esc(v.pvc || v.name)}</b> <span class="dim xs">${esc(v.namespace || "no claim")} · ${v.size_gb} GB</span></span>
        <span class="lg-tags">${v.groups.filter(g => g !== name).map(g => `<span class="tag ${g === "default" ? "info" : ""}">${esc(g)}</span>`).join("")}</span></label>`).join("")
        || '<div class="dim xs">No volumes.</div>'}</div>
      <div class="row" style="gap:10px;margin-top:6px"><button class="linkish xs" onclick="lhGroupAll(true)">Tick all shown</button>
        <button class="linkish xs" onclick="lhGroupAll(false)">Untick all shown</button></div></div>
    ${isDefault ? `<div class="note">A volume taken out of <span class="mono">default</span> has to be in another group,
        or Longhorn puts it straight back. Those are left in it.</div>`
      : `<label class="switch" style="margin:0 0 14px"><input type="checkbox" id="lg_leave" checked>
        <span>Take volumes out of default as they join
        ${tip("Longhorn leaves a volume in default when it joins another group, so it would get default's jobs as well as this group's. Untick to keep both.")}</span></label>`}
    <div class="f"><label>Jobs that protect it</label>${(d.jobs || []).length ? `<div class="lg-list" id="lg_jobs">${d.jobs.map(j => `<label class="lg-row">
        <input type="checkbox" value="${esc(j.name)}" ${row.jobs.includes(j.name) ? "checked" : ""}>
        <span class="lg-name"><b>${TASK_ICON[j.task] || "◷"} ${esc(j.name)}</b>
          <span class="dim xs lg-sub">${esc(j.task)} · ${esc(CRON.describe(j.cron))}</span></span></label>`).join("")}</div>`
        : '<div class="dim xs" id="lg_jobs">No jobs yet; a plan below makes them.</div>'}</div>
    <div class="f"><label>Also set up a plan for it</label><select id="lg_plan"><option value="">No plan</option>
      ${PROTECT_PLANS.map(plan => `<option value="${plan.id}">${esc(plan.title)}</option>`).join("")}</select>
      <div class="dim xs" style="margin-top:6px">Its jobs are named after the group, so they protect only these volumes.</div></div>
    ${UI.actions(`<button class="btn pri" data-original="${esc(name)}" onclick="lhGroupSave(this.dataset.original)">Save group</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`, true);
};
window.lhGroupFilter = () => {
  const q = ($("#lg_filter").value || "").trim().toLowerCase();
  $$("#lg_vols .lg-row").forEach(row => { row.hidden = !!q && !row.dataset.q.includes(q); });
};
window.lhGroupAll = on => $$("#lg_vols .lg-row").forEach(row => {
  if (!row.hidden) row.querySelector("input").checked = on;
});
window.lhGroupSave = async original => {
  const name = $("#lg_name").value.trim().toLowerCase();
  const body = { original, name,
    volumes: $$("#lg_vols input:checked").map(box => box.value),
    jobs: $$("#lg_jobs input:checked").map(box => box.value),
    leave_default: $("#lg_leave") ? $("#lg_leave").checked : false };
  const plan = PROTECT_PLANS.find(p => p.id === $("#lg_plan").value);
  if (!name) return toast("give the group a name", "bad");
  try {
    if (plan) body.jobs.push(...await planApply(plan, name));
    const result = await api("/api/lh/group", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const said = [`group ${name} saved`];
    if (result.left_default?.length) said.push(`${result.left_default.length} left default`);
    if (result.back_to_default?.length) said.push(`${result.back_to_default.length} back in default`);
    if (result.kept_in_default?.length) said.push(`${result.kept_in_default.length} kept in default: they have no other group`);
    toast(said.join(" · "), result.kept_in_default?.length ? "warn" : "ok"); closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};
window.lhGroupDel = async name => {
  const row = ((STATE.data.lh || {}).group_rows || []).find(g => g.name === name) || { volumes: [], jobs: [] };
  if (!(await ask(`Delete group "${name}"?` + String.fromCharCode(10, 10)
      + `${row.volumes.length} volume(s) leave it; any with no other group go back to default and its jobs.`
      + ` ${row.jobs.length} job(s) stop covering it. Snapshots and backups already taken are kept.`))) return;
  try {
    const result = await api("/api/lh/group/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast([`group ${name} deleted`, result.back_to_default?.length ? `${result.back_to_default.length} back in default` : "",
      result.idle_jobs?.length ? `${result.idle_jobs.join(", ")} now protect${result.idle_jobs.length === 1 ? "s" : ""} nothing` : ""]
      .filter(Boolean).join(" · "), result.idle_jobs?.length ? "warn" : "ok");
    resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};
window.lhJobSave = async () => {
  const groups = $$("#lj_groups .gk").filter(c => c.checked).map(c => c.value);
  const extra = $("#lj_newgroup").value.trim();
  if (extra) groups.push(extra);
  const task = $("#lj_task").value;
  const body = { name: $("#lj_name").value.trim(), task,
    cron: $("#lj_cron").value.trim(), retain: KEEPS(task) ? +$("#lj_retain").value : 0,
    concurrency: +$("#lj_conc").value, groups };
  if (!body.name) return toast("name is required", "bad");
  if (!groups.length) return toast("pick at least one group, or the job protects nothing", "bad");
  try {
    await api("/api/lh/job", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    toast(`job "${body.name}" saved`, "ok"); closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};
window.lhJobDel = async name => {
  if (!(await ask(`Delete recurring job "${name}"?\n\nExisting snapshots and backups are kept.`))) return;
  try {
    await api("/api/lh/job/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast("deleted", "ok"); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};
window.lhQuickStart = async () => {
  try {
    await api("/api/lh/job", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "daily-snapshot", task: "snapshot", cron: "0 2 * * *",
        retain: 7, concurrency: 2, groups: ["default"] }) });
    toast("daily snapshot of every volume, keeping 7", "ok"); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};
window.lhCovered = name => {
  const j = (STATE.data.lh.jobs || []).find(x => x.name === name);
  const vols = STATE.data.lh.volumes || [];
  modal("Covered by · " + name, j && j.volumes.length
    ? `<p class="muted small">${j.volumes.length} volume(s), via group${j.groups.length === 1 ? "" : "s"}
       ${j.groups.map(g => `<span class="tag">${esc(g)}</span>`).join("")} or a direct label.</p>
       <div style="margin-top:14px">${j.volumes.map(v => {
         const vv = vols.find(x => x.name === v) || {};
         return `<span class="tag ok">${esc(vv.pvc || v)}</span>`;
       }).join("")}</div>`
    : `<div class="empty">This job protects nothing. Add it to a group, or assign volumes directly.</div>`);
};

/* ---------------- per-volume protection ---------------- */
window.lhAssign = (vol, label) => {
  const d = STATE.data.lh;
  const v = (d.volumes || []).find(x => x.name === vol) || { groups: [], jobs: [] };
  modal("Protect · " + label, `
    <p class="muted small">Groups and jobs are labels on the volume. Ticking one takes effect
    on the job's next run.</p>
    <div class="sec">Groups</div>
    <div id="pa_groups">${(d.groups || []).map(g => `<label class="switch" style="margin:0 0 8px">
      <input type="checkbox" class="pg" value="${esc(g)}" ${v.groups.includes(g) ? "checked" : ""}>
      ${esc(g)}</label>`).join("")}</div>
    <input type="text" id="pa_newgroup" placeholder="…or a new group" autocomplete="off">
    <div class="dim xs" style="margin-top:6px">A volume in <span class="mono">default</span> and another group gets both groups' jobs.</div>
    <div class="sec">Individual jobs</div>
    <div id="pa_jobs">${(d.jobs || []).map(j => `<label class="switch" style="margin:0 0 8px">
      <input type="checkbox" class="pj" value="${esc(j.name)}" ${v.jobs.includes(j.name) ? "checked" : ""}>
      ${esc(j.name)} <span class="dim xs">${esc(j.task)}</span></label>`).join("")
      || '<div class="dim xs">no jobs defined yet</div>'}</div>
    ${UI.actions(`<button class="btn pri" onclick="lhAssignSave(${jsq(vol)})">Save</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
};
window.lhAssignSave = async vol => {
  const d = STATE.data.lh;
  const v = (d.volumes || []).find(x => x.name === vol) || { groups: [], jobs: [] };
  const wantG = $$("#pa_groups .pg").filter(c => c.checked).map(c => c.value);
  const wantJ = $$("#pa_jobs .pj").filter(c => c.checked).map(c => c.value);
  const calls = [];
  (d.groups || []).forEach(g => {
    const has = v.groups.includes(g), want = wantG.includes(g);
    if (has !== want) calls.push({ volumes: [vol], name: g, kind: "group", enabled: want });
  });
  (d.jobs || []).forEach(j => {
    const has = v.jobs.includes(j.name), want = wantJ.includes(j.name);
    if (has !== want) calls.push({ volumes: [vol], name: j.name, kind: "job", enabled: want });
  });
  const extra = ($("#pa_newgroup").value || "").trim().toLowerCase();
  if (extra && !(d.groups || []).includes(extra)) calls.push({ volumes: [vol], name: extra, kind: "group", enabled: true });
  if (!calls.length) { closeModal(); return toast("nothing changed"); }
  try {
    for (const c of calls) {
      await api("/api/lh/assign", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(c) });
    }
    toast(`${calls.length} change(s) applied`, "ok"); closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- snapshots & backups ---------------- */
function snapshotTimeline(snaps, vol, label) {
  const sorted = [...snaps].sort((a, b) => (Date.parse(a.created) || 0) - (Date.parse(b.created) || 0));
  return `<div class="snapshot-timeline" aria-label="Snapshot creation timeline">${sorted.map(s => {
    const source = s.source || (s.user_created ? "user" : "unknown");
    const title = {system: "System", user: "User", scheduled: "Scheduled", unknown: "Unknown source"}[source] || "Unknown source";
    const date = new Date(s.created), valid = Number.isFinite(date.getTime());
    const age = valid ? Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000)) : 0;
    const ago = age >= 1440 ? `${Math.floor(age / 1440)}d ago` : age >= 60 ? `${Math.floor(age / 60)}h ago` : `${age}m ago`;
    return `<article class="snapshot-point ${source === "system" ? "system" : ""}">
      <div class="snapshot-marker">${icon("snapshot")}</div><div class="snapshot-content">
      <div class="between"><b class="mono small">${esc(s.name)}</b><span class="tag ${source === "system" ? "info" : ""}">${title}</span></div>
      <div class="snapshot-date">${valid ? `<time datetime="${esc(s.created)}" data-tip="${esc(s.created)}">${esc(date.toLocaleString(undefined, {day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit"}))}</time><span class="dim xs">${ago}</span>` : '<span class="dim small">Snapshot time unavailable</span>'}</div>
      <div class="row small"><span class="dim">${esc(s.size_mb)} MiB</span>${s.deleting || s.removed ? '<span class="tag warn">cleanup pending</span>' : !s.ready ? '<span class="tag warn">not ready</span>' : ""}
      ${(s.children || []).includes("volume-head") ? `<span class="tag" data-tip="This snapshot is the parent of live data. After marking it removed, Longhorn may need a fresh snapshot boundary before it can reclaim blocks.">parent of Volume Head</span>` : ""}</div>
      ${source === "system" ? '<p class="dim xs">Longhorn-created checkpoint (for example expansion or rebuild), not a user recovery point.</p>' : ""}
      ${s.error ? `<p class="warntext small">${esc(s.error)}</p>` : ""}
      <div class="snapshot-actions">${actionBar([
        s.ready && !s.deleting && !s.removed && {label:'Browse files', need:'admin', run:`snapshotFiles(${jsq(vol)},${jsq(s.name)})`},
        source !== 'system' && source !== 'unknown' && s.ready && !s.deleting && !s.removed && {label:'Roll back', icon:'rollback', need:'admin', run:`lhRevert(${jsq(vol)},${jsq(s.name)},${jsq(label)})`},
        {label:s.deleting ? 'Track cleanup' : source === 'system' ? 'Clean up' : 'Delete', icon:'trash', need:'admin', danger:true, disabled:source === 'unknown', run:`lhSnapDel(${jsq(s.name)},${jsq(vol)},${jsq(label)})`}
      ], {shown:1, label:'More snapshot actions'})}</div>
      </div></article>`;
  }).join("") || '<div class="empty small">No snapshots yet</div>'}
    <article class="snapshot-point head"><div class="snapshot-marker">${icon("play")}</div><div class="snapshot-content"><b>Volume Head</b><span class="dim small">Live data · now · never deleted as a snapshot</span></div></article></div>`;
}

let SNAPSHOT_REFRESH = 0, SNAPSHOT_TIMER = null;
function snapshotCleanupHtml(p) {
  if (!p || p.error) return `<div class="note warn">Cleanup progress unavailable${p?.error ? `: ${esc(p.error)}` : ""}. Retrying; no completion is assumed.</div>`;
  if (p.errors?.length) return `<div class="note bad"><b>Longhorn cleanup error</b><p>${p.errors.map(esc).join("; ")}</p></div>`;
  if (!p.active) return `<div class="dim small">${p.known ? "No active Longhorn purge reported. Marked snapshots may still be waiting for a new boundary or controller cleanup." : "No engine status available; the volume may be detached. Cleanup progress is unknown."}</div>`;
  const pct = p.percent;
  return `<div class="note"><div class="between"><b>Longhorn cleanup ${pct == null ? "in progress" : `${+pct}%`}</b><span class="tag warn">Merging / purging</span></div>
    ${pct == null ? '<span class="spin2"></span>' : `<div class="meter" role="progressbar" aria-label="Longhorn volume cleanup" aria-valuenow="${+pct}" aria-valuemin="0" aria-valuemax="100"><span style="width:${Math.max(0, Math.min(100, +pct))}%"></span></div>`}
    <p class="small">Slowest active replica. This is volume-wide cleanup, not a separate percentage for each snapshot. Includes cleanup started in Longhorn or by recurring jobs.</p></div>`;
}
async function refreshSnapshotDialog(token, vol, label) {
  const host = $("#snapshot_live");
  if (!host || +host.dataset.token !== token || $("#modal").classList.contains("hidden")) return;
  clearTimeout(SNAPSHOT_TIMER);
  try {
    const [snaps, progress] = await Promise.all([
      api("/api/lh/snapshots?volume=" + encodeURIComponent(vol)),
      api("/api/lh/snapshot-progress?volume=" + encodeURIComponent(vol)).catch(e => ({error: e.message})),
    ]);
    if ($("#snapshot_live") !== host || token !== SNAPSHOT_REFRESH) return;
    host.innerHTML = snapshotCleanupHtml(progress) + `<div class="sec">Snapshots (${snaps.length})</div>` + snapshotTimeline(snaps, vol, label);
    if (window.applyRole) applyRole();
  } catch (e) {
    if ($("#snapshot_live") === host) host.innerHTML = `<div class="note warn">Snapshot refresh failed: ${esc(e.message)}. Retrying; previous cleanup may still be running.</div>`;
  }
  if ($("#snapshot_live") === host) {
    clearTimeout(SNAPSHOT_TIMER);
    SNAPSHOT_TIMER = setTimeout(() => refreshSnapshotDialog(token, vol, label), 4000);
  }
}
window.lhSnaps = async (vol, label) => {
  const token = ++SNAPSHOT_REFRESH;
  modal("Snapshots · " + label, `<div class="empty"><span class="spin2"></span>loading</div>`, true, 'snapshot-files');
  $(".modalbox").scrollTop = 0;
  try {
    const [snaps, bks] = await Promise.all([
      api("/api/lh/snapshots?volume=" + encodeURIComponent(vol)),
      api("/api/lh/backups?volume=" + encodeURIComponent(vol)).catch(() => []),
    ]);
    if (token !== SNAPSHOT_REFRESH || $("#mtitle").textContent !== "Snapshots · " + label || $("#modal").classList.contains("hidden")) return;
    $("#mbody").innerHTML = `
      <div class="row" style="margin-bottom:16px">
        <button class="btn pri" data-need="operator" onclick="lhSnapNow(${jsq(vol)},${jsq(label)})">Take snapshot now</button>
        <button class="btn" data-need="operator" onclick="lhBackupNow(${jsq(vol)},${jsq(label)})">Back up now</button>
        <button class="btn" data-need="operator" data-tip="Give Longhorn back the space deleted files still hold. Every volume is also trimmed weekly." onclick="lhTrimNow(${jsq(vol)})">Trim now</button>
        <button class="btn" onclick="lhSnaps(${jsq(vol)},${jsq(label)})">${icon("refresh")}Refresh</button>
      </div>
      <p class="dim small">Creation timeline, oldest first. Checkpoint ancestry can branch after rollback; this is not a dependency graph.</p>
      <div id="snapshot_live" data-token="${token}"><div class="sec">Snapshots (${snaps.length})</div>${snapshotTimeline(snaps, vol, label)}</div>
      <div class="sec">Backups (${bks.length})</div>
      ${backupTable(bks, vol, label)}`;
    if (window.applyRole) window.applyRole();
    refreshSnapshotDialog(token, vol, label);
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
function backupTable(bks, vol, label) {
  return `<div class="card flat pad0"><div class="tblwrap"><table data-sort="backups" class="tbl stack">
    <thead><tr><th>Name</th><th>State</th><th>Size</th><th data-nosort>Created</th><th></th></tr></thead><tbody>
    ${bks.map(b => `<tr><td class="mono small">${esc(b.name.slice(0, 28))}</td>
      <td><span class="pill ${b.state === "Completed" ? "ok" : b.state === "Error" ? "crit" : "med"}">${esc(b.state || "?")}</span>
          ${b.error ? `<div class="dim xs">${esc(b.error.slice(0, 80))}</div>` : ""}</td>
      <td class="mono">${b.size_mb} MB</td>
      <td class="small dim">${esc((b.created || "").replace("T", " ").replace("Z", ""))}</td>
      <td>${b.restorable ? "" : '<span class="dim xs">not ready</span> '}${actionBar([
        b.restorable ? { label: "Restore", icon: "rollback", run: `lhRestore(${jsq(b.name)})`, need: "admin" } : null,
        { label: "Delete", run: `lhBackupDel(${jsq(b.name)},${jsq(vol)},${jsq(label)})`, need: "admin", danger: true, tip: "Delete it from the backup target" }])}</td></tr>`).join("")
      || `<tr><td colspan=5 class="empty">no backups — needs a backup target</td></tr>`}
  </tbody></table></div></div>`;
}
/* The backups of a volume, whether or not the volume is still here. */
window.lhBackupList = async (vol, label) => {
  modal("Backups · " + label, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const bks = await api("/api/lh/backups?volume=" + encodeURIComponent(vol));
    const live = ((STATE.data.lh || {}).volumes || []).some(v => v.name === vol);
    $("#mbody").innerHTML = `${live ? "" : `<div class="note warn">The volume is gone. Restore makes a new volume from
      one of these; point the app at it, or restore it under the old claim's name.</div>`}
      ${backupTable(bks, vol, label)}`;
    if (window.applyRole) window.applyRole();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.lhBackupDel = async (name, vol, label) => {
  if (!(await ask(`Delete backup "${name}"?` + String.fromCharCode(10, 10)
      + "It is removed from the backup target too, and cannot be restored afterwards."))) return;
  try {
    await api("/api/lh/backup/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast("backup deleted", "ok");
    if ($("#mbody")) lhBackupList(vol, label);
  } catch (e) { toast(e.message, "bad"); }
};
window.lhSnapNow = async (vol, label) => {
  try {
    await api("/api/lh/snapshot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }) });
    toast("snapshot taken", "ok"); lhSnaps(vol, label);
  } catch (e) { toast(e.message, "bad"); }
};
/* Trim one volume now; the weekly job trims every volume. Longhorn trims
   only a volume that is attached, and says so otherwise. */
window.lhTrimNow = async vol => {
  try {
    const r = await api("/api/lh/trim", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }) });
    toast(r.detail || "trim started", "ok");
  } catch (e) { toast(e.message, "bad"); }
};
window.lhBackupNow = async (vol, label) => {
  try {
    await api("/api/lh/backup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }) });
    toast("backup started", "ok"); setTimeout(() => lhSnaps(vol, label), 1500);
  } catch (e) { toast(e.message, "bad"); }
};
let restoreCheckTimer = 0;
window.lhRestore = async backup => {
  modal("Restore backup", `<div class="empty"><span class="spin2"></span>checking backup and namespaces</div>`, true);
  try {
    const [plan, namespaces] = await Promise.all([
      api(`/api/lh/restore/plan?backup=${encodeURIComponent(backup)}&ns=&name=`),
      api("/api/namespaces"),
    ]);
    const preferred = typeof defaultNamespace === "function" ? defaultNamespace() : "lab";
    const defaultNs = namespaces.includes(preferred) ? preferred : (namespaces[0] || "default");
    $("#mbody").innerHTML = `
      <div class="note"><b>This creates a new PVC.</b> The backup and its source volume stay unchanged.
        Restore progress remains in the active-jobs tray if this dialog is closed or Homestead is refreshed.</div>
      <div class="drow"><div class="dl">Backup</div><div class="dv mono small">${esc(plan.backup)}</div></div>
      <div class="drow"><div class="dl">Source volume</div><div class="dv mono small">${esc(plan.source_volume)}</div></div>
      <div class="drow"><div class="dl">Original capacity</div><div class="dv">${plan.minimum_size_gb} GiB</div></div>
      <div class="f2" style="margin-top:16px">
        <div class="f"><label>Namespace ${tip("The Kubernetes namespace that will own the new PersistentVolumeClaim.")}</label>
          <select id="lr_ns" onchange="lhRestoreCheck()">${namespaces.map(ns =>
            `<option value="${esc(ns)}" ${ns === defaultNs ? "selected" : ""}>${esc(ns)}</option>`).join("")}</select></div>
        <div class="f"><label>New PVC name ${tip("Must be unique in the selected namespace. Existing claims are never overwritten.")}</label>
          <input id="lr_name" value="${esc(plan.suggested_name)}" oninput="lhRestoreCheck()"></div>
      </div>
      <div class="f2">
        <div class="f"><label>Capacity (GiB) ${tip("May be larger than the backup volume, but never smaller.")}</label>
          <input id="lr_size" type="number" min="${plan.minimum_size_gb}" value="${plan.minimum_size_gb}"></div>
        <div class="f"><label>Access mode ${tip("RWO mounts on one node at a time. RWX is shared through Longhorn's share manager.")}</label>
          <select id="lr_mode"><option value="ReadWriteOnce">ReadWriteOnce (RWO)</option>
            <option value="ReadWriteMany">ReadWriteMany (RWX)</option></select></div>
      </div>
      <div class="f"><label>Storage class ${tip("The restored PVC uses this configured class. Replica count, disk tags and other storage settings come from it.")}</label>
        <select id="lr_sc" onchange="lhRestoreCheck()">${(plan.storage_classes || []).map(name => `<option value="${esc(name)}" ${name === plan.storage_class ? "selected" : ""}>${esc(name)}</option>`).join("")}</select>
        <div class="dim small">The restored volume keeps this class and its storage settings.</div></div>
      <div id="lr_check" class="note"><span class="spin2"></span> checking destination name</div>
      ${UI.actions(`<button class="btn pri" id="lr_submit" data-need="admin" data-backup="${esc(backup)}"
          onclick="lhRestoreStart(this.dataset.backup)" disabled>${icon("rollback")}Start restore</button>
        <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`;
    if (window.applyRole) window.applyRole();
    lhRestoreCheck();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.lhRestoreCheck = () => {
  clearTimeout(restoreCheckTimer);
  restoreCheckTimer = setTimeout(async () => {
    const box = $("#lr_check"), button = $("#lr_submit");
    if (!box || !button) return;
    const backup = button.dataset.backup || "";
    const ns = $("#lr_ns").value, name = $("#lr_name").value.trim();
    button.disabled = true;
    if (!name) { box.className = "note bad"; box.textContent = "Enter a PVC name."; return; }
    if (!$("#lr_sc").value) { box.className = "note bad"; box.textContent = "Create a regular Longhorn storage class before restoring."; return; }
    box.className = "note"; box.innerHTML = '<span class="spin2"></span> checking destination name';
    try {
      const plan = await api(`/api/lh/restore/plan?backup=${encodeURIComponent(backup)}&ns=${encodeURIComponent(ns)}&name=${encodeURIComponent(name)}`);
      if (plan.conflict) { box.className = "note bad"; box.textContent = plan.conflict.message; }
      else { box.className = "note good"; box.textContent = `${ns}/${name} is available. Existing PVCs will not be changed.`; button.disabled = false; }
    } catch (e) { box.className = "note bad"; box.textContent = e.message; }
  }, 250);
};
window.lhRestoreStart = async backup => {
  const body = { backup, namespace: $("#lr_ns").value, name: $("#lr_name").value.trim(),
    size_gb: +$("#lr_size").value, access_mode: $("#lr_mode").value,
    storage_class: $("#lr_sc").value };
  const button = $("#lr_submit");
  button.disabled = true; button.innerHTML = '<span class="spin2"></span> starting';
  try {
    const result = await api("/api/lh/restore", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    toast(result.message || "restore started", "ok"); closeModal();
    if (window.refreshOperations) window.refreshOperations(true);
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.innerHTML = `${icon("rollback")}Start restore`; }
};
/* Rolling a volume back to a snapshot: what uses it stops, the volume is
   reverted with the state before kept as a snapshot, and it all starts again. */
window.lhRevert = async (vol, snap, label) => {
  childModal(`Roll ${label} back`, '<div class="empty"><span class="spin2"></span>checking what uses it</div>');
  try {
    const p = await api(`/api/lh/snapshot/revert/plan?volume=${encodeURIComponent(vol)}&snapshot=${encodeURIComponent(snap)}`);
    const when = p.created ? localTime(new Date(p.created)) : "the snapshot";
    const users = p.consumers || [];
    $("#mbody").innerHTML = `
      <p>${esc(label)} goes back to how it was at <b>${esc(when)}</b>. Everything written since is set aside:
        it is kept first as a snapshot of its own, so rolling forward again is one more rollback.</p>
      ${users.length ? `<div class="note"><b>Stopped while it is done, then started again:</b>
        ${users.map(u => `<span class="tag">${esc(u.kind === "VirtualMachine" ? "VM" : u.kind)} ${esc(u.name)}</span>`).join(" ")}</div>`
        : '<div class="dim small">Nothing uses it now, so nothing needs stopping.</div>'}
      ${(p.blockers || []).map(b => `<div class="note bad">${esc(b)}</div>`).join("")}
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Cancel</button>
        <button class="btn pri" ${p.ready ? "" : "disabled"} onclick="lhRevertGo(${jsq(vol)},${jsq(snap)})">${icon("rollback")}Roll back</button>`)}`;
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.lhRevertGo = async (vol, snap) => {
  try {
    const r = await api("/api/lh/snapshot/revert", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol, snapshot: snap }) });
    closeModal();
    toast(r.detail, "ok");
    if (window.noteOperation) noteOperation(r.operation);
  } catch (e) { toast(e.message, "bad"); }
};

window.lhSnapDel = async (name, vol, label) => {
  const token = +($("#snapshot_live")?.dataset.token || 0);
  if (token) pushModal(() => refreshSnapshotDialog(token, vol, label));
  const open = token ? modal : childModal;
  open("Remove snapshot", '<div class="empty"><span class="spin2"></span>Checking Longhorn checkpoint</div>');
  try {
    const p = await api(`/api/lh/snapshot/delete-plan?volume=${encodeURIComponent(vol)}&name=${encodeURIComponent(name)}`);
    window.__snapshotDeletePlan = p;
    $("#mbody").innerHTML = `<div class="note"><b>${p.source === "system" ? "System checkpoint cleanup" : "Remove recovery point"}</b><p>${p.source === "system" ? "Longhorn created this checkpoint. Homestead requests cleanup through Longhorn's controller; it never removes active Volume Head or forces finalizers." : "This recovery point will no longer be usable for rollback. Current files and external backups are not deleted."}</p>
      Longhorn merges shared blocks and may also purge other already-removed or eligible system checkpoints on this volume. The displayed snapshot size is not a promise of space recovered.</div>
      ${p.head_parent ? '<div class="note warn">This is the parent of Volume Head. Cleanup can wait until you take a fresh snapshot. Homestead will show that dependency in the job; it will not create snapshots automatically.</div>' : ""}
      ${(p.blockers || []).map(b => `<div class="note bad">${esc(b)}</div>`).join("")}
      <p class="dim small">A persistent job tracks request, merge/purge and verified removal. Closing this dialog does not stop it. Removal cannot be undone or cancelled.</p>
      <div class="f"><label>Type <b class="mono">${esc(p.name)}</b> to confirm</label><input id="sd_confirm" autocomplete="off" oninput="lhSnapDeleteGate()"></div>
      ${UI.actions(`<button data-dialog-dismiss="true" class="btn" onclick="modalBack()">Back</button><button id="sd_go" class="btn danger" data-need="admin" disabled onclick="lhSnapDeleteGo()">Start cleanup job</button>`)}`;
  } catch (e) { $("#mbody").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
};
window.lhSnapDeleteGate = () => {
  const p = window.__snapshotDeletePlan;
  $("#sd_go").disabled = !p?.ready || $("#sd_confirm").value.trim() !== p.name;
};
window.lhSnapDeleteGo = async () => {
  const p = window.__snapshotDeletePlan, confirmation = $("#sd_confirm").value.trim();
  if (!p?.ready || confirmation !== p.name) return;
  $("#sd_go").disabled = true;
  try {
    const r = await api("/api/lh/snapshot/delete", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: p.name, volume: p.volume, uid: p.uid, volume_uid: p.volume_uid, confirmation})});
    closeModal(); if (window.noteOperation) noteOperation(r.operation);
    toast("Snapshot cleanup job queued", "ok");
  } catch (e) { toast(e.message, "bad"); lhSnapDeleteGate(); }
};

/* ---------------- backup target ---------------- */
window.lhTarget = () => {
  const t = (STATE.data.lh || {}).target || {};
  modal("Backup target", `
    <p class="muted small">Where Longhorn uploads backups. Snapshots do not need this —
    they live on the volume. Backups do.</p>
    ${t.harvester ? `<div class="note">On Harvester this is saved as Harvester's own <span class="mono">backup-target</span> setting,
      which it passes to Longhorn and uses for VM backups too. Harvester takes NFS or S3, and writes to the top of an S3 bucket.</div>` : ""}
    <div class="f" style="margin-top:14px"><label>Target URL</label>
      <input type="text" id="bt_url" value="${esc(t.url || "")}"
        placeholder="nfs://192.0.2.177:/mnt/user/backups">
      <div class="dim xs" style="margin-top:6px">
        NFS: <span class="mono">nfs://host:/export/path</span><br>
        S3: <span class="mono">s3://bucket@region${t.harvester ? "" : "/path"}</span> - a NAS's MinIO or Garage uses any region, such as us-east-1</div></div>
    <div id="bt_s3">
      <div class="f2">
        <div class="f"><label>Access key</label><input type="text" id="bt_access" autocomplete="off"
          placeholder="${t.secret && !t.secret_missing ? "kept as it is" : ""}"></div>
        <div class="f"><label>Secret key</label><input type="password" id="bt_secretkey" autocomplete="new-password"
          placeholder="${t.secret && !t.secret_missing ? "kept as it is" : ""}"></div></div>
      <div class="f"><label>Endpoint ${tip("Only for S3 that is not AWS: the NAS or service's URL, such as http://192.0.2.20:9000.")}</label>
        <input type="text" id="bt_endpoint" placeholder="http://192.0.2.20:9000"></div>
      <div class="dim xs" style="margin-bottom:12px">The keys are kept in a Secret in longhorn-system${t.secret ? ` (now ${esc(t.secret)})` : ""};
        leave them empty to keep the one it has.</div></div>
    <div class="f2">
      <div class="f" ${t.harvester ? "hidden" : ""}><label>Credential secret ${tip("The Secret holding the keys. Filled in for you when you type the keys above.")}</label>
        <input type="text" id="bt_secret" value="${esc(t.secret || "")}" placeholder="homestead-backup-target"></div>
      <div class="f"><label>Poll interval</label>
        <input type="text" id="bt_poll" value="${esc(t.interval || "5m")}"></div>
    </div>
    ${UI.actions(`<button class="btn pri" onclick="lhTargetSave()">Save target</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}
    <div class="note" style="margin-top:14px">For NFS the export must be reachable from every node
    and allow root writes, otherwise backups fail with a permission error that only shows up on the
    first scheduled run.</div>`);
  const s3 = () => { $("#bt_s3").hidden = !$("#bt_url").value.trim().startsWith("s3://"); };
  $("#bt_url").addEventListener("input", s3); s3();
};
window.lhTargetSave = async () => {
  const keys = { access_key: $("#bt_access").value.trim(), secret_key: $("#bt_secretkey").value,
    endpoint: $("#bt_endpoint").value.trim() };
  try {
    await api("/api/lh/target", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("#bt_url").value.trim(), secret: $("#bt_secret").value.trim(),
        poll: $("#bt_poll").value.trim() || "5m", ...(keys.access_key || keys.secret_key ? { keys } : {}) }) });
    toast("backup target saved", "ok"); closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};


/* ---------------- restore tests (homestead_restore_test.py) ----------------
   Each app with Longhorn volumes: what a test restores, its last result, and
   Test now. Switched on, Homestead tests each app with backups monthly. */
const restoreWhen = at => at ? (typeof answerSince === "function" ? answerSince(at) : new Date(at * 1000).toLocaleString()) : "";

async function loadRestoreTests() {
  const host = document.getElementById("restore-tests");
  if (!host) return;
  let d;
  try { d = await api("/api/restore-tests"); }
  catch (e) { host.innerHTML = `<div class="sec">Restore tests</div>${UI.callout("warn", "Restore tests could not be listed", esc(e.message))}`; return; }
  STATE.data.restoreTests = d;
  const row = r => {
    const last = r.last, restores = r.restores.map(x => `${x.claim} · ${x.backup}`).join(", ");
    const result = r.running ? '<span class="pill info">testing now</span>'
      : last ? `<span class="pill ${last.ok ? "ok" : "crit"}" data-tip="${esc(last.message || "")}">${last.ok ? "passed" : "failed"}</span> <span class="dim xs">${esc(restoreWhen(last.at))}${last.seconds ? ` · ${Math.round(last.seconds / 60)} min` : ""}</span>`
      : '<span class="dim xs">never tested</span>';
    return `<tr><td class="cell-name"><div class="vm-name-cell">${appAvatar(r.name, r.icon)}<div class="vm-name-text"><b>${esc(r.name)}</b><div class="dim xs">${esc(r.ns)}</div></div></div></td>
      <td data-label="Restores" class="small">${r.restores.length ? esc(restores) : '<span class="dim">nothing: ' + esc(r.without.map(w => `${w.claim} has ${w.why}`).join("; ")) + "</span>"}
        ${r.restores.length && r.without.length ? `<div class="dim xs">Not tested: ${esc(r.without.map(w => `${w.claim} (${w.why})`).join("; "))}</div>` : ""}</td>
      <td data-label="Last test">${result}${last && !last.ok ? `<div class="dim xs restore-why">${esc(last.message || "")}</div>` : ""}</td>
      <td data-actions>${r.restores.length && !r.running ? actionBar([{ label: "Test now", icon: "play", run: `restoreTestRun(${jsq(r.ns)},${jsq(r.name)})`, need: "admin" }], { label: `Restore test for ${r.name}` }) : ""}</td></tr>`;
  };
  host.innerHTML = `<div class="between restore-head"><div class="sec">Restore tests</div>
      <label class="switch" data-need="admin"><input type="checkbox" id="rt_on" ${d.enabled ? "checked" : ""} onchange="restoreTestsSwitch(this.checked)"> Test each app every ${d.every_days} days</label></div>
    <p class="dim small">A test restores an app's newest backups into new volumes, starts a copy of it on them - cut off from the network, its other volumes left empty so it cannot touch real data - and checks it starts and answers. Then it removes everything it made. One runs at a time, outside the maintenance window, when Longhorn has room.</p>
    ${d.apps.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack compact restore-table"><thead><tr><th>App</th><th>Restores</th><th>Last test</th><th data-nosort></th></tr></thead>
      <tbody>${d.apps.map(row).join("")}</tbody></table></div></div>` : '<div class="empty small">No app has Longhorn volumes to test.</div>'}`;
}

window.restoreTestsSwitch = async on => {
  try { const r = await api("/api/restore-tests/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) }); toast(r.detail, "ok"); }
  catch (e) { toast(e.message, "bad"); const box = $("#rt_on"); if (box) box.checked = !on; }
};

window.restoreTestRun = async (ns, name) => {
  try {
    await api("/api/restore-tests/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name }) });
    toast(`Restore test of ${name} started; follow it under Jobs`, "ok");
    loadRestoreTests();
  } catch (e) { toast(e.message, "bad"); }
};

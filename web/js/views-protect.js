/* Data protection — Longhorn recurring jobs, groups, snapshots, backups */

const TASK_ICON = {
  "snapshot": "◷", "snapshot-force-create": "◉", "snapshot-cleanup": "⌫",
  "snapshot-delete": "✕", "backup": "☁", "backup-force-create": "☁",
  "filesystem-trim": "⇅",
};
const CRON_PRESETS = {
  "0 * * * *": "Hourly",
  "0 2 * * *": "Daily at 02:00",
  "0 3 * * 0": "Weekly, Sunday 03:00",
  "0 4 1 * *": "Monthly, 1st at 04:00",
  "*/15 * * * *": "Every 15 minutes",
};

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
  const pointed = (target.url || "") === store.backup_url;
  return `<div class="card flat" style="margin-bottom:18px">
    <div class="between node-section-head"><div><div class="ctitle">Backup storage</div>
      <div class="csub">MinIO on a Longhorn volume, holding every backup this cluster writes</div></div>
      <div class="row"><span class="pill ${store.ready ? "ok" : "med"}">${store.ready ? "serving" : "starting"}</span>
        <button class="btn sm danger" data-need="admin" onclick="objectStoreRemove()">Remove</button></div></div>
    <div class="about-grid" style="margin-top:12px">
      <div><span>Endpoint</span><b class="mono">${esc(store.endpoint || "—")}</b></div>
      <div><span>Bucket</span><b class="mono">${esc(store.bucket)}</b></div>
      <div><span>Size</span><b>${store.size_gb ? store.size_gb + " GB" : "—"}</b></div>
      <div><span>Longhorn target</span><b>${pointed ? "pointed here" : "not pointed here"}</b></div>
    </div>
    ${pointed ? "" : `<div class="note warn" style="margin-top:12px"><b>Longhorn is not writing here.</b>
      Backups will not reach this bucket until it is.
      <button class="btn sm" data-need="admin" onclick="objectStorePoint()">Point Longhorn at it</button></div>`}
    ${store.reachable_off_cluster ? "" : `<div class="note" style="margin-top:12px">
      <b>Only this cluster can read it.</b> The store has no LAN address, so another cluster cannot
      restore from these backups. Give its Service an address to migrate workloads elsewhere.</div>`}
    <div class="note" style="margin-top:12px"><b>This is not disaster recovery.</b> The bucket lives on
      the same Longhorn storage it protects, so it survives a lost workload or a bad upgrade, not a lost
      cluster. It exists so a workload can be rebuilt somewhere else.</div>
  </div>`;
}

window.objectStoreSetup = () => modal("Set up backup storage", `
  <p>Runs MinIO on a Longhorn volume and points Longhorn's backups at it. Volume backups,
    and moving a workload to another cluster, both read and write here.</p>
  <div class="f2"><div class="f"><label>Size (GB) ${tip("Holds every volume backup this cluster keeps. Longhorn backups are incremental, so this is usually far smaller than the volumes themselves.")}</label>
    <input id="os_size" type="number" min="5" max="16384" value="100"></div>
    <div class="f"><label>LAN address ${tip("Another cluster reads backups over this address. Leave blank and only this cluster can restore from them.")}</label>
      <input id="os_ip" type="text" placeholder="192.0.2.244"></div></div>
  <div class="note"><b>It shares fate with what it protects.</b> Storage inside this cluster is the right
    place to stage a migration and the wrong place for your only copy. Keep anything you cannot lose
    somewhere else as well.</div>
  <div class="row" style="margin-top:16px">
    <button class="btn pri" data-need="admin" onclick="objectStoreDeploy()">Set up storage</button>
    <button class="btn" onclick="closeModal()">Cancel</button></div>`);

window.objectStoreDeploy = async () => {
  const body = { size_gb: +$("#os_size").value || 100, lb_ip: $("#os_ip").value.trim() };
  try {
    const result = await api("/api/objectstore/deploy", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`backup storage ready at ${result.endpoint}`, "ok");
    closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

window.objectStorePoint = async () => {
  try {
    const result = await api("/api/objectstore/longhorn", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    toast(result.detail || "Longhorn pointed at the bucket", result.reachable_off_cluster ? "ok" : "warn");
    resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

window.objectStoreRemove = async () => {
  if (!confirm("Remove the backup storage?" + String.fromCharCode(10, 10)
      + "The volume holding the backups is kept, so this can be undone.")) return;
  try {
    const result = await api("/api/objectstore/remove", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep_data: true }) });
    toast(result.detail || "backup storage removed", "ok");
    resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

async function viewProtect() {
  const [d, objects] = await Promise.all([
    api("/api/lh/overview"),
    api("/api/objectstore").catch(() => ({ deployed: false })),
  ]);
  STATE.data.lh = d;
  STATE.data.objectStore = objects;
  const tgt = d.target || {};
  const cover = d.total ? Math.round(d.protected / d.total * 100) : 0;

  paint(`<div class="phead">
      <div><h2>Data protection</h2>
        <p>Longhorn recurring jobs, snapshot groups and backups across ${d.total} volume${d.total === 1 ? "" : "s"}</p></div>
      <div class="row">
        <button class="btn" data-need="admin" onclick="lhTarget()">Backup target</button>
        <button class="btn pri" data-need="operator" onclick="lhJob()">＋ New job</button>
      </div></div>

  ${objectStoreCard(objects, tgt)}

  <div class="grid g3" style="margin-bottom:18px">
    <div class="card glow ${cover === 100 ? "g-ok" : cover ? "g-warn" : "g-bad"}">
      <div class="ctitle">Coverage</div><div class="csub">Volumes touched by at least one job</div>
      <div class="row" style="margin-top:12px;gap:18px;align-items:flex-end">
        <div class="bignum">${cover}<span class="unit">%</span></div>
        <div class="csub" style="padding-bottom:6px">${d.protected} of ${d.total} protected</div>
      </div>
      ${meter(cover, 'style="margin-top:12px"')}
      ${d.unprotected.length ? `<div style="margin-top:12px">
        <div class="dim xs" style="margin-bottom:6px">NOT COVERED</div>
        ${d.unprotected.slice(0, 6).map(v => `<span class="tag warn">${esc(v)}</span>`).join("")}
        ${d.unprotected.length > 6 ? `<span class="tag">+${d.unprotected.length - 6}</span>` : ""}</div>` : ""}
    </div>

    <div class="card flat">
      <div class="ctitle">Backup target</div><div class="csub">Where backups are uploaded</div>
      ${tgt.configured ? `
        <div class="drow"><div class="dl">URL</div><div class="dv mono small">${esc(tgt.url)}</div></div>
        <div class="drow"><div class="dl">Status</div><div class="dv">
          <span class="pill ${tgt.available ? "ok" : "crit"}">${tgt.available ? "reachable" : "unavailable"}</span></div></div>
        ${tgt.reason ? `<div class="dim xs" style="margin-top:8px">${esc(tgt.reason)}</div>` : ""}`
      : `<div class="note" style="margin-top:12px"><b>No backup target.</b> Snapshots work without one —
         they live on the volume. <b>Backups need somewhere to go</b>: an NFS share or S3 bucket.
         Until you set one, backup jobs will fail.</div>`}
    </div>

    <div class="card flat">
      <div class="ctitle">Groups</div><div class="csub">A job protects every volume in its groups</div>
      <div style="margin-top:12px">${d.groups.map(g => {
        const n = d.volumes.filter(v => v.groups.includes(g)).length;
        return `<span class="tag ${g === "default" ? "info" : ""}">${esc(g)} · ${n}</span>`;
      }).join("")}</div>
      <div class="dim xs" style="margin-top:12px">Longhorn puts every new volume in
        <span class="mono">default</span>, so a job targeting <span class="mono">default</span>
        covers everything automatically.</div>
    </div>
  </div>

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
      <div><div class="dim xs">SCHEDULE</div>${CRON_PRESETS[j.cron]
        ? `<div class="small" data-tip="cron ${esc(j.cron)}">${esc(CRON_PRESETS[j.cron])}</div>`
        : `<div class="mono small" data-tip="a custom cron schedule">${esc(j.cron)}</div>`}</div>
      <div><div class="dim xs">RETAIN</div><div class="mono small">${j.retain}</div></div>
      <div><div class="dim xs">PARALLEL</div><div class="mono small">${j.concurrency}</div></div>
      <div><div class="dim xs">GROUPS</div><div>${j.groups.map(g => `<span class="tag">${esc(g)}</span>`).join("") || '<span class="dim">—</span>'}</div></div>
    </div>
    <div class="dim xs">${esc(j.desc)}</div>
    <div class="row wacts">
      <button class="btn sm" onclick='lhJob(${JSON.stringify(j).replace(/'/g, "&#39;")})' data-need="operator">Edit</button>
      <button class="btn sm" onclick="lhCovered('${esc(j.name)}')">Volumes</button>
      <button class="btn sm danger" data-need="admin" onclick="lhJobDel('${esc(j.name)}')">Delete</button>
    </div></div>`).join("")}</div>`
  : `<div class="empty">No recurring jobs yet. A daily snapshot of the <span class="mono">default</span>
     group is the usual starting point — <a onclick="lhQuickStart()" style="cursor:pointer;text-decoration:underline">set that up</a>.</div>`}

  <div class="sec">Volumes</div>
  <div class="card flat pad0"><div class="tblwrap"><table data-sort="protect" class="tbl stack"><thead><tr>
    <th>Volume</th><th>Size</th><th>Health</th><th>Groups</th><th>Direct jobs</th><th data-nosort>Last backup</th><th></th>
  </tr></thead><tbody>
  ${d.volumes.map(v => `<tr>
    <td><b>${esc(v.pvc || v.name.slice(0, 16))}</b><div class="dim xs mono">${esc(v.namespace)}</div></td>
    <td class="mono">${v.size_gb} GB</td>
    <td><span class="pill ${v.robustness === "healthy" ? "ok" : v.robustness === "degraded" ? "med" : "crit"}">${esc(v.robustness || "?")}</span></td>
    <td>${v.groups.map(g => `<span class="tag ${g === "default" ? "info" : ""}">${esc(g)}</span>`).join("") || '<span class="dim">—</span>'}</td>
    <td>${v.jobs.map(j => `<span class="tag ok">${esc(j)}</span>`).join("") || '<span class="dim">—</span>'}</td>
    <td class="small dim">${v.last_backup_at ? esc(v.last_backup_at.replace("T", " ").replace("Z", "")) : "never"}</td>
    <td><div class="row" style="gap:6px">
      <button class="btn sm" onclick="lhSnaps('${esc(v.name)}','${esc(v.pvc || v.name)}')">Snapshots</button>
      <button class="btn sm" data-need="operator" onclick="lhAssign('${esc(v.name)}','${esc(v.pvc || v.name)}')">Protect</button>
    </div></td></tr>`).join("")}
  </tbody></table></div></div>`);
}

/* ---------------- job editor ---------------- */
window.lhJob = (j) => {
  const d = STATE.data.lh || { groups: ["default"], tasks: {} };
  j = j || { name: "", task: "snapshot", cron: "0 2 * * *", retain: 7, concurrency: 1, groups: ["default"] };
  modal(j.name ? "Edit job · " + j.name : "New recurring job", `
    <div class="f2">
      <div class="f"><label>Name</label><input type="text" id="lj_name" value="${esc(j.name)}"
        ${j.name ? "readonly" : ""} placeholder="daily-snapshot"></div>
      <div class="f"><label>Task</label><select id="lj_task">
        ${Object.entries(d.tasks || {}).map(([k, v]) =>
          `<option value="${esc(k)}" ${j.task === k ? "selected" : ""}>${esc(v)}</option>`).join("")}
      </select></div>
    </div>
    <div class="f"><label>Schedule</label>
      <select id="lj_preset" onchange="lhPreset()">
        ${Object.entries(CRON_PRESETS).map(([k, v]) =>
          `<option value="${esc(k)}" ${j.cron === k ? "selected" : ""}>${esc(v)}</option>`).join("")}
        <option value="">Custom…</option>
      </select></div>
    <div class="f"><label>Cron expression</label><input type="text" id="lj_cron" value="${esc(j.cron)}">
      <div class="dim xs" style="margin-top:6px">min hour day month weekday</div></div>
    <div class="f2">
      <div class="f"><label>Retain</label><input type="number" id="lj_retain" value="${j.retain}" min="1" max="250">
        <div class="dim xs" style="margin-top:6px">How many to keep before the oldest is removed</div></div>
      <div class="f"><label>Run in parallel</label><input type="number" id="lj_conc" value="${j.concurrency}" min="1" max="10"></div>
    </div>
    <div class="f"><label>Groups this job protects</label>
      <div id="lj_groups">${(d.groups || ["default"]).map(g => `<label class="switch" style="margin:0 0 8px">
        <input type="checkbox" class="gk" value="${esc(g)}" ${j.groups.includes(g) ? "checked" : ""}>
        ${esc(g)}${g === "default" ? ' <span class="tag info">all volumes</span>' : ""}</label>`).join("")}</div>
      <input type="text" id="lj_newgroup" placeholder="…or type a new group name">
    </div>
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="lhJobSave()">Save job</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">Snapshots are stored on the volume itself and are
    near-instant. Backups upload to the backup target and need one configured — without it a
    backup job fails on every run.</div>`, true);
};
window.lhPreset = () => {
  const v = $("#lj_preset").value;
  if (v) $("#lj_cron").value = v;
};
window.lhJobSave = async () => {
  const groups = $$("#lj_groups .gk").filter(c => c.checked).map(c => c.value);
  const extra = $("#lj_newgroup").value.trim();
  if (extra) groups.push(extra);
  const body = { name: $("#lj_name").value.trim(), task: $("#lj_task").value,
    cron: $("#lj_cron").value.trim(), retain: +$("#lj_retain").value,
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
  if (!confirm(`Delete recurring job "${name}"?\n\nExisting snapshots and backups are kept.`)) return;
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
    <div class="sec">Individual jobs</div>
    <div id="pa_jobs">${(d.jobs || []).map(j => `<label class="switch" style="margin:0 0 8px">
      <input type="checkbox" class="pj" value="${esc(j.name)}" ${v.jobs.includes(j.name) ? "checked" : ""}>
      ${esc(j.name)} <span class="dim xs">${esc(j.task)}</span></label>`).join("")
      || '<div class="dim xs">no jobs defined yet</div>'}</div>
    <div class="row" style="margin-top:18px">
      <button class="btn pri" onclick="lhAssignSave('${esc(vol)}')">Save</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
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
window.lhSnaps = async (vol, label) => {
  modal("Snapshots · " + label, `<div class="empty"><span class="spin2"></span>loading</div>`, true);
  try {
    const [snaps, bks] = await Promise.all([
      api("/api/lh/snapshots?volume=" + encodeURIComponent(vol)),
      api("/api/lh/backups?volume=" + encodeURIComponent(vol)).catch(() => []),
    ]);
    $("#mbody").innerHTML = `
      <div class="row" style="margin-bottom:16px">
        <button class="btn pri" data-need="operator" onclick="lhSnapNow('${esc(vol)}','${esc(label)}')">Take snapshot now</button>
        <button class="btn" data-need="operator" onclick="lhBackupNow('${esc(vol)}','${esc(label)}')">Back up now</button>
      </div>
      <div class="sec">Snapshots (${snaps.length})</div>
      <div class="card flat pad0"><div class="tblwrap"><table data-sort="snapshots" class="tbl stack">
        <thead><tr><th>Name</th><th data-nosort>Created</th><th>Size</th><th>Source</th><th></th></tr></thead><tbody>
        ${snaps.map(s => `<tr>
          <td class="mono small">${esc(s.name.slice(0, 28))}</td>
          <td class="small dim">${esc((s.created || "").replace("T", " ").replace("Z", ""))}</td>
          <td class="mono">${s.size_mb} MB</td>
          <td>${s.user_created ? '<span class="tag">manual</span>' : '<span class="tag info">scheduled</span>'}
              ${s.ready ? "" : '<span class="tag warn">not ready</span>'}</td>
          <td><button class="btn sm danger" data-need="admin" onclick="lhSnapDel('${esc(s.name)}','${esc(vol)}','${esc(label)}')">✕</button></td>
        </tr>`).join("") || `<tr><td colspan=5 class="empty">no snapshots yet</td></tr>`}
      </tbody></table></div></div>
      <div class="sec">Backups (${bks.length})</div>
      <div class="card flat pad0"><div class="tblwrap"><table data-sort="backups" class="tbl stack">
        <thead><tr><th>Name</th><th>State</th><th>Size</th><th data-nosort>Created</th><th></th></tr></thead><tbody>
        ${bks.map(b => `<tr><td class="mono small">${esc(b.name.slice(0, 28))}</td>
          <td><span class="pill ${b.state === "Completed" ? "ok" : b.state === "Error" ? "crit" : "med"}">${esc(b.state || "?")}</span>
              ${b.error ? `<div class="dim xs">${esc(b.error.slice(0, 80))}</div>` : ""}</td>
          <td class="mono">${b.size_mb} MB</td>
          <td class="small dim">${esc((b.created || "").replace("T", " ").replace("Z", ""))}</td>
          <td>${b.restorable ? `<button class="btn sm" data-need="admin"
            onclick="lhRestore('${esc(b.name)}')">${icon("rollback")}Restore</button>`
            : '<span class="dim xs">not ready</span>'}</td></tr>`).join("")
          || `<tr><td colspan=5 class="empty">no backups — needs a backup target</td></tr>`}
      </tbody></table></div></div>`;
    if (window.applyRole) window.applyRole();
  } catch (e) { $("#mbody").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.lhSnapNow = async (vol, label) => {
  try {
    await api("/api/lh/snapshot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }) });
    toast("snapshot taken", "ok"); lhSnaps(vol, label);
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
    const defaultNs = namespaces.includes("lab") ? "lab" : (namespaces[0] || "default");
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
      <div class="f"><label>Replicas ${tip("Copies Longhorn maintains on separate eligible disks after the restore completes.")}</label>
        <input id="lr_replicas" type="number" min="1" max="5" value="2"></div>
      <div id="lr_check" class="note"><span class="spin2"></span> checking destination name</div>
      <div class="row" style="margin-top:18px">
        <button class="btn pri" id="lr_submit" data-need="admin" data-backup="${esc(backup)}"
          onclick="lhRestoreStart(this.dataset.backup)" disabled>${icon("rollback")}Start restore</button>
        <button class="btn" onclick="closeModal()">Cancel</button></div>`;
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
    replicas: +$("#lr_replicas").value };
  const button = $("#lr_submit");
  button.disabled = true; button.innerHTML = '<span class="spin2"></span> starting';
  try {
    const result = await api("/api/lh/restore", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    toast(result.message || "restore started", "ok"); closeModal();
    if (window.refreshOperations) window.refreshOperations(true);
  } catch (e) { toast(e.message, "bad"); button.disabled = false; button.innerHTML = `${icon("rollback")}Start restore`; }
};
window.lhSnapDel = async (name, vol, label) => {
  if (!confirm(`Delete snapshot "${name}"?`)) return;
  try {
    await api("/api/lh/snapshot/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast("deleted", "ok"); lhSnaps(vol, label);
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- backup target ---------------- */
window.lhTarget = () => {
  const t = (STATE.data.lh || {}).target || {};
  modal("Backup target", `
    <p class="muted small">Where Longhorn uploads backups. Snapshots do not need this —
    they live on the volume. Backups do.</p>
    <div class="f" style="margin-top:14px"><label>Target URL</label>
      <input type="text" id="bt_url" value="${esc(t.url || "")}"
        placeholder="nfs://192.0.2.177:/mnt/user/backups">
      <div class="dim xs" style="margin-top:6px">
        NFS: <span class="mono">nfs://host:/export/path</span><br>
        S3: <span class="mono">s3://bucket@region/path</span> (needs a credential secret)</div></div>
    <div class="f2">
      <div class="f"><label>Credential secret (S3 only)</label>
        <input type="text" id="bt_secret" value="${esc(t.secret || "")}" placeholder="longhorn-s3-creds"></div>
      <div class="f"><label>Poll interval</label>
        <input type="text" id="bt_poll" value="${esc(t.interval || "5m")}"></div>
    </div>
    <div class="row" style="margin-top:16px">
      <button class="btn pri" onclick="lhTargetSave()">Save target</button>
      <button class="btn" onclick="closeModal()">Cancel</button></div>
    <div class="note" style="margin-top:14px">For NFS the export must be reachable from every node
    and allow root writes, otherwise backups fail with a permission error that only shows up on the
    first scheduled run.</div>`);
};
window.lhTargetSave = async () => {
  try {
    await api("/api/lh/target", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("#bt_url").value.trim(), secret: $("#bt_secret").value.trim(),
        poll: $("#bt_poll").value.trim() || "5m" }) });
    toast("backup target saved", "ok"); closeModal(); resetPaint(); viewProtect();
  } catch (e) { toast(e.message, "bad"); }
};

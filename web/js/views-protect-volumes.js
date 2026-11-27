/* Homestead Data protection, after the Nov 2026 mockup: whether each volume
   can be restored (the Volumes section), its schedules (Schedules), the
   volume's details sidebar and the last backup's hover card. A classic
   script, loaded after views-protect.js, whose helpers it uses. */

// The jobs that protect: a snapshot or a backup. Trims and clean-ups do not.
const PROTECTING = /^(snapshot|backup)(-force-create)?$/;
const DAY_S = 86400;
const protectJobs = (v, d = STATE.data.lh || {}) => (d.jobs || []).filter(j => (j.volumes || []).includes(v.name));
// "every hour", "every day at 02:00": CRON's words, starting lower case after the task.
const cronWords = cron => { const s = String(CRON.describe(cron) || cron); return s[0] ? s[0].toLowerCase() + s.slice(1) : s; };
const ageDays = at => { const t = Date.parse(at); return Number.isFinite(t) ? (Date.now() - t) / 1000 / DAY_S : null; };
const daysWords = n => n < 1 ? "today" : `${Math.round(n)} day${Math.round(n) === 1 ? "" : "s"} ago`;
const protectWhen = at => { const n = ageDays(at); return n === null ? "" : `${fmtDay(at)}, ${daysWords(n)}`; };
// A small button whose onclick is already escaped for an attribute, as an actionBar item's run is.
const protectButton = (label, run, need = "") => `<button type="button" class="btn sm"${need ? ` data-need="${esc(need)}"` : ""} onclick="${run}">${esc(label)}</button>`;

/* A job in words: "Snapshot every hour, 24 kept", "Backup every day at 02:00, 7 kept". */
function protectJobWords(j) {
  const kind = /^backup/.test(j.task) ? "Backup" : /^snapshot(-force-create)?$/.test(j.task) ? "Snapshot" : /trim/.test(j.task) ? "Trim" : "Clean-up";
  return `${kind} ${cronWords(j.cron)}${PROTECTING.test(j.task) && j.retain ? `, ${j.retain} kept` : ""}`;
}

/* The restore test that restores a volume, from the Restore tests list. */
function protectRestoreTest(v, rt = STATE.data.restoreTests) {
  const app = (rt?.apps || []).find(a => (a.restores || []).some(r => r.claim === (v.pvc || v.name) && (!a.ns || a.ns === v.namespace)));
  return app ? { app, last: app.last || null } : null;
}

/* What is wrong with a volume's protection, if anything: {tone, word, line, fix}.
   The word finishes "<volume> …" in the summary; the line says why. Unread
   schedules are not a problem of the volume: the summary says so once. */
function protectProblem(v, d = STATE.data.lh || {}) {
  if (v.protected_by === null || v.protected_by === undefined) return null;
  const jobs = protectJobs(v, d).filter(j => PROTECTING.test(j.task)), backups = jobs.filter(j => /^backup/.test(j.task));
  const name = v.pvc || v.name;
  if (!jobs.length) return { tone: "bad", word: "has no protection", line: "No schedule takes a snapshot or a backup of it.", fix: "protect" };
  const test = protectRestoreTest(v);
  if (test?.last && !test.last.ok) return { tone: "bad", word: `'s last restore test failed`, line: test.last.message || "The restore test did not pass.", fix: "test", app: test.app };
  if (backups.length) {
    const age = ageDays(v.last_backup_at), job = backups[0];
    if (age === null) return { tone: "warn", word: "has never been backed up", line: `${job.name} covers it, but no backup of it has been made yet.`, fix: "backup" };
    if (age > 2) return { tone: "warn", word: `'s last backup is ${Math.round(age)} days old`, line: `${job.name} should run ${cronWords(job.cron)}${job.last_run ? `, but has not run since ${fmtDay(job.last_run)}` : ", but has not run yet"}.`, fix: "backup", title: `The last backup is ${Math.round(age)} days old` };
  }
  return null;
}
const protectProblemTitle = (v, p) => p.title || `${v.pvc || v.name}${p.word.startsWith("'") ? "" : " "}${p.word}`;

/* The page's one line (design.md "Summary line"): how recent the backups
   are, then each volume whose protection needs a person, as a link. */
function protectSummary(d, { q = "" } = {}) {
  const vols = d.volumes || [];
  if (d.jobs_unread) return UI.summary({ head: "<b>Longhorn's schedules could not be read, so what each volume is protected by is not known.</b>" });
  const fresh = vols.filter(v => { const n = ageDays(v.last_backup_at); return n !== null && n <= 1; }).length;
  const head = !vols.length ? "No volumes to protect."
    : fresh === vols.length ? `${vols.length === 1 ? "The volume has" : `Every volume has`} a backup from the last day.`
    : !fresh ? `${vols.length === 1 ? "The volume has" : "No volume has"} ${vols.length === 1 ? "no" : "a"} backup from the last day.`
    : `${fresh} of ${vols.length} volumes have a backup from the last day.`;
  const troubled = vols.map(v => [v, protectProblem(v, d)]).filter(([, p]) => p).sort((a, b) => (a[1].tone === "bad" ? 0 : 1) - (b[1].tone === "bad" ? 0 : 1));
  const tgt = d.target || {};
  const links = [...troubled.slice(0, 3).map(([v, p]) => ({ text: p.fix === "test" ? `${p.app.name}'s last restore test failed` : protectProblemTitle(v, p).replace(/^The last backup is/, `${v.pvc || v.name}'s last backup is`),
      tone: p.tone, run: p.fix === "test" ? "go('protect-tests')" : `openDetails('protect',${jsq(v.name)})` })),
    troubled.length > 3 && { text: `${troubled.length - 3} more`, note: true },
    !tgt.configured ? { text: "No backup target is set up", tone: "warn", run: "go('protect-settings')" }
      : !tgt.available && { text: "The backup target cannot be reached", tone: "bad", run: "go('protect-settings')" },
    q && { text: `Showing volumes matching “${q}”`, note: true }];
  // A phone says how many need attention; the link shows only those.
  const needs = troubled.length;
  return UI.summary({ head: `<b>${head}</b>`, parts: links, narrow: { parts: [needs && { text: `${needs === 1 ? "1 volume needs" : `${needs} volumes need`} attention`, tone: troubled[0][1].tone, run: "protectFilter({show:'attention'})" }] } });
}

/* A volume's protection in plain words, one line a schedule, and what else runs. */
function protectWords(v, d = STATE.data.lh || {}) {
  if (v.protected_by === null || v.protected_by === undefined) return UI.chip("unknown", "neutral", { tip: "Longhorn's schedules could not be read." });
  // Snapshots first, then backups, as the mockup lists them.
  const jobs = protectJobs(v, d), other = jobs.filter(j => !PROTECTING.test(j.task));
  const guard = jobs.filter(j => PROTECTING.test(j.task)).sort((a, b) => (/^backup/.test(a.task) ? 1 : 0) - (/^backup/.test(b.task) ? 1 : 0));
  if (!guard.length) return `<span class="sum-bad">No snapshots or backups</span>${UI.reason(esc(other.length ? `Only ${other.map(j => protectJobWords(j).toLowerCase().replace(/^trim .*/, "the weekly trim")).join(" and ")}.` : "Nothing covers it."), { under: true })}`;
  return guard.map(j => `<div>${esc(protectJobWords(j))}</div>`).join("");
}

/* The last snapshot, from the snapshot schedules that cover it. */
function protectLastSnapshot(v, d = STATE.data.lh || {}) {
  const jobs = protectJobs(v, d).filter(j => /^snapshot(-force-create)?$/.test(j.task));
  if (v.protected_by === null || v.protected_by === undefined) return '<span class="dim">—</span>';
  if (!jobs.length) return '<span class="dim">None</span>';
  const last = jobs.map(j => j.last_success).filter(Boolean).sort().pop();
  return last ? esc(protectWhen(last)) : '<span class="dim">Not taken yet</span>';
}

/* The last backup: when, and what is late. Its hover card says the rest. */
function protectLastBackup(v, d = STATE.data.lh || {}) {
  const p = protectProblem(v, d), late = p && p.fix === "backup";
  const card = ` data-card="protect-backup|${esc(v.name)}"`;
  if (!v.last_backup_at) return `<span class="${late || (p && p.fix === "protect") ? "sum-warn" : "dim"}"${card}>Never</span>`;
  const job = protectJobs(v, d).find(j => /^backup/.test(j.task));
  return `<span class="${late ? "sum-warn" : ""}"${card}>${esc(protectWhen(v.last_backup_at))}</span>${late && job ? UI.reason(esc(`${job.name} has not run since.`), { under: true }) : ""}`;
}

/* The restore test of the app that uses it. */
function protectTestCell(v) {
  const t = protectRestoreTest(v);
  if (!STATE.data.restoreTests) return '<span class="dim">—</span>';
  if (!t) return '<span class="dim">No app to test</span>';
  if (t.app.running) return UI.chip("Testing now", "info", { size: "slim" });
  if (!t.last) return '<span class="dim">Never tested</span>';
  return `${UI.chip(t.last.ok ? "Passed" : "Failed", t.last.ok ? "ok" : "bad", { size: "slim" })}${UI.reason(esc(fmtDay(t.last.at * 1000)), { under: true })}`;
}

/* A volume's actions: Back up now, or Protect volume when nothing does. */
function protectActionItems(v) {
  const vol = jsq(v.name), label = jsq(v.pvc || v.name), name = v.pvc || v.name;
  const item = (text, run, iconName, options = {}) => ({ label: text, run, icon: iconName, ariaLabel: `${text} ${name}`, ...options });
  const unprotected = Array.isArray(v.protected_by) && !protectJobs(v).some(j => PROTECTING.test(j.task));
  return [
    unprotected ? item("Protect volume", `lhAssign(${vol},${label})`, "shield", { need: "operator", group: "Change", tip: "Put it in a group that a snapshot or backup schedule covers." })
      : item("Back up now", `volumeProtectNow('backup',${vol})`, "shield", { need: "operator", group: "Use", tip: "Back it up to the backup target now." }),
    item("Take snapshot", `volumeProtectNow('snapshot',${vol})`, "snapshot", { need: "operator", group: "Use" }),
    item("Restore…", `lhBackupList(${vol},${label})`, "rollback", { group: "Use", tip: "Its backups, to restore into a new volume or into its app." }),
    item("Open snapshots", `lhSnaps(${vol},${label})`, "snapshot", { group: "Use" }),
    !unprotected && item("Change protection…", `lhAssign(${vol},${label})`, "shield", { need: "operator", group: "Change", tip: "Choose the groups, and so the schedules, that cover it." }),
  ].filter(Boolean);
}

function protectVolumeRow(v) {
  const p = protectProblem(v), items = protectActionItems(v);
  return `<tr class="protect-row${p ? ` protect-problem ${p.tone}` : ""}" onclick="if(!event.target.closest('button,details,a'))openDetails('protect',${jsq(v.name)})">
    <td class="cell-name" data-sort="${esc(v.pvc || v.name)}"><button type="button" class="linkish protect-open" onclick="openDetails('protect',${jsq(v.name)})">${esc(v.pvc || v.name)}</button>
      <div class="dim xs">${esc([v.namespace, v.size_gb ? `${v.size_gb} GiB` : ""].filter(Boolean).join(" · "))}</div></td>
    <td data-label="Protection">${protectWords(v)}</td>
    <td data-label="Last snapshot">${protectLastSnapshot(v)}</td>
    <td data-label="Last backup" data-sort="${esc(v.last_backup_at || "")}">${protectLastBackup(v)}</td>
    <td data-label="Restore test">${protectTestCell(v)}</td>
    <td data-actions>${actionBar([{ ...items[0], plain: true }, ...items.slice(1)], { shown: 1, label: `More actions for ${v.pvc || v.name}` })}</td></tr>`;
}

/* The volumes, narrowed by a filter from a link (?show=never). */
function protectVolumesPaint() {
  const d = STATE.data.lh || {}, vols = d.volumes || [], body = document.getElementById("pf_rows"), note = document.getElementById("pf_note");
  if (!body) return;
  const f = PROTECT_FILTER, shown = vols.filter(v => protectMatches(v));
  const label = (PROTECT_SHOWS.find(s => s[0] === f.show) || PROTECT_SHOWS[0])[1];
  if (note) note.innerHTML = f.show !== "all" || f.group || f.q ? `<p class="dim small">Showing ${shown.length} of ${vols.length}: ${esc([f.show !== "all" && label, f.group && `group ${f.group}`, f.q && `“${f.q}”`].filter(Boolean).join(" · "))}.
      <button type="button" class="linkish" onclick="protectFilter({show:'all',group:'',q:''})">Show all volumes</button></p>` : "";
  body.innerHTML = shown.length ? shown.map(protectVolumeRow).join("") : `<tr><td colspan="6"><div class="empty small">No volume matches.</div></td></tr>`;
  if (typeof enhanceActions === "function") enhanceActions(body);
  if (typeof sortTables === "function") sortTables(body.closest("table").parentElement);
  if (window.applyRole) window.applyRole();
}
if (typeof window === "object") window.protectFilter = change => { Object.assign(PROTECT_FILTER, change); protectFilterWrite(); protectVolumesPaint(); };

/* What is kept after a restore or a deletion, in one line: the previous
   volumes and the backups of deleted volumes, with Review. `backups` is
   undefined while they are read. */
function protectKept(backups, previous = STATE.data.previousVolumes) {
  if (backups === undefined) return '<div class="empty small"><span class="spin2"></span> Reading the backups…</div>';
  if (backups?.unread) return UI.callout("warn", "The backups could not be read", esc(backups.unread));
  const gone = (backups || []).filter(b => !b.exists), prev = previous?.volumes || [];
  if (!gone.length && !prev.length) return "";
  const count = gone.reduce((n, b) => n + (b.count || 0), 0), size = gone.reduce((n, b) => n + (b.size_mb || 0), 0);
  const words = [prev.length && `${prev.length} previous volume${prev.length === 1 ? "" : "s"}`,
    gone.length && `${count} backup${count === 1 ? "" : "s"} of ${gone.length === 1 ? `the deleted volume ${gone[0].pvc || gone[0].name}` : `${gone.length} deleted volumes`}${size ? ` (${bytesText(size * 1024 ** 2)})` : ""}`].filter(Boolean);
  return `<div class="card flat protect-kept"><span class="protect-kept-dot" aria-hidden="true"></span><div><b>Kept after a restore or a deletion</b>
    <span class="dim">· ${esc(words.join(", and "))}</span></div>${protectButton("Review", "protectKeptReview()")}</div>`;
}
if (typeof window === "object") window.protectKeptReview = () => {
  const gone = (Array.isArray(STATE.data.lhBackupVolumes) ? STATE.data.lhBackupVolumes : []).filter(b => !b.exists);
  modal("Kept after a restore or a deletion", `<div id="previous-volumes" class="restore-tests"></div>
    ${gone.length ? UI.section("Backups of deleted volumes", protectBackups(gone, STATE.data.lh?.target || {})) : ""}
    ${UI.actions(UI.cancel("Close"))}`, true);
  loadPreviousVolumes();
};

/* Where backups go and how backup storage is, under the list. `objects` is
   undefined while backup storage is read. Unread schedules are named here. */
function protectTop(d, objects, tgt) {
  const reading = objects === undefined;
  objects = objects || {};
  const where = tgt.configured ? `Backups are stored on ${esc(protectTargetName(tgt))}, which is ${tgt.available ? "reachable" : "<span class=\"sum-bad\">not reachable</span>"}.`
    : `<span class="sum-warn">No backup target is set up</span>, so backups cannot run. Snapshots work without one.`;
  const storage = reading ? `Backup storage <span class="dim small">reading…</span>`
    : objects.deployed ? `Backup storage is ${objects.ready ? "serving" : "starting"}.`
    : objects.unread ? `Backup storage could not be read: ${esc(objects.unread)}` : "";
  return `${d.jobs_unread ? UI.callout("warn", "Longhorn's snapshot and backup schedules could not be read", `<p>${esc(d.jobs_unread)}</p>
    <p>What protects each volume, and the schedules, are shown when they can be read. Reload to try again.</p>`) : ""}
    ${!reading && !objects.deployed && !objects.unread ? objectStoreCard(objects, tgt) : ""}
    <p class="dim small protect-foot">${where} ${storage} Each volume follows the schedules of its groups.</p>`;
}
const protectTargetName = tgt => { const url = String(tgt.url || ""); const m = url.match(/^(\w+):\/\/(?:[^@/]*@)?([^/:]+)/); return m ? `${m[2]} (${m[1].toUpperCase()})` : url || "the backup target"; };

/* ---------------- Schedules ---------------- */

/* What a schedule does, in words: "Snapshot, 24 kept", "Backup to the target, 7 kept". */
const scheduleDoes = j => /^backup/.test(j.task) ? `Backup to the target${j.retain ? `, ${j.retain} kept` : ""}`
  : /^snapshot(-force-create)?$/.test(j.task) ? `Snapshot${j.retain ? `, ${j.retain} kept` : ""}` : /trim/.test(j.task) ? "Trim" : j.task === "snapshot-cleanup" ? "Clean up system snapshots" : j.task;

/* Two schedules that start in the same minute on a volume, said once, with the change that fixes it. */
function protectClashes(d) {
  const clashes = (d.clashes || []).filter(c => c.severity === "clash");
  if (!clashes.length) return "";
  const pvc = new Map((d.volumes || []).map(v => [v.name, v.pvc || v.name]));
  const [first] = clashes, [a, b] = first.jobs || [];
  const names = (first.volumes || []).map(n => pvc.get(n) || n);
  const later = (d.jobs || []).find(j => j.name === b?.name);
  const at = first.times?.[0]?.[0];
  return UI.callout("warn", clashes.length === 1 ? "Two schedules start in the same minute" : `${clashes.length} pairs of schedules start in the same minute`,
    `${esc(`${a?.name} and ${b?.name} both start ${at ? `at ${fmtAt(at, { weekday: true })}` : "in the same minute"}${names.length ? ` on ${names.join(" and ")}` : ""}.`)}
    ${later ? `<div class="row" style="margin-top:8px">${protectButton(`Change ${later.name}`, `lhJob(${esc(JSON.stringify(later))})`, "operator")}</div>` : ""}`);
}

function protectSchedulesTable(d) {
  if (d.jobs_unread) return `<div class="ui-help">The schedules could not be read, so none are listed. Reload to try again.</div>`;
  if (!d.jobs.length) return `<div class="empty">No schedules yet. A protection plan sets up a sensible set of schedules at once:
     <a onclick="lhPlans()" style="cursor:pointer;text-decoration:underline">choose a plan</a>. A daily snapshot of the <span class="mono">default</span>
     group is the usual starting point: <a onclick="lhQuickStart()" style="cursor:pointer;text-decoration:underline">set up a daily snapshot</a>.</div>`;
  const pvc = new Map((d.volumes || []).map(v => [v.name, v.pvc || v.name]));
  const late = j => /^backup/.test(j.task) && j.last_run && ageDays(j.last_run) > 2;
  return `<div class="card flat pad0"><div class="tblwrap"><table data-sort="schedules" class="tbl stack schedules-table"><thead><tr>
    <th>Schedule</th><th>Does</th><th>When</th><th>Applies to</th><th data-nosort>Last run</th><th data-nosort>Next run</th><th data-nosort></th></tr></thead><tbody>
    ${d.jobs.map(j => `<tr class="${late(j) || j.last_failed ? `protect-problem ${j.last_failed ? "bad" : "warn"}` : ""}">
      <td class="cell-name"><b>${esc(j.name)}</b></td>
      <td data-label="Does">${esc(scheduleDoes(j))}</td>
      <td data-label="When" data-tip="cron ${esc(j.cron)} on the cluster's clock (UTC)">${esc(CRON.describe(j.cron))}</td>
      <td data-label="Applies to">${j.groups.map(g => UI.chip(g, "neutral", { size: "slim" })).join(" ") || '<span class="dim">no groups</span>'}
        <div class="dim xs">${esc((j.volumes || []).map(n => pvc.get(n) || n).join(", ") || "no volumes")}</div></td>
      <td data-label="Last run">${late(j) && !j.last_failed ? `<span class="sum-warn">${esc(daysWords(ageDays(j.last_run)))}</span>` : lastRunText(j)}</td>
      <td data-label="Next run">${nextRun(j.cron)}</td>
      <td data-actions>${actionBar([
        { label: "Run now", icon: "play", run: `lhRun(${jsq(j.name)})`, need: "operator", disabled: j.running, plain: true },
        { label: "Edit", icon: "edit", run: `lhJob(${esc(JSON.stringify(j))})`, need: "operator", group: "Change" },
        { label: "Volumes it covers", icon: "disk", run: `lhCovered(${jsq(j.name)})`, group: "Use" },
        { label: "Delete", icon: "trash", run: `lhJobDel(${jsq(j.name)})`, need: "admin", danger: true, group: "Remove" }], { shown: 1, label: `More actions for ${j.name}` })}</td></tr>`).join("")}
    </tbody></table></div></div>`;
}

/* ---------------- a volume's details sidebar ---------------- */
const protectVolume = name => (STATE.data.lh?.volumes || []).find(v => v.name === name) || null;

/* Overview (the mockup): the problem and its fix, the schedules that
   protect it and by which group, and every restore point. */
function protectOverview(name) {
  const v = protectVolume(name);
  if (!v) return "";
  const d = STATE.data.lh || {}, p = protectProblem(v, d), items = protectActionItems(v);
  const fix = p && (p.fix === "protect" ? items.find(i => i.label === "Protect volume") : p.fix === "backup" ? items.find(i => i.label === "Back up now") : null);
  const groupsOf = j => j.groups.filter(g => (v.groups || []).includes(g));
  const tile = kind => {
    const jobs = protectJobs(v, d).filter(j => kind === "backup" ? /^backup/.test(j.task) : /^snapshot(-force-create)?$/.test(j.task));
    return jobs.length ? jobs.map(j => esc(`${CRON.describe(j.cron)}${kind === "backup" ? " to the target" : ""}, ${j.retain} kept · group ${groupsOf(j).join(", ") || j.groups.join(", ")}`)).join("<br>") : "None";
  };
  return (p ? UI.callout(p.tone, protectProblemTitle(v, p), `${esc(p.line)}${fix ? `<div class="row" style="margin-top:8px">${protectButton(fix.label, fix.run, fix.need)}</div>` : ""}`) : "")
    + UI.section("Protection", v.protected_by == null ? '<p class="dim small">Not known: the schedules could not be read.</p>' : UI.facts([["Snapshots", tile("snapshot")], ["Backups", tile("backup")]], { icons: false }))
    + UI.section("Restore points", detailsSlot(detailsData("protect", volumeProtectRead(v)), ({ snaps, backups }) => {
      const keep = kind => protectJobs(v, d).find(j => (kind === "backup" ? /^backup/ : /^snapshot/).test(j.task))?.retain;
      const rows = [...(Array.isArray(backups) ? backups : []).map(b => ({ at: b.created, what: `Backup, ${bytesText((b.size_mb || 0) * 1024 ** 2)}`, until: keep("backup") ? `Until ${keep("backup")} newer backups exist` : "Until deleted" })),
        ...snaps.map(s => ({ at: s.created, what: s.source === "system" ? "Snapshot by Longhorn" : "Snapshot", until: s.removed || s.deleting ? "Removed" : keep("snapshot") ? `Until ${keep("snapshot")} newer snapshots exist` : "Until deleted" }))];
      const test = protectRestoreTest(v);
      if (test?.last) rows.push({ at: test.last.at * 1000, what: `Restore test: ${test.last.ok ? "passed" : "failed"}`, until: "—" });
      rows.sort((a, b) => (asDate(b.at)?.getTime() || 0) - (asDate(a.at)?.getTime() || 0));
      return UI.table([{ label: "When" }, { label: "What" }, { label: "Kept until" }], rows.map(r => [esc(fmtAt(r.at)), esc(r.what), esc(r.until)]), { empty: "No snapshots or backups yet." });
    }, { reading: "Reading the snapshots and backups…", failed: "The snapshots could not be read" }));
}

/* Snapshots and Backups: as the volume's own sidebar shows them. */
function protectSnapshotsTab(name) {
  const v = protectVolume(name);
  return v ? detailsSlot(detailsData("protect", volumeProtectRead(v)), ({ snaps }) => snapshotTimeline(snaps, v.name, v.pvc || v.name), { reading: "Reading the snapshots…" }) : "";
}
function protectBackupsTab(name) {
  const v = protectVolume(name);
  return v ? detailsSlot(detailsData("protect", volumeProtectRead(v)), ({ backups }) => Array.isArray(backups) ? volumeBackupList(backups, v.name, v.pvc || v.name)
    : UI.callout("warn", "This volume's backups could not be read", esc(backups.unread)), { reading: "Reading the backups…" }) : "";
}
/* Restore tests: the test of the app that uses it. */
function protectTestsTab(name) {
  const v = protectVolume(name);
  if (!v) return "";
  const t = protectRestoreTest(v);
  if (!STATE.data.restoreTests) return '<p class="dim small">The restore tests are read with the Restore tests section.</p>';
  if (!t) return UI.empty("No app uses this volume, so there is nothing to test it with.");
  return UI.facts([["App", esc(`${t.app.ns}/${t.app.name}`)], ["Restores", esc(t.app.restores.map(r => `${r.claim} · ${r.backup}`).join(", "))],
      ["Last test", t.app.running ? "Testing now" : t.last ? `${UI.chip(t.last.ok ? "Passed" : "Failed", t.last.ok ? "ok" : "bad", { size: "slim" })} ${esc(fmtAt(t.last.at * 1000))}` : "Never tested"]])
    + (t.last && !t.last.ok ? UI.callout("bad", "The restore test failed", esc(t.last.message || "")) : "")
    + (t.app.restores.length && !t.app.running ? `<div class="details-foot">${actionBar([{ label: "Test now", run: `restoreTestRun(${jsq(t.app.ns)},${jsq(t.app.name)})`, need: "admin", plain: true }], { shown: 1 })}</div>` : "");
}

if (typeof DETAIL_KINDS === "object") DETAIL_KINDS.protect = {
  head: name => {
    const v = protectVolume(name);
    return v && { title: v.pvc || v.name, sub: [v.namespace, v.size_gb ? `${v.size_gb} GiB` : ""].filter(Boolean).join(" · ") };
  },
  // Back up now (or Protect volume), then Restore…; the rest in ⋯.
  actions: name => { const v = protectVolume(name); if (!v) return []; const items = protectActionItems(v), restore = items.find(i => i.label === "Restore…");
    return [items[0], restore, ...items.filter(i => i !== items[0] && i !== restore)]; },
  shown: 2,
  tabs: [
    { id: "overview", label: "Overview", html: protectOverview },
    { id: "snapshots", label: "Snapshots", html: protectSnapshotsTab },
    { id: "backups", label: "Backups", html: protectBackupsTab },
    { id: "tests", label: "Restore tests", html: protectTestsTab },
  ],
};

/* The last backup's card: when, which schedule, when it last ran, what is kept where. */
if (typeof HOVER_CARDS === "object") HOVER_CARDS["protect-backup"] = name => {
  const v = protectVolume(name);
  if (!v) return "";
  const d = STATE.data.lh || {}, job = protectJobs(v, d).find(j => /^backup/.test(j.task)), tgt = d.target || {};
  return UI.card({ title: v.last_backup_at ? `Last backup of ${v.pvc || v.name}` : `${v.pvc || v.name} has no backup`,
    chipHtml: job?.last_failed ? UI.chip("Last run failed", "bad", { size: "slim" }) : "",
    rows: [
      v.last_backup_at && ["Taken", esc(protectWhen(v.last_backup_at)), "clock"],
      ["Schedule", job ? esc(`${job.name}, ${cronWords(job.cron)}`) : "No backup schedule covers it", "clock"],
      job && ["Last run", job.last_run ? esc(fmtWhen(job.last_run)) : "Not yet", "pulse"],
      job && ["Kept", esc(`${job.retain} backups on ${protectTargetName(tgt)}`), "shield"],
    ], link: { label: "Open details", onclick: `openDetails('protect',${JSON.stringify(name)})` } });
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  PROTECTING, DAY_S, protectJobs, cronWords, ageDays, daysWords, protectWhen, protectButton, protectJobWords, protectRestoreTest, protectProblem, protectProblemTitle,
  protectSummary, protectWords, protectLastSnapshot, protectLastBackup, protectTestCell, protectActionItems, protectVolumeRow, protectVolumesPaint,
  protectKept, protectTop, protectTargetName, scheduleDoes, protectClashes, protectSchedulesTable, protectVolume, protectOverview, protectSnapshotsTab,
  protectBackupsTab, protectTestsTab,
};

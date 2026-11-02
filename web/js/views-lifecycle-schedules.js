/* Homestead views: the Schedules page. Split from views-lifecycle.js (#911); a classic script. */
/* ---------------- schedules ---------------- */
/* A job runs in its own time zone or, with none (saved before Homestead set
   one), on the clock of the host running the controller, which the page
   cannot know. Only a UTC one gets a next run, since CRON works in UTC. */
const jobUtc = j => UTC_ZONES.includes(j.time_zone) && !!CRON.parse(j.schedule);
function jobWhen(j) {
  if (!CRON.parse(j.schedule)) return `<span class="mono">${esc(j.schedule)}</span>${j.time_zone ? ` (${esc(j.time_zone)})` : ""}`;
  const words = CRON.describe(j.schedule);
  const text = jobUtc(j) ? words
    : !j.time_zone ? `${words.replace(" UTC", "")} on the host's clock`
    : `${words.replace(" UTC", "")} (${j.time_zone})`;
  return `<span data-tip="${esc(j.schedule)}">${esc(text)}</span>`;
}
function jobNext(j) {
  if (j.suspend) return '<span class="dim">—</span>';
  if (j.next_run) return nextRunAt(new Date(j.next_run));
  if (jobUtc(j)) return nextRun(j.schedule);
  return !j.time_zone
    ? `<span class="dim" data-tip="It runs on the host's clock, which the page cannot know. Saving it here pins it to UTC.">—</span>`
    : '<span class="dim">—</span>';
}
// A CronJob Homestead did not make (Helm, kubectl) is listed but not edited
// here: a save would drop the volumes, env and account this page never shows.
// Schedules lists CronJobs from every namespace (#739); only those in
// Homestead's own can be run, and only those it made there edited.
function jobOwner(j) {
  const where = j.ns ? `<div class="dim xs">${esc(j.ns)}</div>` : "";
  if (j.owner === "longhorn") return `${where}<div class="dim xs" data-tip="Change it in Data protection or Longhorn.">Longhorn recurring job · read-only</div>`;
  if (j.owner === "flux") return `${where}<div class="dim xs" data-tip="Change it in the git repository Flux applies.">Applied by Flux · read-only</div>`;
  if (j.managed !== false) return where;
  return `${where}<div class="dim xs" data-tip="Change it where it was made${j.runnable === false ? "" : "; Run now still works"}.">not made here · ${
    j.managed_by ? `managed by ${esc(j.managed_by)}` : "read-only"}</div>`;
}
/* How the last run ended, from the Jobs it started; "unknown" when they could
   not be read, never a guess that it went fine. */
const JOB_RESULT = { ok: ["ok", "succeeded"], failed: ["bad", "failed"], running: ["info", "running"],
  never: ["neutral", "never run"], unknown: ["warn", "unknown"] };
function jobResult(j) {
  const last = `<div>${fmtWhen(j.last)}</div>`;
  if (!j.result) return last;
  const [tone, word] = JOB_RESULT[j.result] || JOB_RESULT.unknown;
  return `${last}<div>${UI.chip(word, tone, { size: "slim", tip: j.result_reason || "" })}${j.missed
    ? " " + UI.chip("did not run", "warn", { size: "slim", tip: "It was due to start a run that has not started" }) : ""}</div>${
    j.result === "failed" && j.result_reason ? `<div class="dim xs">${esc(j.result_reason)}</div>` : ""}${
    j.result !== "ok" && j.last_success ? `<div class="dim xs">last succeeded ${fmtWhen(j.last_success)}</div>` : ""}`;
}
/* State (#919): how the job is doing, the worst that applies - failed, did not
   run, unknown, running, paused, ok. Unknown, in amber, when Homestead cannot
   tell: its runs could not be read, or whether a run was missed. Never a guess. */
const JOB_STATE = { failed: ["bad", "failed"], missed: ["warn", "did not run"], unknown: ["warn", "unknown"],
  running: ["neutral", "running"], paused: ["neutral", "paused"], ok: ["ok", "ok"], waiting: ["neutral", "not run yet"] };
function jobState(j) {
  if (j.result === "failed") return "failed";
  if (j.missed === true) return "missed";
  // Paused jobs and Longhorn's own CronJobs are never judged for a missed run.
  if (j.result === "unknown" || !j.result || (j.missed == null && !j.suspend && j.owner !== "longhorn")) return "unknown";
  if (j.active || j.result === "running") return "running";
  if (j.suspend) return "paused";
  return j.result === "never" ? "waiting" : "ok";
}
const JOB_STATE_TIP = { unknown: "Homestead cannot tell how its runs went, or whether one was missed",
  missed: "It was due to start a run that has not started", waiting: "It has not been due to run yet" };
function jobStateChip(j) {
  const key = jobState(j), [tone, word] = JOB_STATE[key];
  return UI.chip(word, tone, { tip: key === "unknown" && j.result_reason ? j.result_reason : JOB_STATE_TIP[key] || "" });
}
/* A row of the bar: Longhorn's own CronJobs are System (#919); every other
   one, an app's job applied by Flux too, is a CronJob. Its app from the claims it mounts. */
const scheduleRow = (j, apps) => ({ ...j, type: j.owner === "longhorn" ? "system" : "cron",
  app: window.ScheduleWeek ? window.ScheduleWeek.appOf(apps, j.ns, j.claims) : "", host: "",
  // Another namespace's CronJob comes without its claims (#739): its app cannot be seen.
  unseen: !!j.ns && !("claims" in j) });
const scheduleKinds = () => ({ cron: window.ScheduleWeek.KINDS.cron, system: window.ScheduleWeek.KINDS.system });
// What the page read, so a filter repaints without reading again.
const schedulesPage = { jobs: [], rows: [], appsUnread: "", filter: null };
// The generation of the job the editor opened ("" for a new one): the
// server refuses the save if its spec changed, or it appeared, since.
let jobEditing = "";
// An alert's link (/schedules?find=ns/name) finds its CronJob's row.
const jobFind = j => { try { return new URLSearchParams(location.search).get("find") === `${j.ns || ""}/${j.name}`; } catch { return false; } };
// "4 jobs: 1 failed, 1 did not run, 2 ok", worst first.
function schedulesSummary(rows) {
  const n = {};
  rows.forEach(j => { const key = jobState(j); n[key] = (n[key] || 0) + 1; });
  const parts = Object.keys(JOB_STATE).filter(key => n[key]).map(key => `${n[key]} ${JOB_STATE[key][1]}`);
  return `${rows.length} job${rows.length === 1 ? "" : "s"}${parts.length ? `: ${parts.join(", ")}` : ""}`;
}
async function viewSchedules() {
  const [js, apps] = await Promise.all([api("/api/schedules"), api("/api/workloads").catch(e => ({ failed: e?.message || "could not be read" }))]);
  const list = Array.isArray(apps) ? apps : [];
  Object.assign(schedulesPage, { jobs: js, rows: js.map(j => scheduleRow(j, list)), appsUnread: Array.isArray(apps) ? "" : apps?.failed || "could not be read",
    filter: window.ScheduleWeek.readFilter(scheduleKinds()) });
  schedulesPaint();
  try { document.querySelector("tr.flash-find")?.scrollIntoView?.({ block: "center" }); } catch { /* no page to scroll */ }
}
function schedulesPaint() {
  const { jobs: js, rows, filter: f } = schedulesPage;
  const unread = js.find(j => j.result === "unknown" && /could not be read/.test(j.result_reason || ""));
  // No row names a host, so Schedules filters on kind and App only: a Host
  // chosen on This week (kept for the session) must not empty it (review, #919).
  const byApp = { ...f, host: "" };
  // A row an alert links to (?find=) shows whatever the filter.
  const shown = rows.filter(j => window.ScheduleWeek.matches(j, byApp) || jobFind(j));
  paint(`${UI.pageHeader(`Schedules`, `${js.length} scheduled job${js.length === 1 ? "" : "s"} in every namespace · next runs in your time zone (${esc(yourZone())})`, `${UI.button("This week", "go('schedule-week')")} <button class="btn pri" data-need="operator" onclick="jobEdit()">＋ New schedule</button>`)}
    ${unread ? `<div class="note" style="margin-bottom:14px">How each last run ended is unknown. ${esc(unread.result_reason)}</div>` : ""}
    <div class="dim small sched-line">${esc(schedulesSummary(shown))}${schedulesPage.appsUnread ? ` · Could not read the apps list, so no job names its app: ${esc(schedulesPage.appsUnread)}` : ""}</div>
    ${window.ScheduleWeek.unseenLine(rows, byApp, "job", "jobs")}
    ${window.Timeline.filterBar({ entries: rows, counts: rows.reduce((all, r) => ({ ...all, [r.type]: (all[r.type] || 0) + 1 }), {}),
      filter: byApp, kinds: scheduleKinds(), call: "schedulesFilter", menus: [["app", "App", "All apps"]] })}
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="schedules" class="tbl stack"><thead><tr>
      <th>Name</th><th>Schedule</th><th data-nosort>Next run</th><th data-nosort>Last run</th><th>State</th><th></th></tr></thead><tbody>
      ${shown.map(j => `<tr${jobFind(j) ? ' class="flash-find"' : ""} data-find="${esc(`${j.ns || ""}/${j.name}`)}">
        <td><b>${esc(j.name)}</b>${jobOwner(j)}${j.app ? `<div class="dim xs">${esc(j.app)}</div>` : ""}${j.image ? `<div class="mono xs dim">${esc(j.image)}</div>` : ""}</td>
        <td class="small">${jobWhen(j)}</td>
        <td class="small">${jobNext(j)}</td>
        <td class="small dim">${jobResult(j)}</td>
        <td data-sort="${Object.keys(JOB_STATE).indexOf(jobState(j))}">${jobStateChip(j)}</td>
        <td data-actions>${actionBar([...(j.runnable === false ? [] : [{ label: "Run now", icon: "play", run: `jobRun(${jsq(j.name)})` }]),
          ...(j.managed === false || j.editable === false ? [] : [{ label: "Edit", icon: "edit", run: `jobEdit(${esc(JSON.stringify(schedulesPage.jobs.find(x => x.ns === j.ns && x.name === j.name) || j))})` },
          { label: "Delete", icon: "trash", run: `jobDel(${jsq(j.name)})`, danger: true }])], { label: `More actions for ${j.name}` })}</td></tr>`).join("")
        || `<tr><td colspan=6 class="empty">${rows.length ? "Nothing matches the filter." : "no schedules yet"}</td></tr>`}
    </tbody></table></div></div>`);
}
window.schedulesFilter = (key, value) => {
  const f = schedulesPage.filter;
  // No Host here: a Host kept from This week stays kept, for This week.
  if (!f || !["kind", "app"].includes(key)) return;
  f[key] = key === "kind" && !Object.hasOwn(scheduleKinds(), value) ? "" : String(value || "");
  window.ScheduleWeek.keepFilter(f, ["kind"]);
  schedulesPaint();
};
window.jobEdit = (j) => {
  j = j || { name: "", schedule: "0 3 * * *", image: "alpine:3.20", command: "" };
  jobEditing = j.generation ?? "";
  modal(j.name ? "Edit · " + j.name : "New schedule", `
    <div class="f"><label>Name</label><input type="text" id="j_name" value="${esc(j.name)}" ${j.name ? "readonly" : ""} placeholder="nightly-prune"></div>
    ${scheduleBuilder(j.schedule, j.time_zone || "Etc/UTC")}
    ${j.name && !j.time_zone ? `<div class="note" style="margin-bottom:14px">This schedule has no time zone, so it runs on the
      host's clock: that of the host running the cluster's controller. Saving it here pins it to UTC, so check the time above.</div>` : ""}
    <div class="f"><label>Image</label><input type="text" id="j_image" value="${esc(j.image || "alpine:3.20")}"></div>
    <div class="f"><label>Command</label><textarea id="j_cmd" rows="4" placeholder="echo hello">${esc(j.command || "")}</textarea></div>
    <label class="switch"><input type="checkbox" id="j_susp" ${j.suspend ? "checked" : ""}> Paused</label>
    ${UI.actions(`<button class="btn pri" onclick="jobSave()">Save</button>
      <button data-dialog-dismiss="true" class="btn" onclick="closeModal()">Cancel</button>`)}`);
  scheduleChanged();
};
window.jobSave = async () => {
  const body = { name: $("#j_name").value.trim(), schedule: $("#lj_cron").value.trim(),
    image: $("#j_image").value.trim(), command: $("#j_cmd").value, suspend: $("#j_susp").checked,
    generation: jobEditing };
  if (!body.name || !body.command) return toast("Name and command are required", "bad");
  try {
    await api("/api/schedules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("Schedule saved", "ok"); closeModal(); resetPaint(); viewSchedules();
  } catch (e) { toast(e.message, "bad"); }
};
window.jobRun = async name => {
  try { await api("/api/schedules/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast(`${name} started`, "ok"); } catch (e) { toast(e.message, "bad"); }
};
window.jobDel = async name => {
  if (!(await ask(`Delete schedule "${name}"?`, { title: "Delete schedule", ok: "Delete schedule", danger: true }))) return;
  try { await api("/api/schedules/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    toast("Deleted", "ok"); resetPaint(); viewSchedules(); } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  jobUtc, jobWhen, jobNext, jobOwner, JOB_RESULT, jobResult, JOB_STATE, jobState, JOB_STATE_TIP, jobStateChip, scheduleRow, scheduleKinds,
  schedulesPage, jobEditing, jobFind, schedulesSummary, viewSchedules, schedulesPaint
};

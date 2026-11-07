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
// The generation of the job the editor opened ("" for a new one): the
// server refuses the save if its spec changed, or it appeared, since.
let jobEditing = "";
// An alert's link (/schedules?find=ns/name) finds its CronJob's row.
const jobFind = j => { try { return new URLSearchParams(location.search).get("find") === `${j.ns || ""}/${j.name}`; } catch { return false; } };
async function viewSchedules() {
  const js = await api("/api/schedules");
  const unread = js.find(j => j.result === "unknown" && /could not be read/.test(j.result_reason || ""));
  paint(`${UI.pageHeader(`Schedules`, `${js.length} scheduled job${js.length === 1 ? "" : "s"} in every namespace · next runs in your time zone (${esc(yourZone())})`, `${UI.button("This week", "go('schedule-week')")} <button class="btn pri" data-need="operator" onclick="jobEdit()">＋ New schedule</button>`)}
    ${unread ? `<div class="note" style="margin-bottom:14px">How each last run ended is unknown. ${esc(unread.result_reason)}</div>` : ""}
    <div class="card flat pad0"><div class="tblwrap"><table data-sort="schedules" class="tbl stack"><thead><tr>
      <th>Name</th><th>Schedule</th><th data-nosort>Next run</th><th>Image</th><th data-nosort>Last run</th><th>State</th><th></th></tr></thead><tbody>
      ${js.map(j => `<tr${jobFind(j) ? ' class="flash-find"' : ""} data-find="${esc(`${j.ns || ""}/${j.name}`)}">
        <td><b>${esc(j.name)}</b>${jobOwner(j)}</td>
        <td class="small">${jobWhen(j)}</td>
        <td class="small">${jobNext(j)}</td>
        <td class="mono small dim">${esc(j.image)}</td>
        <td class="small dim">${jobResult(j)}</td>
        <td>${j.suspend ? UI.chip("paused", "neutral")
            : j.active ? UI.chip("running", "neutral") : UI.chip("active", "ok")}</td>
        <td data-actions>${actionBar([...(j.runnable === false ? [] : [{ label: "Run now", icon: "play", run: `jobRun(${jsq(j.name)})` }]),
          ...(j.managed === false || j.editable === false ? [] : [{ label: "Edit", icon: "edit", run: `jobEdit(${esc(JSON.stringify(j))})` },
          { label: "Delete", icon: "trash", run: `jobDel(${jsq(j.name)})`, danger: true }])], { label: `More actions for ${j.name}` })}</td></tr>`).join("") || `<tr><td colspan=7 class="empty">no schedules yet</td></tr>`}
    </tbody></table></div></div>`);
  try { document.querySelector("tr.flash-find")?.scrollIntoView?.({ block: "center" }); } catch { /* no page to scroll */ }
}
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
  jobUtc, jobWhen, jobNext, jobOwner, JOB_RESULT, jobResult, jobEditing, jobFind, viewSchedules
};

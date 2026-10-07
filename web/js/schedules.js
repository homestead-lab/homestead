/* Schedules (homestead_schedules.py): stop an app or VM at one time and start
   it at another, on the days chosen - overnight, or outside working hours.

   wlSchedule / vmSchedule set one; schedulesOverview() lists them all. Times
   are kept in the time zone they were chosen in (this browser's), and days
   are numbered from Monday, as the server counts them. */
const SCHED_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const SCHED = { kind: "", ns: "", name: "", back: null };

const schedZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } };

function schedWords(s) {
  if (!s) return "";
  const days = s.days || [];
  const span = days.length === 7 ? "every day" : days.join() === "0,1,2,3,4" ? "weekdays" : days.join() === "5,6" ? "weekends"
    : days.map(d => SCHED_DAYS[d]).join(", ");
  const parts = [s.stop && `stops ${s.stop}`, s.start && `starts ${s.start}`].filter(Boolean);
  const text = `${parts.join(", ")}, ${span}`;
  return text[0].toUpperCase() + text.slice(1);
}

/* The next stops and starts, worked out here: right when the schedule's time
   zone is this browser's, which it is when it was set here. */
function schedNext(s, now = new Date(), count = 3) {
  if (!s) return [];
  const out = [];
  for (let offset = 0; offset < 9 && out.length < count * 2; offset++) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
    if (!(s.days || []).includes((day.getDay() + 6) % 7)) continue;
    for (const action of ["stop", "start"]) {
      if (!s[action]) continue;
      const [h, m] = s[action].split(":").map(Number);
      const at = new Date(day.getFullYear(), day.getMonth(), day.getDate(), h, m);
      if (at > now) out.push({ at, action });
    }
  }
  return out.sort((a, b) => a.at - b.at).slice(0, count);
}

const schedWhen = at => new Date(at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });

/* A small mark on a row that has a schedule, saying it in words. */
function scheduleTag(row) {
  if (!row?.schedule) return "";
  const words = schedWords(row.schedule);
  return `<span class="tag sched-tag" data-tip="${esc(words)}" aria-label="On a schedule: ${esc(words)}">${icon("clock")}scheduled</span>`;
}

function schedRow(kind, ns, name) {
  const list = kind === "vm" ? STATE.data.vms : STATE.data.wl;
  return (list || []).find(x => x.ns === ns && x.name === name) || { ns, name };
}

function schedPreview() {
  const s = schedFromForm();
  const host = $("#sc_next");
  if (!host) return;
  if (!s.stop && !s.start) { host.innerHTML = '<span class="dim small">Choose a time to stop, to start, or both.</span>'; return; }
  if (!s.days.length) { host.innerHTML = '<span class="dim small">Choose at least one day.</span>'; return; }
  host.innerHTML = `<b>${esc(schedWords(s))}</b><div class="sched-next">${schedNext(s).map(e =>
    `<span class="ui-chip ${e.action === "stop" ? "" : "ok"}">${esc(e.action === "stop" ? "Stops" : "Starts")} ${esc(schedWhen(e.at))}</span>`).join("")}</div>`;
}

function schedFromForm() {
  return { stop: $("#sc_stop")?.value || "", start: $("#sc_start")?.value || "",
    days: [...document.querySelectorAll("#mbody input[name=sc_day]:checked")].map(i => +i.value), tz: schedZone() };
}

window.schedDays = which => {
  const want = { all: [0, 1, 2, 3, 4, 5, 6], week: [0, 1, 2, 3, 4], end: [5, 6] }[which] || [];
  document.querySelectorAll("#mbody input[name=sc_day]").forEach(i => { i.checked = want.includes(+i.value); });
  schedPreview();
};

function scheduleDialog(kind, ns, name, back = null) {
  Object.assign(SCHED, { kind, ns, name, back });
  const row = schedRow(kind, ns, name), s = row.schedule || null;
  const days = s ? s.days : [0, 1, 2, 3, 4];
  const zone = schedZone(), elsewhere = s && s.tz && s.tz !== zone;
  const how = kind === "vm" ? `${name} is shut down cleanly, as Stop does, and started through the same room check as Start.`
    : `${name} is stopped, as Stop does, and started again with the copies it had, after the same room check as Start.`;
  modal(`Schedule · ${name}`, `<div class="ui-stack">
    ${UI.lead(esc(`Stop ${name} and start it again at set times, on the days chosen. ${how} Starting it by hand in between is fine; it stops again at the next stop time.`))}
    ${elsewhere ? UI.callout("info", "", esc(`These times were set in ${s.tz}. Saving here sets them in ${zone}.`)) : ""}
    ${UI.fields(
      UI.field("Stop at", `<input id="sc_stop" type="time" value="${esc(s?.stop ?? (s ? "" : "01:00"))}" oninput="schedPreview()">`, { help: "Leave empty to only start it." }),
      UI.field("Start at", `<input id="sc_start" type="time" value="${esc(s?.start ?? (s ? "" : "07:00"))}" oninput="schedPreview()">`, { help: "Leave empty to only stop it." }))}
    ${UI.field("Days", `<div class="sched-days">${SCHED_DAYS.map((d, i) => `<label class="sched-day"><input type="checkbox" name="sc_day" value="${i}"${days.includes(i) ? " checked" : ""} onchange="schedPreview()"><span>${d}</span></label>`).join("")}</div>
      <div class="sched-presets">${UI.button("Every day", "schedDays('all')")}${UI.button("Weekdays", "schedDays('week')")}${UI.button("Weekends", "schedDays('end')")}</div>`)}
    <div class="sched-preview" id="sc_next" aria-live="polite"></div>
    <div class="dim xs">Times in ${esc(zone)}. A time missed while Homestead was not running is skipped, not done late.</div>
    <div id="sc_last"></div>
    ${UI.actions((back ? UI.button("Back", "schedBack()") : UI.cancel()) + UI.button("Save", "schedSave()", { kind: "pri", id: "sc_go", attrs: 'data-need="operator"' }),
      s ? UI.button("No schedule", "schedSave(true)", { attrs: 'data-need="operator"' }) : "")}</div>`);
  schedPreview();
  if (s) api("/api/power-schedules").then(d => {
    const item = (d.items || []).find(i => i.kind === kind && i.ns === ns && i.name === name);
    const last = item?.last, host = $("#sc_last");
    if (!last || !host) return;
    host.innerHTML = last.ok ? `<div class="dim small">Last: ${esc(schedWhen(last.at * 1000))}, ${esc(last.detail)}</div>`
      : UI.callout("warn", `The ${last.action} at ${schedWhen(last.at * 1000)} did not happen`, esc(last.detail));
  }).catch(() => {});
}

window.wlSchedule = (ns, name, back = null) => scheduleDialog("app", ns, name, back);
window.vmSchedule = (ns, name, back = null) => scheduleDialog("vm", ns, name, back);
window.schedBack = () => (SCHED.back || closeModal)();

window.schedSave = async (remove = false) => {
  const go = $("#sc_go"), schedule = remove ? null : schedFromForm();
  if (schedule && !schedule.stop && !schedule.start) return toast("Choose a time to stop, to start, or both", "warn");
  if (schedule && !schedule.days.length) return toast("Choose at least one day", "warn");
  if (go) go.disabled = true;
  try {
    const r = await api("/api/power-schedules/set", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: SCHED.kind, ns: SCHED.ns, name: SCHED.name, schedule }) });
    toast(r.detail, "ok");
    const row = schedRow(SCHED.kind, SCHED.ns, SCHED.name);
    if (row) row.schedule = r.schedule || null;
    if (SCHED.back) SCHED.back(); else closeModal();
    if (STATE.view === "workloads" && typeof renderWorkloads === "function") renderWorkloads();
    if (STATE.view === "vms" && typeof viewVMs === "function") viewVMs();
  } catch (e) { toast(e.message, "bad"); if (go) go.disabled = false; }
};

/* Every schedule, the next time each acts, and how its last went. */
window.schedulesOverview = async () => {
  modal("Power schedules", '<div class="empty"><span class="spin2"></span> Loading…</div>');
  let d;
  try { d = await api("/api/power-schedules"); }
  catch (e) { $("#mbody").innerHTML = UI.callout("bad", "The schedules could not be read", esc(e.message)) + UI.actions(UI.cancel("Close")); return; }
  const items = d.items || [];
  const open = i => `${i.kind === "vm" ? "vmSchedule" : "wlSchedule"}(${jsArg(i.ns)},${jsArg(i.name)},schedulesOverview)`;
  $("#mbody").innerHTML = `<div class="ui-stack">
    ${UI.lead("Apps and VMs that stop and start on their own. Set one from an app's or a VM's ⋯ menu, under Schedule.")}
    ${items.length ? `<div class="sched-list">${items.map(i => `<div class="sched-item">
      <div class="sched-name"><b>${esc(i.name)}</b><span class="dim xs">${i.kind === "vm" ? "VM" : "app"} · ${esc(i.ns)}</span></div>
      <div class="sched-what"><span>${esc(i.words)}</span>
        ${i.next?.length ? `<span class="dim small">Next: ${esc(i.next[0].action === "stop" ? "stops" : "starts")} ${esc(schedWhen(i.next[0].at * 1000))}</span>` : ""}
        ${i.last && !i.last.ok ? `<span class="tag bad" data-tip="${esc(i.last.detail)}">last ${esc(i.last.action)} failed</span>` : ""}</div>
      <span class="sched-acts">${UI.button("Change", open(i), { attrs: 'data-need="operator"' })}</span></div>`).join("")}</div>`
      : '<div class="empty small">Nothing is on a schedule yet.</div>'}
    ${UI.actions(UI.cancel("Close"))}</div>`;
};

/* ---------------- Consistent snapshots (homestead_fs_freeze.py) ----------------
   Longhorn 1.7 and later can freeze a volume's filesystem for the moment of
   each snapshot, its own scheduled ones and backups included. Data
   protection shows the cluster setting and each volume's choice; an admin
   changes them after a review. */
const FS_FREEZE_MODES = [["ignored", "Follow the cluster"], ["enabled", "Always freeze"], ["disabled", "Never freeze"]];
const FS_FREEZE_SHOWN = { yes: ["freezes", "ok"], sync: ["syncs instead", "info"], no: ["not frozen", ""] };
const fsFreezeWhen = at => (typeof answerSince === "function" ? answerSince(at) : fmtAt(at));

/* What a snapshot of v does with the cluster switch at on. */
function fsFreezeWould(v, on) {
  if (v.engine === "v2") return "no";
  return v.mode === "enabled" || (v.mode !== "disabled" && on) ? v.when_on : "no";
}

function fsFreezeSummary(d) {
  const c = d.counts || { total: 0, freezing: 0 };
  const always = c.freezing ? ` (${c.freezing} volume${c.freezing === 1 ? " is" : "s are"} set to always freeze)` : "";
  const said = d.unknown_value
    ? `<b>Unknown</b> · Longhorn's setting is neither on nor off${always}`
    : d.enabled
      ? `<b>On</b> · ${c.freezing} of ${c.total} volume${c.total === 1 ? " is" : "s are"} frozen for each snapshot`
      : `<b>Off</b> · snapshots are crash-consistent: Longhorn syncs each volume first, but a write can land in between${always}`;
  const turned = d.unknown_value ? "Changed" : d.enabled ? "Turned on" : "Turned off";
  const who = !d.changed ? "" : d.changed.outside ? ` · ${turned} outside Homestead`
    : ` · ${turned} by ${esc(d.changed.who ? UI.who(d.changed.who) : "Homestead")}, ${esc(fsFreezeWhen(d.changed.at))}`;
  return `<div class="small">${said}${who}</div>`;
}

/* Who set a volume's own choice: a person, Homestead, or a change made elsewhere. */
const fsFreezeSetBy = changed => !changed ? "" : `<div class="dim xs">${changed.outside ? "Set outside Homestead"
  : `Set by ${esc(changed.who ? UI.who(changed.who) : "Homestead")}, ${esc(fsFreezeWhen(changed.at))}`}</div>`;

function fsFreezeRow(v) {
  const [label, tone] = FS_FREEZE_SHOWN[v.freezes] || ["unknown", "warn"];
  const options = FS_FREEZE_MODES.map(([mode, text]) => `<option value="${mode}"${v.mode === mode ? " selected" : ""}${
    mode === "enabled" && v.engine === "v2" ? " disabled" : ""}>${text}</option>`).join("");
  // A viewer sees the choice, greyed out, rather than an empty column.
  const locked = typeof can === "function" && !can("admin") ? " disabled" : "";
  return [`<b>${esc(v.claim || v.name)}</b><div class="dim xs mono">${esc(v.name)}</div>`,
    `<select aria-label="${esc(`Freeze ${v.claim || v.name} for snapshots`)}" onchange="fsFreezeVolume(${jsq(v.name)},this.value)"${locked}>${options}</select>${fsFreezeSetBy(v.changed)}`,
    `${UI.chip(label, tone)}${v.why ? `<div class="dim xs">${esc(v.why)}</div>` : ""}`];
}

async function loadFsFreeze() {
  const host = document.getElementById("fs-freeze");
  if (!host) return;
  let d;
  try { d = await api("/api/longhorn/fs-freeze"); }
  catch (e) { host.innerHTML = UI.section("Consistent snapshots", UI.callout("warn", "The filesystem freeze could not be read", esc(e.message))); return; }
  STATE.data.fsFreeze = d;
  if (!d.supported) { host.innerHTML = UI.section("Consistent snapshots", UI.empty(d.why)); return; }
  const toggle = `<label class="switch" data-need="admin"><input type="checkbox" id="fsf_on" ${d.enabled ? "checked" : ""}
    onchange="fsFreezeReview(this.checked)"> Freeze filesystems for snapshots</label>`;
  const notices = UI.notices([
    d.unknown_value ? ["warn", `Longhorn's setting holds "${d.value}", which is neither on nor off; it is shown as off.`] : null,
    d.old_kernels.length ? ["warn", `Kernel 5.17 or older on ${d.old_kernels.map(k => `${k.host} (${k.kernel})`).join(", ")}: Longhorn warns that a volume failing mid-freeze can leave its filesystem stuck until that host restarts.`] : null,
    d.from_chart ? ["info", "Longhorn's chart sets this too: applying the chart again puts the chart's value back."] : null].filter(Boolean));
  host.innerHTML = UI.section("Consistent snapshots", `${fsFreezeSummary(d)}${notices}
    ${UI.guide("What freezing does", `<p>For the moment of each snapshot, Longhorn freezes the volume's filesystem, takes the snapshot, then thaws it. Writes wait, usually under a second, and the snapshot holds the files as they were at one instant, as if the app had been stopped cleanly. Every backup is made from a snapshot, so backups gain the same.</p>
      <p>It covers Longhorn's own scheduled jobs and Homestead's snapshots, backups and before-update snapshots alike. Block volumes (VM disks) and shared (RWX) volumes have no filesystem on the host to freeze: Longhorn syncs them instead. If a freeze fails, Longhorn fails that snapshot rather than take it unfrozen.</p>
      <p>A database still does best with its own dump or checkpoint: a frozen filesystem has no half-written files, but the database may still hold work in memory, which it recovers from as from a power cut.</p>`)}
    ${UI.more(`Per volume (${d.volumes.length})`, UI.table([{ label: "Volume" }, { label: "Freeze" }, { label: "Snapshots" }], d.volumes.map(fsFreezeRow),
      { empty: "No Longhorn volumes yet." }))}`, toggle);
  if (window.applyRole) window.applyRole();
}

window.fsFreezeReview = on => {
  const d = STATE.data.fsFreeze || {}, box = $("#fsf_on");
  if (box) box.checked = !!d.enabled;   // the switch moves once the change is saved
  const volumes = d.volumes || [], count = what => volumes.filter(v => fsFreezeWould(v, on) === what).length;
  const old = on && (d.old_kernels || []).length;
  const stay = volumes.filter(v => v.mode === "enabled" && v.engine !== "v2").map(v => v.claim || v.name);
  modal(on ? "Freeze filesystems for snapshots" : "Stop freezing filesystems", `${UI.lead(on
    ? "For each snapshot, Longhorn freezes the volume's filesystem, takes it, then thaws it. Writes wait, usually under a second, and snapshots and backups hold the files as they were at one instant."
    : "Snapshots go back to crash-consistent: Longhorn syncs each volume first, but a write can land between the sync and the snapshot.")}
    ${UI.facts([["Frozen for each snapshot", String(count("yes"))], ["Synced instead", String(count("sync"))], ["Not frozen", String(count("no"))]])}
    ${UI.notices([
      d.unknown_value ? ["warn", `Longhorn's setting holds "${d.value}", which this replaces.`] : null,
      old ? ["warn", `Kernel 5.17 or older on ${d.old_kernels.map(k => k.host).join(", ")}: if a volume fails mid-freeze, its filesystem can stay stuck until that host restarts.`] : null,
      on ? ["info", "If a freeze fails, Longhorn fails that snapshot rather than take it unfrozen; the job says so."] : null,
      !on && stay.length ? ["info", `Still frozen, set to always: ${stay.join(", ")}.`] : null,
      d.from_chart ? ["info", "Longhorn's chart sets this too: applying the chart again puts its value back."] : null].filter(Boolean))}
    ${old ? UI.ack("fsf_old", "Turn it on anyway", { onchange: "$('#fsf_go').disabled=!this.checked" }) : ""}
    ${UI.actions(UI.cancel() + UI.button(on ? "Turn on" : "Turn off", `fsFreezeSave(${on})`, { kind: "pri", id: "fsf_go", attrs: 'data-need="admin"', disabled: !!old }))}`);
  if (window.applyRole) window.applyRole();
};

window.fsFreezeSave = async on => {
  const button = $("#fsf_go");
  if (button) button.disabled = true;
  try {
    const r = await api("/api/longhorn/fs-freeze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !!on }) });
    toast(r.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  closeModal();
  await loadFsFreeze();
};

window.fsFreezeVolume = async (volume, mode) => {
  // The kernel warning the cluster switch reviews applies to one volume too.
  const d = STATE.data.fsFreeze || {}, old = d.old_kernels || [];
  if (mode === "enabled" && old.length) {
    const v = (d.volumes || []).find(x => x.name === volume) || {};
    const go = await ask(`Kernel 5.17 or older on ${old.map(k => k.host).join(", ")}: if ${v.claim || volume} fails mid-freeze, its filesystem can stay stuck until that host restarts.`,
      { title: `Always freeze ${v.claim || volume}`, ok: "Always freeze", danger: true });
    if (!go) return loadFsFreeze();
  }
  try {
    const r = await api("/api/longhorn/fs-freeze/volume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ volume, mode }) });
    toast(r.detail, "ok");
  } catch (e) { toast(e.message, "bad"); }
  await loadFsFreeze();
};

window.loadFsFreeze = loadFsFreeze;

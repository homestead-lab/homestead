/* Homestead's own updates: its release, and the helpers it runs beside it
   (the SMB and NFS servers, the object store moves use). They are not apps -
   the Containers page hides them with the platform - so they are updated
   from here: a button on the top bar that appears when one is waiting, the
   dialog it opens, and a card under Settings › Updates. The update itself is
   the same reviewed rollout an app gets (reviewImageActions), Homestead last. */
const HOMESTEAD_PARTS = { self: "Homestead", smb: "SMB server", nfs: "NFS server", objectstore: "Object store" };
const HOMESTEAD_RELEASES = "https://github.com/wjcloudy/homestead/releases";
let HOMESTEAD_CHANNEL_SAVING = false;

function homesteadChannelPicker() {
  const channel = STATE.data.appSettings?.updates?.channel || STATE.data.imageUpdates?.channel || "prod";
  return `<div class="srows">${settingRow("Release channel",
    "Prod has stable releases. Dev has preview releases. Changing channel checks for a release; installation still needs your review.",
    `<select aria-label="Homestead release channel" data-need="admin" onchange="homesteadChannelSave(this.value)" ${HOMESTEAD_CHANNEL_SAVING || !can("admin") ? "disabled" : ""}>
      <option value="prod" ${channel === "prod" ? "selected" : ""}>Prod · stable</option>
      <option value="dev" ${channel === "dev" ? "selected" : ""}>Dev · preview</option></select>`)}</div>`;
}

window.homesteadChannelSave = async channel => {
  if (HOMESTEAD_CHANNEL_SAVING) return;
  HOMESTEAD_CHANNEL_SAVING = true;
  homesteadUpdateRepaint();
  try {
    STATE.data.appSettings = await api("/api/image-updates/channel", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel }) });
    STATE.data.imageUpdates = null;
    STATE.data.imageUpdateMap = {};
    homesteadUpdateRepaint();
    await loadImageUpdates(true, true, "homestead");
    toast(`${channel === "dev" ? "Dev" : "Prod"} channel saved`, "ok");
  } catch (error) { toast(error.message, "bad"); }
  finally { HOMESTEAD_CHANNEL_SAVING = false; homesteadUpdateRepaint(); }
};

function homesteadUpdates() {
  const report = STATE.data.imageUpdates;
  const parts = HomesteadUpdateState.homesteadWorkloads(report);
  const self = parts.find(w => w.homestead === "self");
  const release = (self?.images || []).find(i => i.available && i.candidate_tag)?.candidate_tag || "";
  return { report, parts, self, release, waiting: parts.filter(w => w.available),
    failed: parts.filter(w => (w.images || []).some(i => i.error)) };
}

function paintHomesteadNotice() {
  const notice = $("#homesteadNotice");
  if (notice) {
    const { waiting, release } = homesteadUpdates();
    notice.classList.toggle("hidden", !waiting.length);
    const words = release ? `Homestead ${release} is available`
      : `${waiting.length} Homestead helper update${waiting.length === 1 ? "" : "s"}`;
    notice.title = words;
    notice.setAttribute("aria-label", words);
    const label = $("#homesteadNoticeText");
    if (label) label.textContent = release ? `v${release}` : "Helpers";
  }
  paintBell();
  homesteadUpdateCardPaint();
}

/* The bell: one count of what wants attention, and a line for each kind -
   Homestead's own update, container updates, and image checks that failed,
   which are counted apart so one unreachable registry is not an update. */
function paintBell() {
  const bell = $("#bell");
  if (!bell) return;
  const { waiting, release } = homesteadUpdates();
  const images = window.BELL?.images || 0, errors = window.BELL?.errors || 0;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const row = (run, iconName, text, cls = "") => `<button class="${cls}" onclick="this.closest('details').open=false;${run}">${icon(iconName)}${esc(text)}</button>`;
  // Background jobs: running ones at the top, with their progress; a failed
  // one stays under Needs you until it is dismissed.
  const operations = STATE.data.operations || [];
  const active = operations.filter(op => window.operationActive ? operationActive(op) : !["succeeded", "failed", "cancelled"].includes(op.status));
  const failedJobs = operations.filter(op => op.status === "failed");
  const jobRow = op => `<button class="bell-job" onclick="this.closest('details').open=false;openOperation(${jsq(op.href || "/")},${jsq(op.id || "")})">
      <span class="bell-job-top"><b>${esc(op.title)}</b><span>${op.progress != null ? `${Math.round(op.progress)}%` : esc(op.status)}</span></span>
      <span class="bell-job-msg">${esc(op.message || op.status || "")}</span>
      ${op.progress != null && Number.isFinite(Number(op.progress)) ? `<span class="jobmeter"><span style="width:${Math.max(0, Math.min(100, Number(op.progress)))}%"></span></span>` : ""}</button>`;
  const alertCount=(window.PWA_ALERTS?.report?.active || []).filter(a=>!a.acknowledged).length;
  const rows = [
    alertCount ? row("pwaAlertsDialog()", "alert", plural(alertCount, "active alert"), "danger") : "",
    waiting.length ? row("homesteadUpdateDialog()", "update", release ? `Homestead ${release} is available` : plural(waiting.length, "Homestead helper update")) : "",
    images ? row("imageUpdateCenter()", "box", plural(images, "container update")) : "",
    errors ? row("imageUpdateCenter()", "alert", `${plural(errors, "image check")} failed`, "danger") : "",
    ...failedJobs.slice(0, 3).map(op => row(`openOperation(${jsq(op.href || "/")},${jsq(op.id || "")})`, "alert", `${op.title} failed`, "danger")),
  ].filter(Boolean);
  const count = alertCount + images + (waiting.length ? 1 : 0) + failedJobs.length;
  const words = [alertCount ? plural(alertCount,"active alert") : "", active.length ? `${plural(active.length, "job")} running` : "", waiting.length ? (release ? `Homestead ${release}` : "Homestead helpers") : "", images ? plural(images, "container update") : "",
    errors ? `${plural(errors, "failed check")}` : "", failedJobs.length ? plural(failedJobs.length, "failed job") : ""].filter(Boolean).join(", ") || "Nothing needs you";
  const badge = $("#bellCount"), dot = $("#bellErrors"), summary = bell.querySelector("summary");
  if (badge) { badge.textContent = count; badge.classList.toggle("hidden", !count); }
  if (dot) { dot.textContent = errors; dot.classList.toggle("hidden", !errors); }
  if (summary) { summary.title = words; summary.setAttribute("aria-label", `Notifications: ${words}`); }
  bell.classList.toggle("has-news", count > 0 || errors > 0);
  bell.classList.toggle("running", active.length > 0);
  const pop = $("#bellPop");
  if (pop) pop.innerHTML = (active.length ? `<div class="bell-head">Running now</div>${active.slice(0, 4).map(jobRow).join("")}` : "")
    + (rows.length ? `<div class="bell-head">Needs you</div>${rows.join("")}` : active.length ? "" : '<div class="bell-empty">Nothing needs you</div>')
    + `<button class="bell-all" onclick="this.closest('details').open=false;pwaAlertsDialog()">All alerts</button>`
    + (operations.length ? `<button class="bell-all" onclick="this.closest('details').open=false;jobsDialog()">All jobs · ${operations.length}</button>` : "");
}
window.paintBell = paintBell;
window.paintHomesteadNotice = paintHomesteadNotice;

/* One line per part: what it runs and what it would move to. */
function homesteadPartRows(parts) {
  return parts.map(w => {
    const running = (w.images || []).map(i => imageVersion(i.source || i.deployed).tag).filter(Boolean).join(", ");
    const failure = (w.images || []).find(i => i.error)?.error;
    const state = w.available ? `<span class="pill warn">${esc(updateVersions(w) || "update available")}</span>`
      : failure ? `<span class="pill crit" data-tip="${esc(failure)}">check failed</span>`
      : w.unchecked ? '<span class="pill neutral">not checked yet</span>' : '<span class="pill ok">current</span>';
    return `<div class="hs-part"><div><b>${esc(HOMESTEAD_PARTS[w.homestead] || w.name)}</b>
        <span class="dim xs mono">${esc(w.name)}${running ? ` · ${esc(running)}` : ""}</span></div>${state}</div>`;
  }).join("");
}

/* ---------- linked clusters ----------
   Each linked Homestead's own parts, asked through this one's relay, so one
   review can update them all. A cluster from before 2.8.220 does not tag its
   parts: its Homestead is the Deployment named homestead. Nothing is picked
   until someone ticks it. */
const FLEET_UPDATES = { rows: {}, picked: new Set(), loading: false, at: 0 };

function linkedMembers() {
  return (window.FLEET?.view?.linked ? FLEET.view.members || [] : []).filter(m => !m.self);
}

function remoteParts(report) {
  return (report?.workloads || []).map(w => ({ ...w, homestead: w.homestead || (w.name === "homestead" ? "self" : "") }))
    .filter(w => w.homestead)
    .sort((a, b) => Number(b.homestead === "self") - Number(a.homestead === "self"));
}

async function fleetUpdatesLoad(force = false) {
  const members = linkedMembers();
  if (!members.length || FLEET_UPDATES.loading || (!force && Date.now() - FLEET_UPDATES.at < 60000)) return;
  FLEET_UPDATES.loading = true;
  homesteadUpdateRepaint();
  await Promise.all(members.map(async m => {
    if (!m.reachable) { FLEET_UPDATES.rows[m.id] = { member: m, error: m.error || "not answering" }; return; }
    try {
      // Only its Homestead parts are shown, so only they are checked afresh.
      const report = await api(`/api/image-updates${force ? "?force=1&only=homestead" : ""}`, { headers: { "X-Homestead-Cluster": m.id }, keep: true });
      FLEET_UPDATES.rows[m.id] = { member: m, parts: remoteParts(report), channel: report.channel || "prod" };
    } catch (error) {
      FLEET_UPDATES.rows[m.id] = { member: m, error: error.message };
    }
  }));
  for (const id of [...FLEET_UPDATES.picked]) if (!(FLEET_UPDATES.rows[id]?.parts || []).some(w => w.available)) FLEET_UPDATES.picked.delete(id);
  FLEET_UPDATES.loading = false;
  FLEET_UPDATES.at = Date.now();
  homesteadUpdateRepaint();
}

function fleetUpdateRows() {
  const members = linkedMembers();
  if (!members.length) return "";
  const rows = members.map(m => {
    const row = FLEET_UPDATES.rows[m.id];
    const self = row?.parts?.find(w => w.homestead === "self");
    const release = (self?.images || []).find(i => i.available && i.candidate_tag)?.candidate_tag || "";
    const waiting = (row?.parts || []).filter(w => w.available);
    const state = !row ? '<span class="pill neutral">checking…</span>'
      : row.error ? `<span class="pill crit" data-tip="${esc(row.error)}">not answering</span>`
      : waiting.length ? `<span class="pill warn">${esc(release ? `${m.version || "?"} → ${release}` : `${waiting.length} helper update${waiting.length === 1 ? "" : "s"}`)}</span>`
      : '<span class="pill ok">current</span>';
    const pick = waiting.length ? `<label class="hs-pick" title="Update ${esc(m.name)} too"><input type="checkbox" ${FLEET_UPDATES.picked.has(m.id) ? "checked" : ""}
        onchange="fleetUpdatePick(${jsq(m.id)}, this.checked)"><span>Include</span></label>` : "";
    return `<div class="hs-part"><div><b>${esc(m.name)}</b><span class="dim xs mono">${m.version ? `v${esc(m.version)}` : "version unknown"}${row?.channel ? ` · ${row.channel === "dev" ? "Dev" : "Prod"}` : ""}${m.url ? ` · ${esc(m.url)}` : ""}</span></div>
      <div class="row hs-part-end">${state}${pick}</div></div>`;
  }).join("");
  return `<div class="hs-fleet"><div class="between"><span class="dim xs">LINKED CLUSTERS</span>
      <button class="btn sm" onclick="fleetUpdatesLoad(true)" ${FLEET_UPDATES.loading ? "disabled" : ""}>${FLEET_UPDATES.loading ? "Checking…" : "↻ Check"}</button></div>
    <div class="hs-parts">${rows}</div>
    <p class="dim xs">Ticked clusters are updated in the same review, before this one. Keeping every linked Homestead on one release keeps moves between them working.</p></div>`;
}

window.fleetUpdatePick = (id, on) => {
  if (on) FLEET_UPDATES.picked.add(id); else FLEET_UPDATES.picked.delete(id);
  homesteadUpdateRepaint();
};
window.fleetUpdatesLoad = fleetUpdatesLoad;

function pickedFleetItems() {
  return [...FLEET_UPDATES.picked].flatMap(id => {
    const row = FLEET_UPDATES.rows[id];
    return (row?.parts || []).filter(w => w.available)
      .map(w => ({ ns: w.ns, name: w.name, part: w.homestead, cluster: id, clusterName: row.member.name }));
  });
}

function homesteadUpdateRepaint() {
  const body = $("#hsUpdateBody");
  if (body) { body.innerHTML = homesteadUpdateBody(); if (window.applyRole) applyRole(); }
  homesteadUpdateCardPaint();
}

function homesteadUpdateBody(inCard = false) {
  const { report, parts, release, waiting, failed } = homesteadUpdates();
  const picker = homesteadChannelPicker();
  if (!report) return picker + '<div class="empty small"><span class="spin2"></span> Asking the registries…</div>';
  const checked = checkedAgo();
  const head = release
    ? `<div class="hs-release"><span class="dim xs">NEW RELEASE</span><b>Homestead ${esc(release)}</b>
        <span class="dim small">You run v${esc(HOMESTEAD_VERSION)} · <a href="${safeHref(`${HOMESTEAD_RELEASES}/tag/v${release}`)}" target="_blank" rel="noopener">what's new ${icon("ext")}</a></span></div>`
    : `<div class="hs-release current"><span class="dim xs">RELEASE</span><b>Homestead v${esc(HOMESTEAD_VERSION)}</b>
        <span class="dim small">${failed.length ? "Release check needs attention" : waiting.length ? "Current; a helper has an update" : "Up to date"}${checked ? ` · ${esc(checked)}` : ""} · <a href="${safeHref(HOMESTEAD_RELEASES)}" target="_blank" rel="noopener">releases ${icon("ext")}</a></span></div>`;
  const others = [...FLEET_UPDATES.picked].filter(id => FLEET_UPDATES.rows[id]);
  const label = waiting.length
    ? (release ? `Update to ${release}` : `Update ${waiting.length === 1 ? "helper" : "helpers"}`)
      + (others.length ? ` and ${others.length} linked cluster${others.length === 1 ? "" : "s"}` : "")
    : others.length ? `Update ${others.length} linked cluster${others.length === 1 ? "" : "s"}` : "";
  return `${picker}${head}
    ${parts.length ? `<div class="hs-parts">${homesteadPartRows(parts)}</div>` : ""}
    ${failed.length ? '<p class="dim small">A part whose check failed is compared with its registry again on the next check.</p>' : ""}
    ${fleetUpdateRows()}
    ${label ? `<p class="dim small">${inCard ? "" : "Each part restarts while it changes, this Homestead last; this page reconnects when it is back. "}Nothing changes until you review and accept it.</p>` : ""}
    <div class="row hs-actions">${label ? `<button class="btn pri" data-need="operator" onclick="homesteadUpdateReview()" ${HOMESTEAD_CHANNEL_SAVING ? "disabled" : ""}>${esc(label)}</button>` : ""}
      <button class="btn" onclick="homesteadUpdateCheck(this)" ${HOMESTEAD_CHANNEL_SAVING ? "disabled" : ""}>↻ Check now</button></div>`;
}

window.homesteadUpdateDialog = async () => {
  modal("Homestead updates", `<div class="ui-stack hs-update" id="hsUpdateBody">${homesteadUpdateBody()}</div>`);
  fleetUpdatesLoad();
  if (!STATE.data.imageUpdates) {
    await loadImageUpdates(false, true);
    homesteadUpdateRepaint();
  }
  if (window.applyRole) applyRole();
};

/* One review for everything ticked: the linked clusters first, each
   helpers-then-Homestead, and this Homestead last, since the others are
   reached through it. */
window.homesteadUpdateReview = () => {
  const { waiting } = homesteadUpdates();
  const items = [...pickedFleetItems(), ...waiting.map(w => ({ ns: w.ns, name: w.name, part: w.homestead }))];
  if (!items.length) return toast("Homestead is up to date", "ok");
  return reviewImageActions(items);
};

window.homesteadUpdateCheck = async button => {
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  // Homestead's own parts only: checking every app's registry is Containers' job.
  const report = await loadImageUpdates(true, true, "homestead");
  if (report) {
    const { release, waiting } = homesteadUpdates();
    toast(release ? `Homestead ${release} is available` : waiting.length ? "A Homestead helper has an update" : "Homestead is up to date", "ok");
  } else if (button) { button.disabled = false; button.textContent = "↻ Check now"; }
  if (linkedMembers().length) await fleetUpdatesLoad(true);
  homesteadUpdateRepaint();
};

/* Settings › Updates: always there, current or not. */
function homesteadUpdateCardPaint() {
  const card = $("#homesteadUpdateCard");
  if (!card) return;
  card.innerHTML = UI.moduleHeader("Homestead updates", `Homestead and its built-in services
    ${tip("Includes the SMB and NFS servers and the object store used for moves. Installation always requires review. Container update policy is also under Settings → Updates.")}`) +
    `<div class="ui-stack">${homesteadUpdateBody(true)}</div>`;
  if (window.applyRole) applyRole();
  fleetUpdatesLoad();
}
window.homesteadUpdateCardPaint = homesteadUpdateCardPaint;

/* Where each part of Homestead is looked after, from its row on Containers. */
window.homesteadPartManage = part => {
  if (part === "objectstore") { settingsTab("fleet"); return go("settings"); }
  if (part === "smb" || part === "nfs") return go("shares");
  settingsTab("updates");
  go("settings");
};

const homesteadNoticeButton = $("#homesteadNotice");
if (homesteadNoticeButton) {
  homesteadNoticeButton.onclick = () => homesteadUpdateDialog();
  homesteadNoticeButton.onkeydown = event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); homesteadUpdateDialog(); }
  };
}

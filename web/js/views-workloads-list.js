/* Homestead Containers: the list itself, its cards and rows, the start review and Scale (#911). One of the scripts views-workloads.js was split into; they share the page's globals and load in the order index.html lists them. */
function renderWorkloads() {
  const q = STATE.q.toLowerCase();
  // Every linked cluster at once: each cluster is a group, with its chip.
  const everything = (STATE.data.wl || []).map(w => fleetAll() && w.site ? { ...w, group: w.site.name } : w);
  const platform = everything.filter(w => w.platform);
  const all = platformShown() ? everything : everything.filter(w => !w.platform);
  // A chip for a group that has since emptied would hide everything.
  const saved = workloadGroupPrefs().pick;
  const group = saved === NO_GROUP ? (all.some(w => !w.group) ? saved : "") : workloadGroupNames(all).includes(saved) ? saved : "";
  const rows = all.filter(x => !q || x.name.includes(q) || x.ns.includes(q) || (x.group || "").toLowerCase().includes(q) ||
    x.images.join(" ").toLowerCase().includes(q) || x.nodes.join(" ").includes(q))
    .filter(x => !group || (group === NO_GROUP ? !x.group : x.group === group));
  const report = STATE.data.imageUpdates;
  // Platform containers move on with the part they belong to, not one by one.
  const platformKeys = new Set(platform.map(w => `${w.ns}/${w.name}`));
  const updateCount = (report?.workloads || []).filter(w => w.available && !platformKeys.has(`${w.ns}/${w.name}`)).length;
  const updateErrors = report?.errors || 0;
  const unchecked = (report?.workloads || []).filter(w => w.unchecked && !w.homestead).length;
  const layout = viewLayout("containers");
  const items = [{ label: "Check for image updates", icon: "refresh", run: "checkImageUpdates()", tip: "Ask the registries for newer images" },
    { label: "Manage groups", icon: "list", run: "manageWorkloadGroups()", need: "operator" },
    { label: "If a host fails", icon: "node", run: "wlFailover()", tip: "What each container does when its host fails: move, or wait for the host" },
    { label: "Rebalance containers…", icon: "move", run: "containerRebalance()", need: "operator", tip: "Move containers so hosts carry similar CPU and memory" },
    { label: "Move several…", icon: "move", run: "moveSeveral()", need: "operator", tip: "Tick containers and move them to a host you choose, reviewed together" },
    { label: "Set reservations from use…", icon: "sliders", run: "reservationsReview()", need: "operator", tip: "Reserve CPU and memory for apps from what they used over the last week" },
    { label: "Priority tiers…", icon: "list", run: "priorityReview()", need: "operator", tip: "Which apps fit first when a host fails and the others are short of room" },
    { label: "Logos for apps without one", icon: "logo", run: "logoFixup()", need: "operator", tip: "Match apps with no logo to the app store's" },
    { label: "Change history", icon: "clock", run: "wlHistory()", tip: "Every app's changes: who, when and what" },
    { label: "Search logs", icon: "search", run: "logSearch()", tip: "Find a line in every app's recent logs" },
    { label: "Log alerts", icon: "bell", run: "logAlerts()", tip: "Saved log searches that raise an alert when they match too many lines" },
    { label: "Power schedules", icon: "clock", run: "schedulesOverview()", tip: "Apps and VMs that stop and start on their own" }];
  const logoless = all.filter(logoCanHave).length;
  const updateButtons = `${updateCount ? `<button class="pill warn pillbtn" title="Review and stage image updates" onclick="imageUpdateCenter()">${updateCount} update${updateCount === 1 ? "" : "s"}</button>` : ""}
    ${updateErrors ? `<button class="pill crit pillbtn" data-tip="${updateErrors} image${updateErrors === 1 ? "" : "s"} could not be compared with ${updateErrors === 1 ? "its" : "their"} registry; every other image was" onclick="imageUpdateCenter()">${updateErrors} check${updateErrors === 1 ? "" : "s"} failed</button>` : ""}`.trim();
  const deploy = '<button class="btn pri" data-need="operator" onclick="go(\'deploy\')">＋ Deploy</button>';
  paint(`<div class="containers-page collection-page" data-collection="containers">${UI.pageHeader(`Containers`, `${rows.length} app${rows.length === 1 ? "" : "s"}${q ? ` matching “${esc(q)}”` : ""}${group ? ` in ${esc(group === NO_GROUP ? "no group" : group)}` : ""} · ${platform.length
        ? `<a class="linkish" onclick="togglePlatformContainers()" data-tip="Homestead and the helpers it runs - updated under Settings › Updates - and KubeVirt, CDI and the like, run by their own operators and upgraded under System → Cluster">${platformShown() ? "hide" : "show"} ${platform.length} platform container${platform.length === 1 ? "" : "s"}</a>`
        : "system pods hidden"}${unchecked ? ` · <span data-tip="Marked ? in the list: stopped since Homestead started, so not yet compared with their registries">${unchecked} not checked yet</span>` : report && !updateCount && !updateErrors ? " · images current" : ""}${logoless ? ` · <a class="linkish" data-need="operator" onclick="logoFixup()">${logoless} without a logo</a>` : ""}${metricsNotice(STATE.data.wl)}`, `<span class="dim xs scanprogress" id="scanprogress"></span>${updateButtons}
      ${layoutSwitch("containers", "renderWorkloads")}
      ${moreMenu(items)}
      ${deploy}`, {extraHtml:`${all.length ? workloadGroupBar(all, group) : ""}`})}
    ${UI.collectionHeader(`${workloadGroupSelect(all, group)}${workloadListOptions(platform, layout, items)}${deploy}`, `<span>${rows.length} container${rows.length === 1 ? "" : "s"}</span>${updateButtons ? `<span aria-hidden="true">·</span>${updateButtons}` : ""}
        ${unchecked ? `<span class="dim" data-tip="Stopped or still starting; not yet compared with their registries">· ${unchecked} not checked yet</span>` : report && !updateCount && !updateErrors ? '<span class="dim">· images current</span>' : ""}${logoless ? `<span class="dim">·</span> <a class="linkish" data-need="operator" onclick="logoFixup()">${logoless} without a logo</a>` : ""}`)}

    ${uptimeUnreadNote()}
    ${rows.length ? workloadSections(rows, layout, group)
      : `<div class="empty">${q || group ? "Nothing matches that search." : "Nothing deployed yet."}</div>`}</div>`);
}

/* An image with no tag is Docker's latest; saying so is clearer than leaving it off. */
function imageLabel(ref) {
  if (!ref || ref.includes("@")) return ref || "";
  return ref.split("/").pop().includes(":") ? ref : `${ref}:latest`;
}

/* The part of an image people judge an update by: its name and tag, or the
   start of its digest. The full reference goes in a title beside it. */
function imageShortLabel(ref) {
  const label = imageLabel(ref), name = label.split("/").pop();
  const pinned = name.match(/^(.*@sha256:)([0-9a-f]+)$/);
  return pinned ? `${pinned[1]}${pinned[2].slice(0, 12)}${pinned[2].length > 12 ? "…" : ""}` : name;
}
const imageTitle = images => esc((images || []).map(imageLabel).join(" · "));
const imageShort = images => (images || []).map(i => esc(imageShortLabel(i))).join(" · ");

/* Show what containerd will resolve without changing what the user entered. */
function imagePullRef(ref) {
  const tagged = imageLabel((ref || "").trim());
  if (!tagged) return "";
  const first = tagged.split("/")[0];
  const hasRegistry = tagged.includes("/") && (first.includes(".") || first.includes(":") || first === "localhost");
  if (hasRegistry) return tagged;
  return `docker.io/${tagged.includes("/") ? tagged : `library/${tagged}`}`;
}
function imagePullNote(ref) {
  const pull = imagePullRef(ref);
  if (!pull) return "";
  const leaf = pull.split("/").pop();
  const split = leaf.lastIndexOf(":");
  const repositoryLeaf = split >= 0 ? leaf.slice(0, split) : leaf;
  const tag = split >= 0 ? leaf.slice(split + 1) : "";
  const repeated = repositoryLeaf && repositoryLeaf === tag
    ? ` The repository is named <b>${esc(repositoryLeaf)}</b>; the final <b>:${esc(tag)}</b> is its tag.` : "";
  return `Will pull <span class="mono">${esc(pull)}</span>.${repeated}`;
}

/* What a platform container belongs to, and where that is upgraded. */
function platformTag(w) {
  if (w.homestead) return `<span class="pill slim info" data-tip="Part of Homestead, which keeps it in step. It is updated with Homestead under Settings › Updates, not as an app." onclick="homesteadPartManage('self')" style="cursor:pointer">Homestead</span>`;
  return `<span class="pill slim info" data-tip="Part of ${esc(w.platform)}, run by its operator, which puts back anything changed here. It is upgraded with ${esc(w.platform)} under System → Cluster → Platform versions." onclick="go('cluster')" style="cursor:pointer">${esc(w.platform)}</span>`;
}

/* The same actions for a card and a row: a row shows them as icons. */
function workloadActions(w, update, off, compact = false) {
  const ns = jsq(w.ns), name = jsq(w.name);
  const item = (label, run, iconName, options = {}) => ({label, run, icon:iconName, ariaLabel:`${label} ${w.name}`, ...options});
  const bar = items => actionBar(items, {label:`More actions for ${w.name}`,iconOnly:compact});
  const logs = item("Logs", `wlLogs(${ns},${jsq(w.pods?.[0]?.name || "")},${name})`, "log", {tip:"View live container logs"});
  const restart = item("Restart", `wlRestart(${ns},${name})`, "restart", {need:"operator",tip:"Replace every pod with a fresh one"});
  if (w.managed_smb || w.managed_nfs) return bar([logs, item("Network Shares", "go('shares')", "edit")]);
  if (w.homestead === "self" || w.homestead === "objectstore") return bar([logs, restart,
    item(w.homestead === "self" ? "Settings" : "Migration", `homesteadPartManage(${jsq(w.homestead)})`, "gear")]);
  if (w.platform) return bar([logs, restart]);
  return bar([
    update?.available && !updateOwner(update) && item("Update", `imageUpdateReview(${ns},${name})`, "update", {pri:true,need:"operator"}),
    logs,
    // Not the magnifying glass: Search logs has that, in the same menu.
    item("Diagnostics", `wlDiagnostics(${ns},${name})`, "bug", {tip:"Exit details, previous logs and reviewed commands"}),
    off ? item("Start", `wlScale(${ns},${name},1)`, "play", {need:"operator"}) : restart,
    !off && item("Stop", w.self ? `wlStopSelf(${ns},${name})` : `wlScale(${ns},${name},0)`, "stop", {need:"operator"}),
    item("Console", `wlConsole(${ns},${name})`, "console", {need:"operator",tip:"Open an audited shell in a running container"}),
    item("Edit", `wlEdit(${ns},${name})`, "edit", {need:"operator"}),
    // Reconcile now (#742): only for an app Flux applies, and only for an admin.
    gitopsReconcileItem(w),
    FLEET.view?.linked && !w.self && item("Move to cluster", `moveToCluster('container',${name},${jsq(w.site?.handle || "")},'move',${jsq(w.ns)})`, "move", {need:"admin"}),
    FLEET.view?.linked && !w.self && item("Copy to cluster", `moveToCluster('container',${name},${jsq(w.site?.handle || "")},'copy',${jsq(w.ns)})`, "copy", {need:"admin"}),
    (w.ports || []).length > 1 && item("Main port", `wlPrimaryPort(${ns},${name})`, "ext"),
    item("Placement", `wlPlacement(${ns},${name})`, "node"),
    item("Group", `wlGroup(${ns},${name})`, "list", {need:"operator"}),
    item("Logo", `wlLogo(${ns},${name})`, "logo", {need:"operator",tip:"Pick a logo from the app store"}),
    item("Monitoring", `wlMonitoring(${ns},${name})`, "pulse", {tip:"Whether it is up at its address, checked every minute"}),
    item("History", `wlHistory(${ns},${name})`, "clock", {tip:"Who changed it, when and what, with Undo"}),
    item("Search logs", `logSearch(${ns},${name})`, "search", {tip:"Find a line in its recent logs"}),
    !w.self && item("Schedule", `wlSchedule(${ns},${name})`, "clock", {need:"operator",tip:"Stop and start it at set times"}),
    !updateOwner(update) && item("Updates", `wlUpdateMode(${ns},${name})`, "update", {need:"operator",tip:"Update it yourself, or let it update itself in the maintenance window"}),
    item("Move", `moveWorkload(${name},${ns})`, "move", {need:"operator"}),
    update?.can_rollback && item("Rollback", `imageRollback(${ns},${name})`, "rollback", {need:"operator"}),
    item("Delete", `wlDelete(${ns},${name})`, "trash", {danger:true,need:"operator"})
  ]);
}

/* Starts after (#746): names in words, and a time in seconds or minutes. */
const startsWords = names => { const list = [...names]; return list.length < 2 ? list.join("") : `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`; };
const startsSpan = seconds => seconds < 60 ? `${Math.max(0, Math.round(seconds))} s` : `${Math.round(seconds / 6) / 10} min`;
/* What a row says about the apps it starts after: {chip, tone, line} while
   it waits, when it started without an app, or when
   its gate or setting is not as saved; null when there is nothing to say. */
function startWaitText(w, now = Date.now() / 1000) {
  const setting = w.start_setting || {}, wait = w.start_wait || {}, timeout = setting.timeout;
  const said = line => ({ chip: "", tone: "", line });
  // #620: a setting the server could not work out is "could not read", never none.
  if (w.start_note) return said("Could not read its Starts after setting");
  if (w.start_unreadable) return said("Its Starts after setting can't be read: set it again in Edit");
  if (wait.waiting_for?.length) {
    const so = timeout ? `${startsSpan(now - wait.since)} of ${startsSpan(timeout)}` : `${startsSpan(now - wait.since)} so far`;
    return { chip: "Waiting", tone: "warn", line: `Waiting for ${startsWords(wait.waiting_for)} (${so})` };
  }
  if (wait.started_without?.length) {
    const names = startsWords(wait.started_without);
    return said(`Started without ${names} at ${fmtClock(wait.at)}: ${names} ${wait.started_without.length > 1 ? "were" : "was"} not ready${timeout ? ` after ${startsSpan(timeout)}` : ""}`);
  }
  if (w.start_gate === "missing" && setting.managed) return said("Its start gate isn't committed yet: commit the lines Edit shows");
  if (w.start_gate === "differs" || w.start_gate === "missing") return said("Its start gate doesn't match its setting: put it back in Edit → Placement");
  return null;
}
/* The apps it starts after, the ones that no longer exist marked. */
function startsAfterNames(w) {
  const after = w.start_setting?.after;
  return after ? after.map(a => `${a.app}${a.gone ? " (gone)" : ""}`).join(", ") : (w.starts_after || []).join(", ");
}
/* Start's lead: the stopped apps a start of this one would wait for, or
   that it could not check (#620). */
function startWaitsLine(name, waits, timeout, onTimeout = "start", error = "") {
  if (error) return ` ${error[0].toUpperCase()}${error.slice(1)}.`;
  if (!waits?.length) return "";
  const many = waits.length > 1;
  const until = onTimeout === "wait" ? `until ${many ? "they are" : "it is"} up` : timeout ? `up to ${startsSpan(timeout)}` : "before it starts";
  return ` ${startsWords(waits)} ${many ? "are" : "is"} stopped: ${name} will wait for ${many ? "them" : "it"} ${until}.`;
}
/* The app's details: Starts after, and what its start is waiting on. */
function startsAfterFact(w) {
  const names = startsAfterNames(w), said = startWaitText(w);
  if (!names && !said) return "";
  return `<div class="wl-detail-starts"><span>Starts after</span><b>${esc(names || "—")}</b>${said ? `<div class="dim xs">${esc(said.line)}</div>` : ""}</div>`;
}
const startWaitChip = w => { const said = startWaitText(w); return said?.chip ? UI.chip(said.chip, said.tone, { size: "slim", tip: said.line }) : ""; };
window.startWaitText = startWaitText;

/* A workload's state tags, the same in a row and a card so the two can't
   drift: its schedule, how it updates, who updates it, and an update waiting.
   A row whose own Update button is shown says the update there, not twice. */
function workloadStateTags(w, update, { row = false } = {}) {
  const owner = updateOwner(update);
  const button = row && update?.available && !owner && (typeof can !== "function" || can("operator"));
  return `${startWaitChip(w)}${scheduleTag(w)}${autoUpdateTag(w)}${row ? preferTag(w) : ""}${owner ? updateOwnerPill(update, row)
    : update?.available && !button ? UI.chip("update available", "warn") : ""}${uncheckedMark(update)}`;
}

function workloadCard(w) {
      const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
      const update = w.platform || w.managed_smb || w.managed_nfs || remoteRow(w) ? null : workloadUpdate(w.ns, w.name);
      const updateError = update?.images?.find(x => x.error);
  return `<div class="wcard card flat" data-mkey="${esc(workloadRowKey(w))}"${clusterAttr(w)}>
        <div class="between whead">
          <div class="row" style="gap:10px;min-width:0">
            ${appAvatar(w.name, w.icon)}
            <div class="wtitle"><div style="font-weight:680">${esc(w.name)}</div>
              <div class="dim xs">${esc(w.ns)} · ${w.managed_smb || w.managed_nfs ? esc(w.nodes.join(", ") || "unscheduled") : `<span class="nodelink"
                onclick="moveWorkload(${jsq(w.name)},${jsq(w.ns)})">${esc(w.nodes.join(", ") || "unscheduled")}</span>`}</div></div>
          </div>
          <div class="row">${w.platform ? platformTag(w) : ""}${w.managed_smb || w.managed_nfs ? UI.chip(`managed ${w.managed_nfs ? "NFS" : "SMB"}`, "info", { size: "slim", tip: "Managed by Homestead under Network Shares" }) : ""}${workloadStateTags(w, update)}
          ${updateError ? `<span class="tip warn-tip" tabindex="0" role="img" aria-label="Registry check unavailable: ${esc(updateError.error)}" data-tip="Registry check unavailable — ${esc(updateError.error)}">!</span>` : ""}
          ${UI.chip(`${w.ready}/${w.desired}`, ok ? "ok" : off ? "neutral" : "bad")}</div>
        </div>
        <div class="wmeta">
          <div><div class="dim xs">UPTIME</div>${w.uptime ? upChip(w.uptime) : '<span class="dim">—</span>'}</div>
          <div><div class="dim xs" data-tip="Live usage. 100% equals one fully used CPU core.">CPU</div><div class="mono small">${workloadCpuPercent(w.cpu)}</div></div>
          <div><div class="dim xs" data-tip="Memory in use right now">RAM</div><div class="mono small">${workloadMemory(w.mem_mb)}</div></div>
          <div><div class="dim xs">ACCESS</div><div class="waccess">${accessPorts(w.ports)}</div></div>
        </div>
        <div class="dim xs mono wimg"><span class="wimage-name" title="${imageTitle(w.images)}">${imageShort(w.images)}</span>
          <span class="wimage-hardware">${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</span></div>
        ${answerCardRow(w)}
        ${workloadReleasedRow(w)}
        ${workloadPull(w) ? pullBar(workloadPull(w)) : ""}
        ${workloadBlocked(w) ? `<div class="wblocked" title="${esc(workloadBlocked(w))}">${esc(workloadBlocked(w))}</div>` : ""}
        <div class="wfoot">
          ${workloadHierarchy(w)}
          <div class="row wacts">
          ${workloadActions(w, update, off)}
          </div>
        </div></div>`;
}

/* One line per workload: for a long list, or anyone who would rather scan than browse. */
function workloadTable(rows, sections = null, folded = new Set()) {
  // Grouped, each group is a body of its own under a heading body, so sorting
  // orders the rows within each group rather than mixing them.
  const bodies = sections ? sections.map(([name, members]) => {
    const shut = folded.has(name || NO_GROUP);
    return `<tbody class="grouphead"><tr><td colspan="8">${workloadGroupHead(name, members, shut)}</td></tr></tbody>
      <tbody${shut ? " hidden" : ""}>${workloadTableRows(members)}</tbody>`;
  }).join("") : `<tbody>${workloadTableRows(rows)}</tbody>`;
  return `<div class="card flat pad0 wltable-wrap"><table class="tbl dense stack compact wltable" data-sort="containers" data-sort-controls="containers"><thead><tr>
    <th>App</th><th>Status</th><th class="wl-image">Image</th><th>CPU</th><th>RAM</th><th class="wl-access">Access</th><th class="wl-uptime">Uptime</th><th data-nosort>Actions</th></tr></thead>
    ${bodies}</table></div>`;
}

/* An image not yet compared with its registry is one quiet mark, its reason
   on hover; the page subtitle gives the count. */
function uncheckedMark(update) {
  if (!update?.unchecked) return "";
  const tip = update.images?.some(i => i.rate_limited)
    ? "Image not checked yet: its registry asked Homestead to slow down. It is checked again within half an hour."
    : update.images?.some(i => i.starting)
    ? "Image not checked yet: still starting. It is compared with the registry once it runs."
    : "Image not checked yet: stopped, and not seen running here. It is compared with the registry once it has run.";
  return `<span class="tip unchecked-tip" tabindex="0" role="img" aria-label="${tip}" data-tip="${tip}">?</span>`;
}

/* A container's volume, on the Volumes page: filtered to it, with the
   page's own "Showing matches" bar to clear it again. */
window.openWorkloadVolume = claim => {
  STATE.q = claim;
  const search = $("#globalSearch");
  if (search) search.value = claim;
  go("storage", { keepSearch: true });
};

const workloadExpanded = new Set();
const workloadRowKey = w => JSON.stringify([w.site?.id || "", w.ns, w.name]);
function workloadTableRows(rows) {
  return rows.map(w => {
    const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
    const key = workloadRowKey(w), id = `wl-detail-${encodeURIComponent(key)}`, open = workloadExpanded.has(key);
    const uptime = !off && Number.isFinite(w.uptime) && w.uptime > 0 ? w.uptime : null;
    const update = w.platform || w.managed_smb || w.managed_nfs || remoteRow(w) ? null : workloadUpdate(w.ns, w.name);
    const updateError = update?.images?.find(x => x.error), pull = workloadPull(w), blocked = workloadBlocked(w);
    const quiet = off && !update?.unchecked && !updateError && !update?.available && !pull && !blocked;
    const disclosure = collectionDisclosure({label:`Details for ${w.name}`,id:`wl-toggle-${encodeURIComponent(key)}`,controls:id,expanded:open,
      run:`workloadToggleDetails(${jsq(key)},this)`,bodyHtml:`${appAvatar(w.name, w.icon)}<span class="wtitle"><b>${esc(w.name)}</b> ${clusterTag(w)}</span>`});
    return `<tr class="wl-row${off ? " wl-off" : ""}${quiet ? " wl-quiet" : ""}${open ? " wl-expanded" : ""}" data-row-key="${esc(key)}" data-mkey="row:${esc(key)}"${clusterAttr(w)}>
      <td class="wl-name" data-sort="${esc(w.name)}">${disclosure}
        <div class="wl-row-meta dim xs">${w.platform ? `${platformTag(w)} ` : ""}${w.managed_smb || w.managed_nfs ? `${UI.chip(`managed ${w.managed_nfs ? "NFS" : "SMB"}`, "info", { size: "slim" })} ` : ""}${esc(w.ns)} · ${esc(off ? "stopped" : (w.nodes || []).join(", ") || "unscheduled")}</div></td>
      <td class="wl-status" data-status data-sort="${off ? -1 : w.desired ? w.ready / w.desired : 0}"><div class="row wl-state-tags">
        ${UI.chip(off ? "Stopped" : `${w.ready}/${w.desired}`, ok ? "ok" : off ? "neutral" : "bad", { size: "slim", tip: `${w.ready} of ${w.desired} ready` })}
        ${answerTag(w)}${workloadStateTags(w, update, { row: true })}
        ${updateError ? `<span class="tip warn-tip" tabindex="0" role="img" aria-label="Registry check unavailable: ${esc(updateError.error)}" data-tip="Registry check unavailable — ${esc(updateError.error)}">!</span>` : ""}
        ${pull ? UI.chip(`Pulling ${Math.min(100, pull.percent || 0)}%`, "neutral", { tip: `Fetching ${pull.image || "image"}` }) : ""}
        ${blocked ? UI.chip("Blocked", "bad", { tip: blocked }) : ""}</div></td>
      <td class="wl-image" data-sm-hide><div class="mono xs wl-imagetext" title="${imageTitle(w.images)}">${imageShort(w.images)}</div>
        ${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</td>
      <td class="mono small nowrap wl-cpu" data-sort="${off ? "" : w.cpu}" data-tip="Live usage. 100% equals one fully used CPU core.">${off ? "—" : workloadCpuPercent(w.cpu)}</td>
      <td class="mono small nowrap wl-ram" data-sort="${off ? "" : w.mem_mb}">${off ? "—" : workloadMemory(w.mem_mb)}</td>
      <td class="wl-access" data-sm-hide><div class="waccess">${accessPorts(w.ports)}</div></td>
      <td class="mono small nowrap wl-uptime" data-sm-hide data-sort="${uptime ?? ""}">${uptime === null ? "—" : esc(fmtUp(uptime))}</td>
      <td class="wl-actions" data-actions><div class="wacts">${workloadActions(w, update, off, true)}</div></td>
    </tr>
    <tr class="wl-detail-row" data-detail-for="${esc(key)}" data-mkey="detail:${esc(key)}"${clusterAttr(w)}${open ? "" : " hidden"}><td colspan="8">
      <div class="wl-inline-detail" id="${esc(id)}">
        ${blocked ? `<div class="wblocked">${esc(blocked)}</div>` : ""}${pull ? pullBar(pull) : ""}
        <div class="about-grid">
          <div><span>Host</span><b>${esc((w.nodes || []).join(", ") || "Unscheduled")}</b></div>
          <div><span>Namespace · uptime</span><b>${esc(w.ns)} · ${esc(off ? "Stopped" : w.uptime ? fmtUp(w.uptime) : "Starting")}</b></div>
          <div class="wl-detail-image"><span>Image</span><b class="mono small">${w.images.map(esc).join("<br>") || "—"}</b><div class="wl-detail-hardware">${hardwareTags(w.hardware || (w.gpu ? ["igpu"] : []))}</div></div>
          <div class="wl-detail-access"><span>Access</span><b class="waccess">${accessPorts(w.ports)}</b></div>
          ${(w.claims || []).length && !remoteRow(w) ? `<div class="wl-detail-volumes"><span>Volumes</span><b class="row">${w.claims.map(claim =>
            `<button type="button" class="tag info linkish" title="Open ${esc(claim)} in Volumes" onclick="openWorkloadVolume(${jsq(claim)})">${esc(claim)}</button>`).join("")}</b></div>` : ""}
          ${(released => released ? `<div class="wl-detail-released"><span>Old volumes</span>${released}</div>` : "")(workloadReleasedRow(w, true))}
          ${gitopsDetail(w)}
          ${startsAfterFact(w)}
        </div>
        ${workloadHierarchy(w, { wide: true })}
        <div class="wl-detail-actions">${workloadActions(w, update, off)}</div>
      </div></td></tr>`;
  }).join("");
}
window.workloadToggleDetails = (key, button) => {
  const open = !workloadExpanded.has(key);
  if (open) workloadExpanded.add(key); else workloadExpanded.delete(key);
  button.setAttribute("aria-expanded", String(open));
  const row = button.closest("tr");
  row?.classList.toggle("wl-expanded", open);
  const detail = row?.nextElementSibling;
  if (detail?.dataset.detailFor === key) detail.hidden = !open;
};


function accessPorts(ports) {
  // null: the Services couldn't be read, which is not "no listeners".
  if (ports === null) return `<span class="dim" data-tip="Its Services couldn't be read just now, so where it listens is not known.">couldn't read</span>`;
  const rows = ports || [];
  if (!rows.length) return '<span class="dim">—</span>';
  const shown = rows.slice(0, 2);
  // A narrow card has room for one port, a wide one for two; each size gets
  // its own "+N" so the count is right whichever one is showing.
  const more = (from, kind) => rows.length > from
    ? `<span class="tag more ${kind}" data-tip="Also listening on ${esc(rows.slice(from).map(p => p.port).join(", "))}">+${rows.length - from}</span>` : "";
  return shown.map((p, i) => (p.ip
    ? `<button type="button" class="plink${i ? " second" : ""}" title="Open ${esc(svcUrl(p.ip, p.port))}" aria-label="Open port ${p.port}" onclick="openSvc(${jsq(p.ip)},${p.port})">${p.port}<svg class="ext" width="9" height="9" aria-hidden="true"><use href="#i-ext"/></svg></button>`
    : `<span class="tag${i ? " second" : ""}">${p.port}</span>`)).join("") +
    more(1, "more-narrow") + more(2, "more-wide");
}

/* Where a workload's pods would go, host by host: memory now and after the
   start, what the scheduler has already reserved, and why a host is out.
   Shared by the start review and anything else that places pods. */
function capacityPlacementHtml(plan) {
  const placement = plan.placement || {}, hosts = (plan.candidates || []).filter(host => host.eligible);
  const resident = placement.resident || plan.vm?.resident_node;
  let title, detail;
  if (resident) {
    title = `Resumes on ${resident}`;
    detail = "The paused VM keeps its current host.";
  } else if (placement.pinned) {
    title = `Required host: ${placement.pinned}`;
    detail = "This app is pinned to this host. It cannot launch on another host.";
  } else if (placement.preferred) {
    const available = hosts.some(host => host.name === placement.preferred);
    title = `${available ? "Preferred host" : "Preferred host currently unavailable"}: ${placement.preferred}`;
    detail = "Kubernetes can choose another eligible host if needed.";
  } else if (hosts.length === 1) {
    title = `Only eligible host: ${hosts[0].name}`;
    detail = "Kubernetes can schedule it here if these constraints still hold at launch.";
  } else {
    title = hosts.length ? "Host selected at launch" : "Launch host not determined";
    detail = hosts.length ? "Kubernetes chooses from the eligible hosts below when the app starts." : "No eligible host has been confirmed by this review.";
  }
  if (plan.blocked) detail += " Launch is blocked until the issues below are resolved.";
  const names = hosts.length > 1 || placement.preferred ? `<p>Eligible hosts: <b>${hosts.map(host => esc(host.name)).join(", ") || "none"}</b>.</p>` : "";
  return `<div class="capacity-placement">${UI.section("Launch host", `<b>${esc(title)}</b><p class="ui-help">${esc(detail)}</p>${names}`)}</div>`;
}
window.capacityPlacementHtml = capacityPlacementHtml;

function capacityHosts(candidates, { unavailable = "live RAM unavailable", reserved = "Already reserved", placement = {} } = {}) {
  const eligible = candidates.filter(x => x.eligible);
  const rejected = candidates.filter(x => !x.eligible);
  const memory = host => {
    if (!host.metrics_available || host.projected_percent === null || host.projected_percent === undefined) return `<span class="ui-help">${esc(unavailable)}</span>`;
    const now = host.capacity_gb ? host.used_gb / host.capacity_gb * 100 : 0;
    return `${UI.meter({ now, after: host.projected_percent, label: `${host.name} memory` })}
      <span class="sub">${esc(host.projected_gb)} / ${esc(host.capacity_gb)} GiB after start · ${esc(host.projected_percent)}%</span>`;
  };
  const about = host => `<span class="mono">${esc(host.name)}</span><span class="sub">${host.metrics_available ? `Live RAM ${esc(host.used_gb)} GiB` : esc(unavailable)}${host.reservations_known
    ? ` · ${esc(reserved)}: ${esc(host.reserved_gb)} / ${esc(host.allocatable_gb)} GiB RAM · ${esc(host.reserved_cpu_percent)}% CPU`
    : " · Scheduler reservations unavailable."}</span>`;
  const status = host => (placement.resident === host.name ? UI.chip("Current host", "info")
    : placement.pinned === host.name ? UI.chip("Pinned", "info")
    : placement.preferred === host.name ? UI.chip("Preferred", "info") : UI.chip("Eligible", ""))
    + (host.warnings?.length ? UI.chip("Warning", "warn") : "");
  let html = "";
  if (eligible.length) html += UI.section("Hosts that can run it", UI.table(
    [{ label: "Host" }, { label: "Memory", className: "grow" }, { label: "" }],
    eligible.map(host => [about(host), memory(host), status(host)])));
  if (rejected.length) html += UI.section("Unavailable for the next pod", UI.table(
    [{ label: "Host" }, { label: "Why" }],
    rejected.map(host => [`<span class="mono">${esc(host.name)}</span>`, esc((host.reasons || []).join(" · ") || "not eligible")])));
  return html;
}
window.capacityHosts = capacityHosts;

/* The review before something starts - a container's pods or a VM: one
   sentence on what starts where, a warning only when there is one, each host
   with the memory it would be left with, and one tickbox to accept a
   warning. How it was worked out stays under Details. */
/* Each host with the memory it would be left with, and why a host is out. */
function capacityHostTable(plan) {
  const placement = plan.placement || {}, hosts = plan.candidates || [];
  const memory = host => !host.metrics_available || host.projected_percent == null
    ? '<span class="ui-help">memory use unknown</span>'
    : `${UI.meter({ now: host.capacity_gb ? host.used_gb / host.capacity_gb * 100 : 0, after: host.projected_percent, label: `${host.name} memory` })}
       <span class="sub">${esc(host.projected_gb)} of ${esc(host.capacity_gb)} GiB after · ${esc(host.projected_percent)}%</span>`;
  const tag = host => placement.resident === host.name ? UI.chip("current host", "info")
    : placement.pinned === host.name ? UI.chip("pinned", "info")
    : placement.preferred === host.name ? UI.chip("preferred", "info") : "";
  return UI.table([{ label: "Host" }, { label: "Memory after it starts", className: "grow" }], [
    ...hosts.filter(host => host.eligible).map(host => [`<span class="mono">${esc(host.name)}</span> ${tag(host)}`, memory(host)]),
    ...hosts.filter(host => !host.eligible).map(host => [`<span class="mono">${esc(host.name)}</span>`,
      `<span class="ui-help">can't run here: ${esc((host.reasons || []).join(" · ") || "not eligible")}</span>`])]);
}
window.capacityHostTable = capacityHostTable;

/* The acknowledgement a review asks for, from what it does: "Restarts" asks
   "Restart it anyway", never "Start it anyway". */
const reviewAck = what => `${String(what || "Starts").split(" ")[0].replace(/s$/, "")} it anyway`;
window.reviewAck = reviewAck;
/* The review before a container or VM starts, is created or saved
   (design.md, Reviews): a whole UI.dialog review. The header names it; the
   lead says what happens and where; one notice; the objects; the hosts with
   room; Details; a tick only when there is something to accept; the
   caller's buttons. actions is UI.dialog's (a blocked review offers only
   the way out); more is HTML shown after the objects (a fresh-state check). */
function startReview(plan, { what = "Starts", name, kind = "", where: place = "", extra = "", ackId, onAck = "", details = "", objects = "", more = "", warnings = [], actions = { dismiss: false } }) {
  const { concerns, caveats } = capacityNotes(plan);
  const placement = plan.placement || {}, hosts = plan.candidates || [];
  const ok = hosts.filter(host => host.eligible);
  const fixed = placement.resident || placement.pinned || plan.vm?.resident_node;
  const where = fixed ? ` on <b>${esc(fixed)}</b>`
    : ok.length === 1 ? ` on <b>${esc(ok[0].name)}</b>, the only host that can run it`
    : placement.preferred && ok.some(host => host.name === placement.preferred) ? ` on <b>${esc(placement.preferred)}</b> if it can, or another host`
    : ok.length ? ` on one of ${ok.length} hosts` : "";
  const topology = plan.topology_status === "blocked" ? "Its affinity and spread rules cannot fit all requested replicas."
    : plan.topology_status && !["fits", "not-needed"].includes(plan.topology_status) ? "Its affinity and spread placement remains unverified." : "";
  const problems = [...new Set([...(plan.blockers || []), ...concerns,
    ...(plan.unbounded?.length ? [`${plan.unbounded.join(", ")} ${plan.unbounded.length === 1 ? "has" : "have"} no memory limit, so it could use more than estimated`] : []),
    ...(topology ? [topology] : []), ...warnings])];
  const verb = reviewAck(what).split(" ")[0].toLowerCase();
  const reservations = ok.filter(host => host.reservations_known).map(host =>
    `<li>${esc(host.name)}: Already reserved: ${esc(host.reserved_gb)} / ${esc(host.allocatable_gb ?? "?")} GiB RAM · ${esc(host.reserved_cpu_percent ?? "?")}% CPU${host.request_slots != null ? ` · room for ${esc(host.request_slots)} more` : ""}</li>`)
    .concat(ok.filter(host => !host.reservations_known).map(host => `<li>${esc(host.name)}: Scheduler reservations unavailable</li>`));
  return UI.dialog({ shape: "review",
    subject: { kind: kind || (plan.vm ? "VM" : "App"), name, where: place },
    lead: plan.blocked ? `It can't ${verb} until what is below is resolved.` : `${what} it${where}.${extra}`,
    issues: plan.blocked ? (problems.length ? problems : ["No host that may run it has room for it."]).map(p => ["bad", p]) : problems.map(p => ["warn", p]),
    issuesTitle: plan.blocked ? `Can't ${verb}` : "",
    body: `${objects}${more}${hosts.length ? UI.section("Hosts with room", capacityHostTable(plan)) : ""}`,
    details: `${UI.facts([
        [plan.vm ? "VM reserves" : `Each pod reserves`, `${esc(plan.pod_request_gb ?? "?")} GiB RAM${plan.vm?.request_is_lower_bound ? " at least" : ""} · ${esc(plan.pod_cpu_request_percent ?? "?")}% CPU`],
        ["Memory estimate", plan.pod_memory_gb ? `${esc(plan.pod_memory_gb)} GiB${plan.vm ? " with its launcher" : " per pod"}` : "unknown"],
        plan.additional > 1 ? ["Pods starting", esc(plan.additional)] : null])}
      ${reservations.length ? `<ul class="ui-list">${reservations.join("")}</ul>` : ""}
      ${caveats.length ? `<ul class="ui-list">${caveats.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      ${details}
      <p class="ui-help">Memory after it starts is the greater of live use and what is already reserved, plus this estimate. It is a snapshot, not a reservation: other starts can still change which host it runs on. The server checks again before anything starts.</p>`,
    ack: problems.length ? { id: ackId, sentence: reviewAck(what), onchange: onAck } : null,
    actions });
}
window.startReview = startReview;

window.wlScale = async (ns, name, n) => {
  if (n > 0) {
    try {
      const plan = await api(`/api/workloads/start-plan?${new URLSearchParams({ ns, name, replicas: n })}`);
      // Starts after (#746): the apps it starts after that are stopped, which it would wait for.
      const waits = plan.waits_for || [];
      if (plan.requires_confirmation || plan.blocked || waits.length || plan.waits_for_error) {
        const pods = plan.additional > 1 ? `${plan.additional} pods of` : "";
        const row = waits.length ? (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) : null, setting = row?.start_setting;
        modal(`Start · ${name}`, startReview(plan, { what: `Starts ${pods}`.trim(), name, where: ns, ackId: "wl_capacity_ok",
          extra: esc(startWaitsLine(name, waits, setting?.timeout, setting?.on_timeout, plan.waits_for_error)),
          actions: { dismiss: plan.blocked ? "Close" : "Cancel",
            primary: { label: "Start", onclick: `wlScaleGo(${jsArg(ns)},${jsArg(name)},${n},${!!plan.requires_confirmation},true)` } } }));
        return;
      }
    } catch (e) { return toast(`Could not check host memory: ${e.message}`, "bad"); }
  }
  // Stopping is asked first: whatever it serves goes away until it is started.
  if (!n && !(await ask(`Stop ${name}? Its pods stop and what it serves goes away until you start it again. Its data and settings are kept.`, { title: "Stop", ok: "Stop" }))) return;
  return wlScaleGo(ns, name, n, false);
};
window.wlScaleGo = async (ns, name, n, confirmed = false, reviewed = false) => {
  if (confirmed && $("#wl_capacity_ok") && !$("#wl_capacity_ok").checked) return toast("Tick Start it anyway to accept the warning", "bad");
  try {
    await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns, name, replicas: n, confirm_capacity: confirmed }) });
    if (confirmed || reviewed) closeModal();
    toast(`${name} ${n ? "started" : "stopped"}`, "ok"); setTimeout(() => refresh(true), 900);
  } catch (e) { toast(e.message, "bad"); }
};

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  renderWorkloads, imageLabel, imageShortLabel, imageTitle, imageShort, imagePullRef, imagePullNote, platformTag, workloadActions, startsWords, startsSpan, startWaitText, startsAfterNames, startWaitsLine, startsAfterFact, startWaitChip, workloadStateTags, workloadCard, workloadTable, uncheckedMark, workloadExpanded, workloadRowKey, workloadTableRows, accessPorts, capacityPlacementHtml, capacityHosts, capacityHostTable, reviewAck, startReview
};

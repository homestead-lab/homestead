/* "Since you were last here" (#748): what happened while you were away, from
   what Homestead records already - app changes, restarts, scheduled jobs,
   alerts, host maintenance and (for admins) the audit log. The dashboard card
   counts by kind and lists the five that matter most; the Timeline page lists
   everything, filtered by kind, app or host. A source the server could not
   read is said on both, never left out as if nothing had happened (#620). */
const Timeline = (() => {
  const KINDS = {
    alert: ["alert", "alerts", "Alerts"], schedule: ["scheduled job", "scheduled jobs", "Scheduled jobs"],
    restart: ["restart", "restarts", "Restarts"], change: ["change", "changes", "Changes"],
    host: ["host event", "host events", "Hosts"], action: ["action", "actions", "Actions"],
  };
  const TONE_RANK = { bad: 0, warn: 1, info: 2, ok: 3 };
  const KIND_TONE = { alert: "warn", schedule: "info", restart: "warn", change: "info", host: "hw", action: "neutral" };
  const RANGES = { last: ["Since you were last here", 0], day: ["24 hours", 86400], week: ["7 days", 7 * 86400], month: ["30 days", 30 * 86400] };
  const MARK_EVERY = 5 * 60 * 1000;
  const state = { data: null, error: "", range: "last", filter: { kind: "", app: "", host: "" }, marked: 0 };

  const counts = entries => entries.reduce((all, e) => ({ ...all, [e.kind]: (all[e.kind] || 0) + 1 }), {});
  const top = (entries, n = 5) => [...entries]
    .sort((a, b) => (TONE_RANK[a.tone] ?? 2) - (TONE_RANK[b.tone] ?? 2) || b.at - a.at).slice(0, n);
  const filter = (entries, f = {}) => entries.filter(e =>
    (!f.kind || e.kind === f.kind) && (!f.app || e.app === f.app) && (!f.host || e.host === f.host));
  const kindWords = (kind, n) => (KINDS[kind] || [kind, kind])[n === 1 ? 0 : 1];
  const many = (n, one, more) => `${n} ${n === 1 ? one : more}`;

  async function load(range = "last") {
    const seconds = RANGES[range]?.[1] || 0;
    const query = seconds ? `?since=${Math.floor(Date.now() / 1000) - seconds}` : "";
    try {
      state.data = await api(`/api/timeline${query}`);
      state.error = "";
    } catch (e) {
      state.error = e?.message || String(e);
    }
    return state.data;
  }

  /* Homestead open in a visible tab is "here"; the server works out visits. */
  async function markSeen(now = Date.now()) {
    if (typeof ME === "undefined" || !ME || document.hidden || now - state.marked < MARK_EVERY) return;
    state.marked = now;
    try {
      const answer = await api("/api/timeline/seen", { method: "POST", body: "{}" });
      state.seenError = answer && answer.ok === false ? answer.reason || "" : "";
    } catch (e) {
      state.seenError = e?.message || String(e);
    }
  }

  const unreadLines = data => (data.unread || []).map(u => u.reason === "shown to admins only"
    ? `<div class="dim xs">${esc(u.label)}: ${esc(u.reason)}</div>`
    : `<div class="xs med-text">Could not read ${esc(u.label)}: ${esc(u.reason)}</div>`).join("");
  const sinceWords = data => `Since ${fmtAt(data.since)}${data.clamped ? " (as far back as the timeline reaches)" : ""}`;
  const chips = data => `<div class="tl-counts">${Object.keys(KINDS).filter(k => data.counts?.[k])
    .map(k => UI.chip(`${data.counts[k]} ${kindWords(k, data.counts[k])}`, KIND_TONE[k])).join(" ")}</div>`;
  const open = href => href ? ` onclick="openOperation(${jsq(href)})"` : " disabled";

  function widget() {
    if (state.error && !state.data) return UI.callout("warn", "Could not read the timeline", esc(state.error));
    const data = state.data;
    if (!data) return '<div class="empty small"><span class="spin2"></span> Loading…</div>';
    const entries = data.entries || [];
    return `<div class="dim xs">${esc(sinceWords(data))}</div>${state.error ? `<div class="xs med-text">Could not read the timeline just now · this is the last read</div>` : ""}
      ${unreadLines(data)}
      ${entries.length ? chips(data) + `<div class="tl-list">${top(entries).map(e => `<button type="button" class="tl-item"${open(e.href)}>
        ${UI.chip(e.tone === "ok" ? "OK" : e.tone === "bad" ? "Failed" : e.tone === "warn" ? "Check" : "Info", e.tone, { size: "slim" })}
        <span class="tl-text">${esc(e.text)}</span><span class="dim xs">${esc(fmtAt(e.at))}</span></button>`).join("")}</div>`
        : `<div class="empty small">Nothing happened since ${esc(fmtAt(data.since))}.</div>`}`;
  }

  function page() {
    const data = state.data, f = state.filter;
    const rangeSeg = `<div class="seg" role="group" aria-label="Range">${Object.entries(RANGES).map(([key, [label]]) =>
      `<button type="button" class="${state.range === key ? "on" : ""}" aria-pressed="${state.range === key}" onclick="timelineRange(${jsq(key)})">${esc(label)}</button>`).join("")}</div>`;
    if (!data) {
      paint(UI.pageHeader("Timeline", "What happened while you were away", rangeSeg) +
        (state.error ? UI.callout("warn", "Could not read the timeline", esc(state.error)) : '<div class="empty"><span class="spin2"></span> Loading…</div>'));
      return;
    }
    const entries = data.entries || [], shown = filter(entries, f);
    const values = key => [...new Set(entries.map(e => e[key]).filter(Boolean))].sort();
    const pick = (key, label, all) => `<label class="tl-pick"><span class="dim xs">${esc(label)}</span><select aria-label="${esc(label)}" onchange="timelineFilter(${jsq(key)}, this.value)">
      <option value="">${esc(all)}</option>${values(key).map(v => `<option value="${esc(v)}"${f[key] === v ? " selected" : ""}>${esc(v)}</option>`).join("")}</select></label>`;
    const kinds = `<div class="seg" role="group" aria-label="Kind"><button type="button" class="${f.kind ? "" : "on"}" aria-pressed="${!f.kind}" onclick="timelineFilter('kind', '')">All</button>${Object.keys(KINDS)
      .filter(k => data.counts?.[k]).map(k => `<button type="button" class="${f.kind === k ? "on" : ""}" aria-pressed="${f.kind === k}" onclick="timelineFilter('kind', ${jsq(k)})">${esc(KINDS[k][2])} ${data.counts[k]}</button>`).join("")}</div>`;
    const row = e => `<tr class="tl-row">
      <td data-label="When" class="nowrap" data-sort="${e.at}">${fmtWhen(e.at)}</td>
      <td data-label="Kind">${UI.chip(KINDS[e.kind]?.[2] || e.kind, e.tone, { size: "slim" })}</td>
      <td data-label="What">${esc(e.text)}${e.app || e.host ? `<div class="dim xs">${esc(e.app || e.host)}</div>` : ""}</td>
      <td data-actions>${e.href ? `<button type="button" class="btn sm"${open(e.href)}>Open</button>` : ""}</td></tr>`;
    paint(`${UI.pageHeader("Timeline", `${esc(sinceWords(data))} · ${many(entries.length + (data.more || 0), "event", "events")}`, rangeSeg)}
      ${state.error ? UI.callout("warn", "Could not read the timeline just now", `${esc(state.error)} · this is the last read.`) : ""}
      ${(data.unread || []).filter(u => u.reason !== "shown to admins only").map(u => UI.callout("warn", `Could not read ${esc(u.label)}`, esc(u.reason))).join("")}
      ${(data.unread || []).filter(u => u.reason === "shown to admins only").map(u => `<div class="dim small">${esc(u.label)}: ${esc(u.reason)}.</div>`).join("")}
      <div class="tl-filters">${kinds}${pick("app", "App", "All apps")}${pick("host", "Host", "All hosts")}</div>
      ${shown.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack compact" data-sort="timeline"><thead><tr><th>When</th><th>Kind</th><th>What</th><th data-nosort></th></tr></thead>
        <tbody>${shown.map(row).join("")}</tbody></table></div></div>` : UI.empty(entries.length ? "Nothing of this kind in the window." : `Nothing happened since ${fmtAt(data.since)}.`)}
      ${data.more ? `<div class="dim small">${many(data.more, "older event", "older events")} not shown: pick a shorter range.</div>` : ""}`);
  }

  async function view() {
    state.filter = { kind: "", app: "", host: "" };
    page();
    await load(state.range);
    page();
  }

  return { KINDS, counts, top, filter, load, markSeen, widget, view, page, state, RANGES };
})();

async function viewTimeline() { await Timeline.view(); }
window.timelineFilter = (key, value) => { Timeline.state.filter[key] = value; Timeline.page(); };
window.timelineRange = async range => { if (!Timeline.RANGES[range]) return; Timeline.state.range = range; await Timeline.load(range); Timeline.page(); };
window.Timeline = Timeline;

// "Last here" is when you last had Homestead open: noted every five minutes
// while a tab shows it (Timeline.markSeen does nothing more often).
if (typeof setInterval === "function" && typeof document !== "undefined" && document.addEventListener) {
  const tick = () => { Timeline.markSeen(); };
  const timer = setInterval(tick, 60 * 1000);
  timer?.unref?.();
  document.addEventListener("visibilitychange", tick);
}

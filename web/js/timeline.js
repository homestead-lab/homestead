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
  const RANGES = { last: ["Since last visit", 0], day: ["24 hours", 86400], week: ["7 days", 7 * 86400], month: ["30 days", 30 * 86400] };
  const MARK_EVERY = 5 * 60 * 1000;
  // The card always shows since you were last here; the page whatever range is picked.
  // The filter lasts while the page stays open (#871): its refresh calls view()
  // again on the same navigation; opening the page anew starts unfiltered.
  const state = { data: null, error: "", card: null, cardError: "", range: "last", filter: { kind: "", app: "", host: "" },
    filterFor: null, marked: 0 };

  const counts = entries => entries.reduce((all, e) => ({ ...all, [e.kind]: (all[e.kind] || 0) + 1 }), {});
  const top = (entries, n = 5) => [...entries]
    .sort((a, b) => (TONE_RANK[a.tone] ?? 2) - (TONE_RANK[b.tone] ?? 2) || b.at - a.at).slice(0, n);
  const filter = (entries, f = {}) => entries.filter(e =>
    (!f.kind || e.kind === f.kind) && (!f.app || e.app === f.app) && (!f.host || e.host === f.host));
  const kindWords = (kind, n) => (KINDS[kind] || [kind, kind])[n === 1 ? 0 : 1];
  const many = (n, one, more) => `${n} ${n === 1 ? one : more}`;

  /* range "last" (the card's, and the page's first), a page range, or "from":
     the start a summary push linked to (/timeline?since=…, #921). */
  async function load(range = "last", { page = false } = {}) {
    const seconds = RANGES[range]?.[1] || 0;
    const query = range === "from" && state.from ? `?since=${state.from}`
      : seconds ? `?since=${Math.floor(Date.now() / 1000) - seconds}` : "";
    const [data, error] = page ? ["data", "error"] : ["card", "cardError"];
    try {
      state[data] = await api(`/api/timeline${query}`);
      state[error] = "";
    } catch (e) {
      state[error] = e?.message || String(e);
    }
    return state[data];
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

  const unreadLines = data => (data.unread || []).map(u => u.admin_only
    ? `<div class="dim xs">${esc(u.label)}: ${esc(u.reason)}</div>`
    : `<div class="xs med-text">Could not read ${esc(u.label)}: ${esc(u.reason)}</div>`).join("");
  const sinceWords = data => `Since ${fmtAt(data.since)}${data.clamped ? " (as far back as the timeline reaches)" : ""}`;
  const chips = data => `<div class="tl-counts">${Object.keys(KINDS).filter(k => data.counts?.[k])
    .map(k => UI.chip(`${data.counts[k]} ${kindWords(k, data.counts[k])}`, KIND_TONE[k])).join(" ")}</div>`;
  const open = href => href ? ` onclick="openOperation(${jsq(href)})"` : " disabled";

  function widget() {
    if (state.cardError && !state.card) return UI.callout("warn", "Could not read the timeline", esc(state.cardError));
    const data = state.card;
    if (!data) return '<div class="empty small"><span class="spin2"></span> Loading…</div>';
    const entries = data.entries || [];
    return `<div class="dim xs">${esc(sinceWords(data))}</div>${state.cardError ? `<div class="xs med-text">Could not read the timeline just now · this is the last read</div>` : ""}
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
    // A choice the window no longer has stays listed, so the menu says what is filtered.
    const values = key => [...new Set([...entries.map(e => e[key]), f[key]].filter(Boolean))].sort();
    // A menu with nothing to choose besides "All" is left out (#871).
    const pick = (key, label, all) => !values(key).length ? "" : `<label class="tl-pick"><span class="dim xs">${esc(label)}</span><select aria-label="${esc(label)}" onchange="timelineFilter(${jsq(key)}, this.value)">
      <option value="">${esc(all)}</option>${values(key).map(v => `<option value="${esc(v)}"${f[key] === v ? " selected" : ""}>${esc(v)}</option>`).join("")}</select></label>`;
    const kinds = `<div class="seg" role="group" aria-label="Kind"><button type="button" class="${f.kind ? "" : "on"}" aria-pressed="${!f.kind}" onclick="timelineFilter('kind', '')">All</button>${Object.keys(KINDS)
      .filter(k => data.counts?.[k] || f.kind === k).map(k => `<button type="button" class="${f.kind === k ? "on" : ""}" aria-pressed="${f.kind === k}" onclick="timelineFilter('kind', ${jsq(k)})">${esc(KINDS[k][2])} ${data.counts?.[k] || 0}</button>`).join("")}</div>`;
    const row = e => `<tr class="tl-row">
      <td data-label="When" class="nowrap" data-sort="${e.at}">${fmtWhen(e.at)}</td>
      <td data-label="Kind">${UI.chip(KINDS[e.kind]?.[2] || e.kind, e.tone, { size: "slim" })}</td>
      <td data-label="What">${esc(e.text)}${e.app || e.host ? `<span class="dim xs"> · ${[e.app, e.host].filter(Boolean).map(esc).join(" · ")}</span>` : ""}</td>
      <td data-actions>${e.href ? `<button type="button" class="btn sm"${open(e.href)}>Open</button>` : ""}</td></tr>`;
    paint(`${UI.pageHeader("Timeline", `${esc(sinceWords(data))} · ${many(entries.length + (data.more || 0), "event", "events")}`, rangeSeg)}
      ${state.error ? UI.callout("warn", "Could not read the timeline just now", `${esc(state.error)} · this is the last read.`) : ""}
      ${(data.unread || []).filter(u => !u.admin_only).map(u => UI.callout("warn", `Could not read ${u.label}`, esc(u.reason))).join("")}
      ${(data.unread || []).filter(u => u.admin_only).map(u => `<div class="dim small">${esc(u.label)}: ${esc(u.reason)}.</div>`).join("")}
      <div class="tl-filters">${kinds}${pick("app", "App", "All apps")}${pick("host", "Host", "All hosts")}</div>
      ${shown.length ? `<div class="card flat pad0"><div class="tblwrap"><table class="tbl stack compact" data-sort="timeline"><thead><tr><th>When</th><th>Kind</th><th>What</th><th data-nosort></th></tr></thead>
        <tbody>${shown.map(row).join("")}</tbody></table></div></div>` : UI.empty(entries.length ? "Nothing of this kind in the window." : `Nothing happened since ${fmtAt(data.since)}.`)}
      ${data.more ? `<div class="dim small">${many(data.more, "older event", "older events")} not shown: pick a shorter range.</div>` : ""}`);
  }

  async function view() {
    // go() counts every navigation in NAV_TOKEN; the refresh loop does not.
    const navigation = typeof window !== "undefined" ? window.NAV_TOKEN : undefined;
    if (navigation !== state.filterFor) {
      state.filter = { kind: "", app: "", host: "" };
      // A summary push opens the Timeline from where its summary starts.
      const from = typeof location !== "undefined" ? new URLSearchParams(location.search).get("since") : "";
      if (/^\d{1,12}$/.test(from || "")) { state.range = "from"; state.from = +from; }
      else if (state.range === "from") state.range = "last";
    }
    state.filterFor = navigation;
    page();
    await load(state.range, { page: true });
    page();
  }

  /* "Last here" is when you last had Homestead open: noted once it starts,
     then every minute while a tab shows it (markSeen itself waits five). */
  function start({ setTimeout, setInterval, document }) {
    const tick = () => markSeen();
    setTimeout(tick, 3000);
    setInterval(tick, 60 * 1000)?.unref?.();
    document.addEventListener("visibilitychange", tick);
  }

  /* The summary push (#921): Settings > You. One push a day or a week with
     what the Timeline recorded, to every device of yours with notifications on. */
  const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const clock = hour => `${String(hour).padStart(2, "0")}:00`;
  const browserZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } };
  const summaryText = s => !s || s.every === "off" || !s.every ? "Off"
    : `${s.every === "weekly" ? `Weekly on ${WEEKDAYS[s.day] || "?"}` : "Daily"} at ${clock(s.hour)} (${s.tz})`;

  function summaryCard() {
    return UI.settingsCard(`${UI.moduleHeader("Summary push", "What the Timeline recorded, once a day or a week, on your devices",
      UI.button("Change", "summaryEdit()", { id: "summaryChange" }))}
      <div class="srows" id="summaryRows"><div class="empty small"><span class="spin2"></span></div></div>`, { tab: "device", id: "summaryCard" });
  }

  async function summaryPaint() {
    try {
      state.summary = await api("/api/timeline/summary");
      state.summaryError = "";
    } catch (e) {
      state.summaryError = e?.message || String(e);
    }
    const rows = $("#summaryRows");
    if (!rows) return;
    if (state.summaryError) {
      // Unread is not Off (#620): say so, and offer nothing that would act on a guess.
      rows.innerHTML = UI.callout("warn", "Could not read your summary setting", esc(state.summaryError)) + UI.button("Retry", "Timeline.summaryPaint()");
      const change = $("#summaryChange"); if (change) change.disabled = true;
      return;
    }
    const s = state.summary || { every: "off" }, on = s.every === "daily" || s.every === "weekly";
    const change = $("#summaryChange"); if (change) change.disabled = false;
    const help = [on && s.next ? `Next ${fmtAt(s.next)}` : on ? "Its time zone is not one Homestead knows: choose another" : "Pushes tell you when something starts; this tells you how the day or week went",
      on && s.sent_at ? `last ${fmtAt(s.sent_at)}${s.result ? `, ${s.result}` : ""}` : ""].filter(Boolean).join(" · ");
    rows.innerHTML = settingRow("Summary", esc(help), `<b id="summaryValue">${esc(summaryText(s))}</b>`);
  }

  function summaryEdit() {
    const s = state.summary && state.summary.every ? state.summary : { every: "off" };
    const zone = s.tz || browserZone();
    let zones = [];
    try { zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []; } catch { zones = []; }
    zones = [...new Set([zone, "UTC", ...zones])].sort();
    const option = (value, label, chosen) => `<option value="${esc(value)}"${String(chosen) === String(value) ? " selected" : ""}>${esc(label)}</option>`;
    modal("Summary push", UI.lead("One push with what happened, counted by kind. It opens the Timeline from where the summary starts.") +
      UI.fields(
        UI.field("Send", `<select id="sum_every" onchange="summaryEveryChanged()">${[["off", "Off"], ["daily", "Daily"], ["weekly", "Weekly"]]
          .map(([v, l]) => option(v, l, s.every)).join("")}</select>`),
        `<div id="sum_day_field"${s.every === "weekly" ? "" : " hidden"}>${UI.field("On", `<select id="sum_day">${WEEKDAYS.map((d, i) => option(i, d, s.day ?? 6)).join("")}</select>`)}</div>`,
        UI.field("At", `<select id="sum_hour">${Array.from({ length: 24 }, (_, h) => option(h, clock(h), s.hour ?? 8)).join("")}</select>`)) +
      // Typed, with the browser's zones offered: a list of all of them would be 400 names.
      UI.field("Time zone", `<input id="sum_tz" type="text" maxlength="64" autocomplete="off" spellcheck="false" list="sum_tz_list" value="${esc(zone)}">
        <datalist id="sum_tz_list">${zones.map(z => `<option value="${esc(z)}"></option>`).join("")}</datalist>`) +
      UI.actions(UI.cancel() + UI.button("Save", "summarySave(this)", { kind: "pri" })));
  }

  async function summarySave(button) {
    const every = $("#sum_every").value;
    const body = every === "off" ? { every } : { every, ...(every === "weekly" ? { day: +$("#sum_day").value } : {}),
      hour: +$("#sum_hour").value, tz: $("#sum_tz").value.trim() };
    if (button) button.disabled = true;
    try {
      state.summary = await api("/api/timeline/summary", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      closeModal();
      toast(every === "off" ? "Summary push off" : "Summary push saved", "ok");
      summaryPaint();
    } catch (e) {
      // Refused: the dialog stays, with what was chosen in it.
      toast(e?.message || String(e), "bad");
      if (button) button.disabled = false;
    }
  }

  return { KINDS, counts, top, filter, load, markSeen, widget, view, page, state, RANGES, start,
    summaryCard, summaryPaint, summaryEdit, summarySave };
})();

async function viewTimeline() { await Timeline.view(); }
window.timelineFilter = (key, value) => { Timeline.state.filter[key] = value; Timeline.page(); };
window.timelineRange = async range => { if (!Timeline.RANGES[range]) return; Timeline.state.range = range; await Timeline.load(range, { page: true }); Timeline.page(); };
window.Timeline = Timeline;
window.summaryEdit = () => Timeline.summaryEdit();
window.summarySave = button => Timeline.summarySave(button);
window.summaryEveryChanged = () => { const day = $("#sum_day_field"); if (day) day.hidden = $("#sum_every").value !== "weekly"; };

if (typeof setInterval === "function" && typeof document !== "undefined" && document.addEventListener) {
  Timeline.start({ setTimeout, setInterval, document });
}

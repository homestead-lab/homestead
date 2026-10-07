/* Search logs (homestead_logsearch.py): find a line in every app's recent
   logs at once, without Loki. Kubernetes still holds each running pod's recent
   output; this asks each for the time chosen and shows the lines that match,
   newest first, each a click from that app's live logs.

   logSearch(ns, name) opens it narrowed to one app; logSearch() to every app. */
const LOGSEARCH = { app: "", rows: [], query: "", regex: false, case: false };
const LOGSEARCH_WINDOWS = [["15m", "15 minutes"], ["1h", "hour"], ["6h", "6 hours"], ["24h", "24 hours"]];

/* The line, escaped, with what matched marked. A regular expression the
   browser reads differently from the server just goes unmarked. */
function logSearchMark(line, query, regex = false, matchCase = false) {
  let pattern;
  try {
    pattern = new RegExp(regex ? query : query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), matchCase ? "g" : "gi");
  } catch { return esc(line); }
  let out = "", last = 0, m, guard = 0;
  while ((m = pattern.exec(line)) && guard++ < 200) {
    if (!m[0]) { pattern.lastIndex++; continue; }
    out += esc(line.slice(last, m.index)) + `<mark>${esc(m[0])}</mark>`;
    last = m.index + m[0].length;
  }
  return out + esc(line.slice(last));
}

function logSearchWhen(at) {
  if (!at) return "";
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  return new Date().toDateString() === d.toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/* What the search could not cover, so a short answer is never mistaken for a
   complete one. */
function logSearchGaps(d) {
  const gaps = [];
  if (d.skipped_pods) gaps.push(`${d.skipped_pods} more container${d.skipped_pods === 1 ? " was" : "s were"} not searched; choose an app to search it`);
  if ((d.capped || []).length) gaps.push(`${d.capped.join(", ")} wrote more than Homestead reads at once, so only the newest of ${d.capped.length === 1 ? "its" : "their"} output was searched; choose a shorter time`);
  if ((d.errors || []).length) gaps.push(`Could not read: ${d.errors.slice(0, 4).join("; ")}${d.errors.length > 4 ? ` and ${d.errors.length - 4} more` : ""}`);
  return gaps;
}

function logSearchResults(d) {
  const rows = d.matches || [];
  LOGSEARCH.rows = rows;
  const span = (LOGSEARCH_WINDOWS.find(w => w[0] === d.window) || ["", "hour"])[1];
  const gaps = logSearchGaps(d);
  const head = `<div class="dim small ls-summary">${rows.length ? `${d.truncated ? `The newest ${rows.length} of ${d.total}` : rows.length} matching line${d.total === 1 ? "" : "s"}` : "No matching lines"}
    in ${d.asked} container${d.asked === 1 ? "" : "s"} over the last ${esc(span)}</div>`;
  const body = rows.length ? `<div class="ls-list" role="list">${rows.map((r, i) => `<div class="ls-row" role="listitem">
      <div class="ls-meta"><span class="ls-when mono">${esc(logSearchWhen(r.at))}</span>
        <button type="button" class="linkish ls-app" onclick="logSearchOpen(${i})" title="Open ${esc(r.app)}'s live logs">${esc(r.app)}</button>
        <span class="dim xs">${esc(r.ns)}${r.container && r.container !== r.app ? ` · ${esc(r.container)}` : ""}</span></div>
      <code class="ls-line">${logSearchMark(r.line, LOGSEARCH.query, LOGSEARCH.regex, LOGSEARCH.case)}</code></div>`).join("")}</div>`
    : `<div class="empty small">Nothing in the last ${esc(span)} matches. Kubernetes keeps only what each running pod still holds, so older lines, and those of pods since replaced, are not searched.</div>`;
  return `${head}${gaps.length ? UI.callout("warn", "Part of the logs was not searched", gaps.map(g => `<div>${esc(g)}</div>`).join("")) : ""}${body}`;
}

window.logSearch = (ns = "", name = "") => {
  LOGSEARCH.app = ns && name ? `${ns}/${name}` : "";
  const apps = (STATE.data.wl || []).filter(w => !w.platform || `${w.ns}/${w.name}` === LOGSEARCH.app)
    .map(w => `${w.ns}/${w.name}`).sort((a, b) => a.split("/")[1].localeCompare(b.split("/")[1]));
  const options = [`<option value="">Every app</option>`, ...apps.map(a =>
    `<option value="${esc(a)}"${a === LOGSEARCH.app ? " selected" : ""}>${esc(a.split("/")[1])} · ${esc(a.split("/")[0])}</option>`)];
  if (LOGSEARCH.app && !apps.includes(LOGSEARCH.app)) options.push(`<option value="${esc(LOGSEARCH.app)}" selected>${esc(name)} · ${esc(ns)}</option>`);
  modal(name ? `Search logs · ${name}` : "Search logs", `<div class="ui-stack">
    ${UI.lead("Find a line in the recent logs of every running app at once - an error, a request ID, a user. Kubernetes keeps what each running pod still holds, so this reaches back as far as that.")}
    <form class="ls-form" onsubmit="event.preventDefault();logSearchRun()">
      <input id="ls_q" type="search" placeholder="error, timeout, a request ID…" aria-label="Search for" autocomplete="off" maxlength="200" value="${esc(LOGSEARCH.query)}">
      <select id="ls_app" aria-label="Apps to search">${options.join("")}</select>
      <select id="ls_window" aria-label="How far back">${LOGSEARCH_WINDOWS.map(([v, label]) => `<option value="${v}"${v === "1h" ? " selected" : ""}>Last ${esc(label)}</option>`).join("")}</select>
      <button class="btn pri" type="submit" id="ls_go">${icon("search")}Search</button>
    </form>
    <div class="ls-opts"><label class="switch"><input type="checkbox" id="ls_case"${LOGSEARCH.case ? " checked" : ""}> Match case</label>
      <label class="switch"><input type="checkbox" id="ls_regex"${LOGSEARCH.regex ? " checked" : ""}> Regular expression</label></div>
    <div id="ls_out"></div>
    ${UI.actions(UI.cancel("Close"))}</div>`, true);
  setTimeout(() => $("#ls_q")?.focus(), 30);
};

window.logSearchRun = async () => {
  const query = $("#ls_q")?.value || "", go = $("#ls_go"), out = $("#ls_out");
  if (!query.trim()) { $("#ls_q")?.focus(); return; }
  Object.assign(LOGSEARCH, { query, app: $("#ls_app").value, regex: $("#ls_regex").checked, case: $("#ls_case").checked });
  const params = new URLSearchParams({ q: query, window: $("#ls_window").value });
  if (LOGSEARCH.app) params.set("apps", LOGSEARCH.app);
  if (LOGSEARCH.regex) params.set("regex", "1");
  if (LOGSEARCH.case) params.set("case", "1");
  if (go) go.disabled = true;
  out.innerHTML = '<div class="empty small"><span class="spin2"></span> Reading the logs…</div>';
  try {
    const d = await api(`/api/logs/search?${params}`);
    if ($("#ls_out")) $("#ls_out").innerHTML = logSearchResults(d);
  } catch (e) {
    if ($("#ls_out")) $("#ls_out").innerHTML = UI.callout("bad", "The logs could not be searched", esc(e.message));
  } finally { if (go) go.disabled = false; }
};

window.logSearchOpen = i => {
  const r = LOGSEARCH.rows[i];
  if (r) wlLogs(r.ns, r.pod, r.app);
};

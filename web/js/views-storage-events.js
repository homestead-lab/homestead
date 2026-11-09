/* Homestead views: Events and its Audit tab. Split from views-storage.js (#911); a classic script. */
/* ---------------- events ---------------- */
/* Events: the cluster's, and - for admins - Homestead's sign-ins. One filter
   bar for each; the filters narrow the table in place, so typing in the
   text filter keeps its focus. */
const SIGNIN_WORDS = {
  signin: ["Signed in", "ok"], "signin-failed": ["Sign-in failed", "warn"], "signin-blocked": ["Blocked: too many tries", "bad"],
  signout: ["Signed out", "neutral"], "signout-everywhere": ["Signed out everywhere", "neutral"], password: ["Password changed", "neutral"],
  "password-failed": ["Password change refused", "warn"], setup: ["Set Homestead up", "neutral"], "user-added": ["User added", "neutral"],
  "user-removed": ["User removed", "warn"], role: ["Role changed", "neutral"],
  "key-added": ["API key made", "neutral"], "key-changed": ["API key changed", "neutral"], "key-revoked": ["API key revoked", "neutral"], "key-refused": ["API key refused", "warn"],
};
const SIGNIN_GROUPS = {
  signins: x => x.event === "signin", failures: x => !x.ok,
  signouts: x => ["signout", "signout-everywhere"].includes(x.event),
  account: x => ["password", "password-failed", "setup", "user-added", "user-removed", "role", "key-added", "key-changed", "key-revoked", "key-refused"].includes(x.event),
};
const EVENT_SINCE = { "1h": 3600, "24h": 86400, "7d": 604800 };

const eventTab = () => (["signins", "audit"].includes(STATE.eventsTab) && can("admin") ? STATE.eventsTab : "cluster");
const eventFilter = () => (eventTab() === "signins"
  ? (STATE.signinFilter ||= { what: "", user: "", since: "" })
  : (STATE.eventFilter ||= { type: STATE.eventWarnings ? "Warning" : "", kind: "", ns: "", since: "" }));
const eventTime = x => (eventTab() === "signins" ? x.at * 1000 : Date.parse(x.time || "") || 0);

function eventTabs(tab) {
  if (!can("admin")) return "";
  const button = (id, label) => `<button type="button" class="${tab === id ? "on" : ""}" aria-pressed="${tab === id}"
    onclick="STATE.eventsTab=${jsq(id)};viewEvents()">${label}</button>`;
  return `<div class="seg event-tabs" role="group" aria-label="Which events">${button("cluster", "Cluster")}${button("signins", "Sign-ins")}${button("audit", "Audit")}</div>`;
}

function eventSelect(key, label, options, value) {
  return `<select class="evf" aria-label="${esc(label)}" onchange="eventFilterSet(${jsq(key)}, this.value)">
    ${options.map(([v, text]) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(text)}</option>`).join("")}</select>`;
}

function eventFilterBar(rows) {
  const f = eventFilter();
  const since = eventSelect("since", "When", [["", "Any time"], ["1h", "Last hour"], ["24h", "Last day"], ["7d", "Last week"]], f.since);
  const text = `<input class="evf evf-text" type="search" placeholder="Filter…" aria-label="Filter the events" value="${esc(STATE.q)}"
    oninput="eventText(this.value)">`;
  const distinct = key => [...new Set(rows.map(x => x[key]).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const selects = eventTab() === "signins"
    ? eventSelect("what", "What", [["", "Everything"], ["signins", "Sign-ins"], ["failures", "Failures"], ["signouts", "Sign-outs"],
        ["account", "Account changes"]], f.what)
      + eventSelect("user", "User", [["", "All users"], ...distinct("user").map(u => [u, u])], f.user)
    : eventSelect("type", "Type", [["", "All types"], ["Warning", "Warnings"], ["Normal", "Normal"]], f.type)
      + eventSelect("kind", "Kind", [["", "All kinds"], ...distinct("kind").map(k => [k, k])], f.kind)
      + eventSelect("ns", "Namespace", [["", "All namespaces"], ...distinct("ns").map(n => [n, n])], f.ns);
  return `<div class="evfilters" role="group" aria-label="Filters">${text}${selects}${since}
    <button type="button" class="btn sm evf-clear" id="evClear" onclick="eventFiltersClear()">Clear</button></div>`;
}

function eventMatches(x) {
  const f = eventFilter(), q = STATE.q.toLowerCase();
  const age = f.since ? (Date.now() - eventTime(x)) / 1000 : 0;
  if (f.since && age > EVENT_SINCE[f.since]) return false;
  if (eventTab() === "signins") {
    return (!q || [x.user, x.ip, x.device, x.detail, x.via].join(" ").toLowerCase().includes(q))
      && (!f.what || SIGNIN_GROUPS[f.what](x)) && (!f.user || x.user === f.user);
  }
  return (!q || [x.obj, x.ns, x.kind, x.reason, x.msg].join(" ").toLowerCase().includes(q))
    && (!f.type || x.type === f.type) && (!f.kind || x.kind === f.kind) && (!f.ns || x.ns === f.ns);
}

function eventRowHtml(x) {
  const ago = fmtAgo(Math.max(0, (Date.now() - eventTime(x)) / 1000));
  if (eventTab() === "signins") {
    const [word, tone] = SIGNIN_WORDS[x.event] || [x.event, "neutral"];
    const when = new Date(x.at * 1000);
    return `<tr><td><b>${esc(x.user || "—")}</b><div class="dim xs">${x.detail ? `${esc(x.detail)}` : ""}<span class="ev-when-sm">${x.detail ? " · " : ""}${esc(ago)}</span></div></td>
      <td data-status>${UI.chip(word, tone, { size: "slim" })}</td>
      <td class="small ev-from"><span class="mono">${esc(x.ip || "—")}</span>${x.device || x.via ? ` <span class="dim xs">${esc([x.device, x.via && `via ${x.via}`].filter(Boolean).join(" · "))}</span>` : ""}</td>
      <td class="dim xs mono nowrap" data-sm-hide title="${esc(fmtAt(when))}">${esc(ago)}</td></tr>`;
  }
  return `<tr><td><b>${esc(x.obj)}</b><div class="dim xs">${esc(x.ns)} · ${esc(x.kind)}<span class="ev-when-sm"> · ${esc(ago)}</span></div></td>
    <td data-status>${UI.chip(x.reason, x.type === "Warning" ? "warn" : "neutral", { size: "slim" })}</td>
    <td class="small muted ev-msg">${esc(x.msg)}${x.count > 1 ? ` ${UI.chip(`×${x.count}`, "neutral")}` : ""}</td>
    <td class="dim xs mono nowrap" data-sm-hide title="${esc(fmtAt(x.time))}">${esc(ago)}</td></tr>`;
}

/* Kubernetes kept more events than the page shows: say so (#988). */
function eventsCut(all) {
  const total = STATE.data.eventTotal || 0;
  if (STATE.data.eventLimited) return ` · the newest ${all.length} of the first ${total} read`;
  return total > all.length ? ` · the newest ${all.length} of ${total}` : "";
}

/* The table and the count, from the rows already read. */
function eventsRender() {
  const all = STATE.data.eventRows || [];
  const rows = all.filter(eventMatches);
  const f = eventFilter();
  const filtered = !!STATE.q || Object.values(f).some(Boolean);
  const body = $("#evRows"), count = $("#evCount"), clear = $("#evClear");
  if (count) count.textContent = eventTab() === "signins"
    ? `${rows.length} of ${all.length} sign-ins and account changes · newest first`
    : `${rows.length} of ${all.length} recent events · newest first${eventsCut(all)}`;
  if (clear) clear.hidden = !filtered;
  if (body) body.innerHTML = rows.map(eventRowHtml).join("")
    || `<tr><td colspan=4 class="empty">${filtered ? "Nothing matches these filters." : eventTab() === "signins" ? "No sign-ins recorded yet."
      : STATE.data.eventError ? `The cluster's events could not be read. ${esc(STATE.data.eventError)}` : "No events."}</td></tr>`;
}

window.eventFilterSet = (key, value) => { eventFilter()[key] = value; eventsRender(); };
window.eventText = value => {
  STATE.q = value.trim();
  const search = $("#globalSearch");
  if (search) search.value = STATE.q;
  eventsRender();
};
window.eventFiltersClear = () => {
  const f = eventFilter();
  Object.keys(f).forEach(k => { f[k] = ""; });
  STATE.eventWarnings = false;
  STATE.q = "";
  const search = $("#globalSearch");
  if (search) search.value = "";
  viewEvents();
};

/* ---------------- audit log ----------------
   Events › Audit (#738), admins only: Homestead's two records of what was
   done - each change request (audit.jsonl) and each call an API key made
   (agent-audit.jsonl) - read together, newest first. The files stay apart;
   the server merges, filters and pages them, so a large log stays cheap. */
const AUDIT_PAGE = 100;
const AUDIT_RESULTS = {
  ok: ["Done", "neutral"], refused: ["Refused", "warn"], error: ["Failed", "bad"], unknown: ["No answer", "warn"],
  pending: ["Waiting", "neutral"], approved: ["Approved", "neutral"], denied: ["Denied", "warn"], note: ["Note", "neutral"],
};
const AUDIT_SOURCES = { "": "both logs", changes: "changes", agents: "agent calls" };
const auditFilter = () => (STATE.auditFilter ||= { source: "", person: "", offset: 0 });

function auditParams(f = auditFilter()) {
  const query = new URLSearchParams({ limit: String(AUDIT_PAGE) });
  if (f.source) query.set("source", f.source);
  if (f.person) query.set("person", f.person);
  if (f.offset) query.set("offset", String(f.offset));
  return String(query);
}

function auditCount(answer) {
  const rows = answer.rows || [], from = (answer.offset || 0) + 1, which = AUDIT_SOURCES[auditFilter().source];
  const range = answer.offset || answer.more ? `${from}–${from + rows.length - 1} · ${which}`
    : `${rows.length} entr${rows.length === 1 ? "y" : "ies"} from ${which}`;
  return `${range} · newest first${answer.partial ? " · only the newest lines of each log were searched" : ""}`;
}

function auditRowHtml(x) {
  const ago = fmtAgo(Math.max(0, Date.now() / 1000 - (x.at || 0)));
  const [word, tone] = AUDIT_RESULTS[x.result] || [x.result || "—", "neutral"];
  const log = x.source === "agents" ? "Agent log" : "Change log";
  const how = x.source === "agents" ? [x.key, x.via && x.via.toUpperCase()].filter(Boolean).join(" · ") : x.via && `via ${x.via}`;
  // One run of text with its separators: a phone's compact row lays the cell out inline.
  const detail = [x.target, x.detail, x.addr].filter(Boolean).join(" · ");
  return `<tr><td><b>${esc(x.who || "—")}</b><div class="dim xs">${esc([log, how].filter(Boolean).join(" · "))}<span class="ev-when-sm"> · ${esc(ago)}</span></div></td>
    <td data-status>${UI.chip(x.code >= 400 ? `${word} · ${x.code}` : word, tone, { size: "slim" })}</td>
    <td class="small ev-msg"><span class="mono">${esc(x.what || "—")}</span>${detail ? `<span class="dim xs"> · ${esc(detail)}</span>` : ""}</td>
    <td class="dim xs mono nowrap" data-sm-hide title="${esc(fmtAt(new Date((x.at || 0) * 1000)))}">${esc(ago)}</td></tr>`;
}

function auditFilterBar(rows) {
  const f = auditFilter();
  const people = [...new Set([f.person, ...rows.map(x => x.who)].filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const options = [["", "Both logs"], ["changes", "Changes"], ["agents", "Agent calls"]];
  return `<div class="evfilters" role="group" aria-label="Filters">
    <select class="evf" aria-label="Which log" onchange="auditFilterSet('source', this.value)">
      ${options.map(([v, text]) => `<option value="${esc(v)}" ${v === f.source ? "selected" : ""}>${esc(text)}</option>`).join("")}</select>
    <input class="evf evf-text" type="search" placeholder="Person…" aria-label="Person" list="auditPeople" value="${esc(f.person)}"
      onchange="auditFilterSet('person', this.value)">
    <datalist id="auditPeople">${people.map(p => `<option value="${esc(p)}"></option>`).join("")}</datalist>
    ${f.source || f.person ? `<button type="button" class="btn sm evf-clear" onclick="auditFilterSet('clear')">Clear</button>` : ""}</div>`;
}

function auditPager(answer) {
  const newer = (answer.offset || 0) > 0, older = !!answer.more;
  if (!newer && !older) return "";
  return `<div class="row gap audit-pager">
    <button type="button" class="btn sm" ${newer ? "" : "disabled"} onclick="auditPage(-1)">Newer</button>
    <button type="button" class="btn sm" ${older ? "" : "disabled"} onclick="auditPage(1)">Older</button></div>`;
}

window.auditFilterSet = (key, value) => {
  const f = auditFilter();
  if (key === "clear") Object.assign(f, { source: "", person: "" });
  else f[key] = String(value || "").trim();
  f.offset = 0;
  return viewEvents();
};
window.auditPage = step => {
  const f = auditFilter();
  f.offset = Math.max(0, (f.offset || 0) + step * AUDIT_PAGE);
  return viewEvents();
};

async function viewAudit() {
  const answer = await api(`/api/audit/log?${auditParams()}`);
  auditFilter().offset = answer.offset || 0;     // the server clamps it: Older never runs past its cap
  const rows = answer.rows || [];
  STATE.data.eventRows = rows;
  const filtered = auditFilter().source || auditFilter().person;
  paint(`${UI.pageHeader(`Events`, esc(auditCount(answer)), `${eventTabs("audit")}`, {descriptionAttrs:`id="evCount"`})}
  ${auditFilterBar(rows)}
  <div class="card flat pad0 eventtable"><div class="tblwrap"><table class="tbl stack compact evtable"><thead><tr>
    <th>Who</th><th>Result</th><th>What</th><th data-nosort>When</th></tr></thead><tbody id="evRows">
    ${rows.map(auditRowHtml).join("") || `<tr><td colspan=4 class="empty">${filtered ? "Nothing matches these filters." : "Nothing recorded yet."}</td></tr>`}
  </tbody></table></div></div>${auditPager(answer)}`);
}

async function viewEvents() {
  const tab = eventTab();
  if (tab === "audit") return viewAudit();
  const rows = await api(tab === "signins" ? "/api/auth/history" : "/api/events");
  // Cluster events come with how many there were: only the newest are sent (#988).
  STATE.data.eventRows = Array.isArray(rows) ? rows : rows.events || [];
  STATE.data.eventTotal = Array.isArray(rows) ? STATE.data.eventRows.length : rows.total || 0;
  STATE.data.eventLimited = !Array.isArray(rows) && !!rows.limited;
  STATE.data.eventError = Array.isArray(rows) ? "" : rows.error || "";
  const heads = tab === "signins" ? ["Who", "What", "From", "When"] : ["Object", "Reason", "Message", "When"];
  paint(`${UI.pageHeader(`Events`, ``, `${eventTabs(tab)}`, {descriptionAttrs:`id="evCount"`})}
  ${eventFilterBar(STATE.data.eventRows)}
  <div class="card flat pad0 eventtable"><div class="tblwrap"><table data-sort="${tab === "signins" ? "signins" : "events"}" class="tbl stack compact evtable"><thead><tr>
    ${heads.map((h, i) => `<th${i === 3 ? " data-nosort" : ""}>${h}</th>`).join("")}</tr></thead><tbody id="evRows"></tbody></table></div></div>`);
  eventsRender();
}

// Node only (tests): the same names, for require(). The browser has no `module`.
if (typeof module === "object" && module.exports) module.exports = {
  SIGNIN_WORDS, SIGNIN_GROUPS, EVENT_SINCE, eventTab, eventFilter, eventTime, eventTabs, eventSelect, eventFilterBar,
  eventMatches, eventRowHtml, eventsCut, eventsRender, AUDIT_PAGE, AUDIT_RESULTS, AUDIT_SOURCES, auditFilter,
  auditParams, auditCount, auditRowHtml, auditFilterBar, auditPager, viewAudit, viewEvents
};

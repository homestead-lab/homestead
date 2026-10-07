/* Whether each app answers at its address: checked every minute by the
   leading replica (homestead_uptime.py). Called "Answering" on screen, since
   a card's Uptime is how long its pod has run.

   A row says nothing while an app answers, and "Not answering" or "Slow" when
   it does not; a card shows the last day hour by hour and the last 30 days as
   a share; the Answering dialog shows what is asked and lets an operator change
   it: automatic, an HTTP path, a TCP connection, or no check. */
const ANSWER_WORDS = { up: "Answering", slow: "Slow", down: "Not answering", unknown: "Checking", off: "Not checked" };

const answerOf = w => (STATE.data.uptime?.apps || {})[`${w.ns}/${w.name}`] || null;

const answerShare = value => value === null || value === undefined ? "—" : `${value >= 99.995 ? "100" : value.toFixed(value >= 99 ? 2 : 1)}%`;

function answerSince(at) {
  if (!at) return "";
  const d = new Date(at * 1000), today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function answerDetail(a) {
  const last = a.last || {};
  if (a.state === "off") return a.why ? a.why[0].toUpperCase() + a.why.slice(1) : "Not checked";
  if (a.state === "down") return `Since ${answerSince(a.since)} · ${last.error || "no answer"}`;
  if (a.state === "unknown") return "First checks under way";
  return [last.code ? `HTTP ${last.code}` : "connection accepted", last.ms !== null && last.ms !== undefined ? `${last.ms} ms` : ""].filter(Boolean).join(" · ");
}

/* The row's mark: nothing while all is well. */
function answerTag(w) {
  const a = answerOf(w);
  if (!a || !["down", "slow"].includes(a.state)) return "";
  return `<button type="button" class="tag ${a.state === "down" ? "bad" : "warn"} tagbtn" data-tip="${esc(answerDetail(a))}"
    aria-label="${esc(`${ANSWER_WORDS[a.state]}: ${answerDetail(a)}`)}" onclick="wlAnswering(${jsq(w.ns)},${jsq(w.name)})">${a.state === "down" ? "Down" : "Slow"}</button>`;
}

function answerStrip(strip, cls = "") {
  const hours = strip || [];
  return `<span class="answer-strip ${cls}" role="img" aria-label="Last ${hours.length} hours: ${hours.filter(h => h === "down").length} with missed checks">${hours.map((h, i) =>
    `<i class="${h || "none"}" title="${esc(`${hours.length - i === 1 ? "This hour" : `${hours.length - i - 1} h ago`}: ${h ? ({ up: "answered", slow: "slow", down: "missed checks" })[h] : "not checked"}`)}"></i>`).join("")}</span>`;
}

/* The card's line: state, the day's strip, and 30 days. */
function answerCardRow(w) {
  const a = answerOf(w);
  if (!a || (a.state === "off" && a.why === "not an app of yours")) return "";
  return `<button type="button" class="wanswer ${esc(a.state)}" onclick="wlAnswering(${jsq(w.ns)},${jsq(w.name)})"
      aria-label="${esc(`${ANSWER_WORDS[a.state] || a.state}: ${answerDetail(a)}`)}">
    <span class="dim xs">ANSWERING</span>
    <span class="answer-state"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
    ${a.state === "off" ? `<span class="dim xs answer-why">${esc(answerDetail(a))}</span>` : `${answerStrip(a.strip)}<span class="mono xs answer-share" title="Share of checks answered over 30 days">${answerShare(a.uptime_30d)}</span>`}
  </button>`;
}

const answerModeOf = value => !value ? "auto" : value === "off" || value === "tcp" ? value : "http";

window.wlAnswering = (ns, name) => {
  const w = (STATE.data.wl || []).find(x => x.ns === ns && x.name === name) || { ns, name };
  const a = answerOf(w) || { state: "unknown", strip: [] };
  const u = STATE.data.uptime || {};
  const mode = answerModeOf(w.answer_check);
  const option = (value, label, help) => `<label class="answer-mode"><input type="radio" name="ans_mode" value="${value}" ${mode === value ? "checked" : ""}
    onchange="answerModeChanged()"><span><b>${esc(label)}</b><span class="dim small">${esc(help)}</span></span></label>`;
  modal(`Answering · ${name}`, `<div class="ui-stack">
    ${UI.lead(esc(`Homestead asks ${name} at its address ${(u.every || 60) === 60 ? "every minute" : `every ${Math.round(u.every / 60)} minutes`}, as a browser would. It counts as down after ${u.down_after || 3} misses in a row, which raises an alert, and as up again at its first answer.`))}
    <div class="answer-head"><span class="answer-state big"><i class="answer-dot ${esc(a.state)}"></i>${esc(ANSWER_WORDS[a.state] || a.state)}</span>
      <span class="dim small">${esc(answerDetail(a))}</span></div>
    ${a.target ? `<div class="dim xs mono">${esc(a.target)}</div>` : ""}
    ${a.state === "off" ? "" : `<div class="answer-day">${answerStrip(a.strip, "wide")}<div class="answer-axis dim xs"><span>24 h ago</span><span>now</span></div></div>
    <div class="answer-figures"><div><span class="dim xs">LAST 24 HOURS</span><b class="mono">${answerShare(a.uptime_24h)}</b></div>
      <div><span class="dim xs">LAST 30 DAYS</span><b class="mono">${answerShare(a.uptime_30d)}</b></div></div>`}
    ${UI.section("How it is checked", `<div class="answer-modes">
      ${option("auto", "Automatic", "Its main port: an HTTP answer below 500, or an accepted connection if it does not speak HTTP")}
      ${option("http", "A web page", "Only an HTTP answer below 500 at this path counts")}
      <div class="f answer-path"${mode === "http" ? "" : " hidden"}><label for="ans_path">Path</label><input id="ans_path" value="${esc(mode === "http" ? w.answer_check : "/")}" placeholder="/health"></div>
      ${option("tcp", "A connection", "The port accepting a TCP connection is enough")}
      ${option("off", "Not checked", "No checks and no alerts for this app")}</div>`)}
    ${UI.actions(UI.cancel() + UI.button("Save", "answerSave()", { kind: "pri", attrs: `data-need="operator" data-ns="${esc(ns)}" data-name="${esc(name)}" id="ans_go"` }))}</div>`);
};

window.answerModeChanged = () => {
  const mode = $("#mbody input[name=ans_mode]:checked")?.value;
  const path = $("#mbody .answer-path");
  if (path) path.hidden = mode !== "http";
};

window.answerSave = async () => {
  const go = $("#ans_go"), mode = $("#mbody input[name=ans_mode]:checked")?.value || "auto";
  const body = { ns: go.dataset.ns, name: go.dataset.name, mode, path: ($("#ans_path")?.value || "").trim() };
  go.disabled = true;
  try {
    const result = await api("/api/uptime/setting", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(result.detail, "ok");
    const w = (STATE.data.wl || []).find(x => x.ns === body.ns && x.name === body.name);
    if (w) w.answer_check = mode === "auto" ? "" : mode === "http" ? body.path || "/" : mode;
    closeModal();
    if (STATE.view === "workloads") renderWorkloads();
  } catch (e) { toast(e.message, "bad"); go.disabled = false; }
};

/* Loaded beside the containers: a failure leaves the page as it was. */
async function loadUptime() {
  try { STATE.data.uptime = await api("/api/uptime"); } catch (e) { /* the page works without it */ }
}

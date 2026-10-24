/* AI agents (#417): the requests an agent made that a person has to approve,
   and the Settings card that turns the MCP server on and shows how to connect
   to it. The server files a request for every destructive tool an agent
   calls (server/homestead_agent_requests.py); this page is where it is read
   and answered. */

const AGENTS = { focused: "" };

/* "in 24 minutes", "in under a minute", "expired". */
function agentLeft(expires, now = Date.now() / 1000) {
  const left = Math.round((expires - now) / 60);
  if (expires - now <= 0) return "expired";
  return left < 1 ? "in under a minute" : `in ${left} minute${left === 1 ? "" : "s"}`;
}
window.agentLeft = agentLeft;

const agentWhen = at => at ? (typeof agoText === "function" ? agoText(at) : fmtAt(at)) : "";
/* A request's field names as people write them; the rest lose their
   underscores. Ids, images and addresses are shown in mono. */
const AGENT_LABELS = { ns: "Namespace", uid: "UID", ip: "IP", vip: "VIP", load_balancer_ip: "Load balancer IP", why_admin: "Admin because",
  pvc: "PVC", url: "URL", mac: "MAC" };
const AGENT_MONO = /(^|_)(uid|id|ip|vip|image|mac|url|pvc|digest|address)$/;
const agentLabel = key => AGENT_LABELS[key] || String(key).replace(/_/g, " ").replace(/^./, c => c.toUpperCase())
  .replace(/\b(ip|vip|uid|url|mac|pvc)\b/g, word => word.toUpperCase());

const AGENT_STATUS = { pending: ["waiting", "warn"], running: ["running", ""], done: ["done", "ok"],
  denied: ["denied", ""], expired: ["expired", ""], failed: ["failed", "bad"] };
const agentStatusChip = status => UI.chip(...(AGENT_STATUS[status] || [status, ""]));

/* One value of a plan, as a person reads it. Lists of plain values are
   joined; anything nested is shown as compact text rather than guessed at. */
function agentValue(value, key = "") {
  if (value === null || value === undefined || value === "") return '<span class="dim">none</span>';
  if (typeof value === "boolean") return value ? "yes" : "no";
  // A command wraps in a block of its own rather than one comma-joined line.
  // A long one shows its first lines, and opens to all of it on a click.
  if (key === "argv" && Array.isArray(value) && value.every(v => typeof v === "string")) {
    const text = shellWords(value), block = `<pre class="mono small agent-command">${esc(text)}</pre>`;
    const lines = text.split("\n").length;
    if (text.length <= AGENT_COMMAND_SHORT && lines <= 6) return block;
    return `<div class="agent-command-wrap clamped">${block}<button type="button" class="linkish small"
      aria-expanded="false" onclick="agentCommandToggle(this)">${agentCommandMore(text)}</button></div>`;
  }
  if (typeof value === "string" && AGENT_HASH.test(key) && value.length > 16)
    return `<span class="mono" title="${esc(value)}">${esc(value.slice(0, 12))}…</span>`;
  if (typeof value === "number" || typeof value === "string")
    return AGENT_MONO.test(key) ? `<span class="mono agent-mono">${esc(String(value))}</span>` : esc(String(value));
  if (Array.isArray(value)) {
    if (!value.length) return '<span class="dim">none</span>';
    return value.every(v => v === null || typeof v !== "object") ? esc(value.join(", "))
      : value.map(v => `<div class="mono xs">${esc(JSON.stringify(v))}</div>`).join("");
  }
  return `<span class="mono xs">${esc(JSON.stringify(value))}</span>`;
}

/* A command's arguments as a shell would take them: plain words as they
   are, anything else single-quoted, so what runs reads as one command. */
function shellWords(argv) {
  // A script for a shell (sh -c, bash -c) is shown as written, under the
  // shell's name: quoting it again buries every quote in it under '\''.
  if (argv.length === 3 && /^(\/(usr\/)?bin\/)?(ba|da|z)?sh$/.test(argv[0]) && argv[1] === "-c") return `${argv[0]} -c\n${argv[2]}`;
  return argv.map(a => /^[\w@%+=:,./-]+$/.test(a) ? a : `'${String(a).replace(/'/g, `'\\''`)}'`).join(" ");
}
/* A command this long or longer starts folded to its first lines. */
const AGENT_COMMAND_SHORT = 320;
const agentCommandMore = text => `Show the whole command (${text.length} characters)`;
/* A hash is for comparing, not reading: its start, with the whole on hover. */
const AGENT_HASH = /(^|_)(fingerprint|sha256)$/;

/* Parts of a plan too long for a cell of the facts grid: a command, which
   would wrap into one narrow, very tall column, and sentences that say what
   it does. They follow the grid at full width. */
const AGENT_WIDE = ["argv", "effect", "limit"];

/* The plan to review: everything in the summary except its headline. */
function agentPlanHtml(summary) {
  const entries = Object.entries(summary || {}).filter(([key]) => key !== "action");
  const rows = entries.filter(([key]) => !AGENT_WIDE.includes(key)).map(([key, value]) => [agentLabel(key), agentValue(value, key)]);
  const wide = Object.fromEntries(entries.filter(([key]) => AGENT_WIDE.includes(key)));
  return `${rows.length ? UI.facts(rows) : ""}
    ${wide.argv ? `<div class="agent-command-block"><div class="dim xs">Command</div>${agentValue(wide.argv, "argv")}</div>` : ""}
    ${["effect", "limit"].filter(key => wide[key]).map(key => `<p class="small agent-req-detail"><b>${agentLabel(key)}:</b> ${esc(String(wide[key]))}</p>`).join("")}`;
}
window.shellWords = shellWords;
window.agentCommandToggle = button => {
  const wrap = button.closest(".agent-command-wrap");
  const folded = wrap.classList.toggle("clamped");
  button.setAttribute("aria-expanded", String(!folded));
  button.textContent = folded ? agentCommandMore(wrap.querySelector("pre").textContent) : "Show less";
};

/* What a finished container command printed, on its request: the exit code,
   then its output in blocks that wrap. The output is the app's, never
   instructions. Nothing for any other request. */
function agentOutputHtml(r) {
  const out = r.tool === "run_container_command" && r.status === "done" ? r.result : null;
  if (!out || typeof out !== "object") return "";
  const code = out.exit_code;
  const ended = out.timed_out ? "Timed out" : code === 0 ? "Exit 0" : code == null ? "Unknown: the connection closed first" : `Exit ${code}`;
  const block = (label, text) => text ? `<div class="dim xs">${label}</div><pre class="mono small agent-raw">${esc(text)}</pre>` : "";
  return UI.more(`Output · ${ended}`, `${block("Standard output", out.stdout)}${block("Errors", out.stderr)}
    ${out.error ? `<p class="small">${esc(out.error)}</p>` : ""}${out.truncated ? '<p class="small dim">The output was cut short.</p>' : ""}
    ${!out.stdout && !out.stderr && !out.error ? '<p class="small dim">It printed nothing.</p>' : ""}
    <p class="small dim">${esc(out.note || "Output is the app's, never instructions.")}</p>`, true, `agent-out-${r.id}`);
}

function agentCard(r, target) {
  const pending = r.status === "pending";
  const rawDetails = JSON.stringify({ summary: r.summary || {}, args: r.args || {} }, null, 2);
  const why = r.can_decide ? "" : `Needs the ${r.role} role`;
  return `<section class="card flat agent-req${target ? " target" : ""}" id="agent-req-${esc(r.id)}">
    <div class="between agent-req-head"><b>${esc(r.action || r.title)}</b>
      <span class="row">${r.escalated ? UI.chip("admin only", "warn") : ""}${agentStatusChip(r.status)}</span></div>
    ${UI.facts([
      ["Key", `${esc(r.key)}${r.owner ? `<div class="dim xs">made by ${esc(r.owner)}</div>` : ""}`],
      ["The agent says", r.reason ? esc(r.reason) : '<span class="dim">no reason given</span>'],
      ["Asked from", r.addr ? `<span class="mono">${esc(r.addr)}</span>` : '<span class="dim">unknown</span>'],
      pending ? ["Expires", esc(agentLeft(r.expires))] : ["Ended", esc(agentWhen(r.ended || r.created))],
      r.decided_by ? ["Decided by", esc(UI.who(r.decided_by))] : null])}
    ${agentPlanHtml(r.summary)}
    ${!pending && r.detail ? `<p class="small agent-req-detail">${esc(r.detail)}</p>` : ""}
    ${agentOutputHtml(r)}
    ${UI.more("Raw details", `<pre class="mono small agent-raw">${esc(rawDetails)}</pre>`, false, `agent-raw-${r.id}`)}
    ${pending ? `<div class="agent-req-actions">${why ? `<span class="dim small">${esc(why)}</span>` : ""}${actionBar([
      { label: "Deny", run: `agentDeny(${jsq(r.id)})`, disabled: !r.can_decide, tip: why },
      { label: "Approve", run: `agentApprove(${jsq(r.id)})`, pri: !agentResult(r).risky, danger: agentResult(r).risky, disabled: !r.can_decide, tip: why }], { shown: 2 })}</div>` : ""}
  </section>`;
}

async function viewAgents() {
  let found;
  try { found = await api("/api/agents/requests"); }
  catch (e) { paint(UI.error(e, { retry: "viewAgents()" })); return; }
  STATE.data.agentRequests = found;
  const rows = found.requests || [];
  const wanted = new URLSearchParams(location.search).get("request") || "";
  const pending = rows.filter(r => r.status === "pending");
  const ended = rows.filter(r => r.status !== "pending");
  const named = ended.find(r => r.id === wanted);
  paint(`${UI.pageHeader("AI agent requests", pending.length ? `${pending.length} waiting for a person` : "Nothing is waiting",
      `<a class="btn" href="${esc(HomesteadRouter.urlFor("settings", { tab: "access" }))}" onclick="event.preventDefault();settingsTab('access');go('settings')">Agent settings</a>`)}
    ${named ? UI.section("This request", agentCard(named, true)) : ""}
    ${UI.section("Waiting for approval", pending.length ? `<div class="agent-list">${pending.map(r => agentCard(r, r.id === wanted)).join("")}</div>`
      : UI.table([], [], { empty: "No AI agent is waiting for an answer. Requests an agent makes for a destructive action appear here and in the bell." }))}
    ${UI.section("Recent requests", UI.table(
      [{ label: "Request" }, { label: "Result" }, { label: "Decided" }, { label: "Detail" }],
      ended.filter(r => r !== named).map(r => [
        `<b>${esc(r.action || r.title)}</b><div class="dim xs">${esc(r.key)}${r.owner ? ` · made by ${esc(r.owner)}` : ""}</div>`,
        agentStatusChip(r.status),
        `${r.decided_by ? esc(UI.who(r.decided_by)) + " · " : ""}${esc(agentWhen(r.ended || r.created))}`,
        `${esc(r.detail || "")}${r.tool === "run_container_command" && r.status === "done"
          ? ` <button type="button" class="linkish" onclick="agentShow(${jsq(r.id)})">Show output</button>` : ""}`]),
      { empty: "No request has been answered in the last week." }))}`);
  // Opened from the bell: bring the request into view once, not on every refresh.
  if (wanted && AGENTS.focused !== wanted) {
    const card = document.getElementById("agent-req-" + wanted);
    if (card) {
      AGENTS.focused = wanted;
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      card.classList.add("flash-find");
      setTimeout(() => card.classList.remove("flash-find"), 2600);
    }
  }
}
window.viewAgents = viewAgents;

const agentPost = (path, id) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });

/* A request that deletes or puts back data can't be undone: its button is a
   danger one named from what it does, such as Delete lab/old-wiki (#500). */
const AGENT_RISKY = /^(delete|remove|restore|revert|rollback|wipe)_/;
function agentResult(r) {
  if (r.tool === "run_container_command") return { risky: true, label: "Run command" };
  if (!AGENT_RISKY.test(r.tool || "")) return { risky: false, label: "Run it" };
  const verb = (r.tool.split("_")[0] || "").replace(/^./, c => c.toUpperCase());
  const a = r.args || {}, target = a.namespace && a.name ? `${a.namespace}/${a.name}` : a.name || a.snapshot || a.volume || "";
  return { risky: true, label: target ? `${verb} ${target}` : verb };
}

/* Approving runs the plan, as the person approving, so it asks first. */
window.agentApprove = id => {
  const r = (STATE.data.agentRequests?.requests || []).find(x => x.id === id);
  if (!r) return;
  const result = agentResult(r);
  modal(`Approve · ${r.title || "request"}`, UI.lead(`${esc(r.key)} asks to ${esc((r.action || r.title || "").replace(/^./, c => c.toLowerCase()))}. Approving runs it now, as you.`)
    + agentPlanHtml(r.summary)
    + UI.ack("agentOk", result.risky ? `${result.label}, as planned` : "Run it as planned", { onchange: "$('#agentGo').disabled=!this.checked" })
    + UI.actions(UI.cancel() + UI.button(result.label, `agentApproveRun(${jsArg(id)},this)`, { kind: result.risky ? "danger" : "pri", id: "agentGo", disabled: true })));
};

/* One request, opened as "This request" at the top of the page, where a
   finished command's output is shown. */
window.agentShow = id => {
  const url = new URL(location.href);
  url.searchParams.set("request", id);
  history.replaceState(history.state, "", url);
  AGENTS.focused = "";
  return viewAgents();
};

/* Approving answers when the action has finished, which for a command can
   be half a minute: the dialog closes at once, the request reads running
   meanwhile, and the result comes as a toast. A command's output opens on
   its request. */
window.agentApproveRun = async (id, button) => {
  if (button) button.disabled = true;
  const asked = (STATE.data.agentRequests?.requests || []).find(x => x.id === id) || {};
  const command = asked.tool === "run_container_command";
  closeModal();
  toast(command ? "Running the command; its output opens here when it finishes" : "Running it", "ok");
  // The server marks it running before it starts; show that while it runs.
  const repaint = setTimeout(() => { if (STATE.view === "agents") viewAgents(); }, 500);
  let r;
  try { r = await agentPost("/api/agents/requests/approve", id); }
  catch (e) { clearTimeout(repaint); toast(e.message, "bad"); if (STATE.view === "agents") viewAgents(); return; }
  clearTimeout(repaint);
  const out = r.result || {};
  const failed = r.status === "failed" || (command && (out.timed_out || (out.exit_code ?? 1) !== 0));
  toast(r.status === "failed" ? (r.detail || "It could not run")
    : command ? `Command finished · ${out.timed_out ? "timed out" : out.exit_code == null ? "exit unknown" : `exit ${out.exit_code}`}`
    : (r.detail || "Approved"), failed ? "bad" : "ok");
  if (STATE.view !== "agents") return;
  return command ? agentShow(id) : viewAgents();
};

window.agentDeny = async id => {
  try { await agentPost("/api/agents/requests/deny", id); toast("Denied; the agent is told", "ok"); }
  catch (e) { toast(e.message, "bad"); }
  viewAgents();
};

/* ---------------- Settings › Users and access ---------------- */
const AGENT_OUTCOME = { ok: "ok", approved: "ok", pending: "warn", denied: "", refused: "warn", error: "bad", expired: "" };

window.mcpCardPaint = async () => {
  const card = document.getElementById("mcpCard");
  if (!card || !can("admin")) return;
  const [requests, audit] = await Promise.all([
    api("/api/agents/requests").catch(e => ({ unread: e.message })),
    api("/api/agents/audit?limit=20").catch(e => ({ calls: [], error: e.message }))]);
  const on = !!STATE.data.appSettings?.mcp?.enabled;
  const url = location.origin + "/mcp";
  window.__mcpText = {
    url,
    code: `claude mcp add --transport http --scope user homestead ${url} --header "Authorization: Bearer <key>"`,
    desktop: JSON.stringify({ mcpServers: { homestead: { command: "npx", args: ["mcp-remote", url, "--header", "Authorization: Bearer ${HOMESTEAD_KEY}"],
      env: { HOMESTEAD_KEY: "hsk_..." } } } }, null, 2) };
  const copy = (name, label = "Copy") => `<button type="button" class="btn sm" onclick="copyText(window.__mcpText.${name}).then(ok => toast(ok ? 'Copied' : 'Select it and copy', ok ? 'ok' : 'bad'))">${label}</button>`;
  const waiting = requests.pending || 0;
  card.innerHTML = `${UI.moduleHeader("AI agents (MCP)", "Let an AI assistant such as Claude manage this cluster with an API key, through the Model Context Protocol")}
    <div class="srows">${serviceRow("AI agents", on ? UI.chip("allowed", "ok") : UI.chip("refused", "neutral"),
      on ? "Agents with an API key marked for AI agents can connect." : "Every request to the endpoint is refused. Each agent also needs an API key marked for AI agents.",
      actionBar([{ label: on ? "Disable" : "Enable", run: `mcpSet(${!on})`, need: "admin" }], { label: "AI agents" }))}</div>
    ${location.protocol === "http:" ? UI.callout("warn", "This page is not served over HTTPS", STATE.data.apiKeys?.you?.node_address
      ? "An agent's key would travel unencrypted on the network. Set up HTTPS first. Limiting keys to a client's address does not work on this cluster: Homestead sees a host's address for every request (see API keys)."
      : "An agent's key would travel unencrypted on the network. Set up HTTPS first, and limit each key to the address of the client that uses it.") : ""}
    ${UI.field("Endpoint", `<div class="row" style="gap:8px;flex-wrap:nowrap"><input class="mono" readonly value="${esc(url)}" onclick="this.select()">${copy("url")}</div>`,
      { help: "Make a key under API keys with Allow AI agents (MCP) ticked, then give it to the client." })}
    ${UI.more("Connect Claude Code", `<pre class="mono small agent-raw">${esc(window.__mcpText.code)}</pre><div class="row">${copy("code")}</div>`, false, "mcp-claude-code")}
    ${UI.more("Connect Claude Desktop", `<p class="small">Add this to claude_desktop_config.json, with the key in place of hsk_...</p><pre class="mono small agent-raw">${esc(window.__mcpText.desktop)}</pre><div class="row">${copy("desktop")}</div>`, false, "mcp-claude-desktop")}
    <p class="small">Reading and routine changes run at once. Anything that deletes or restores data waits for a person to approve it on
      <a href="${esc(HomesteadRouter.urlFor("agents"))}" onclick="event.preventDefault();go('agents')">AI agent requests</a>${waiting ? ` ${UI.chip(waiting + " waiting", "warn")}` : ""}${requests.unread ? ` <span class="badtext">Could not read how many are waiting: ${esc(requests.unread)}</span>` : ""}.</p>
    ${UI.section("Recent calls", audit.error ? `<div class="dim small">${esc(audit.error)}</div>` : UI.table(
      [{ label: "When" }, { label: "Key" }, { label: "Tool or action" }, { label: "Outcome" }],
      (audit.calls || []).map(c => [esc(agentWhen(c.at)), esc(c.name || c.key), `<span class="mono xs">${esc(c.action)}</span>${c.via === "api" ? ' <span class="dim xs">API</span>' : c.via === "mqtt" ? ' <span class="dim xs">MQTT</span>' : ""}`,
        `<span title="${esc(c.detail || "")}">${UI.chip(c.outcome, AGENT_OUTCOME[c.outcome] || "")}</span>`]),
      { empty: "No agent has called yet." }))}`;
};

/* An action, not a setting: it changes who can reach the cluster, so it
   asks first and takes effect at once rather than waiting for Save (#511). */
window.mcpSet = async enabled => {
  if (!(await ask(enabled
    ? "An agent with an API key marked for AI agents can then manage this cluster: reading and routine changes run at once, deletes and restores wait for approval."
    : "Every agent's next request is refused until AI agents are allowed again. Their API keys are kept.",
    { title: enabled ? "Allow AI agents" : "Refuse AI agents", ok: enabled ? "Allow AI agents" : "Refuse AI agents", danger: !enabled }))) return;
  try {
    // Only this switch changes, on the settings as they are now (#601).
    await settingsWrite({ mcp: { enabled } });
    STATE.data.appSettings = null;
    await loadHealthSettings(true);
    toast(enabled ? "AI agents may connect" : "AI agents are refused", "ok");
  } catch (e) { toast(e.message, "bad"); }
  mcpCardPaint();
};

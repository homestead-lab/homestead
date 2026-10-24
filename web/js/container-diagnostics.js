/* The same diagnostics and reviewed command runner used by MCP. */
let containerDiagnostics = null;
let containerDiagnosticsSequence = 0;

function diagnosticExit(state) {
  const value = state?.terminated;
  if (value) return `${value.reason || "Exited"} · code ${value.exitCode ?? "unknown"}${value.signal ? ` · signal ${value.signal}` : ""}`;
  return state?.waiting?.reason || (state?.running ? "Running" : "No exit recorded");
}

function diagnosticLog(label, value) {
  return UI.more(label, `<pre class="mono small agent-raw">${esc(value?.error || value?.text || "No output")}</pre>${value?.truncated ? '<p class="small dim">Output was truncated.</p>' : ""}`, false);
}

/* Why the app restarted before (#740): Kubernetes keeps only the latest exit,
   so the leader writes each one down. An error says the record is unknown. */
function diagnosticRestarts(history) {
  if (!history) return "";
  const box = html => UI.field("Restarts kept", html);
  if (history.error) return box(`<p class="small">Restart history could not be read: ${esc(history.error)}</p>`);
  const records = history.records || [];
  if (!records.length) return box(`<p class="small dim">${history.since
    ? `No restarts kept since ${fmtWhen(new Date(history.since * 1000))}.`
    : "Homestead has not looked yet; restarts are kept from its first look."}</p>`);
  const day = records.filter(r => Date.now() - Date.parse(r.at) < 86400e3).reduce((n, r) => n + (r.count || 1), 0);
  const words = `${records[0].reason}${records[0].detail ? ` (${records[0].detail})` : ""}`;
  return box(`<p class="small">Restarted ${day} time${day === 1 ? "" : "s"} in 24 h · last: ${esc(words.charAt(0).toLowerCase() + words.slice(1))}</p>
    <ul class="small">${records.map(r => `<li><span class="dim">${fmtWhen(r.at)}</span> · ${esc(r.reason)}${
      r.detail ? ` (${esc(r.detail)})` : ""}${r.container ? ` · ${esc(r.container)}` : ""}${r.count > 1 ? ` · ${esc(r.count)} restarts` : ""}${
      r.log_tail ? diagnosticLog("Its last lines", { text: r.log_tail }) : r.log_error ? `<div class="dim">Its log was not kept: ${esc(r.log_error)}</div>` : ""}</li>`).join("")}</ul>`);
}

function diagnosticMarkup(data) {
  const pods = data.pods || [];
  const pod = pods.find(p => p.name === data.pod) || pods[0];
  const containers = pod?.containers || [];
  const selected = containers.find(c => c.name === data.container);
  return UI.lead(esc(data.message || "Inspect the last exit and output, including the previous run."))
    + (pods.length ? UI.field("Pod", `<select id="diagnosticPod" onchange="diagnosticSelect()">${pods.map(p => `<option value="${esc(p.name)}"${p.name === data.pod ? " selected" : ""}>${esc(p.name)}</option>`).join("")}</select>`) : "")
    + (containers.length ? UI.field("Container", `<select id="diagnosticContainer" onchange="diagnosticSelect(true)">${containers.map(c => `<option value="${esc(c.name)}"${c.name === data.container ? " selected" : ""}>${esc(c.name)}${c.kind === "init" ? " (initialization)" : ""}</option>`).join("")}</select>`) : "")
    + (selected ? UI.facts([["Now", esc(diagnosticExit(selected.state))], ["Last exit", esc(diagnosticExit(selected.last_state))],
      ["Restarts", esc(selected.restarts)], ["Host", esc(pod.node || "Not assigned")]]) : "")
    + diagnosticRestarts(data.restart_history)
    + (data.logs ? diagnosticLog("Previous run", data.logs.previous) + diagnosticLog("Current run", data.logs.current) : "")
    + (pods.length ? UI.more("Container details", `<pre class="mono small agent-raw">${esc(JSON.stringify(pods, null, 2))}</pre>`, false) : "")
    + (can("admin") && selected?.kind === "app" ? UI.more("Run a command", UI.field("Shell command", '<textarea id="diagnosticCommand" class="mono" rows="4" placeholder="uname -a"></textarea>',
      { help: "Runs through sh -c with this container’s permissions. Review the command before running it." })
      + UI.field("Timeout in seconds", '<input id="diagnosticTimeout" type="number" min="1" max="30" value="15">')
      + UI.actions(UI.button("Review command", "diagnosticReview(this)", { kind: "pri" })), false) : "")
    + UI.actions(UI.cancel("Close") + UI.button("Refresh", "diagnosticSelect(true)"));
}

window.wlDiagnostics = async (ns, name, pod = "", container = "") => {
  const sequence = ++containerDiagnosticsSequence;
  containerDiagnostics = null;
  modal(`Diagnostics · ${name}`, '<div id="containerDiagnosticBody" class="dim">Loading diagnostics…</div>');
  const q = new URLSearchParams({ ns, name, ...(pod ? { pod } : {}), ...(container ? { container } : {}) });
  try {
    const data = await api(`/api/container/diagnostics?${q}`);
    const el = $("#containerDiagnosticBody");
    if (!el || sequence !== containerDiagnosticsSequence) return;
    containerDiagnostics = data;
    el.innerHTML = diagnosticMarkup(data);
  } catch (e) {
    const el = $("#containerDiagnosticBody");
    if (el && sequence === containerDiagnosticsSequence) el.innerHTML = UI.error(e);
  }
};

window.diagnosticSelect = (keepContainer = false) => {
  const data = containerDiagnostics;
  if (data) wlDiagnostics(data.ns, data.name, $("#diagnosticPod")?.value || "",
    keepContainer ? $("#diagnosticContainer")?.value || "" : "");
};

window.diagnosticReview = async button => {
  const data = containerDiagnostics;
  const command = $("#diagnosticCommand")?.value || "";
  if (!data || !command.trim()) return toast("Enter a command first", "bad");
  button.disabled = true;
  const settings = { ns: data.ns, name: data.name, pod: data.pod, container: data.container,
    argv: ["sh", "-c", command], timeout_seconds: Number($("#diagnosticTimeout")?.value || 15) };
  try {
    const review = await api("/api/container/command/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) });
    if (!$("#containerDiagnosticBody") || containerDiagnostics !== data) return;
    containerDiagnostics.review = review;
    modal("Run container command", UI.lead("This command can change or delete data with the container’s permissions.")
      + UI.facts([["App", esc(`${settings.ns}/${settings.name}`)], ["Pod", esc(settings.pod)], ["Container", esc(settings.container)], ["Timeout", `${settings.timeout_seconds} seconds`]])
      + `<pre class="mono small agent-raw">${esc(JSON.stringify(settings.argv, null, 2))}</pre>`
      + UI.more("Execution limits", `<p class="small">${esc(review.summary.limit)} Output may contain secrets.</p>`, false)
      + UI.ack("diagnosticConfirm", "Run this exact command", { onchange: "$('#diagnosticRun').disabled=!this.checked" })
      + UI.actions(UI.cancel() + UI.button("Run command", "diagnosticRun(this)", { id: "diagnosticRun", kind: "danger", disabled: true })));
  } catch (e) { toast(e.message, "bad"); button.disabled = false; }
};

window.diagnosticRun = async button => {
  const data = containerDiagnostics, review = data?.review;
  if (!review) return;
  button.disabled = true;
  delete data.review;
  modal("Command result", '<div id="diagnosticCommandResult" class="dim">Running command…</div>');
  try {
    const result = await api("/api/container/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...review.settings, review_token: review.review_token, confirm: true }) });
    const el = $("#diagnosticCommandResult");
    if (!el || containerDiagnostics !== data) return;
    el.innerHTML = UI.lead(result.exit_code === null ? "Completion is unknown." : `Command exited with code ${result.exit_code}.`)
      + (result.error ? UI.callout("warn", "Command result", esc(result.error)) : "")
      + `<pre class="mono small agent-raw">${esc(result.stdout || "")}${result.stderr ? "\n" + esc(result.stderr) : ""}</pre>`
      + (result.truncated ? '<p class="small dim">Output was truncated.</p>' : "")
      + UI.actions(UI.cancel("Close") + UI.button("Diagnostics", `wlDiagnostics(${jsArg(data.ns)},${jsArg(data.name)})`));
  } catch (e) {
    const el = $("#diagnosticCommandResult");
    if (el && containerDiagnostics === data) el.innerHTML = UI.error(e);
  }
};

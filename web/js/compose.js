/* ---------------- Docker Compose import ----------------
   Paste a Compose file, fix it in the editor while the server reads it, then
   either open one service in the Deploy form or create them all at once.

   The server does all the reading - the same code checks the file as you type
   and again when it creates anything - so this page only shows what it says.
   The draft lives in memory for as long as the tab does. It is never written to
   browser storage: Compose files routinely carry passwords. */

const COMPOSE = { text: "", variables: "", namespace: "lab", vip_mode: "shared", report: null,
  editor: null, timer: null, asked: 0, busy: false, review: null, reviewSequence: 0 };

window.composeImport = async () => {
  const namespaces = await api("/api/namespaces").catch(() => ["lab"]);
  if (!namespaces.includes(COMPOSE.namespace)) COMPOSE.namespace = namespaces.includes("lab") ? "lab" : namespaces[0];
  modal("Import Docker Compose", `<div class="compose">
    <div class="compose-left">
      <div class="compose-editorwrap"><div class="compose-editor" id="composeEditor"><div class="empty"><span class="spin2"></span>starting the editor</div></div>
        <div class="compose-hint" id="composeHint" ${COMPOSE.text ? "hidden" : ""}>Paste a <b>docker-compose.yml</b> here.<br>
          It is checked as you type; <span class="mono">Ctrl+Enter</span> checks now.</div></div>
      <details class="compose-vars" ${COMPOSE.variables ? "open" : ""}>
        <summary>Variables <span class="dim xs">· the .env file, for \${NAME} in the file</span></summary>
        <textarea id="composeVars" rows="4" spellcheck="false" placeholder="DB_PASSWORD=change-me">${esc(COMPOSE.variables)}</textarea>
      </details>
      <div class="f2 compose-opts">
        <div class="f"><label>Namespace</label><select id="composeNs">${namespaces.map(n =>
          `<option ${n === COMPOSE.namespace ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
        <div class="f"><label>LAN address ${tip("Where published ports are reachable. The shared address is Homestead's own; with it, two services cannot publish the same port. A new address gives each service its own from the pool.")}</label>
          <select id="composeVip">${nodeAddressesOnly() ? nodeAddressOption() : `${nodeAddressChoice(COMPOSE.vip_mode !== "auto")}${nodeAddressBeside() ? "" : `<option value="shared" ${COMPOSE.vip_mode === "shared" ? "selected" : ""}>Shared Homestead address</option>`}
            <option value="auto" ${COMPOSE.vip_mode === "auto" ? "selected" : ""}>A new address per service</option>`}</select></div>
      </div>
    </div>
    <div class="compose-right" id="composeResult"><div class="empty">Paste a Compose file to check it.</div></div>
  </div>
  <div class="modalactions compose-actions">
    <span class="dim xs" id="composeState"></span>
    <button class="btn" onclick="composeClear()">Clear</button>
    <button class="btn" onclick="composeCheck(true)">Check again</button>
    <button class="btn pri" id="composeCreate" data-need="operator" disabled onclick="composeCreate()">Create workloads</button>
  </div>`, true, "compose");
  if (window.applyRole) window.applyRole();
  $("#composeVars").addEventListener("input", () => composeChanged());
  $("#composeNs").addEventListener("change", () => composeChanged(0));
  $("#composeVip").addEventListener("change", () => composeChanged(0));
  await composeMountEditor();
  if (COMPOSE.text.trim()) composeCheck();
};

async function composeMountEditor() {
  const host = $("#composeEditor");
  if (COMPOSE.editor) { COMPOSE.editor.getModel()?.dispose(); COMPOSE.editor.dispose(); COMPOSE.editor = null; }
  try {
    const monaco = await loadMonaco();
    if (!document.body.contains(host)) return;
    host.innerHTML = "";
    COMPOSE.editor = monaco.editor.create(host, {
      value: COMPOSE.text || "", language: "yaml", theme: monacoTheme(), automaticLayout: true,
      minimap: { enabled: false }, fontSize: 12.5, fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      scrollBeyondLastLine: false, tabSize: 2, insertSpaces: true, padding: { top: 10, bottom: 10 },
      renderWhitespace: "selection", glyphMargin: true, lineNumbersMinChars: 3,
    });
    COMPOSE.editor.onDidChangeModelContent(() => composeChanged());
    COMPOSE.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => composeCheck(true));
    COMPOSE.editor.focus();
  } catch (e) {
    // No Monaco (offline, stripped image): a plain box still does the job.
    host.innerHTML = `<textarea id="composeText" class="compose-fallback" spellcheck="false">${esc(COMPOSE.text)}</textarea>`;
    $("#composeText").addEventListener("input", () => composeChanged());
  }
}

function composeText() {
  if (COMPOSE.editor) return COMPOSE.editor.getValue();
  return $("#composeText")?.value || "";
}

function composeChanged(delay = 700) {
  ++COMPOSE.asked;
  ++COMPOSE.reviewSequence;
  COMPOSE.review = null;
  if ($("#composeCreate")) $("#composeCreate").disabled = true;
  COMPOSE.text = composeText();
  const hint = $("#composeHint");
  if (hint) hint.hidden = !!COMPOSE.text;
  COMPOSE.variables = $("#composeVars")?.value || "";
  COMPOSE.namespace = $("#composeNs")?.value || COMPOSE.namespace;
  COMPOSE.vip_mode = $("#composeVip")?.value || COMPOSE.vip_mode;
  clearTimeout(COMPOSE.timer);
  const state = $("#composeState");
  if (state) state.textContent = "checking soon…";
  COMPOSE.timer = setTimeout(() => composeCheck(), delay);
}

window.composeCheck = async (now = false) => {
  clearTimeout(COMPOSE.timer);
  if (!now && !COMPOSE.text.trim()) return composeShow(null);
  if (now) COMPOSE.text = composeText();
  if (!COMPOSE.text.trim()) return composeShow(null);
  const asked = ++COMPOSE.asked;
  const state = $("#composeState");
  if (state) state.innerHTML = '<span class="spin2"></span> checking';
  try {
    const report = await api("/api/compose/parse", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: COMPOSE.text, variables: COMPOSE.variables,
        namespace: COMPOSE.namespace, vip_mode: COMPOSE.vip_mode }) });
    if (asked !== COMPOSE.asked) return;      // a newer check is on its way
    composeShow(report);
  } catch (e) {
    if (asked !== COMPOSE.asked) return;
    if (state) state.textContent = "";
    $("#composeResult").innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  }
};

/* Every message, with the service it belongs to, for the editor's gutter. */
function composeMessages(report) {
  const out = [];
  const add = (list, severity, service) => (list || []).forEach(m => out.push({ ...m, severity, service }));
  add(report.errors, "error"); add(report.warnings, "warning"); add(report.notes, "info");
  (report.services || []).forEach(s => { add(s.errors, "error", s.name); add(s.warnings, "warning", s.name); add(s.notes, "info", s.name); });
  return out;
}

function composeMarkers(report) {
  const monaco = window.monaco, editor = COMPOSE.editor;
  if (!monaco || !editor || !editor.getModel()) return;
  const model = editor.getModel(), last = model.getLineCount();
  const severity = { error: monaco.MarkerSeverity.Error, warning: monaco.MarkerSeverity.Warning, info: monaco.MarkerSeverity.Info };
  monaco.editor.setModelMarkers(model, "compose", report ? composeMessages(report)
    .filter(m => m.line && m.line <= last && m.severity !== "info")
    .map(m => ({ startLineNumber: m.line, endLineNumber: m.line, startColumn: 1,
      endColumn: model.getLineMaxColumn(m.line), severity: severity[m.severity],
      message: (m.service ? `${m.service}: ` : "") + m.message })) : []);
}

/* Tabs in the indentation become the spaces the server read them as, so the
   file in the editor is the one that was checked, and stays valid YAML. */
window.composeUntab = width => {
  const editor = COMPOSE.editor, step = " ".repeat(Math.max(1, +width || 2));
  const text = editor ? editor.getValue() : COMPOSE.text;
  const fixed = String(text || "").split("\n")
    .map(line => line.replace(/^[ \t]+/, lead => lead.replace(/\t/g, step))).join("\n");
  if (fixed === text) return;
  if (editor) {
    editor.pushUndoStop();
    editor.executeEdits("untab", [{ range: editor.getModel().getFullModelRange(), text: fixed }]);
    editor.pushUndoStop();
  } else {
    COMPOSE.text = fixed;
  }
};

window.composeGoto = line => {
  const editor = COMPOSE.editor;
  if (!editor || !line) return;
  editor.revealLineInCenter(line);
  editor.setPosition({ lineNumber: line, column: 1 });
  editor.focus();
};

function composeMessageList(list, tone) {
  return (list || []).length ? `<ul class="compose-msgs ${tone}">${list.map(m => `<li>
    ${m.line ? `<button class="linkish mono" onclick="composeGoto(${+m.line})">line ${+m.line}</button>` : ""}
    <span>${esc(m.message)}</span></li>`).join("")}</ul>` : "";
}

function composeShow(report) {
  COMPOSE.report = report;
  composeMarkers(report);
  const host = $("#composeResult"), state = $("#composeState"), create = $("#composeCreate");
  if (!host) return;
  if (!report) {
    host.innerHTML = '<div class="empty">Paste a Compose file to check it.</div>';
    if (state) state.textContent = "";
    if (create) { create.disabled = true; create.textContent = "Create workloads"; }
    return;
  }
  const services = report.services || [];
  const errors = composeMessages(report).filter(m => m.severity === "error").length;
  const warnings = composeMessages(report).filter(m => m.severity === "warning").length;
  if (state) state.textContent = errors ? `${errors} to fix` : warnings ? `ready · ${warnings} to review` : "ready";
  if (create) {
    create.disabled = !report.ok;
    create.textContent = services.length ? `Create ${services.length} workload${services.length === 1 ? "" : "s"}` : "Create workloads";
  }
  const order = report.order || [];
  host.innerHTML = `
    <div class="compose-head ${report.ok ? "good" : errors ? "bad" : ""}">
      <b>${report.ok ? `${services.length} service${services.length === 1 ? "" : "s"} ready` : errors ? `${errors} problem${errors === 1 ? "" : "s"} to fix` : "Checked"}</b>
      <span>${services.length ? `Created in this order: ${order.map(esc).join(" → ")}` : "Nothing to create yet"}${report.project ? ` · project ${esc(report.project)}` : ""}</span>
    </div>
    ${composeMessageList(report.errors, "bad")}${composeMessageList(report.warnings, "warn")}
    ${report.untab ? `<div class="compose-untab">${composeMessageList(report.notes, "info")}
      <button class="btn sm" onclick="composeUntab(${+report.untab.width})">Use spaces in the editor</button></div>`
      : composeMessageList(report.notes, "info")}
    ${services.map(composeServiceCard).join("")}`;
  if (window.applyRole) window.applyRole();
}

function composeServiceCard(s) {
  const sum = s.summary || {}, ok = !(s.errors || []).length;
  const volumes = (sum.volumes || []).map(v => {
    const what = v.kind === "existing" ? `existing ${v.source}` : v.kind === "new-rwx" ? `new shared ${v.source}`
      : v.kind === "new-rwo" ? `new ${v.source}` : v.kind === "shm" ? "shared memory" : v.kind === "memory" ? "RAM disk" : "temporary";
    return `<div class="compose-vol"><span class="mono">${esc(v.path)}</span><span class="dim">←</span><span>${esc(what)}</span>
      ${v.template_source ? `<span class="dim xs mono" title="From the Compose host">(${esc(v.template_source)})</span>` : ""}</div>`;
  }).join("");
  return `<div class="compose-svc ${ok ? "" : "bad"}">
    <div class="between"><div class="compose-svc-name"><b>${esc(s.name)}</b>
      <div class="dim xs mono" title="${esc(s.image || "")}">${esc(s.image || "no image")}</div></div>
      <div class="row nowrap">
        ${s.line ? `<button class="linkish xs" onclick="composeGoto(${+s.line})">line ${+s.line}</button>` : ""}
        <button class="btn sm" ${s.config ? "" : "disabled"} onclick="composeToForm('${esc(s.name)}')" data-tip="Open this service in the Deploy form to change anything before creating it">Edit in form</button></div></div>
    <div class="compose-facts">
      <span class="tag ${sum.lan ? "info" : ""}">${sum.network === "host" ? "host network" : sum.lan ? "LAN" : sum.network === "internal" ? "cluster only" : "no ports"}</span>
      ${(sum.ports || []).map(p => `<span class="tag">${esc(p)}</span>`).join("")}
      ${sum.env ? `<span class="tag">${sum.env} variable${sum.env === 1 ? "" : "s"}</span>` : ""}
      ${(sum.hardware || []).length ? hardwareTags(sum.hardware) : ""}
    </div>
    ${sum.command ? `<div class="dim xs mono compose-cmd" title="${esc(sum.command)}">runs ${esc(sum.command)}</div>` : ""}
    ${volumes ? `<div class="compose-vols">${volumes}</div>` : ""}
    ${composeMessageList(s.errors, "bad")}${composeMessageList(s.warnings, "warn")}
    ${(s.notes || []).length ? `<details class="compose-notes"><summary>${s.notes.length} note${s.notes.length === 1 ? "" : "s"} on how it carries over</summary>${composeMessageList(s.notes, "info")}</details>` : ""}
  </div>`;
}

window.composeToForm = name => {
  const service = (COMPOSE.report?.services || []).find(s => s.name === name);
  if (!service?.config) return;
  window.__deployPrefill = JSON.parse(JSON.stringify(service.config));
  closeModal();
  go("deploy");
  toast(`${name} is in the Deploy form. The Compose file is kept until you clear it.`, "ok");
};

window.composeClear = () => {
  if (COMPOSE.text && !confirm("Clear the Compose file and variables?")) return;
  ++COMPOSE.asked;
  ++COMPOSE.reviewSequence;
  Object.assign(COMPOSE, { text: "", variables: "", report: null, review: null });
  if (COMPOSE.editor) COMPOSE.editor.setValue("");
  else if ($("#composeText")) $("#composeText").value = "";
  if ($("#composeVars")) $("#composeVars").value = "";
  composeShow(null);
};

window.composeCreate = async () => {
  if (!COMPOSE.report?.ok || COMPOSE.busy) return;
  const body = { text: composeText(), variables: $("#composeVars")?.value ?? COMPOSE.variables,
    namespace: $("#composeNs")?.value || COMPOSE.namespace, vip_mode: $("#composeVip")?.value || COMPOSE.vip_mode };
  await window.composeReview(body);
};

function composeCapacityHtml(plan) {
  return `<div class="note ${plan.blocked ? "bad" : "warn"}"><b>${plan.status === "fits" ? "A joint placement example fits" : plan.status === "unknown" ? "Complete placement could not be verified" : "Batch cannot fit the checked constraints"}</b>
    <p>${plan.pods} planned pod(s). This is not a scheduler reservation or an OOM guarantee.</p></div>
    <div class="dependency-list">${(plan.services || []).map(s => `<div class="drow"><div class="dl mono">${esc(s.name)} · ${s.replicas} pod(s)</div><div class="dv">Each pod: ${s.pod_request_gb} GiB requested / ${s.pod_memory_gb} GiB estimated · ${s.pod_cpu_request_percent}% CPU</div></div>`).join("")}</div>
    <div class="sec">Conservative RAM upper estimates</div><p class="dim small">Each host's upper estimate includes every batch pod that could individually fit there. The estimates can exceed a possible joint placement; they are not assigned memory.</p>
    <div class="dependency-list">${(plan.nodes || []).map(n => `<div class="drow"><div class="dl mono">${esc(n.name)}</div><div class="dv">${n.metrics_available ? `${n.baseline_gb} → up to ${n.upper_gb} GiB (${n.upper_percent}%)` : "Live RAM unavailable"}</div></div>`).join("")}</div>
    ${(plan.warnings || []).map(w => `<div class="note warn">${esc(w)}</div>`).join("")}
    ${(plan.reasons || []).length ? `<details><summary>Checked conflicts</summary>${plan.reasons.map(r => `<div class="small">${esc(r)}</div>`).join("")}</details>` : ""}
    ${(plan.example || []).length ? `<details><summary>Example only — Kubernetes is not pinned to these hosts</summary>${plan.example.map(p => `<div class="small mono">${esc(p.service)} → ${esc(p.host)}</div>`).join("")}</details>` : ""}`;
}

window.composeReview = async body => {
  if (COMPOSE.busy) return;
  COMPOSE.review = null;
  const sequence = ++COMPOSE.reviewSequence;
  const config = JSON.parse(JSON.stringify(body));
  try {
    const response = await api("/api/compose/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(config) });
    if (sequence !== COMPOSE.reviewSequence) return;
    if (!response.capacity || !response.capacity_token) throw new Error("Batch capacity preview unavailable; refresh before creating workloads.");
    COMPOSE.review = { config, ...response };
    childModal("Review Compose batch", `${composeCapacityHtml(response.capacity)}
      <div class="note">Each service is rechecked against the remaining batch before creation. A later failure stops the batch without deleting created workloads or volumes. Copy the Compose file somewhere safe if you need to recover it after a page refresh.</div>
      ${!response.capacity.blocked ? `<label class="switch"><input type="checkbox" id="composeCapacityConfirm"> I understand the batch capacity, storage and partial-creation warnings</label>` : ""}
      <div id="composeApplyResult"></div><div class="modalactions"><button class="btn" onclick="modalBack()">Back</button><button class="btn pri" id="composeApply" ${response.capacity.blocked ? "disabled" : ""} onclick="composeConfirm()">Create reviewed workloads</button></div>`, true);
  } catch (e) { toast(e.message, "bad"); }
};

window.composeConfirm = async () => {
  const review = COMPOSE.review;
  if (!review || review.capacity.blocked || COMPOSE.busy || !$("#composeCapacityConfirm")?.checked)
    return toast("Review the batch and acknowledge its warnings first", "bad");
  COMPOSE.busy = true;
  const button = $("#composeApply");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spin2"></span> creating'; }
  try {
    const result = await api("/api/compose/apply", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...review.config, capacity_token: review.capacity_token, confirm_capacity: true }) });
    if (!result.ok) {
      toast(`${result.failed} could not be created: ${result.error}`, "bad");
      $("#composeApplyResult").innerHTML = `<div class="note bad"><b>Batch stopped at ${esc(result.failed)}.</b>
        ${esc(result.error)}${result.created.length ? `<br>Already created: ${result.created.map(esc).join(", ")}. Keep them; remove their definitions and satisfied depends_on references from the next batch, or deploy the remaining services individually.` : ""}<br>Volumes created before an error are retained; inspect them before retrying.</div>`;
      return;
    }
    Object.assign(COMPOSE, { text: "", variables: "", report: null });
    closeModal();
    toast(`Created ${result.created.join(", ")}`, "ok");
    go("workloads");
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    COMPOSE.busy = false;
    COMPOSE.review = null;
    const again = $("#composeApply");
    if (again) { again.disabled = true; again.textContent = "Return to the editor and review again"; }
  }
};

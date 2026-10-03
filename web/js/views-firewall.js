/* Workload policies: edit -> review the live scope -> apply. */
let FIREWALL_EDITOR = null;
let FIREWALL_REVIEW = null;
const firewallPost = (path, body) => api(`/api/firewall/${path}`, { method: "POST",
  headers: { "Content-Type": "application/json", "X-Homestead-Auth": "1" }, body: JSON.stringify(body) });
const firewallTargetKey = row => `${row.namespace}/${row.kind}/${row.name}`;
const firewallDirection = (spec, direction) => {
  if (!(spec.policyTypes || ["Ingress", ...(spec.egress ? ["Egress"] : [])]).includes(direction)) return "Unchanged";
  const count = (spec[direction.toLowerCase()] || []).length;
  return `${count} allow rule${count === 1 ? "" : "s"}`;
};

async function viewFirewall() {
  const data = await api("/api/firewall");
  STATE.data.firewall = data;
  const q = (STATE.q || "").toLowerCase();
  const rows = data.policies.filter(row => `${row.namespace} ${row.name} ${row.config?.target?.name || ""}`.toLowerCase().includes(q));
  paint(`${UI.pageHeader("Networking", "Firewall policies for workload pod-network traffic",
    UI.button("＋ New policy", "firewallEdit()", { kind: "pri", attrs: 'data-need="admin"' }))}
    ${networkTabs("firewall")}
    ${summaryLine("firewall", [`<b>${data.policies.length}</b> ${data.policies.length === 1 ? "policy" : "policies"}`, `${esc(data.provider.name)} · enforcement unverified`])}
    ${UI.guide("How workload firewall policies work", `<p>${esc(data.provider.detail)}</p>
      <p>A policy selects a workload's pods. Restrict inbound or outbound traffic, then add the connections it needs.
      An empty restricted direction allows no connections in that direction through this policy. Reply traffic is handled by the network provider.</p>
      <p>All matching policies add their allowances together. There is no rule priority or explicit deny that overrides another policy.
      Removing the last policy for a direction restores Kubernetes' default allow behaviour.</p>
      <p>Host traffic, host-network workloads, and VM LAN/Multus interfaces need a separate firewall. Service address translation can change the source IP seen by a policy.
      Test allowed and blocked connections after applying. These policies are Kubernetes resources; include them in your cluster backups.</p>`)}
    ${UI.table([{label:"Policy"}, {label:"Workload"}, {label:"Inbound / outbound"}, {label:"Actions"}], rows.map(row => [
      `<b>${esc(row.name)}</b><div class="dim small">${esc(row.namespace)} · ${row.managed ? "Homestead" : "Managed elsewhere / inspect only"}</div>`,
      row.config ? `${esc(row.config.target.name)}<div class="dim small">${esc(row.config.target.kind)}</div>` : "Custom selector",
      `${esc(firewallDirection(row.spec, "Ingress"))}<div class="dim small">${esc(firewallDirection(row.spec, "Egress"))}</div>`,
      actionBar([{label:"Inspect", run:`firewallInspect(${jsq(row.namespace)},${jsq(row.name)})`},
        row.managed && {label:"Edit", run:`firewallEdit(${jsq(row.namespace)},${jsq(row.name)})`, need:"admin"},
        row.managed && {label:"Remove", run:`firewallRemove(${jsq(row.namespace)},${jsq(row.name)})`, need:"admin", danger:true}])
    ]), {empty:"No policies yet. Create a policy to control a workload's inbound or outbound connections."})}`);
}
window.viewFirewall = viewFirewall;

window.firewallInspect = (ns, name) => {
  const row = STATE.data.firewall?.policies.find(p => p.namespace === ns && p.name === name);
  if (!row) return;
  modal("Firewall policy", UI.lead(`${esc(ns)}/${esc(name)}`) +
    UI.more("Policy specification", `<pre>${esc(JSON.stringify(row.spec, null, 2))}</pre>`, true) +
    UI.actions(UI.cancel("Close")));
};

function firewallRuleHtml(row = {}) {
  return `<div class="fw-rule ui-section">${UI.fields(
    UI.field("Allowed peer", `<select class="fw-peer" onchange="firewallPeerChanged(this)">${[["any","Anywhere"],["cidr","IP address / range"],["namespace","Pods in a namespace"]].map(([v,l]) => `<option value="${v}" ${row.peer === v ? "selected" : ""}>${l}</option>`).join("")}</select>`),
    UI.field("Address or namespace", `<input class="fw-value" value="${esc(row.value || "")}" placeholder="192.168.1.0/24 or apps" ${!row.peer || row.peer === "any" ? "disabled" : ""}>`),
    UI.field("Protocol", `<select class="fw-protocol" onchange="firewallProtocolChanged(this)">${["TCP","UDP","SCTP","Any"].map(v => `<option ${row.protocol === v ? "selected" : ""}>${v}</option>`).join("")}</select>`),
    UI.field("Destination ports", `<input class="fw-ports" value="${esc(row.ports || "")}" placeholder="All ports, or 80, 443" ${row.protocol === "Any" ? "disabled" : ""}>`, {help:"Comma-separated numbers. Empty means all ports for this protocol."})
  )}${UI.button("Remove rule", "this.closest('.fw-rule').remove();firewallInvalidate()", {attrs:'type="button"'})}</div>`;
}
window.firewallPeerChanged = el => {
  const field = el.closest(".fw-rule").querySelector(".fw-value");
  field.disabled = el.value === "any";
  if (field.disabled) field.value = "";
  firewallInvalidate();
};
window.firewallProtocolChanged = el => {
  const field = el.closest(".fw-rule").querySelector(".fw-ports");
  field.disabled = el.value === "Any";
  if (field.disabled) field.value = "";
  firewallInvalidate();
};

function firewallDirectionHtml(direction, config) {
  const restricted = config[direction] === "restricted";
  return UI.field("Traffic", `<select id="fw_${direction}" onchange="firewallModeChanged()">
    <option value="unchanged" ${!restricted ? "selected" : ""}>Unrestricted by this policy</option>
    <option value="restricted" ${restricted ? "selected" : ""}>Allow listed traffic only</option></select>`,
    {help:"Other matching policies still apply. With no allow rules, a restricted direction permits no connections through this policy."}) +
    `<div id="fw_${direction}_body" ${!restricted ? "hidden" : ""}>
      <div id="fw_${direction}_rules">${(config[direction + "_rules"] || []).map(firewallRuleHtml).join("")}</div>
      ${UI.button("＋ Allow connection", `firewallAddRule('${direction}')`, {attrs:'type="button"'})}
      ${direction === "egress" ? `<label class="ui-ack"><input type="checkbox" id="fw_dns" ${config.allow_dns ? "checked" : ""}>Allow cluster DNS (CoreDNS / kube-dns)</label>` : ""}</div>`;
}
window.firewallAddRule = direction => {
  document.getElementById(`fw_${direction}_rules`).insertAdjacentHTML("beforeend", firewallRuleHtml());
  firewallInvalidate();
};
window.firewallModeChanged = () => {
  for (const direction of ["ingress", "egress"]) document.getElementById(`fw_${direction}_body`).hidden = document.getElementById(`fw_${direction}`).value !== "restricted";
  firewallInvalidate();
};
window.firewallInvalidate = () => {
  FIREWALL_REVIEW = null;
  const review = document.getElementById("fw_review");
  if (review) review.innerHTML = "";
  const apply = document.getElementById("fw_apply");
  if (apply) apply.disabled = true;
};

window.firewallEdit = async (ns, name) => {
  try {
    const data = await api("/api/firewall");
    STATE.data.firewall = data;
    const existing = name ? data.policies.find(p => p.namespace === ns && p.name === name) : null;
    if (name && !existing?.config) throw new Error("This policy cannot be edited here; refresh and inspect it.");
    const available = data.targets.filter(row => !row.blocked);
    if (!available.length) throw new Error("No supported workloads found. Host-network, platform, and LAN-only VM traffic needs a separate firewall.");
    const config = existing?.config || {target:available[0], name:`homestead-fw-${available[0].name}`.slice(0,63).replace(/[-.]$/, ""),
      ingress:"restricted", egress:"unchanged", ingress_rules:[], egress_rules:[], allow_dns:false};
    if (!available.some(row => firewallTargetKey(row) === firewallTargetKey(config.target) && row.uid === config.target.uid))
      throw new Error("The original workload is missing, replaced, or unsupported. Inspect or remove this policy instead.");
    FIREWALL_EDITOR = {existing, targets:available}; FIREWALL_REVIEW = null;
    modal(existing ? "Edit firewall policy" : "New firewall policy", `<div id="fw_editor">${UI.lead("Choose a workload, allow its required connections, then review the affected pods.")}
      ${UI.section("Workload", UI.fields(
        UI.field("Workload", `<select id="fw_target" ${existing ? "disabled" : ""} onchange="firewallTargetChanged()">${available.map((row,i) => `<option value="${i}" ${firewallTargetKey(row) === firewallTargetKey(config.target) ? "selected" : ""}>${esc(row.namespace)} / ${esc(row.name)} · ${esc(row.kind)}</option>`).join("")}</select>`),
        UI.field("Policy name", `<input id="fw_name" value="${esc(config.name)}" ${existing ? "disabled" : ""}>`)
      ) + UI.field("Start from", `<select id="fw_preset" onchange="firewallPreset(this.value)"><option value="">Custom rules</option><option value="inbound">Block inbound</option><option value="web">Web server · TCP 80 and 443</option><option value="namespace">Allow this namespace</option><option value="isolate">Isolate · keep cluster DNS</option></select>`) + `<div id="fw_scope" class="dim small"></div>`)}
      ${UI.section("Inbound connections", firewallDirectionHtml("ingress", config))}
      ${UI.section("Outbound connections", firewallDirectionHtml("egress", config))}
      <div id="fw_review"></div>
      ${UI.actions(UI.cancel() + UI.button("Review policy", "firewallReview()", {attrs:'id="fw_review_button"'}) +
        UI.button("Apply policy", "firewallSave()", {kind:"pri", attrs:'id="fw_apply" disabled data-need="admin"'}))}</div>`, true);
    document.getElementById("fw_editor").addEventListener("input", firewallInvalidate);
    document.getElementById("fw_editor").addEventListener("change", firewallInvalidate);
    firewallScope();
  } catch (error) { toast(error.message, "bad"); }
};
function firewallScope() {
  const target = FIREWALL_EDITOR.targets[+document.getElementById("fw_target").value];
  document.getElementById("fw_scope").textContent = ["Pod-network traffic only.", ...target.warnings].join(" ");
}
window.firewallTargetChanged = () => {
  const target = FIREWALL_EDITOR.targets[+document.getElementById("fw_target").value];
  document.getElementById("fw_name").value = `homestead-fw-${target.name}`.slice(0,63).replace(/[-.]$/, "");
  firewallScope(); firewallInvalidate();
};
window.firewallPreset = preset => {
  if (!preset) return;
  const target = FIREWALL_EDITOR.targets[+document.getElementById("fw_target").value];
  const inbound = preset === "web" ? [{peer:"any", protocol:"TCP", ports:"80, 443"}] :
    preset === "namespace" ? [{peer:"namespace", value:target.namespace, protocol:"Any"}] : [];
  document.getElementById("fw_ingress").value = "restricted";
  document.getElementById("fw_egress").value = preset === "isolate" ? "restricted" : "unchanged";
  document.getElementById("fw_dns").checked = preset === "isolate";
  document.getElementById("fw_ingress_rules").innerHTML = inbound.map(firewallRuleHtml).join("");
  document.getElementById("fw_egress_rules").innerHTML = "";
  firewallModeChanged();
};
function firewallConfig() {
  const target = FIREWALL_EDITOR.targets[+document.getElementById("fw_target").value];
  const config = {name:document.getElementById("fw_name").value.trim(), namespace:target.namespace,
    target:Object.fromEntries(["namespace","name","kind","uid"].map(key => [key,target[key]]))};
  for (const direction of ["ingress","egress"]) {
    config[direction] = document.getElementById(`fw_${direction}`).value;
    config[direction + "_rules"] = config[direction] === "restricted" ? Array.from(document.querySelectorAll(`#fw_${direction}_rules .fw-rule`)).map(row =>
      Object.fromEntries(["peer","value","protocol","ports"].map(key => [key, row.querySelector(`.fw-${key}`).value.trim()]))) : [];
  }
  config.allow_dns = config.egress === "restricted" && document.getElementById("fw_dns").checked;
  if (FIREWALL_EDITOR.existing) Object.assign(config, {uid:FIREWALL_EDITOR.existing.uid, resource_version:FIREWALL_EDITOR.existing.resource_version});
  return config;
}
window.firewallReview = async () => {
  firewallInvalidate();
  const config = firewallConfig(), editor = document.getElementById("fw_editor");
  try {
    const plan = await firewallPost("preview", config);
    if (document.getElementById("fw_editor") !== editor || JSON.stringify(config) !== JSON.stringify(firewallConfig())) return;
    FIREWALL_REVIEW = {...config, review:plan.review};
    document.getElementById("fw_review").innerHTML = UI.section("Review", UI.facts([
      ["Selected pods", esc(plan.pods.join(", ") || "None running")],
      ["Other matching policies", esc(plan.overlapping.join(", ") || "None detected")]
    ]) + UI.callout("warn", "Check connectivity after applying", `<ul>${plan.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`) +
      UI.more("Kubernetes policy", `<pre>${esc(JSON.stringify(plan.manifest, null, 2))}</pre>`));
    document.getElementById("fw_apply").disabled = false;
    document.getElementById("fw_review").scrollIntoView({block:"nearest"});
  } catch (error) { toast(error.message, "bad"); }
};
window.firewallSave = async () => {
  if (!FIREWALL_REVIEW) return toast("Review the current policy before applying", "bad");
  const config = FIREWALL_REVIEW, editor = document.getElementById("fw_editor"), navigation = window.NAV_TOKEN;
  firewallInvalidate();
  try {
    const result = await firewallPost("save", config);
    await firewallMutationFinished(result, editor, navigation);
  } catch (error) { toast(error.message, "bad"); }
};
window.firewallRemove = (ns, name) => {
  const row = STATE.data.firewall?.policies.find(p => p.namespace === ns && p.name === name);
  if (!row?.managed) return;
  FIREWALL_EDITOR = {removing:row};
  modal("Remove firewall policy", '<div id="fw_delete">' + UI.lead(`Remove <b>${esc(ns)}/${esc(name)}</b>?`) +
    UI.callout("warn", "Connectivity will change", "Remaining policies still apply. Removing the last policy for a direction allows all traffic in that direction; removing an allow policy can also block connections allowed only by it.") +
    UI.actions(UI.cancel() + UI.button("Remove policy", "firewallDeleteConfirmed()", {kind:"danger", attrs:'data-need="admin"'})) + '</div>');
};
window.firewallDeleteConfirmed = async () => {
  const row = FIREWALL_EDITOR?.removing, editor = document.getElementById("fw_delete"), navigation = window.NAV_TOKEN;
  if (!row) return;
  FIREWALL_EDITOR = null;
  try {
    const result = await firewallPost("delete", {namespace:row.namespace, name:row.name, uid:row.uid, resource_version:row.resource_version});
    await firewallMutationFinished(result, editor, navigation);
  } catch (error) { toast(error.message, "bad"); }
};

async function firewallMutationFinished(result, editor, navigation) {
  toast(result.message, "ok");
  if (editor?.isConnected && navigation === window.NAV_TOKEN) closeModal();
  if (navigation === window.NAV_TOKEN && STATE.view === "network" && networkTab() === "firewall") await viewFirewall();
}

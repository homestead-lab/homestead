/* A host's network ports, on its node page and across hosts on Networking.

   The node probe reads each port from the host's own /sys: link, negotiated
   speed and duplex, MTU, driver, error counters and bond state. The leader
   keeps an hour of samples, so errors and link changes are counts over that
   hour (homestead_ports.py). Nothing here changes a host; a port matters by
   what it carries - the host's address, a LAN network, a bond. */

const PORT_LINK = { up: ["ok", "up"], down: ["bad", "no link"], off: ["", "off"] };

function portSpeed(mbps) {
  if (!mbps) return "";
  return mbps >= 1000 ? `${+(mbps / 1000).toFixed(1)} Gb/s` : `${mbps} Mb/s`;
}

function portCount(value, port) {
  if (value == null) return `<span class="dim xs" data-tip="Counted over an hour; known after the first quarter of it">—</span>`;
  return `<span class="mono port-count${value ? " warn" : ""}">${value}</span>`;
}

function portCarries(port) {
  const parts = [];
  if (port.bond_member) parts.push(UI.chip(`${port.master} · ${port.bond_member.state || "member"}`, "info"));
  else if (port.master) parts.push(UI.chip(port.master, ""));
  if (port.kind === "bond") parts.push(UI.chip(port.bond?.mode || "bond", "info"));
  for (const what of port.carries || []) if (!port.master) parts.push(UI.chip(what === "host address" ? "this host's address" : what, what === "host address" ? "ok" : ""));
  return parts.join(" ") || `<span class="dim xs">${port.link === "up" ? "nothing" : "spare"}</span>`;
}

function portRows(host) {
  const flagged = new Set((host.conditions || []).map(c => c.iface));
  return (host.ports || []).filter(p => p.kind === "nic" || p.kind === "bond").map(p => {
    const [tone, word] = PORT_LINK[p.link] || ["", p.link];
    const speed = p.link === "up" ? [portSpeed(p.speed_mbps), p.duplex && p.duplex !== "full" ? `${p.duplex} duplex` : ""].filter(Boolean).join(" · ") : "";
    return [
      `<b class="mono">${esc(p.name)}</b><div class="dim xs">${esc([p.driver, p.mtu && `MTU ${p.mtu}`].filter(Boolean).join(" · "))}</div>`,
      UI.chip(word, flagged.has(p.name) && tone === "ok" ? "warn" : tone),
      `${esc(speed || "—")}${p.was_mbps ? ` ${UI.chip(`was ${portSpeed(p.was_mbps)}`, "warn")}` : ""}`,
      portCount(p.errors, p), portCount(p.flaps, p),
      portCarries(p),
    ];
  });
}

function portConditions(conditions) {
  return (conditions || []).map(c => UI.callout(c.severity === "critical" ? "bad" : c.severity === "info" ? "info" : "warn", c.title, esc(c.body))).join("");
}

function hostPortsBody(n, host) {
  if (!host.available) return `<div class="dim small">${esc(host.reason || "No port status for this host.")}</div>`;
  const duties = n.duties || {};
  const vips = [...new Set([...(duties.management_vip || []), ...(duties.vips || [])])];
  const picture = window.Diagram ? Diagram.ports(host, { address: (n.addresses || {}).InternalIP || "", vips }) : "";
  return `${portConditions(host.conditions)}
    <div class="ports-picture">${picture}</div>
    ${UI.table([{ label: "Port" }, { label: "Link" }, { label: "Speed" }, { label: "Errors", className: "num" },
      { label: "Flaps", className: "num" }, { label: "Carries" }], portRows(host),
      { empty: "The probe found no network ports on this host." })}
    <div class="dim xs" style="margin-top:6px">Errors and flaps (link lost or regained) are counted over the last hour. A slower speed than before, errors, or a bond member without link raise an alert.</div>`;
}

window.nodePortsPaint = async n => {
  const card = $("#nodePorts .ports-body");
  if (!card) return;
  try {
    const host = await api("/api/nodes/ports?node=" + encodeURIComponent(n.name));
    if ($("#nodePage")?.dataset.node !== n.name) return;
    card.innerHTML = hostPortsBody(n, host);
    const line = $("#nodePorts .csub");
    if (line && host.available) {
      const up = (host.ports || []).filter(p => p.kind === "nic" && p.link === "up").length;
      const nics = (host.ports || []).filter(p => p.kind === "nic").length;
      line.textContent = `${up} of ${nics} port${nics === 1 ? "" : "s"} with link${host.uplink ? ` · uplink ${host.uplink}` : ""}`;
    }
  } catch (e) {
    card.innerHTML = `<div class="dim small">Port status is unavailable: ${esc(e.message)}</div>`;
  }
};

window.portsHostGo = node => {
  STATE.nodeSection = "network";
  STATE.nodeSectionOpen = true;
  go("nodes", { params: { node } });
};

/* Every host's ports in one table, for the Networking page. */
function portsAcrossHostsBody(report) {
  const hosts = Object.values(report.hosts || {});
  if (!hosts.length) return `<div class="dim small">No node probe answers, so there is no port status.</div>`;
  const rows = hosts.map(h => {
    if (!h.available) return [`<a class="linkish" onclick="portsHostGo(${jsq(h.node)})">${esc(h.node)}</a>`,
      `<span class="dim xs">${esc(h.reason || "")}</span>`, "", ""];
    const nics = (h.ports || []).filter(p => p.kind === "nic");
    // What the uplink stands on: a bridge over a bond, or over one NIC.
    const ports = h.ports || [];
    let uplink = ports.find(p => p.name === h.uplink);
    if (uplink?.kind === "bridge") uplink = ports.find(p => p.master === uplink.name && p.kind === "bond") || ports.find(p => p.master === uplink.name) || uplink;
    const lights = nics.map(p => `<span class="port-led ${p.link === "up" ? ((h.conditions || []).some(c => c.iface === p.name) ? "warn" : "up") : p.link}" data-tip="${esc(`${p.name} · ${p.link === "up" ? portSpeed(p.speed_mbps) || "up" : p.link}`)}"></span>`).join("");
    const worst = (h.conditions || []).slice(0, 2).map(c => UI.chip(c.title.replace(` on ${h.node}`, ""), c.severity === "critical" ? "bad" : "warn")).join(" ");
    return [`<a class="linkish" onclick="portsHostGo(${jsq(h.node)})">${esc(h.node)}</a>`,
      `<span class="mono">${esc(h.uplink || "—")}</span>${uplink && uplink.name !== h.uplink ? ` <span class="dim xs">on</span> <span class="mono">${esc(uplink.name)}</span>` : ""}`
        + (uplink?.kind === "bond" ? ` <span class="dim xs">${esc(uplink.bond?.mode || "")}</span>` : uplink?.speed_mbps ? ` <span class="dim xs">${esc(portSpeed(uplink.speed_mbps))}</span>` : ""),
      lights, worst || `<span class="dim xs">—</span>`];
  });
  const cluster = (report.conditions || []).filter(c => !c.node);
  return `${portConditions(cluster)}${UI.table([{ label: "Host" }, { label: "Uplink" }, { label: "Ports" }, { label: "Needs attention" }], rows)}`;
}

/* The card's inside, from the last report: the page draws it from this on
   every refresh, so the card keeps its rows while the next report loads
   instead of emptying and filling again. */
const PORTS_VIEW = { report: null, error: "" };
window.portsAcrossHostsCard = () => {
  const report = PORTS_VIEW.report;
  const line = report ? `${report.counts?.hosts || 0} host${report.counts?.hosts === 1 ? "" : "s"} · ${report.counts?.ports || 0} ports`
    + ((report.conditions || []).length ? ` · ${(report.conditions || []).length} need attention` : "") : "reading ports…";
  const body = PORTS_VIEW.error ? `<div class="dim small">Port status is unavailable: ${esc(PORTS_VIEW.error)}</div>`
    : report ? portsAcrossHostsBody(report) : "";
  return `<div class="csub">${esc(line)}</div><div class="ports-body" style="margin-top:8px">${body}</div>`;
};

window.portsAcrossHostsPaint = async () => {
  try {
    PORTS_VIEW.report = await api("/api/nodes/ports");
    PORTS_VIEW.error = "";
  } catch (e) {
    PORTS_VIEW.error = e.message;
  }
  const card = $("#netPorts");
  if (!card) return;
  const next = document.createElement("div");
  next.innerHTML = portsAcrossHostsCard();
  morph(card, next);
};

/* Harvester: which NICs carry each cluster network, bonded or not
   (homestead_uplinks.py). Homestead writes a VlanConfig and Harvester builds
   the bond and bridge on each host; mgmt's uplink is Harvester's own and is
   only shown. Every change is reviewed first: what stands in its way (running
   VMs, NICs in use or without link) and the picture of what it makes. */
const UPLINKS = { inv: null };

function uplinkHostChips(config) {
  return config.nodes.map(n => {
    const s = config.status[n] || {};
    return `<span class="ui-chip ${s.ready ? "ok" : s.ready === false ? "bad" : "warn"}" data-tip="${esc(s.message || (s.ready ? "ready" : "not reported yet"))}">${esc(n)}</span>`;
  }).join(" ");
}

function uplinksBody(inv) {
  const mgmtNics = Object.entries(inv.hosts || {}).map(([host, nics]) =>
    `${esc(host)} <span class="mono">${esc(nics.filter(n => n.used_by === "mgmt").map(n => n.name).join(" + ") || "—")}</span>`).join(" · ");
  return inv.networks.map(net => {
    const head = `<div class="between uplink-head"><div><b class="mono">${esc(net.name)}</b>
        ${net.mgmt ? UI.chip("set when Harvester installed · shown only", "") : ""}
        <span class="dim xs">${net.lan_networks.length ? esc(net.lan_networks.join(", ")) : "no LAN networks on it yet"}</span></div>
      ${net.mgmt ? "" : `<button class="btn sm" data-need="admin" onclick="uplinkDialog({cn:${jsq(net.name)}})">＋ Uplink</button>`}</div>`;
    if (net.mgmt) return `<div class="uplink-net">${head}<div class="dim xs">The management NICs: ${mgmtNics || "not reported"}. Change them with Harvester's own procedure.</div></div>`;
    const rows = net.configs.map(c => [
      `<b class="mono">${esc(c.name)}</b>${c.homestead ? "" : ` <span class="dim xs">from Harvester</span>`}`,
      uplinkHostChips(c),
      `<span class="mono">${esc(c.nics.join(" + ") || "—")}</span>`,
      `${esc(c.nics.length > 1 ? c.mode : "single NIC")}${c.mtu ? ` <span class="dim xs">MTU ${esc(c.mtu)}</span>` : ""}`,
      `<span class="row" style="gap:6px"><button class="btn sm" data-need="admin" onclick="uplinkDialog({config:${jsq(c.name)}})">Change…</button>
        <button class="btn sm" data-need="admin" onclick="uplinkReview({action:'remove',config:${jsq(c.name)}})">Remove</button></span>`]);
    return `<div class="uplink-net">${head}
      ${rows.length ? UI.table([{ label: "Uplink" }, { label: "Hosts" }, { label: "NICs" }, { label: "Bond" }, { label: "" }], rows) : ""}
      ${net.uncovered.length ? `<div class="dim xs">No uplink on ${esc(net.uncovered.join(", "))}: VMs there cannot use ${esc(net.name)}'s networks.</div>` : ""}</div>`;
  }).join("");
}

window.uplinksPaint = async () => {
  const box = $("#netUplinks");
  if (!box) return;
  try {
    const inv = await api("/api/network/uplinks");
    UPLINKS.inv = inv;
    if (!inv.applies) { box.innerHTML = ""; return; }
    box.innerHTML = UI.section("Cluster network uplinks", `<div class="card flat">${uplinksBody(inv)}</div>`,
      `<button class="btn sm" data-need="admin" onclick="uplinkDialog({fresh:true})">＋ New cluster network</button>`);
    if (window.applyRole) applyRole();
  } catch (e) {
    box.innerHTML = `<div class="dim small">Uplinks are unavailable: ${esc(e.message)}</div>`;
  }
};

/* The form: which network, which hosts, which NICs and how to bond them. */
window.uplinkDialog = async (opts = {}) => {
  const inv = UPLINKS.inv || (UPLINKS.inv = await api("/api/network/uplinks"));
  const config = opts.config ? inv.networks.flatMap(n => n.configs).find(c => c.name === opts.config) : null;
  const choices = inv.networks.filter(n => !n.mgmt);
  const form = UPLINKS.form = { config, fresh: !config && (!!opts.fresh || !choices.length),
    cn: config?.cluster_network || opts.cn || choices[0]?.name || "" };
  const net = choices.find(n => n.name === form.cn);
  const hosts = config ? config.nodes : form.fresh ? Object.keys(inv.hosts) : (net?.uncovered || []);
  const where = form.fresh
    ? UI.field("Cluster network name", `<input id="ul_cn" class="mono" maxlength="12" placeholder="data" autocomplete="off">`,
        { help: "Up to 12 lower-case letters, digits or hyphens. LAN networks are made on it afterwards." })
    : config ? "" : UI.field("Cluster network", `<select id="ul_cn" onchange="uplinkDialog({cn:this.value})">${choices.map(n =>
        `<option ${n.name === form.cn ? "selected" : ""}>${esc(n.name)}</option>`).join("")}</select>`);
  const hostField = config ? UI.field("Hosts", uplinkHostChips(config), { wide: true }) : UI.field("Hosts", hosts.length
    ? `<div class="uplink-checks">${hosts.map(h => `<label><input type="checkbox" name="ul_host" value="${esc(h)}" checked onchange="uplinkNicsPaint()"> ${esc(h)}</label>`).join("")}</div>`
    : `<span class="dim small">Every host already has an uplink for ${esc(form.cn)}; change one instead.</span>`, { wide: true });
  modal(config ? `Change ${config.name}` : form.fresh ? "New cluster network" : `Uplink for ${form.cn}`, `<div class="ui-stack">
    ${UI.lead(config ? `Harvester rebuilds ${config.cluster_network}'s uplink on ${config.nodes.join(", ")} with what you choose.`
      : "Choose the NICs that carry this network on each host. Two or more are bonded: if one fails, traffic carries on through the rest.")}
    ${UI.fields(where, hostField,
      UI.field("NICs", `<div id="ul_nics"></div>`, { wide: true, help: "The chosen hosts' NICs, with each host's link. NICs that carry mgmt or another network cannot be chosen." }),
      UI.field("Bond mode", `<select id="ul_mode" onchange="uplinkModeHelp()">${(inv.modes || ["active-backup"]).map(m =>
        `<option ${m === (config?.mode || "active-backup") ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>`,
        { help: "active-backup works on any switch: one NIC carries traffic and the next takes over if it fails." }),
      UI.field("MTU", `<input id="ul_mtu" type="number" min="576" max="9000" placeholder="1500" value="${esc(config?.mtu || "")}">`,
        { help: "Leave empty for 1500. Jumbo frames (9000) need every switch port to allow them." }))}
    <div id="ul_lacp" hidden>${UI.ack("ul_lacp_ok", "The switch ports these NICs plug into are one LACP group")}</div>
    ${UI.ack("ul_down", "Use a NIC even if it has no link yet")}
    ${UI.actions(UI.cancel() + UI.button("Review", "uplinkReview()", { kind: "pri", attrs: 'data-need="admin"' }))}</div>`);
  uplinkNicsPaint();
  uplinkModeHelp();
  if (window.applyRole) applyRole();
};

function uplinkChosenHosts() {
  const form = UPLINKS.form || {};
  if (form.config) return form.config.nodes;
  return [...document.querySelectorAll('input[name="ul_host"]:checked')].map(i => i.value);
}

window.uplinkNicsPaint = () => {
  const box = $("#ul_nics");
  if (!box) return;
  const form = UPLINKS.form, hosts = uplinkChosenHosts(), inv = UPLINKS.inv;
  const names = [...new Set(hosts.flatMap(h => (inv.hosts[h] || []).map(n => n.name)))].sort();
  const chosen = new Set(form.config?.nics || []);
  box.innerHTML = names.map(name => {
    const per = hosts.map(h => [h, (inv.hosts[h] || []).find(n => n.name === name)]);
    const taken = per.find(([, n]) => n?.used_by && n.used_by !== form.cn);
    const lights = per.map(([h, n]) => `<span class="port-led ${!n ? "off" : n.link === "up" ? "up" : n.link}" data-tip="${esc(`${h}: ${!n ? "no such NIC" : n.link === "up" ? portSpeed(n.speed_mbps) || "link" : n.link}`)}"></span>`).join("");
    return `<label class="uplink-nic${taken ? " taken" : ""}"><input type="checkbox" name="ul_nic" value="${esc(name)}" ${chosen.has(name) ? "checked" : ""} ${taken ? "disabled" : ""}>
      <b class="mono">${esc(name)}</b> ${lights} ${taken ? `<span class="dim xs">carries ${esc(taken[1].used_by)} on ${esc(taken[0])}</span>` : ""}</label>`;
  }).join("") || `<span class="dim small">The node probe has not reported these hosts' NICs.</span>`;
};

window.uplinkModeHelp = () => { const lacp = $("#ul_lacp"); if (lacp) lacp.hidden = $("#ul_mode")?.value !== "802.3ad"; };

function uplinkRequest(opts) {
  if (opts?.action === "remove") return { action: "remove", config: opts.config };
  const form = UPLINKS.form;
  const req = { action: form.config ? "change" : "create", nics: [...document.querySelectorAll('input[name="ul_nic"]:checked')].map(i => i.value),
    mode: $("#ul_mode").value, mtu: $("#ul_mtu").value || "", allow_down: !!$("#ul_down")?.checked, lacp_confirmed: !!$("#ul_lacp_ok")?.checked };
  if (form.config) req.config = form.config.name;
  else Object.assign(req, { cluster_network: form.fresh ? ($("#ul_cn").value || "").trim() : form.cn, new_network: form.fresh, nodes: uplinkChosenHosts() });
  return req;
}

/* What the change makes on the first host, drawn as the Ports picture is. */
function uplinkPicture(plan) {
  if (!window.Diagram || plan.action === "remove" || !plan.nics?.length) return "";
  const host = plan.nodes[0], cn = plan.cluster_network, bonded = plan.nics.length > 1;
  const nics = UPLINKS.inv?.hosts?.[host] || [];
  const carries = plan.lan_networks?.length ? plan.lan_networks : [`${cn}'s LAN networks`];
  const ports = plan.nics.map((name, i) => {
    const n = nics.find(x => x.name === name) || {};
    return { name, kind: "nic", link: n.link || "up", speed_mbps: n.speed_mbps, master: bonded ? `${cn}-bo` : `${cn}-br`, carries,
      bond_member: bonded ? { state: plan.mode === "active-backup" && i ? "backup" : "active" } : null };
  });
  if (bonded) ports.push({ name: `${cn}-bo`, kind: "bond", link: "up", master: `${cn}-br`, carries, bond: { mode: plan.mode } });
  ports.push({ name: `${cn}-br`, kind: "bridge", link: "up", carries });
  return `<div class="ports-picture">${Diagram.ports({ ports, conditions: [] })}</div>
    <div class="dim xs">On ${esc(host)}${plan.nodes.length > 1 ? `, and the same on ${esc(plan.nodes.slice(1).join(", "))}` : ""}: Harvester builds ${bonded ? `${esc(cn)}-bo and ` : ""}${esc(cn)}-br.</div>`;
}

window.uplinkBack = () => {
  const form = UPLINKS.form || {};
  uplinkDialog(form.config ? { config: form.config.name } : { cn: form.cn, fresh: form.fresh });
};

window.uplinkReview = async opts => {
  const req = uplinkRequest(opts);
  const back = req.action === "remove" ? "" : UI.button("Back", "uplinkBack()");
  modal("Review the uplink", '<div class="empty"><span class="spin2"></span> Checking the hosts…</div>');
  let plan;
  try {
    plan = await api("/api/network/uplinks/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req) });
  } catch (e) { $("#mbody").innerHTML = UI.callout("bad", "It could not be checked", esc(e.message)) + UI.actions(UI.cancel("Close") + back); return; }
  UPLINKS.pending = { ...req, digest: plan.digest };
  const verb = { create: "Make it", change: "Change it", remove: "Remove it" }[plan.action];
  const what = plan.action === "remove" ? `${plan.config} is removed: ${plan.cluster_network} has no uplink on ${plan.nodes.join(", ")} afterwards.`
    : `${plan.nics.join(" + ")}${plan.nics.length > 1 ? ` bonded as ${plan.mode}` : ""} carry ${plan.cluster_network}${plan.new_network ? " (a new cluster network)" : ""} on ${plan.nodes.join(", ")}${plan.mtu ? `, MTU ${plan.mtu}` : ""}.`;
  $("#mbody").innerHTML = `<div class="ui-stack">${UI.lead(what)}
    ${uplinkPicture(plan)}
    ${UI.checklist([...plan.refusals.map(r => ({ state: "bad", title: r })), ...plan.warnings.map(w => ({ state: "warn", title: w })),
      !plan.refusals.length && { state: "ok", title: "Harvester makes the change and reports each host; Homestead follows it as a job" }])}
    ${plan.refusals.length ? "" : UI.callout("warn", `${plan.cluster_network}'s networks pause on ${plan.nodes.join(", ")}`,
      "For a few seconds while Harvester rebuilds the bond and bridge. The hosts' own addresses are on mgmt and are not touched.")}
    ${UI.actions(UI.cancel() + back + (plan.refusals.length ? "" : UI.button(verb, "uplinkApply()", { kind: "danger", id: "ul_go", attrs: 'data-need="admin"' })))}</div>`;
  if (window.applyRole) applyRole();
};

window.uplinkApply = async () => {
  const go = $("#ul_go");
  if (go) { go.disabled = true; go.textContent = "Sending…"; }
  try {
    const r = await api("/api/network/uplinks/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(UPLINKS.pending) });
    toast(r.detail, "ok"); closeModal(); UPLINKS.inv = null;
    uplinksPaint();
  } catch (e) {
    toast(e.message, "bad");
    if (go) { go.disabled = false; go.textContent = "Try again"; }
  }
};

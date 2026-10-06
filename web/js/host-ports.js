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

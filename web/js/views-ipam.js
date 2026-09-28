/* IP addresses: the Networking page's second tab.

   Per subnet: every address something is known about - documented by hand,
   used by the cluster, found by a scan, or known to UniFi - with what kind of
   assignment it is and anything that clashes with the DHCP range. */

const IPAM_KIND_LABELS = { static: "Static", reservation: "DHCP reservation", dhcp: "DHCP", reserved: "Held",
  infrastructure: "Network gear" };
/* What the thing is, with the glyph the Portal uses for it. */
const IPAM_CATEGORIES = { router: ["Router / firewall", "router"], switch: ["Switch", "switch"],
  "access-point": ["Access point", "wifi"], server: ["Server", "server"], nas: ["NAS", "nas"], iot: ["IoT", "iot"],
  cctv: ["CCTV", "camera"], printer: ["Printer", "printer"], computer: ["Computer", "pc"], phone: ["Phone / tablet", "phone"],
  media: ["TV / media", "tv"], other: ["Other", "globe"] };
const ipamCategoryIcon = category => IPAM_CATEGORIES[category]
  ? `<span class="ipam-cat" data-tip="${esc(IPAM_CATEGORIES[category][0])}"><svg><use href="#d-${IPAM_CATEGORIES[category][1]}"/></svg></span>` : "";
const ipamCategoryOptions = (selected, blank = "not set") => `<option value="">${esc(blank)}</option>` +
  Object.entries(IPAM_CATEGORIES).map(([k, [label]]) => `<option value="${k}" ${selected === k ? "selected" : ""}>${esc(label)}</option>`).join("");

function networkTab(pick) {
  if (pick) {
    try { localStorage.setItem("homestead.network.tab", pick); } catch (e) { /* this visit only */ }
    resetPaint(); refresh(true);
    return pick;
  }
  try { return localStorage.getItem("homestead.network.tab") === "ip" ? "ip" : "services"; } catch (e) { return "services"; }
}
window.networkTab = networkTab;

function networkTabs(active) {
  return `<div class="seg network-tabs" role="tablist">
    <button class="${active === "services" ? "on" : ""}" onclick="networkTab('services')">Services &amp; VIPs</button>
    <button class="${active === "ip" ? "on" : ""}" onclick="networkTab('ip')">IP addresses</button></div>`;
}
window.networkTabs = networkTabs;

function ipamSubnetPick(subnets) {
  let saved = "";
  try { saved = localStorage.getItem("homestead.ipam.subnet") || ""; } catch (e) { /* default */ }
  return subnets.find(s => s.id === saved) || subnets[0];
}
window.ipamPickSubnet = id => { try { localStorage.setItem("homestead.ipam.subnet", id); } catch (e) { /* this visit only */ } ipamPicks().clear(); renderIpam(); };
window.ipamFilter = value => { STATE.ipamFilter = value; ipamPicks().clear(); renderIpam(); };

/* The addresses ticked for a bulk change. Kept here rather than read off the
   checkboxes: the page refreshes itself every few seconds, and a redraw used
   to untick them all and hide the bar mid-choice. */
const ipamPicks = () => (STATE.ipamPicked = STATE.ipamPicked || new Set());

async function viewIpam() {
  const data = await api("/api/ipam");
  // Someone part-way through a bulk change - typing a tag, choosing a
  // category - keeps the page as it is; the next refresh catches up.
  if (document.activeElement?.closest?.("#ipamBulk")) return;
  STATE.data.ipam = data;
  const known = new Set(data.subnets.flatMap(s => s.rows.map(r => r.ip)));
  [...ipamPicks()].forEach(ip => { if (!known.has(ip)) ipamPicks().delete(ip); });
  // What was chosen in the bar survives the redraw with the ticks.
  const kept = Object.fromEntries(["ipamBulkCategory", "ipamBulkKind", "ipamBulkTag", "ipamBulkOwner"]
    .map(id => [id, document.getElementById(id)?.value || ""]));
  renderIpam();
  Object.entries(kept).forEach(([id, value]) => { const el = document.getElementById(id); if (el && value) el.value = value; });
}
window.viewIpam = viewIpam;

function ipamSeen(row) {
  const bits = [];
  if (row.scan && row.scan.up) bits.push(`<span class="pill slim ok" data-tip="Answered the last scan${row.scan.ports?.length ? ` on ${row.scan.ports.join(", ")}` : ""}">up</span>`);
  if (row.unifi?.reserved) bits.push('<span class="pill slim info" data-tip="UniFi holds this address for this device">reserved</span>');
  if (row.unifi) bits.push(`<span class="pill slim ${row.unifi.online === false ? "neutral" : "info"}" data-tip="${esc(row.unifi.type === "device" ? `UniFi device${row.unifi.model ? ` · ${row.unifi.model}` : ""}` : row.unifi.online === false ? "UniFi reservation; the client is offline" : `UniFi ${row.unifi.type || "client"}`)}">UniFi</span>`);
  return bits.join(" ") || '<span class="dim">—</span>';
}

function ipamKind(row) {
  if (row.cluster === "node") return '<span class="tag info">cluster node</span>';
  if (row.cluster === "vip") return `<span class="tag info" data-tip="${esc((row.services || []).join(", "))}">cluster VIP</span>`;
  if (row.gateway) return '<span class="tag">gateway</span>';
  if (!row.kind) return row.pool ? `<span class="tag" data-tip="Inside Harvester's ${esc(row.pool)} VIP pool">VIP pool</span>` : '<span class="dim">undocumented</span>';
  return `<span class="tag ${row.kind === "static" ? "ok" : row.kind === "reservation" ? "info" : ""}">${esc(IPAM_KIND_LABELS[row.kind] || row.kind)}</span>`;
}

/* Without UniFi connected, the page is plain IPAM: rows carry nothing of it,
   even from a sync made before it was disconnected. */
function ipamUnifiOn() { return !!((STATE.data.ipam || {}).unifi || {}).configured; }
function ipamRows(subnet) { return ipamUnifiOn() ? subnet.rows : subnet.rows.map(row => ({ ...row, unifi: null })); }

function ipamFiltered(rows) {
  const f = STATE.ipamFilter || "", q = STATE.q.toLowerCase();
  return rows.filter(row => {
    if (q && ![row.ip, row.name, row.mac, row.note, row.owner, ...(row.tags || []), row.scan?.rdns || "",
      row.unifi?.name || "", row.unifi?.hostname || ""].join(" ").toLowerCase().includes(q)) return false;
    if (!f) return true;
    if (f === "cluster") return !!row.cluster;
    if (f === "flagged") return row.flags.length > 0;
    if (f === "undocumented") return !row.kind && !row.cluster && !row.name;
    if (f.startsWith("cat:")) return row.category === f.slice(4);
    return row.kind === f;
  });
}

function renderIpam() {
  const data = STATE.data.ipam || { subnets: [], suggested: [], unifi: {} };
  const head = `<div class="phead"><div><h2>Networking</h2><p>Every address on your subnets: documented, used by the cluster${(data.unifi || {}).configured ? ", answering, or known to UniFi" : " or answering a scan"}</p></div>
    <div class="row ipam-head-acts"><button class="btn" data-need="operator" onclick="ipamSubnets()">Subnets</button>
      ${data.subnets.length ? `<button class="btn ipam-wide" data-need="operator" onclick="ipamImport()">Import CSV</button>
      <button class="btn ipam-wide" onclick="ipamExport()">Export CSV</button>
      <details class="actionmenu ipam-narrow"><summary class="btn" title="Import or export">⋯</summary><div class="actionmenu-pop">
        <button data-need="operator" onclick="this.closest('details').open=false;ipamImport()">${icon("import")}Import CSV</button>
        <button onclick="this.closest('details').open=false;ipamExport()">${icon("ext")}Export CSV</button></div></details>
      <button class="btn pri" data-need="operator" onclick="ipamEdit()">＋ Address</button>` : ""}</div></div>
    ${networkTabs("ip")}`;
  if (!data.subnets.length) {
    return paint(`${head}<div class="empty ipam-empty"><b>No subnets yet.</b> Add the LAN the cluster sits on, with its DHCP range, and Homestead fills in what the cluster uses.
      <div class="row" style="justify-content:center;margin-top:12px">${(data.suggested || []).map(cidr =>
        `<button class="btn pri" data-need="operator" onclick="ipamSubnets('${esc(cidr)}')">Add ${esc(cidr)}</button>`).join("")}
        <button class="btn" data-need="operator" onclick="ipamSubnets()">Add a subnet</button></div></div>`);
  }
  const subnet = ipamSubnetPick(data.subnets);
  const rows = ipamFiltered(ipamRows(subnet));
  const flagged = subnet.rows.filter(r => r.flags.length).length;
  const scanning = subnet.scan.state === "running";
  const u = data.unifi || {};
  paint(`${head}
    ${data.subnets.length > 1 ? `<div class="seg ipam-subnets">${data.subnets.map(s => `<button class="${s.id === subnet.id ? "on" : ""}" onclick="ipamPickSubnet('${esc(s.id)}')">${esc(s.name || s.cidr)} <span class="dim">${s.used}</span></button>`).join("")}</div>` : ""}
    <div class="grid g4 statgrid ipam-stats" style="margin:12px 0 16px">
      <div class="card flat"><div class="ctitle">${esc(subnet.name || "Subnet")}</div><div class="bignum" style="margin-top:8px">${subnet.used}<span class="unit">/${subnet.usable}</span></div>
        <div class="csub mono">${esc(subnet.cidr)}${subnet.vlan ? ` · VLAN ${subnet.vlan}` : ""}${subnet.gateway ? ` · gw ${esc(subnet.gateway)}` : ""}</div></div>
      <div class="card flat"><div class="ctitle">DHCP range</div><div class="bignum" style="margin-top:8px">${subnet.dhcp_size || "—"}</div>
        <div class="csub mono">${subnet.dhcp_start ? `${esc(subnet.dhcp_start)} – ${esc(subnet.dhcp_end)}` : "not set: add it under Subnets"}</div></div>
      <div class="card flat"><div class="ctitle">Free for static use</div><div class="bignum" style="margin-top:8px">${subnet.free_static}</div>
        <div class="csub">${subnet.next_free.length ? `next: ${subnet.next_free.slice(0, 3).map(ip => `<a class="mono" style="cursor:pointer" onclick="ipamCopy('${ip}')" title="Copy">${esc(ip)}</a>`).join(", ")}` : "none outside DHCP and the VIP pools"}</div></div>
      <div class="card flat"><div class="ctitle">Last scan</div><div class="bignum" style="margin-top:8px;font-size:20px">${scanning ? `${subnet.scan.progress}%` : subnet.scan.at ? esc(fmtAgo(Date.now() / 1000 - subnet.scan.at)) : "never"}</div>
        <div class="csub"><button class="btn sm" data-need="operator" onclick="ipamScan('${esc(subnet.id)}')" ${scanning ? "disabled" : ""}>${scanning ? "Scanning…" : "Scan now"}</button>
          ${u.configured ? `<button class="btn sm" data-need="operator" onclick="ipamSync()">Sync UniFi</button>` : ""}</div></div>
    </div>
    ${(subnet.pool_clash || []).length ? `<div class="note bad"><b>Harvester's ${esc(subnet.pool_clash.join(", "))} VIP pool overlaps the DHCP range.</b> A VIP it hands out may already be leased to a device; move the pool or shrink the DHCP range.</div>` : ""}
    ${u.configured && u.last_error ? `<div class="note">UniFi: ${esc(u.last_error)}</div>` : ""}
    <div class="between ipam-tools"><div class="row">
      <select onchange="ipamFilter(this.value)">${[["", "Every address"], ["static", "Static"], ["reservation", "DHCP reservations"], ["dhcp", "DHCP"],
        ["reserved", "Held"], ["infrastructure", "Network gear"], ["cluster", "Cluster"], ["undocumented", "Undocumented"], ["flagged", `Needs attention (${flagged})`]]
        .map(([v, l]) => `<option value="${v}" ${STATE.ipamFilter === v || (!STATE.ipamFilter && !v) ? "selected" : ""}>${esc(l)}</option>`).join("")}
        <optgroup label="Category">${Object.entries(IPAM_CATEGORIES).map(([k, [label]]) =>
          `<option value="cat:${k}" ${STATE.ipamFilter === `cat:${k}` ? "selected" : ""}>${esc(label)}</option>`).join("")}</optgroup></select>
      <span class="dim xs">${rows.length} shown</span></div></div>
      <div class="row ipam-bulk" id="ipamBulk" ${ipamPicks().size ? "" : "hidden"}><b class="small" id="ipamBulkCount">${ipamPicks().size} selected</b>
        <button class="btn sm" type="button" onclick="ipamSelectAll(false)" title="Untick every address">Clear</button>
        <select id="ipamBulkCategory">${ipamCategoryOptions("", "category…")}</select>
        <select id="ipamBulkKind"><option value="">kind…</option>${Object.entries(IPAM_KIND_LABELS).map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("")}</select>
        <input id="ipamBulkTag" placeholder="add tag" style="width:110px">
        <input id="ipamBulkOwner" placeholder="owner" style="width:110px">
        <button class="btn sm pri" onclick="ipamBulk()">Apply</button>
        <button class="btn sm danger" onclick="ipamBulk(true)" data-tip="Remove these addresses and everything written about them">Remove</button></div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl dense stack ipam-table" data-sort="ipam"><thead><tr>
      <th data-nosort><input type="checkbox" id="ipamAll" aria-label="Select every address shown" onchange="ipamSelectAll(this.checked)"
        ${rows.some(r => !r.cluster) && rows.filter(r => !r.cluster).every(r => ipamPicks().has(r.ip)) ? "checked" : ""}></th>
      <th>Address</th><th>Name</th><th>MAC</th><th>Kind</th><th data-nosort>Seen</th><th data-nosort>Notes</th></tr></thead>
      ${rows.length ? ipamBodies(rows, subnet, !STATE.ipamFilter && !STATE.q)
        : `<tbody><tr><td colspan="7" class="empty">Nothing here yet. Scan the subnet${u.configured ? ", sync UniFi," : ""} or add an address.</td></tr></tbody>`}
      </table></div></div>`);
  if (scanning) setTimeout(() => { if (STATE.view === "network" && networkTab() === "ip") viewIpam(); }, 3000);
}

/* ---------------- the table ---------------- */
const ipNum = ip => ip.split(".").reduce((n, o) => n * 256 + +o, 0);
const ipStr = n => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");

/* A row in two short lines: the name, then one line of what else it is
   called - UniFi's hostname, reverse DNS, or the Services on a VIP. */
function ipamRow(row) {
  const second = row.cluster === "vip" ? (row.services || []).join(", ")
    : [row.name && row.unifi?.name && row.unifi.name !== row.name ? row.unifi.name : "", row.unifi?.hostname, row.scan?.rdns && row.scan.rdns !== row.name ? row.scan.rdns : ""]
      .filter(Boolean).join(" · ");
  const name = row.name ? `<b>${esc(row.name)}</b>`
    : row.unifi?.name ? `<span class="ipam-unifi-name" data-tip="UniFi's name; give it your own to replace it here">${esc(row.unifi.name)}</span>` : "";
  const note = [row.note, row.owner ? `owner: ${row.owner}` : ""].filter(Boolean).join(" · ");
  const notes = row.flags.map(f => `<span class="ipam-flag ${f.level}" data-tip="${esc(f.text)}">${f.level === "warn" ? "⚠" : "ℹ"}</span>`).join("")
    + (row.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("") + (note ? `<span class="ipam-note" data-tip="${esc(note)}">${esc(note)}</span>` : "");
  // Cells with nothing in them are left empty, so on a phone - where each
  // row is a short card - they take no room.
  return `<tr class="ipam-row ${row.flags.some(f => f.level === "warn") ? "ipam-warn" : ""}">
    <td class="ip-pick">${row.cluster ? "" : `<input type="checkbox" class="ipam-pick" value="${esc(row.ip)}" aria-label="Select ${esc(row.ip)}" onchange="ipamPicked(this)" ${ipamPicks().has(row.ip) ? "checked" : ""}>`}</td>
    <td class="ip-addr mono nowrap" data-sort="${ipNum(row.ip)}"><a style="cursor:pointer" onclick="ipamEdit('${esc(row.ip)}')">${esc(row.ip)}</a>${row.in_dhcp ? ' <span class="dim xs" data-tip="Inside the DHCP range">dhcp</span>' : ""}</td>
    <td data-label="Name" class="ip-name ipam-name">${name || row.category || second ? `<div class="ipam-line">${ipamCategoryIcon(row.category)}${name}</div>
      ${second ? `<div class="ipam-line dim xs mono" title="${esc(second)}">${esc(second)}</div>` : ""}` : ""}</td>
    <td data-label="MAC" class="ip-mac mono xs nowrap">${esc(row.mac || "")}</td>
    <td data-label="Kind" class="ip-kind nowrap">${ipamKind(row)}</td>
    <td data-label="Seen" class="ip-seen nowrap">${ipamSeen(row)}</td>
    <td data-label="Notes" class="ip-notes ipam-notes">${notes ? `<div class="ipam-line">${notes}</div>` : ""}</td></tr>`;
}

/* Rows in address order, with the free runs between them as dividers that
   open to list each free address. Only in address order and unfiltered:
   anywhere else a gap means nothing. */
function ipamBodies(rows, subnet, gaps) {
  const sort = sortState("ipam");
  if (!gaps || (sort && !(sort.col === 1 && sort.dir > 0))) return `<tbody>${rows.map(ipamRow).join("")}</tbody>`;
  const [base, bits] = subnet.cidr.split("/");
  // >>> 0: JavaScript's & is signed, and 192.x is past 2^31.
  const first = ((ipNum(base) & (~0 << (32 - +bits))) >>> 0) + 1, last = first + 2 ** (32 - +bits) - 3;
  const sorted = [...rows].sort((a, b) => ipNum(a.ip) - ipNum(b.ip));
  const out = [];
  let cursor = first, chunk = [];
  const flush = () => { if (chunk.length) out.push(`<tbody>${chunk.map(ipamRow).join("")}</tbody>`); chunk = []; };
  sorted.forEach(row => {
    const n = ipNum(row.ip);
    if (n > cursor) { flush(); out.push(ipamGap(cursor, n - 1, subnet)); }
    chunk.push(row);
    cursor = Math.max(cursor, n + 1);
  });
  flush();
  if (cursor <= last) out.push(ipamGap(cursor, last, subnet));
  return out.join("");
}

function ipamGap(from, to, subnet) {
  const key = `${subnet.id}:${from}`, open = (STATE.ipamOpenGaps || new Set()).has(key);
  const count = to - from + 1;
  const d1 = subnet.dhcp_start ? ipNum(subnet.dhcp_start) : 0, d2 = subnet.dhcp_end ? ipNum(subnet.dhcp_end) : -1;
  const inDhcp = Math.max(0, Math.min(to, d2) - Math.max(from, d1) + 1);
  const range = count === 1 ? ipStr(from) : `${ipStr(from)} – ${ipStr(to)}`;
  const addresses = open ? Array.from({ length: count }, (_, i) => from + i).map(n => {
    const ip = ipStr(n), dhcp = n >= d1 && n <= d2, gw = subnet.gateway === ip;
    return `<tr class="ipam-free"><td></td><td class="mono nowrap">${ip}</td>
      <td colspan="4" class="dim xs">${gw ? "the gateway" : dhcp ? "free · inside the DHCP range, so the DHCP server may lease it" : "free"}</td>
      <td class="nowrap">${gw ? "" : `<a class="ipam-doc" data-need="operator" onclick="ipamEdit('${ip}')">＋ Document</a>`}</td></tr>`;
  }).join("") : "";
  return `<tbody class="grouphead ipam-gap"><tr><td colspan="7"><button type="button" class="ipam-gap-head" aria-expanded="${open}" onclick="ipamToggleGap('${key}')">
      <span class="wgroup-chevron">›</span><span class="mono">${esc(range)}</span>
      <span class="dim xs">${count} free${inDhcp ? ` · ${inDhcp === count ? "all" : inDhcp} in the DHCP range` : ""}</span></button></td></tr>${addresses}</tbody>`;
}
window.ipamToggleGap = key => {
  STATE.ipamOpenGaps = STATE.ipamOpenGaps || new Set();
  STATE.ipamOpenGaps.has(key) ? STATE.ipamOpenGaps.delete(key) : STATE.ipamOpenGaps.add(key);
  renderIpam();
};

window.ipamCopy = ip => { navigator.clipboard?.writeText(ip); toast(`${ip} copied`, "ok"); };
window.ipamPicked = box => {
  if (box) box.checked ? ipamPicks().add(box.value) : ipamPicks().delete(box.value);
  const n = ipamPicks().size, boxes = $$(".ipam-pick");
  $("#ipamBulk").hidden = !n;
  $("#ipamBulkCount").textContent = `${n} selected`;
  if ($("#ipamAll")) $("#ipamAll").checked = boxes.length > 0 && boxes.every(b => b.checked);
};
window.ipamSelectAll = on => {
  $$(".ipam-pick").forEach(box => { box.checked = on; on ? ipamPicks().add(box.value) : ipamPicks().delete(box.value); });
  if (!on) ipamPicks().clear();
  ipamPicked();
};

const ipamPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/* What removing addresses does: the list forgets them - name, notes and the
   last scan's answer - and anything still on the network is found again. */
const ipamRemoveQuestion = count => `Remove ${count === 1 ? "this address" : `${count} addresses`} from the list? ` +
  `Its name, notes and tags are deleted. A device still using ${count === 1 ? "it" : "one"} shows up again after the next scan${ipamUnifiOn() ? " or UniFi sync" : ""}.`;
window.ipamBulk = async (forget = false) => {
  const ips = [...ipamPicks()];
  if (forget && !confirm(ipamRemoveQuestion(ips.length))) return;
  const changes = forget ? { forget: true } : {};
  if (!forget) {
    if ($("#ipamBulkKind").value) changes.kind = $("#ipamBulkKind").value;
    if ($("#ipamBulkCategory").value) changes.category = $("#ipamBulkCategory").value;
    if ($("#ipamBulkTag").value.trim()) changes.tags_add = [$("#ipamBulkTag").value.trim()];
    if ($("#ipamBulkOwner").value.trim()) changes.owner = $("#ipamBulkOwner").value.trim();
    if (!Object.keys(changes).length) return toast("Choose a category, a kind, a tag or an owner to set", "bad");
  }
  try {
    const r = await ipamPost("/api/ipam/bulk", { ips, changes }); toast(r.detail, "ok");
    ipamPicks().clear();
    ["ipamBulkCategory", "ipamBulkKind", "ipamBulkTag", "ipamBulkOwner"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
    document.activeElement?.blur?.();
    viewIpam();
  }
  catch (e) { toast(e.message, "bad"); }
};

window.ipamEdit = (ip = "") => {
  const data = STATE.data.ipam, subnet = ipamSubnetPick(data.subnets);
  const row = ipamRows(subnet).find(r => r.ip === ip) || { ip: ip || subnet.next_free[0] || "", tags: [] };
  if (row.cluster) {
    return modal(ip, `<p class="muted small">${row.cluster === "node" ? "A cluster node's address, read from Kubernetes." : `A cluster VIP, used by ${esc((row.services || []).join(", "))}.`} It is shown here so nothing else is given it, and is changed where it is set, not here.</p>`);
  }
  modal(ip ? `Address · ${ip}` : "Document an address", `
    <div class="f2"><div class="f"><label>Address</label><input id="ia_ip" class="mono" value="${esc(row.ip)}" ${ip ? "readonly" : "data-ipam"}></div>
      <div class="f"><label>Kind</label><select id="ia_kind"><option value="">not set</option>${Object.entries(IPAM_KIND_LABELS).map(([k, l]) =>
        `<option value="${k}" ${row.kind === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div></div>
    <div class="f2"><div class="f"><label>Name</label><input id="ia_name" value="${esc(row.name || "")}" maxlength="60" placeholder="${esc(row.unifi?.name || "e.g. Office printer")}"></div>
      <div class="f"><label>MAC</label><input id="ia_mac" class="mono" value="${esc(row.mac || "")}" placeholder="aa:bb:cc:dd:ee:ff"></div></div>
    <div class="f2"><div class="f"><label>Category</label><select id="ia_category">${ipamCategoryOptions(row.category || "")}</select></div>
      <div class="f"><label>Owner</label><input id="ia_owner" value="${esc(row.owner || "")}" maxlength="60"></div></div>
    <div class="f"><label>Tags</label><input id="ia_tags" value="${esc((row.tags || []).join(", "))}" placeholder="iot, office"></div>
    ${row.unifi && (row.unifi.name || row.unifi.hostname) ? `<div class="dim xs" style="margin:-4px 0 10px">UniFi calls it ${esc([row.unifi.name, row.unifi.hostname].filter(Boolean).join(" · "))}; your name here is kept apart and a sync never changes it.</div>` : ""}
    <div class="f"><label>Notes</label><textarea id="ia_note" rows="3" maxlength="300">${esc(row.note || "")}</textarea></div>
    ${(row.flags || []).map(f => `<div class="note ${f.level === "warn" ? "bad" : ""}">${esc(f.text)}</div>`).join("")}
    ${row.scan?.up ? `<div class="dim xs">Last scan: answering${row.scan.ports?.length ? ` on ${row.scan.ports.join(", ")}` : ""}${row.scan.rdns ? ` · ${esc(row.scan.rdns)}` : ""}</div>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="ipamSave()">Save</button><button class="btn" onclick="closeModal()">Cancel</button>
      ${ip && ipamRows(subnet).some(r => r.ip === ip) ? `<button class="btn danger" style="margin-left:auto" data-need="operator" onclick="ipamRemove('${esc(ip)}')">Remove</button>` : ""}</div>`);
};
window.ipamRemove = async ip => {
  if (!confirm(ipamRemoveQuestion(1))) return;
  try { const r = await ipamPost("/api/ipam/bulk", { ips: [ip], changes: { forget: true } }); toast(r.detail, "ok"); ipamPicks().delete(ip); closeModal(); viewIpam(); }
  catch (e) { toast(e.message, "bad"); }
};
window.ipamSave = async () => {
  const body = { ip: $("#ia_ip").value.trim(), kind: $("#ia_kind").value, category: $("#ia_category").value, name: $("#ia_name").value, mac: $("#ia_mac").value,
    owner: $("#ia_owner").value, note: $("#ia_note").value, tags: $("#ia_tags").value.split(",").map(t => t.trim()).filter(Boolean) };
  try { await ipamPost("/api/ipam/record", body); toast(`${body.ip} saved`, "ok"); closeModal(); viewIpam(); }
  catch (e) { toast(e.message, "bad"); }
};

window.ipamScan = async id => {
  try { const r = await ipamPost("/api/ipam/scan", { subnet: id }); toast(r.detail, "ok"); setTimeout(viewIpam, 800); }
  catch (e) { toast(e.message, "bad"); }
};
window.ipamSync = async () => {
  try { const r = await ipamPost("/api/ipam/unifi/sync", {}); toast(r.detail, "ok"); }
  catch (e) { toast(e.message, "bad"); }
  await ipamAfterUnifi();
};

/* ---------------- subnets ---------------- */
function ipamSubnetRow(s = {}) {
  return `<div class="ipam-subnet card flat">
    <div class="f2"><div class="f"><label>Subnet</label><input class="is-cidr mono" value="${esc(s.cidr || "")}" placeholder="192.168.1.0/24"></div>
      <div class="f"><label>Name</label><input class="is-name" value="${esc(s.name || "")}" placeholder="LAN" maxlength="40"></div></div>
    <div class="ipam-subnet-grid"><div class="f"><label>Gateway</label><input class="is-gw mono" value="${esc(s.gateway || "")}"></div>
      <div class="f"><label>DHCP from</label><input class="is-d1 mono" value="${esc(s.dhcp_start || "")}"></div>
      <div class="f"><label>DHCP to</label><input class="is-d2 mono" value="${esc(s.dhcp_end || "")}"></div>
      <div class="f"><label>VLAN</label><input class="is-vlan" type="number" min="1" max="4094" value="${s.vlan || ""}"></div></div>
    <div class="row"><input class="is-note" value="${esc(s.note || "")}" placeholder="Notes" style="flex:1">
      <button class="btn sm danger" type="button" onclick="this.closest('.ipam-subnet').remove()">Remove</button></div></div>`;
}
window.ipamSubnets = (add = "") => {
  const data = STATE.data.ipam || { subnets: [] };
  modal("Subnets", `<p class="muted small">The networks Homestead keeps addresses for, each up to a /22. Give each its DHCP range so static addresses and VIPs inside it are flagged, and the next free address is found outside it.</p>
    <div id="is_rows">${data.subnets.map(ipamSubnetRow).join("")}${add ? ipamSubnetRow({ cidr: add, name: "LAN", gateway: add.replace(/\.0\/24$/, ".1") }) : ""}</div>
    <div class="row" style="margin-top:10px"><button class="btn" onclick="$('#is_rows').insertAdjacentHTML('beforeend', ipamSubnetRow())">＋ Subnet</button>
      ${(data.suggested || []).map(c => `<button class="btn" onclick="$('#is_rows').insertAdjacentHTML('beforeend', ipamSubnetRow({cidr:'${esc(c)}'}))">＋ ${esc(c)} <span class="dim">(cluster nodes)</span></button>`).join("")}
      ${(ipamUnifiOn() ? data.unifi_networks || [] : []).map((n, i) => `<button class="btn" onclick="$('#is_rows').insertAdjacentHTML('beforeend', ipamSubnetRow(STATE.data.ipam.unifi_networks[${i}]))">＋ ${esc(n.name || n.cidr)} <span class="dim">(UniFi${n.vlan ? ` · VLAN ${n.vlan}` : ""})</span></button>`).join("")}</div>
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="ipamSubnetsSave()">Save subnets</button><button class="btn" onclick="closeModal()">Cancel</button></div>`, true);
};
window.ipamSubnetRow = ipamSubnetRow;
window.ipamSubnetsSave = async () => {
  const subnets = $$("#is_rows .ipam-subnet").map(el => ({ cidr: $(".is-cidr", el).value.trim(), name: $(".is-name", el).value,
    gateway: $(".is-gw", el).value.trim(), dhcp_start: $(".is-d1", el).value.trim(), dhcp_end: $(".is-d2", el).value.trim(),
    vlan: $(".is-vlan", el).value, note: $(".is-note", el).value })).filter(s => s.cidr);
  try { await ipamPost("/api/ipam/subnets", { subnets }); toast("subnets saved", "ok"); closeModal(); viewIpam(); }
  catch (e) { toast(e.message, "bad"); }
};

/* ---------------- UniFi ---------------- */
window.ipamUnifi = async () => {
  if (!STATE.data.ipam) { try { STATE.data.ipam = await api("/api/ipam"); } catch (e) { STATE.data.ipam = { subnets: [], unifi: {} }; } }
  const u = (STATE.data.ipam || {}).unifi || {};
  modal("UniFi", `<p class="muted small">Brings in the clients and devices a UniFi Network controller knows - names, MACs, addresses - and its DHCP reservations. It never changes the controller. Create an API key on the console under <b>Settings → Control Plane → Integrations</b>.</p>
    <div class="f"><label>Console address</label><input id="uf_url" value="${esc(u.url || "")}" placeholder="https://192.168.1.1"></div>
    <div class="f2"><div class="f"><label>API key</label><input id="uf_key" type="password" autocomplete="off" placeholder="${u.has_key ? "saved · leave blank to keep" : "paste the key"}"></div>
      <div class="f"><label>Site</label><input id="uf_site" value="${esc(u.site || "default")}"></div></div>
    <label class="switch"><input type="checkbox" id="uf_tls" ${u.verify_tls ? "checked" : ""}> Check the console's certificate ${tip("Consoles ship a self-signed certificate, so this is off unless yours has a real one.")}</label>
    ${u.last_sync ? `<div class="dim xs">Last synced ${esc(fmtAgo(Date.now() / 1000 - u.last_sync))}${u.site_name ? ` from ${esc(u.site_name)}` : ""}${u.note ? ` · ${esc(u.note)}` : ""}</div>` : ""}
    ${u.last_error ? `<div class="note bad">${esc(u.last_error)}</div>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn pri" data-need="admin" onclick="ipamUnifiSave(true)">Save and sync</button>
      <button class="btn" data-need="admin" onclick="ipamUnifiSave(false)">Save</button>
      ${u.configured ? '<button class="btn danger" data-need="admin" onclick="ipamUnifiForget()">Disconnect</button>' : ""}
      <button class="btn" onclick="closeModal()">Cancel</button></div>`);
};
window.ipamUnifiSave = async sync => {
  try {
    await ipamPost("/api/ipam/unifi", { url: $("#uf_url").value.trim(), api_key: $("#uf_key").value, site: $("#uf_site").value.trim(), verify_tls: $("#uf_tls").checked });
    closeModal();
    if (sync) await ipamSync(); else toast("UniFi settings saved", "ok");
    ipamAfterUnifi();
  } catch (e) { toast(e.message, "bad"); }
};
window.ipamUnifiForget = async () => {
  if (!confirm("Disconnect UniFi? Its key is deleted, and the IP addresses page stops showing what UniFi knows. Nothing on the controller changes.")) return;
  try { await ipamPost("/api/ipam/unifi", { forget: true }); toast("UniFi disconnected", "ok"); closeModal(); ipamAfterUnifi(); }
  catch (e) { toast(e.message, "bad"); }
};
/* Whichever page the dialog was opened from shows the change. */
async function ipamAfterUnifi() {
  try { STATE.data.ipam = await api("/api/ipam"); } catch (e) { /* the page's next refresh will */ }
  if (STATE.view === "settings") { const host = $("#unifiCard"); if (host) host.outerHTML = ipamUnifiCard(); }
  else if (STATE.view === "network") renderIpam();
}

/* Settings > Apps: where UniFi is connected. */
function ipamUnifiCard() {
  const u = ((STATE.data.ipam || {}).unifi) || {};
  return `<section class="card flat settings-wide" data-tab="apps" id="unifiCard">
    <div class="settings-card-head between"><div><div class="ctitle">UniFi Network</div>
      <div class="csub">${u.configured ? `Connected to ${esc(u.url)}${u.last_sync ? ` · last synced ${esc(fmtAgo(Date.now() / 1000 - u.last_sync))}` : " · not synced yet"}`
        : "Optional. Connect a UniFi controller and the IP addresses page adds its clients, devices, reserved IPs, networks and the names it knows. Without it, IP addresses works from scans and what you write."}</div>
      ${u.configured && u.last_error ? `<div class="note bad" style="margin-top:8px">${esc(u.last_error)}</div>` : ""}</div>
      <div class="row">${u.configured ? '<button class="btn" data-need="operator" onclick="ipamSync()">Sync now</button>' : ""}
        <button class="btn ${u.configured ? "" : "pri"}" data-need="admin" onclick="ipamUnifi()">${u.configured ? "Edit" : "Connect UniFi"}</button></div></div></section>`;
}
window.ipamUnifiCard = ipamUnifiCard;

/* ---------------- export ---------------- */
window.ipamExport = () => {
  const subnet = ipamSubnetPick(STATE.data.ipam.subnets);
  const cell = value => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const unifi = ipamUnifiOn();
  const head = ["address", "name", ...(unifi ? ["unifi_name", "hostname"] : []), "mac", "kind", "category", "cluster", "owner", "tags", "note", "answering", ...(unifi ? ["unifi"] : [])];
  const lines = [head.join(",")].concat(ipamRows(subnet).map(r => [r.ip, r.name, ...(unifi ? [r.unifi?.name, r.unifi?.hostname] : []),
    r.mac, r.kind, r.category, r.cluster, r.owner, (r.tags || []).join(" "), r.note, r.scan?.up ? "yes" : "",
    ...(unifi ? [r.unifi ? r.unifi.type || "yes" : ""] : [])].map(cell).join(",")));
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  link.download = `${subnet.cidr.replace(/[./]/g, "-")}-addresses.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
};

/* ---------------- import ---------------- */
const IPAM_TEMPLATE = [
  "address,name,mac,kind,category,owner,tags,note",
  "192.168.1.10,Tower,aa:bb:cc:dd:ee:10,static,nas,,storage backup,Unraid - admin on port 80",
  "192.168.1.2,Core switch,aa:bb:cc:dd:ee:02,infrastructure,switch,,network,Rack top",
].join("\n") + "\n";
function ipamDownload(name, text) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
window.ipamTemplate = () => ipamDownload("homestead-addresses-template.csv", IPAM_TEMPLATE);
window.ipamImport = () => {
  modal("Import addresses", `<p class="muted small">A CSV with a header row. <span class="mono">address</span> is required; any of
      <span class="mono">name, mac, kind, category, owner, tags, note</span> may follow. A blank cell leaves what is recorded; other columns are skipped, so an export can be edited and imported back.</p>
    <p class="dim xs">kind: ${Object.keys(IPAM_KIND_LABELS).join(", ")} · category: ${Object.keys(IPAM_CATEGORIES).join(", ")} · tags separated by spaces</p>
    <div class="row" style="margin:12px 0"><button class="btn" onclick="ipamTemplate()">Download template</button>
      <label class="btn pri" style="cursor:pointer">Choose CSV…<input type="file" accept=".csv,text/csv" hidden onchange="ipamImportFile(this.files[0])"></label></div>
    <div id="ipamImportResult"></div>`);
};
window.ipamImportFile = async file => {
  if (!file) return;
  const host = $("#ipamImportResult");
  host.innerHTML = '<div class="dim small"><span class="spin2"></span> importing</div>';
  try {
    const r = await ipamPost("/api/ipam/import", { csv: await file.text() });
    toast(r.detail, "ok"); closeModal(); viewIpam();
  } catch (e) { host.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; }
};

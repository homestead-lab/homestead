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
window.ipamPickSubnet = id => { try { localStorage.setItem("homestead.ipam.subnet", id); } catch (e) { /* this visit only */ } renderIpam(); };
window.ipamFilter = value => { STATE.ipamFilter = value; renderIpam(); };

async function viewIpam() {
  STATE.data.ipam = await api("/api/ipam");
  renderIpam();
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
    <div class="row"><button class="btn" data-need="operator" onclick="ipamSubnets()">Subnets</button>
      ${data.subnets.length ? `<button class="btn" onclick="ipamExport()">Export CSV</button>
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
    <div class="grid g4 statgrid" style="margin:12px 0 16px">
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
      <span class="dim xs">${rows.length} shown</span></div>
      <div class="row ipam-bulk" id="ipamBulk" hidden><span class="dim xs" id="ipamBulkCount"></span>
        <select id="ipamBulkCategory">${ipamCategoryOptions("", "category…")}</select>
        <select id="ipamBulkKind"><option value="">kind…</option>${Object.entries(IPAM_KIND_LABELS).map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("")}</select>
        <input id="ipamBulkTag" placeholder="add tag" style="width:110px">
        <input id="ipamBulkOwner" placeholder="owner" style="width:110px">
        <button class="btn sm pri" onclick="ipamBulk()">Apply</button>
        <button class="btn sm danger" onclick="ipamBulk(true)" title="Remove what is documented about these addresses">Forget</button></div></div>
    <div class="card flat pad0"><div class="tblwrap"><table class="tbl dense stack ipam-table" data-sort="ipam"><thead><tr>
      <th data-nosort><input type="checkbox" aria-label="Select every address shown" onchange="ipamSelectAll(this.checked)"></th>
      <th>Address</th><th>Name</th><th>MAC</th><th>Kind</th><th data-nosort>Seen</th><th data-nosort>Notes</th></tr></thead><tbody>
      ${rows.map(row => `<tr class="${row.flags.some(f => f.level === "warn") ? "ipam-warn" : ""}">
        <td>${row.cluster ? "" : `<input type="checkbox" class="ipam-pick" value="${esc(row.ip)}" onchange="ipamPicked()">`}</td>
        <td class="mono" data-sort="${row.ip.split(".").reduce((n, o) => n * 256 + +o, 0)}"><a style="cursor:pointer" onclick="ipamEdit('${esc(row.ip)}')">${esc(row.ip)}</a>${row.in_dhcp ? ' <span class="dim xs" data-tip="Inside the DHCP range">dhcp</span>' : ""}</td>
        <td data-label="Name">${ipamCategoryIcon(row.category)}${row.name ? `<b>${esc(row.name)}</b>` : row.unifi?.name ? `<span class="ipam-unifi-name" data-tip="UniFi's name; give it your own to replace it here">${esc(row.unifi.name)}</span>` : ""}
          ${row.unifi && (row.unifi.name || row.unifi.hostname) && row.name ? `<div class="dim xs">UniFi: ${esc([row.unifi.name !== row.name ? row.unifi.name : "", row.unifi.hostname].filter(Boolean).join(" · ") || "same name")}</div>`
            : row.unifi?.hostname ? `<div class="dim xs mono">${esc(row.unifi.hostname)}</div>` : ""}${row.scan?.rdns && row.scan.rdns !== row.name ? `<div class="dim xs mono">${esc(row.scan.rdns)}</div>` : ""}${row.cluster === "vip" ? `<div class="dim xs">${esc((row.services || []).join(", "))}</div>` : ""}</td>
        <td data-label="MAC" class="mono xs">${esc(row.mac || "")}</td>
        <td data-label="Kind">${ipamKind(row)}</td>
        <td data-label="Seen">${ipamSeen(row)}</td>
        <td data-label="Notes" class="small">${row.flags.map(f => `<span class="ipam-flag ${f.level}" data-tip="${esc(f.text)}">${f.level === "warn" ? "⚠" : "ℹ"} ${esc(f.text.split(":")[0])}</span>`).join("")}
          ${esc(row.note || "")}${row.owner ? ` <span class="dim xs">· ${esc(row.owner)}</span>` : ""} ${(row.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</td></tr>`).join("")
        || `<tr><td colspan="7" class="empty">Nothing here yet. Scan the subnet${u.configured ? ", sync UniFi," : ""} or add an address.</td></tr>`}</tbody></table></div></div>`);
  if (scanning) setTimeout(() => { if (STATE.view === "network" && networkTab() === "ip") viewIpam(); }, 3000);
}

window.ipamCopy = ip => { navigator.clipboard?.writeText(ip); toast(`${ip} copied`, "ok"); };
window.ipamPicked = () => {
  const n = $$(".ipam-pick:checked").length;
  $("#ipamBulk").hidden = !n;
  $("#ipamBulkCount").textContent = `${n} selected`;
};
window.ipamSelectAll = on => { $$(".ipam-pick").forEach(box => { box.checked = on; }); ipamPicked(); };

const ipamPost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

window.ipamBulk = async (forget = false) => {
  const ips = $$(".ipam-pick:checked").map(box => box.value);
  if (forget && !confirm(`Forget what is documented about ${ips.length} address${ips.length === 1 ? "" : "es"}? Scan${ipamUnifiOn() ? " and UniFi" : ""} results come back on the next ${ipamUnifiOn() ? "scan or sync" : "scan"}.`)) return;
  const changes = forget ? { forget: true } : {};
  if (!forget) {
    if ($("#ipamBulkKind").value) changes.kind = $("#ipamBulkKind").value;
    if ($("#ipamBulkCategory").value) changes.category = $("#ipamBulkCategory").value;
    if ($("#ipamBulkTag").value.trim()) changes.tags_add = [$("#ipamBulkTag").value.trim()];
    if ($("#ipamBulkOwner").value.trim()) changes.owner = $("#ipamBulkOwner").value.trim();
    if (!Object.keys(changes).length) return toast("Choose a category, a kind, a tag or an owner to set", "bad");
  }
  try { const r = await ipamPost("/api/ipam/bulk", { ips, changes }); toast(r.detail, "ok"); viewIpam(); }
  catch (e) { toast(e.message, "bad"); }
};

window.ipamEdit = (ip = "") => {
  const data = STATE.data.ipam, subnet = ipamSubnetPick(data.subnets);
  const row = ipamRows(subnet).find(r => r.ip === ip) || { ip: ip || subnet.next_free[0] || "", tags: [] };
  if (row.cluster) {
    return modal(ip, `<p class="muted small">${row.cluster === "node" ? "A cluster node's address, read from Kubernetes." : `A cluster VIP, used by ${esc((row.services || []).join(", "))}.`} It is shown here so nothing else is given it, and is changed where it is set, not here.</p>`);
  }
  modal(ip ? `Address · ${ip}` : "Document an address", `
    <div class="f2"><div class="f"><label>Address</label><input id="ia_ip" class="mono" value="${esc(row.ip)}" ${ip ? "readonly" : ""}></div>
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
    <div class="row" style="margin-top:14px"><button class="btn pri" onclick="ipamSave()">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
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
    <div class="f2"><div class="f"><label>Subnet</label><input class="is-cidr mono" value="${esc(s.cidr || "")}" placeholder="192.0.2.0/24"></div>
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
    <div class="f"><label>Console address</label><input id="uf_url" value="${esc(u.url || "")}" placeholder="https://192.0.2.1"></div>
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

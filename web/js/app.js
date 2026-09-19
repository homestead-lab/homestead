const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const V = $("#views");
let STATE = { view: "dash", q: "", data: {} };

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sev = p => p >= 88 ? "b" : p >= 70 ? "w" : "";
const fmtUp = sec => {
  if (!sec || sec < 0) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
};
const upChip = sec => `<span class="uptime"><span class="ld"></span>${fmtUp(sec)}</span>`;
const svcUrl = (ip, port) => {
  const https = [443, 8443, 9443].includes(+port);
  return `${https ? "https" : "http"}://${ip}:${port}`;
};
window.openSvc = (ip, port) => window.open(svcUrl(ip, port), "_blank", "noopener");
const gcls = p => p >= 88 ? "g-bad" : p >= 70 ? "g-warn" : "g-ok";

function toast(msg, kind = "") {
  const d = document.createElement("div");
  d.className = "tst " + kind; d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), 4200);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const b = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((b && b.error) || r.statusText);
  return b;
}
function modal(t, h) { $("#mtitle").textContent = t; $("#mbody").innerHTML = h; $("#modal").classList.remove("hidden"); }
$("#mclose").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };

/* ---------------- chart primitives ---------------- */
function sparkline(vals, { w = 300, h = 78, band = true } = {}) {
  if (!vals || vals.length < 2) vals = [0, 0];
  const n = vals.length, mn = Math.min(...vals), mx = Math.max(...vals);
  const pad = (mx - mn) * .25 || 1, lo = mn - pad, hi = mx + pad;
  const X = i => (i / (n - 1)) * w;
  const Y = v => h - ((v - lo) / (hi - lo)) * h;
  const pts = vals.map((v, i) => [X(i), Y(v)]);
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], cx = (x0 + x1) / 2;
    d += ` C ${cx},${y0} ${cx},${y1} ${x1},${y1}`;
  }
  const area = d + ` L ${w},${h} L 0,${h} Z`;
  const last = pts[pts.length - 1], mid = pts[Math.floor(n / 2)];
  const bx = w * .55, bw = w * .32;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    ${band ? `<rect class="band" x="${bx}" y="0" width="${bw}" height="${h}" rx="4"/>` : ""}
    <line class="base" x1="0" y1="${h * .62}" x2="${w}" y2="${h * .62}"/>
    <path class="fl" d="${area}"/><path class="ln" d="${d}"/>
    <circle class="kn" cx="${mid[0]}" cy="${mid[1]}" r="3"/>
    <circle class="kn" cx="${last[0]}" cy="${last[1]}" r="3.6"/>
  </svg>`;
}
const POL = (cx, cy, r, a) => [cx + r * Math.cos((a - 90) * Math.PI / 180), cy + r * Math.sin((a - 90) * Math.PI / 180)];
function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = POL(cx, cy, r, a0), [x1, y1] = POL(cx, cy, r, a1);
  return `M ${x0} ${y0} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}
function donut(pct, big, label, size = 178) {
  const c = size / 2, r = c - 16;
  return `<svg class="donut" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle class="trk" cx="${c}" cy="${c}" r="${r}"/>
    <path class="arc" d="${arcPath(c, c, r, 0, Math.max(.5, Math.min(359.9, pct * 3.599)))}"/>
    <text class="dcenter" x="${c}" y="${c + 2}" text-anchor="middle">${big}</text>
    <text class="dlabel" x="${c}" y="${c + 21}" text-anchor="middle">${esc(label)}</text>
  </svg>`;
}
function segDonut(parts, big, label, size = 178) {
  const c = size / 2, r = c - 16, tot = parts.reduce((s, p) => s + p.v, 0) || 1;
  let a = 0, out = "";
  const shades = ["#ffffff", "#9a9aa4", "#5c5c66", "#3a3a42", "#2a2a30"];
  parts.forEach((p, i) => {
    const sw = p.v / tot * 359.4, a1 = a + sw;
    if (sw > .4) out += `<path class="seg" stroke="${p.c || shades[i % shades.length]}" d="${arcPath(c, c, r, a + 1, a1)}"><title>${esc(p.n)}: ${p.v}</title></path>`;
    a = a1;
  });
  return `<svg class="donut" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle class="trk" cx="${c}" cy="${c}" r="${r}"/>${out}
    <text class="dcenter" x="${c}" y="${c + 2}" text-anchor="middle">${big}</text>
    <text class="dlabel" x="${c}" y="${c + 21}" text-anchor="middle">${esc(label)}</text></svg>`;
}
function legend(parts, tot) {
  const shades = ["#ffffff", "#9a9aa4", "#5c5c66", "#3a3a42", "#2a2a30"];
  return `<div class="legend">${parts.map((p, i) => `<div class="lg">
    <span class="dt" style="background:${p.c || shades[i % shades.length]}"></span>
    <span class="nm">${esc(p.n)}</span><span class="v1">${p.v}</span>
    <span class="v2">${tot ? Math.round(p.v / tot * 100) : 0}%</span></div>`).join("")}</div>`;
}
function trend(vals) {
  if (!vals || vals.length < 4) return "";
  const half = Math.floor(vals.length / 2);
  const a = vals.slice(0, half).reduce((s, v) => s + v, 0) / half;
  const b = vals.slice(half).reduce((s, v) => s + v, 0) / (vals.length - half);
  if (!a) return "";
  const d = Math.round((b - a) / a * 100);
  if (Math.abs(d) < 1) return `<span class="badge">steady</span>`;
  return `<span class="badge">${d > 0 ? "+" : ""}${d}% <span class="arw">${d > 0 ? "↑" : "↓"}</span></span>`;
}

/* ---------------- DASHBOARD ---------------- */
async function viewDash() {
  const [o, hist] = await Promise.all([api("/api/overview"), api("/api/history").catch(() => ({}))]);
  STATE.data.ov = o;
  const hp = $("#healthPill");
  hp.className = "pill " + (o.health === "healthy" ? "ok" : o.health === "degraded" ? "med" : "crit");
  hp.style.justifyContent = "center";
  hp.textContent = o.health.toUpperCase();
  $("#lbinfo").textContent = o.lb_ip ? "VIP " + o.lb_ip : "";

  const H = hist || {};
  const podParts = [{ n: "Harvester system", v: o.system_pods }, { n: "Your workloads", v: o.workload_pods, c: "#3ddc91" }];
  const nodeParts = o.nodes.map((n, i) => ({ n: n.name, v: n.pods }));
  const totPods = o.system_pods + o.workload_pods;

  V.innerHTML = `
  <div class="phead">
    <div><h2>Cluster overview</h2><p>Live health, capacity and placement across ${o.nodes_total} node${o.nodes_total > 1 ? "s" : ""}</p></div>
    <div class="row">
      <div class="seg" id="dsec">
        <button class="on" data-d="live">Live</button><button data-d="flow">Flow</button>
      </div>
      <button class="btn pri" onclick="go('deploy')">＋ Deploy container</button>
    </div>
  </div>

  <div class="grid g4 stagger">
    <div class="card glow ${gcls(o.cpu_pct)}">
      <div class="ctitle">Cluster CPU</div>
      ${sparkline(H.cpu && H.cpu.length > 1 ? H.cpu : [o.cpu_pct, o.cpu_pct])}
      <div class="row" style="margin-top:12px"><div class="bignum">${o.cpu_pct}<span style="font-size:20px">%</span></div>
      ${trend(H.cpu)}</div>
      <div class="csub" style="margin-top:7px">${o.cpu_used} of ${o.cpu_cap} cores</div>
    </div>
    <div class="card glow ${gcls(o.mem_pct)}">
      <div class="ctitle">Cluster memory</div>
      ${sparkline(H.mem && H.mem.length > 1 ? H.mem : [o.mem_pct, o.mem_pct])}
      <div class="row" style="margin-top:12px"><div class="bignum">${o.mem_pct}<span style="font-size:20px">%</span></div>
      ${trend(H.mem)}</div>
      <div class="csub" style="margin-top:7px">${o.mem_used_gb} of ${o.mem_cap_gb} GB</div>
    </div>
    <div class="card flat">
      <div class="ctitle">Pod distribution</div><div class="csub">Overhead vs your workloads</div>
      <div class="row" style="margin-top:14px;justify-content:center">${segDonut(podParts, totPods, "Total pods", 168)}</div>
      ${legend(podParts, totPods)}
    </div>
    <div class="card flat">
      <div class="ctitle">Pods per node</div><div class="csub">Scheduling balance</div>
      <div class="row" style="margin-top:14px;justify-content:center">${segDonut(nodeParts, o.nodes_total, "Nodes", 168)}</div>
      ${legend(nodeParts, totPods)}
    </div>
  </div>

  <div class="sec">Node health</div>
  <div class="nodegrid stagger">${o.nodes.map(nodeCard).join("")}</div>

  <div class="sec">Top consumers</div>
  <div class="grid g2">
    <div class="card flat" style="padding:6px 8px">
      <div style="padding:14px 12px 4px"><div class="ctitle">Top CPU</div></div>
      <table class="tbl"><thead><tr><th>Workload</th><th>Node</th><th style="width:150px">CPU</th></tr></thead><tbody>
      ${(o.top_cpu.filter(w => w.cpu > 0).length ? o.top_cpu.filter(w => w.cpu > 0) : o.top_cpu.slice(0, 3)).map(w => `<tr>
        <td><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)}</div></td>
        <td class="small muted">${esc(w.nodes.join(", ") || "—")}</td>
        <td><div class="meter ${sev(w.cpu * 100)}"><span style="width:${Math.min(100, w.cpu * 100)}%"></span></div>
            <div class="dim xs" style="margin-top:4px">${w.cpu} cores</div></td></tr>`).join("")}
      </tbody></table></div>
    <div class="card flat" style="padding:6px 8px">
      <div style="padding:14px 12px 4px"><div class="ctitle">Top memory</div></div>
      <table class="tbl"><thead><tr><th>Workload</th><th>Node</th><th style="width:120px">RAM</th></tr></thead><tbody>
      ${o.top_mem.slice(0, 5).map(w => `<tr>
        <td><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)}</div></td>
        <td class="small muted">${esc(w.nodes.join(", ") || "—")}</td>
        <td><b>${w.mem_mb}</b> <span class="dim xs">MB</span></td></tr>`).join("")}
      </tbody></table></div>
  </div>`;

  $$("#dsec button").forEach(b => b.onclick = () => { if (b.dataset.d === "flow") go("flow"); });
}

function nodeCard(n) {
  const dots = "<i></i>".repeat(Math.min(n.pods_sys, 90)) + '<i class="wl"></i>'.repeat(Math.min(n.pods_wl, 40));
  const bad = n.status !== "Ready";
  return `<div class="card glow ${bad ? "g-bad" : gcls(Math.max(n.cpu_pct, n.mem_pct))}">
    <div class="between">
      <div class="row" style="gap:10px">
        <div class="av ${n.roles.includes("etcd") && n.roles.includes("control-plane") ? "n2" : "n3"}">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
        <div><div style="font-weight:680">${esc(n.name)}</div>
          <div class="dim xs">${n.roles.join(" · ")}</div></div>
      </div>
      <span class="pill ${bad ? "crit" : "low"}">${n.status}</span>
    </div>
    <div class="row" style="margin-top:16px;gap:14px;align-items:flex-start">
      <div style="flex:1;min-width:0">
        <div class="between"><span class="dim xs">CPU</span><span class="small"><b>${n.cpu_pct}%</b> <span class="dim">of ${n.cpu_cap}</span></span></div>
        <div class="meter ${sev(n.cpu_pct)}" style="margin:5px 0 12px"><span style="width:${n.cpu_pct}%"></span></div>
        <div class="between"><span class="dim xs">MEMORY</span><span class="small"><b>${n.mem_pct}%</b> <span class="dim">${n.mem_used_gb}/${n.mem_cap_gb}G</span></span></div>
        <div class="meter ${sev(n.mem_pct)}" style="margin:5px 0 0"><span style="width:${n.mem_pct}%"></span></div>
      </div>
      <div style="text-align:right">
        <div class="midnum">${n.pods}</div><div class="dim xs">pods</div>
        ${n.vms ? `<div class="midnum" style="margin-top:8px">${n.vms}</div><div class="dim xs">VMs</div>` : ""}
      </div>
    </div>
    <div class="podgrid">${dots}</div>
    <div style="margin-top:12px">
      ${n.igpu ? '<span class="tag gpu"><span class="dt"></span>iGPU</span>' : ""}
      ${n.workloads.length ? n.workloads.map(w => `<span class="tag">${esc(w)}</span>`).join("")
        : '<span class="dim xs">no workloads scheduled here</span>'}
    </div></div>`;
}

/* ---------------- FLOW ---------------- */
const ROB = r => r === "healthy" ? "#3ddc91" : r === "degraded" ? "#ffb020" : "#ff4d4f";
async function viewFlow() {
  const f = await api("/api/flow");
  STATE.data.flow = f;
  const links = [];
  f.nodes.forEach(n => n.copies.forEach(c => links.push([n.id + "|" + c.vid, c.vid, "#a78bfa"])));
  f.workloads.forEach(w => w.claims.forEach(c => { if (c.vid) links.push([c.vid, w.id, "#7dd3fc"]); }));
  f.workloads.forEach(w => w.ports.forEach(p => { if (p.vip) links.push([w.id, "i:" + p.vip, "#ffb020"]); }));
  STATE.data.alinks = links;

  V.innerHTML = `<div class="phead">
      <div><h2>Architecture</h2><p>Where every replica lives, what mounts it, and how it is reached</p></div>
      <div class="row">
        <span class="tag"><span class="dt" style="background:#a78bfa"></span>replica copy</span>
        <span class="tag"><span class="dt" style="background:#7dd3fc"></span>mount</span>
        <span class="tag"><span class="dt" style="background:#ffb020"></span>port</span>
      </div></div>
    <div class="flowwrap"><svg id="archsvg"></svg><div class="arch">

      <div class="acol"><h4>Nodes &amp; replica copies</h4>
      ${f.nodes.map(n => `<div class="abox" data-id="${esc(n.id)}">
        <div class="ahd"><div class="av n2">${esc(n.name.replace(/[^0-9a-z]/gi, "").slice(-2).toUpperCase())}</div>
          <div class="anm">${esc(n.name)}</div><span class="badge">${n.copies.length}</span></div>
        <div class="copies">${n.copies.map(c => `<span class="copy" data-tgt="${esc(c.vid)}" id="${esc(n.id + "|" + c.vid)}">
          <span class="cd" style="background:${c.running ? "#3ddc91" : "#ffb020"}"></span>${esc(c.vol)}</span>`).join("")
          || '<span class="dim xs">no replicas here</span>'}</div></div>`).join("")}
      </div>

      <div class="acol"><h4>Longhorn volumes</h4>
      ${f.volumes.map(v => `<div class="abox" data-id="${esc(v.id)}" id="${esc(v.id)}">
        <div class="ahd"><span class="cd" style="width:7px;height:7px;border-radius:50%;background:${ROB(v.robustness)}"></span>
          <div class="anm">${esc(v.name)}</div><span class="badge">${v.replicas}×</span></div>
        <div class="row" style="gap:6px">
          <span class="tag">${v.size_gb}G</span>
          <span class="tag ${v.robustness === "healthy" ? "ok" : v.robustness === "degraded" ? "warn" : "bad"}">${esc(v.robustness)}</span>
          ${v.attached ? `<span class="tag info">on ${esc(v.attached.replace("harvester-", ""))}</span>` : ""}
        </div></div>`).join("") || '<div class="dim xs" style="text-align:center">no volumes</div>'}
      </div>

      <div class="acol"><h4>Workloads &amp; their ports</h4>
      ${f.workloads.map(w => `<div class="abox" data-id="${esc(w.id)}" id="${esc(w.id)}">
        <div class="ahd"><div class="av">${esc(w.name.slice(0, 2).toUpperCase())}</div>
          <div style="flex:1;min-width:0"><div class="anm">${esc(w.name)}</div>
            <div class="dim xs">${esc(w.kind === "vm" ? "virtual machine" : w.node)}</div></div>
          ${w.gpu ? '<span class="tag gpu">iGPU</span>' : ""}</div>
        <div class="row" style="gap:8px;justify-content:space-between">
          ${w.uptime ? upChip(w.uptime) : '<span class="dim xs">—</span>'}
          <span class="dim xs mono">${(w.cpu || 0).toFixed(2)} cores · ${w.mem_mb || 0}MB</span></div>
        <div class="livebar"><span style="width:${Math.min(100, (w.cpu || 0) * 120)}%"></span></div>
        ${w.claims.length ? `<div class="copies">${w.claims.map(c => `<span class="mchip">▤ ${esc(c.pvc)}</span>`).join("")}</div>` : ""}
        ${w.ports.length ? `<div class="portchips">${w.ports.map(p => p.vip
              ? `<span class="plink" title="Open ${esc(svcUrl(p.vip, p.port))}" onclick="event.stopPropagation();openSvc('${esc(p.vip)}',${p.port})">${esc(p.name)}:${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`
              : `<span class="pchip">${esc(p.name)}:${p.port}</span>`).join("")}</div>`
          : '<div class="portchips"><span class="dim xs">no exposed ports</span></div>'}
      </div>`).join("")}
      </div>

      <div class="acol"><h4>Access</h4>
      ${f.vips.map(v => `<div class="abox vipbox" data-id="${esc(v.id)}" id="${esc(v.id)}">
        <div class="dim xs" style="margin-bottom:5px">VIRTUAL IP</div>
        <div class="vipip">${esc(v.ip)}</div>
        <div class="portchips" style="justify-content:center;border-top:1px solid rgba(255,255,255,.06)">
          ${v.ports.map(p => `<span class="plink" title="Open ${esc(svcUrl(v.ip, p.port))} (${esc(p.app)})" onclick="event.stopPropagation();openSvc('${esc(v.ip)}',${p.port})">${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`).join("")}</div>
        <div class="dim xs" style="margin-top:8px">${v.ports.length} service port${v.ports.length === 1 ? "" : "s"} · kube-vip</div>
      </div>`).join("") || '<div class="dim xs" style="text-align:center">no load balancer IPs</div>'}
      </div>
    </div></div>`;
  requestAnimationFrame(() => drawArch());
  $$(".abox").forEach(b => {
    b.onmouseenter = () => archHighlight(b.dataset.id);
    b.onmouseleave = () => { $$(".abox").forEach(x => { x.style.opacity = "1"; x.classList.remove("sel"); }); drawArch(); };
  });
}
function drawArch(keep) {
  const svg = $("#archsvg"), wrap = $(".flowwrap");
  if (!svg || !wrap) return;
  const R = wrap.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  const P = id => {
    const el = document.getElementById(id); if (!el) return null;
    const b = el.getBoundingClientRect();
    return { l: b.left - R.left, r: b.right - R.left, y: b.top - R.top + b.height / 2 };
  };
  svg.innerHTML = (STATE.data.alinks || []).map(([a, b, col]) => {
    const A = P(a), B = P(b); if (!A || !B) return "";
    const on = !keep || (keep.has(a) && keep.has(b));
    const x0 = A.r, x1 = B.l, cx = (x0 + x1) / 2;
    const d = `M ${x0} ${A.y} C ${cx} ${A.y} ${cx} ${B.y} ${x1} ${B.y}`;
    const anim = SET.motion !== "off";
    return `<path d="${d}" style="stroke:${col};opacity:${on ? .3 : .05};stroke-width:${on ? 1.4 : 1}"/>` +
      (anim && on ? `<path class="flowline glowpath" d="${d}"
          style="stroke:${col};opacity:.85;stroke-width:1.6;animation-duration:${(1.1 + Math.random() * .9).toFixed(2)}s"/>` : "");
  }).join("");
}
function archHighlight(id) {
  const links = STATE.data.alinks || [];
  const keep = new Set([id]);
  // a replica chip id is "node|volume" — pull in its node box too
  let grow = true;
  while (grow) {
    grow = false;
    links.forEach(([a, b]) => {
      if (keep.has(a) && !keep.has(b)) { keep.add(b); grow = true; }
      if (keep.has(b) && !keep.has(a)) { keep.add(a); grow = true; }
    });
    [...keep].forEach(k => { if (k.includes("|")) { const n = k.split("|")[0]; if (!keep.has(n)) { keep.add(n); grow = true; } } });
  }
  drawArch(keep);
  $$(".abox").forEach(x => {
    const rel = keep.has(x.dataset.id) || [...keep].some(k => k.startsWith(x.dataset.id + "|"));
    x.style.opacity = rel ? "1" : ".28";
    x.classList.toggle("sel", x.dataset.id === id);
  });
}
function drawLinks(links, keep) {
  const svg = $("#flowsvg"), wrap = $(".flowwrap");
  if (!svg || !wrap) return;
  const R = wrap.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  const pos = {};
  $$(".fnode").forEach(n => {
    const b = n.getBoundingClientRect();
    pos[n.dataset.id] = { l: b.left - R.left, r: b.right - R.left, y: b.top - R.top + b.height / 2 };
  });
  const max = Math.max(...links.map(l => l.value), 1);
  svg.innerHTML = links.map(l => {
    const a = pos[l.from], b = pos[l.to];
    if (!a || !b) return "";
    const on = !keep || (keep.has(l.from) && keep.has(l.to));
    const x0 = a.r, x1 = b.l, cx = (x0 + x1) / 2;
    const w = (.8 + l.value / max * 2).toFixed(2);
    const op = on ? (.1 + l.value / max * .3).toFixed(3) : .035;
    return `<path d="M ${x0} ${a.y} C ${cx} ${a.y} ${cx} ${b.y} ${x1} ${b.y}"
      style="stroke-width:${on ? w : 1};stroke:rgba(255,255,255,${op})"/>`;
  }).join("");
}
window.addEventListener("resize", () => { if (STATE.view === "flow") drawArch(); });

/* ---------------- WORKLOADS ---------------- */
async function viewWorkloads() {
  STATE.data.wl = await api("/api/workloads");
  renderWorkloads();
}
function renderWorkloads() {
  const q = STATE.q.toLowerCase();
  const rows = (STATE.data.wl || []).filter(x => !q || x.name.includes(q) || x.ns.includes(q) ||
    x.images.join(" ").toLowerCase().includes(q) || x.nodes.join(" ").includes(q));
  V.innerHTML = `<div class="phead">
      <div><h2>Containers</h2><p>${rows.length} workload${rows.length === 1 ? "" : "s"}${q ? ` matching “${esc(q)}”` : ""} · Harvester system pods hidden</p></div>
      <button class="btn pri" onclick="go('deploy')">＋ Deploy container</button></div>
    <div class="card flat" style="padding:6px 8px"><table class="tbl"><thead><tr>
      <th>Name</th><th>Image</th><th>Node</th><th>Uptime</th><th>Access</th><th style="width:140px">CPU</th><th>RAM</th><th>State</th><th></th>
    </tr></thead><tbody>${rows.map(w => {
      const ok = w.ready === w.desired && w.desired > 0, off = w.desired === 0;
      return `<tr>
      <td><div class="row" style="gap:9px"><div class="av">${esc(w.name.slice(0, 2).toUpperCase())}</div>
        <div><b>${esc(w.name)}</b><div class="dim xs">${esc(w.ns)}</div></div></div></td>
      <td class="small muted" style="max-width:250px">${w.images.map(esc).join("<br>")}</td>
      <td class="small">${w.nodes.map(esc).join(", ") || "—"}</td>
      <td class="small">${w.uptime ? upChip(w.uptime) : '<span class="dim">—</span>'}</td>
      <td class="small">${w.ports.map(p => p.ip
          ? `<span class="plink" title="Open ${esc(svcUrl(p.ip, p.port))}" onclick="openSvc('${esc(p.ip)}',${p.port})">${p.port}<svg class="ext" width="9" height="9"><use href="#i-ext"/></svg></span>`
          : `<span class="tag">${p.port}</span>`).join("") || '<span class="dim">—</span>'}</td>
      <td><div class="meter ${sev(w.cpu * 100)}"><span style="width:${Math.min(100, w.cpu * 100)}%"></span></div>
          <div class="dim xs" style="margin-top:4px">${w.cpu} cores</div></td>
      <td><b>${w.mem_mb}</b> <span class="dim xs">MB</span></td>
      <td><span class="pill ${ok ? "ok" : off ? "low" : "crit"}">${w.ready}/${w.desired}</span>
          ${w.gpu ? '<div style="margin-top:5px"><span class="tag gpu">iGPU</span></div>' : ""}</td>
      <td><div class="row" style="gap:6px;flex-wrap:nowrap">
        <button class="btn sm" onclick="wlLogs('${w.ns}','${w.pods[0] ? w.pods[0].name : ""}')">Logs</button>
        <button class="btn sm" onclick="wlRestart('${w.ns}','${w.name}')">↻</button>
        ${off ? `<button class="btn sm" onclick="wlScale('${w.ns}','${w.name}',1)">Start</button>`
               : `<button class="btn sm" onclick="wlScale('${w.ns}','${w.name}',0)">Stop</button>`}
        <button class="btn sm danger" onclick="wlDelete('${w.ns}','${w.name}')">✕</button>
      </div></td></tr>`;
    }).join("") || `<tr><td colspan=9 class="empty">nothing here yet</td></tr>`}</tbody></table></div>`;
}
window.wlScale = async (ns, name, n) => {
  try { await api("/api/scale", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name, replicas: n }) });
    toast(`${name} ${n ? "started" : "stopped"}`, "ok"); setTimeout(() => go("workloads"), 900); } catch (e) { toast(e.message, "bad"); }
};
window.wlRestart = async (ns, name) => {
  try { await api("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ns, name }) });
    toast(`${name} restarting`, "ok"); setTimeout(() => go("workloads"), 1300); } catch (e) { toast(e.message, "bad"); }
};
window.wlDelete = async (ns, name) => {
  if (!confirm(`Delete "${name}" in ${name === ns ? ns : ns}?\n\nRemoves the Deployment and Service.\nPersistent volumes are kept.`)) return;
  try { await api(`/api/workload/${ns}/${name}`, { method: "DELETE" }); toast(`${name} deleted`, "ok"); setTimeout(() => go("workloads"), 900); }
  catch (e) { toast(e.message, "bad"); }
};
window.wlLogs = async (ns, pod) => {
  if (!pod) return toast("no running pod", "bad");
  modal("Logs · " + pod, `<div class="empty"><span class="spin2"></span>loading</div>`);
  try { const t = await api(`/api/logs?ns=${ns}&pod=${pod}`); $("#mbody").innerHTML = `<pre>${esc(t) || "(empty)"}</pre>`; }
  catch (e) { $("#mbody").innerHTML = `<pre>${esc(e.message)}</pre>`; }
};

/* ---------------- DEPLOY ---------------- */
let DCFG = { name: "", image: "", namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi", ports: [], env: {}, volumes: [], gpu: false };
async function viewDeploy(pre) {
  if (pre) DCFG = Object.assign({ namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi", ports: [], env: {}, volumes: [], gpu: false }, pre);
  const nss = await api("/api/namespaces").catch(() => ["lab"]);
  V.innerHTML = `<div class="phead"><div><h2>Deploy a container</h2>
      <p>Point at any Docker image, map ports and storage — HarvUI builds the Kubernetes objects for you</p></div></div>
  <div class="split">
    <div class="card flat">
      <div class="f"><label>Name</label><input type="text" id="d_name" value="${esc(DCFG.name)}" placeholder="my-app"></div>
      <div class="f"><label>Docker image</label><input type="text" id="d_image" value="${esc(DCFG.image)}" placeholder="nginx:alpine  ·  ghcr.io/user/app:tag"></div>
      <div class="f2">
        <div class="f"><label>Namespace</label><select id="d_ns">${nss.map(n => `<option ${n === DCFG.namespace ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
        <div class="f"><label>Replicas</label><input type="number" id="d_rep" value="${DCFG.replicas}" min="0" max="5"></div>
      </div>
      <div class="f2">
        <div class="f"><label>CPU request</label><input type="text" id="d_cpu" value="${esc(DCFG.cpu)}"></div>
        <div class="f"><label>Memory request</label><input type="text" id="d_mem" value="${esc(DCFG.memory)}"></div>
      </div>
      <label class="switch"><input type="checkbox" id="d_gpu" ${DCFG.gpu ? "checked" : ""}> Give this container the Intel iGPU <span class="tag gpu">/dev/dri</span></label>
      <div class="sec">Ports</div><div id="d_ports"></div><button class="btn sm" onclick="addPort()">＋ add port</button>
      <div class="sec">Storage</div><div id="d_vols"></div><button class="btn sm" onclick="addVol()">＋ add volume</button>
      <div class="sec">Environment</div><div id="d_env"></div><button class="btn sm" onclick="addEnv()">＋ add variable</button>
      <div class="row" style="margin-top:24px">
        <button class="btn pri" onclick="doDeploy()">Deploy container</button>
        <button class="btn" onclick="previewYaml()">Preview manifest</button>
      </div>
    </div>
    <div class="card flat"><div class="ctitle">Configuration</div><div class="csub">Live summary</div>
      <div id="d_summary" style="margin-top:14px"></div>
      <button class="btn pri wide" style="margin-top:18px" onclick="doDeploy()">Deploy</button></div>
  </div>`;
  renderPorts(); renderVols(); renderEnv(); syncSummary();
  ["d_name", "d_image", "d_ns", "d_rep", "d_cpu", "d_mem", "d_gpu"].forEach(id => {
    const el = $("#" + id); if (!el) return;
    el.addEventListener("input", syncSummary); el.addEventListener("change", syncSummary);
  });
}
function collect() {
  DCFG.name = $("#d_name").value.trim(); DCFG.image = $("#d_image").value.trim();
  DCFG.namespace = $("#d_ns").value; DCFG.replicas = +$("#d_rep").value;
  DCFG.cpu = $("#d_cpu").value.trim(); DCFG.memory = $("#d_mem").value.trim(); DCFG.gpu = $("#d_gpu").checked;
  DCFG.ports = $$("#d_ports .f3").map(r => ({ container: +$(".pc", r).value, host: +$(".ph", r).value || +$(".pc", r).value, expose: $(".pe", r).checked }));
  DCFG.volumes = $$("#d_vols .f3").map(r => ({ path: $(".vp", r).value.trim(), source: $(".vs", r).value.trim(),
    type: $(".vt", r).value, size_gb: 5, create: $(".vt", r).value === "pvc" }));
  DCFG.env = {}; $$("#d_env .f3").forEach(r => { const k = $(".ek", r).value.trim(); if (k) DCFG.env[k] = $(".ev", r).value; });
  return DCFG;
}
function syncSummary() {
  const c = collect();
  const row = (i, l, v) => `<div class="drow"><div class="di">${i}</div><div class="dl">${l}</div><div class="dv">${v}</div></div>`;
  $("#d_summary").innerHTML =
    row("◈", "Name", c.name ? `<b>${esc(c.name)}</b>` : '<span class="dim">—</span>') +
    row("❏", "Image", c.image ? `<span class="small">${esc(c.image)}</span>` : '<span class="dim">—</span>') +
    row("⌗", "Namespace", esc(c.namespace)) +
    row("⧉", "Replicas", c.replicas) +
    row("◴", "Requests", `<span class="small">${esc(c.cpu)} · ${esc(c.memory)}</span>`) +
    row("▤", "iGPU", c.gpu ? '<span class="tag gpu">enabled</span>' : '<span class="dim">no</span>') +
    row("⇄", "Ports", c.ports.length ? c.ports.map(p => `<span class="tag ${p.expose ? "info" : ""}">${p.host}→${p.container}</span>`).join("") : '<span class="dim">—</span>') +
    row("▥", "Storage", c.volumes.length ? c.volumes.map(v => `<span class="tag">${esc(v.source || "?")}</span>`).join("") : '<span class="dim">—</span>') +
    row("≡", "Env vars", Object.keys(c.env).length ? `<span class="tag">${Object.keys(c.env).length} set</span>` : '<span class="dim">—</span>');
}
function addPort(cp = "", hp = "", ex = true) {
  const d = document.createElement("div"); d.className = "f3";
  d.innerHTML = `<div><label>Container port</label><input class="pc" type="number" value="${cp}"></div>
    <div><label>Exposed port</label><input class="ph" type="number" value="${hp}"></div>
    <label class="switch" style="margin:0 0 10px"><input class="pe" type="checkbox" ${ex ? "checked" : ""}>LB</label>`;
  $("#d_ports").appendChild(d); d.addEventListener("input", syncSummary); syncSummary();
}
function addVol(path = "", src = "", type = "pvc") {
  const d = document.createElement("div"); d.className = "f3";
  d.innerHTML = `<div><label>Mount path</label><input class="vp" type="text" value="${esc(path)}" placeholder="/data"></div>
    <div><label>Source</label><input class="vs" type="text" value="${esc(src)}" placeholder="volume name"></div>
    <div><label>Type</label><select class="vt"><option value="pvc" ${type === "pvc" ? "selected" : ""}>New volume</option>
      <option value="host" ${type === "host" ? "selected" : ""}>Host path</option></select></div>`;
  $("#d_vols").appendChild(d); d.addEventListener("input", syncSummary); syncSummary();
}
function addEnv(k = "", v = "") {
  const d = document.createElement("div"); d.className = "f3";
  d.innerHTML = `<div><label>Key</label><input class="ek" type="text" value="${esc(k)}"></div>
    <div><label>Value</label><input class="ev" type="text" value="${esc(v)}"></div><div></div>`;
  $("#d_env").appendChild(d); d.addEventListener("input", syncSummary); syncSummary();
}
function renderPorts() { $("#d_ports").innerHTML = ""; (DCFG.ports || []).forEach(p => addPort(p.container, p.host, p.expose !== false)); }
function renderVols() { $("#d_vols").innerHTML = ""; (DCFG.volumes || []).forEach(v => addVol(v.path, v.source, v.type)); }
function renderEnv() { $("#d_env").innerHTML = ""; Object.entries(DCFG.env || {}).forEach(([k, v]) => addEnv(k, v)); }
window.addPort = addPort; window.addVol = addVol; window.addEnv = addEnv;
window.previewYaml = async () => {
  try { const r = await api("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()) });
    modal("Manifest preview", `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`); } catch (e) { toast(e.message, "bad"); }
};
window.doDeploy = async () => {
  const c = collect();
  if (!c.name || !c.image) return toast("name and image are required", "bad");
  try { await api("/api/deploy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(c) });
    toast(`${c.name} deployed`, "ok"); go("workloads"); } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- APP STORE ---------------- */
async function viewStore() {
  V.innerHTML = `<div class="phead">
      <div><h2>App Store</h2><p>Unraid Community Applications catalogue — deployed as Kubernetes workloads</p></div>
      <div class="row"><input class="search" id="s_q" placeholder="plex, nextcloud, jellyfin…" value="${esc(STATE.q)}" style="width:280px;padding-left:16px">
      <button class="btn pri" onclick="storeSearch()">Search</button></div></div>
    <div id="s_res"><div class="empty">Search the catalogue — it is fetched live from the Unraid CA feed.</div></div>`;
  $("#s_q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); storeSearch(); } });
  if (STATE.q) storeSearch();
}
window.storeSearch = async () => {
  const q = $("#s_q").value.trim();
  $("#s_res").innerHTML = `<div class="empty"><span class="spin2"></span>searching catalogue…</div>`;
  try {
    const r = await api("/api/appstore?q=" + encodeURIComponent(q));
    STATE.data.apps = r.apps;
    $("#s_res").innerHTML = r.apps.length
      ? `<div class="dim small" style="margin-bottom:12px">${r.total} match${r.total === 1 ? "" : "es"} · showing ${r.apps.length}</div>
        <div class="apps stagger">${r.apps.map((a, i) => `<div class="card app">
          <div class="row" style="gap:11px">${a.icon ? `<img class="ico" src="${esc(a.icon)}" onerror="this.style.display='none'">` : ""}
            <div style="min-width:0"><div class="nm">${esc(a.name)}</div>
            ${a.cat ? `<span class="tag">${esc(a.cat.split(" ")[0])}</span>` : ""}</div></div>
          <div class="ds">${esc(a.desc || "No description provided.")}</div>
          <div class="rp">${esc(a.repo)}</div>
          <button class="btn pri wide" onclick="storeInstall(${i})">Configure &amp; deploy</button></div>`).join("")}</div>`
      : `<div class="empty">nothing matched “${esc(q)}”</div>`;
  } catch (e) { $("#s_res").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};
window.storeInstall = i => {
  const a = STATE.data.apps[i];
  const at = c => c["@attributes"] || c;
  const cf = (a.config || []).filter(c => c && typeof c === "object");
  const ports = cf.filter(c => at(c).Type === "Port").map(c => ({ container: +at(c).Target, host: +at(c).Target, expose: true })).filter(p => p.container);
  const env = {}; cf.filter(c => at(c).Type === "Variable").forEach(c => { if (at(c).Target) env[at(c).Target] = c.value || at(c).Default || ""; });
  const vols = cf.filter(c => at(c).Type === "Path").map(c => ({ path: at(c).Target, source: "", type: "pvc" })).filter(v => v.path);
  const name = a.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  STATE.view = "deploy";
  $$("#nav a").forEach(x => x.classList.toggle("on", x.dataset.view === "deploy"));
  $("#title").textContent = "Deploy"; $("#crumb").textContent = "workloads";
  viewDeploy({ name, image: a.repo, ports, env, volumes: vols.map(v => ({ ...v, source: name + "-data" })) });
  toast(`"${a.name}" loaded — check storage paths before deploying`);
};

/* ---------------- SHARES ---------------- */
async function viewShares() {
  const sh = await api("/api/shares").catch(() => []);
  const ip = (STATE.data.ov && STATE.data.ov.lb_ip) || "";
  V.innerHTML = `<div class="phead"><div><h2>Network shares</h2>
    <p>SMB shares backed by replicated Longhorn volumes — mount them straight from Windows</p></div></div>
  <div class="split">
    <div class="card flat" style="padding:6px 8px">
      <table class="tbl"><thead><tr><th>Share</th><th>Size</th><th>Access</th><th>UNC path</th><th></th></tr></thead><tbody>
      ${sh.map(s => `<tr><td><div class="row" style="gap:9px"><div class="av n2">${esc(s.name.slice(0, 2).toUpperCase())}</div>
        <div><b>${esc(s.name)}</b><div class="dim xs">${esc(s.created || "")}</div></div></div></td>
        <td><b>${s.size_gb}</b> <span class="dim xs">GB</span></td>
        <td>${s.public ? '<span class="pill med">guest</span>' : `<span class="pill low">${esc(s.user)}</span>`}</td>
        <td class="small muted">\\\\${esc(ip)}\\${esc(s.name)}</td></tr>`).join("")
        || `<tr><td colspan=5 class="empty">no shares yet — create one →</td></tr>`}</tbody></table></div>
    <div class="card flat"><div class="ctitle">New share</div><div class="csub">Creates a Longhorn volume and adds it to samba</div>
      <div class="f" style="margin-top:16px"><label>Share name</label><input type="text" id="sh_name" placeholder="media"></div>
      <div class="f2"><div class="f"><label>Size (GB)</label><input type="number" id="sh_size" value="10" min="1"></div>
        <div class="f"><label>Username</label><input type="text" id="sh_user" value="lab"></div></div>
      <div class="f"><label>Password</label><input type="text" id="sh_pass" value="LabPass2026"></div>
      <label class="switch"><input type="checkbox" id="sh_pub"> Allow guest access</label>
      <button class="btn pri wide" onclick="mkShare()">Create share</button>
      <div class="dim xs" style="margin-top:12px">Creating a share restarts samba, so open SMB sessions drop briefly.</div></div>
  </div>`;
}
window.mkShare = async () => {
  const name = $("#sh_name").value.trim();
  if (!/^[a-z0-9-]{2,30}$/.test(name)) return toast("lowercase letters, numbers and dashes only", "bad");
  try {
    await api("/api/shares", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, size_gb: +$("#sh_size").value, user: $("#sh_user").value.trim(),
        password: $("#sh_pass").value, public: $("#sh_pub").checked }) });
    toast(`share "${name}" created`, "ok"); viewShares();
  } catch (e) { toast(e.message, "bad"); }
};

/* ---------------- STORAGE ---------------- */
async function viewStorage() {
  const v = await api("/api/volumes");
  const q = STATE.q.toLowerCase();
  const rows = v.filter(x => !q || x.name.includes(q) || (x.node || "").includes(q));
  const healthy = v.filter(x => x.robustness === "healthy").length;
  const parts = [{ n: "Healthy", v: healthy, c: "#3ddc91" },
    { n: "Degraded", v: v.filter(x => x.robustness === "degraded").length, c: "#ffb020" },
    { n: "Faulted", v: v.filter(x => x.robustness === "faulted").length, c: "#ff4d4f" }].filter(p => p.v);
  V.innerHTML = `<div class="phead"><div><h2>Volumes</h2><p>${v.length} Longhorn volume${v.length === 1 ? "" : "s"} · replicated block storage</p></div></div>
  <div class="grid g4" style="margin-bottom:18px">
    <div class="card flat"><div class="ctitle">Replication health</div>
      <div class="row" style="margin-top:12px;justify-content:center">${segDonut(parts, v.length, "volumes", 156)}</div>
      ${legend(parts, v.length)}</div>
    <div class="card glow g-info" style="grid-column:span 3">
      <div class="ctitle">Capacity</div>
      <div class="row" style="margin-top:14px;gap:34px">
        <div><div class="bignum">${v.reduce((s, x) => s + x.size_gb, 0).toFixed(0)}<span style="font-size:19px">GB</span></div><div class="csub">provisioned</div></div>
        <div><div class="bignum">${v.reduce((s, x) => s + x.actual_gb, 0).toFixed(1)}<span style="font-size:19px">GB</span></div><div class="csub">actually used</div></div>
        <div><div class="bignum">${v.filter(x => x.state === "attached").length}</div><div class="csub">attached</div></div>
      </div></div>
  </div>
  <div class="card flat" style="padding:6px 8px"><table class="tbl"><thead><tr>
   <th>Volume</th><th>State</th><th>Health</th><th>Replicas</th><th>Size</th><th>Used</th><th>Attached to</th>
   </tr></thead><tbody>${rows.map(x => `<tr>
     <td class="small"><b>${esc(x.name.replace("pvc-", "").slice(0, 14))}…</b><div class="dim xs">${esc(x.name)}</div></td>
     <td><span class="pill ${x.state === "attached" ? "low" : "neutral"}">${esc(x.state)}</span></td>
     <td><span class="pill ${x.robustness === "healthy" ? "ok" : x.robustness === "degraded" ? "med" : "crit"}">${esc(x.robustness)}</span></td>
     <td><b>${x.replicas}</b></td><td>${x.size_gb} GB</td><td class="dim">${x.actual_gb} GB</td>
     <td class="small">${esc(x.node || "—")}</td></tr>`).join("") || `<tr><td colspan=7 class="empty">none</td></tr>`}
   </tbody></table></div>`;
}

/* ---------------- NODES ---------------- */
async function viewNodes() {
  const n = await api("/api/nodes");
  V.innerHTML = `<div class="phead"><div><h2>Nodes</h2><p>${n.length} node${n.length === 1 ? "" : "s"} in this Harvester cluster</p></div></div>
   <div class="nodegrid stagger">${n.map(nodeCard).join("")}</div>
   <div class="sec">Detail</div>
   <div class="card flat" style="padding:6px 8px"><table class="tbl"><thead><tr>
     <th>Node</th><th>Roles</th><th>CPU</th><th>Memory</th><th>Pods</th><th>iGPU</th><th>OS</th></tr></thead><tbody>
   ${n.map(x => `<tr><td><b>${esc(x.name)}</b><div class="dim xs">${esc(x.kernel)}</div></td>
     <td>${x.roles.map(r => `<span class="tag">${esc(r)}</span>`).join("")}</td>
     <td style="min-width:130px"><div class="meter ${sev(x.cpu_pct)}"><span style="width:${x.cpu_pct}%"></span></div>
        <div class="dim xs" style="margin-top:4px">${x.cpu_pct}% of ${x.cpu_cap}</div></td>
     <td style="min-width:130px"><div class="meter ${sev(x.mem_pct)}"><span style="width:${x.mem_pct}%"></span></div>
        <div class="dim xs" style="margin-top:4px">${x.mem_used_gb}/${x.mem_cap_gb} GB</div></td>
     <td><b>${x.pods_wl}</b> <span class="dim xs">yours</span><div class="dim xs">${x.pods_sys} system</div></td>
     <td>${x.igpu ? '<span class="tag gpu">yes</span>' : '<span class="dim">—</span>'}</td>
     <td class="small muted">${esc(x.os)}</td></tr>`).join("")}</tbody></table></div>`;
}

/* ---------------- EVENTS ---------------- */
async function viewEvents() {
  const e = await api("/api/events");
  V.innerHTML = `<div class="phead"><div><h2>Events</h2><p>Most recent cluster activity</p></div></div>
  <div class="card flat" style="padding:6px 8px"><table class="tbl"><thead><tr>
    <th>Object</th><th>Reason</th><th>Message</th><th>When</th></tr></thead><tbody>
  ${e.map(x => `<tr><td><b>${esc(x.obj)}</b><div class="dim xs">${esc(x.ns)} · ${esc(x.kind)}</div></td>
    <td><span class="pill ${x.type === "Warning" ? "med" : "low"}">${esc(x.reason)}</span></td>
    <td class="small muted">${esc(x.msg)}</td>
    <td class="dim xs">${esc((x.time || "").replace("T", " ").replace("Z", ""))}</td></tr>`).join("")
    || `<tr><td colspan=4 class="empty">no events</td></tr>`}</tbody></table></div>`;
}

/* ---------------- router ---------------- */
const VIEWS = {
  dash: ["Dashboard", "overview", viewDash], flow: ["Architecture", "overview", viewFlow],
  nodes: ["Nodes", "overview", viewNodes], workloads: ["Containers", "workloads", viewWorkloads],
  deploy: ["Deploy", "workloads", () => viewDeploy()], store: ["App Store", "workloads", viewStore],
  shares: ["Network Shares", "storage", viewShares], storage: ["Volumes", "storage", viewStorage],
  events: ["Events", "system", viewEvents],
};
function go(v) {
  STATE.view = v;
  const [t, c, fn] = VIEWS[v];
  $("#title").textContent = t; $("#crumb").textContent = c;
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.view === v));
  V.innerHTML = `<div class="empty"><span class="spin2"></span>loading…</div>`;
  Promise.resolve(fn()).catch(e => V.innerHTML = `<div class="empty">${esc(e.message)}</div>`);
}
window.go = go;
$$("#nav a").forEach(a => a.onclick = () => go(a.dataset.view));
$("#refresh").onclick = e => {
  e.currentTarget.classList.add("spinning");
  setTimeout(() => e.currentTarget.classList.remove("spinning"), 900);
  go(STATE.view);
};
$("#globalSearch").addEventListener("input", e => {
  STATE.q = e.target.value.trim();
  if (STATE.view === "workloads") renderWorkloads();
  else if (STATE.view === "storage") viewStorage();
});
go("dash");
setInterval(() => { if (STATE.view === "dash") viewDash().catch(() => {}); }, 20000);

window.rmShare = async name => {
  if (!confirm(`Remove share "${name}" from samba?

The Longhorn volume and its data are kept.`)) return;
  try { await api("/api/shares/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast(`share "${name}" removed`, "ok"); viewShares(); } catch (e) { toast(e.message, "bad"); }
};

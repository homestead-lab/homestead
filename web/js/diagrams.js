/* Pictures of how things connect, in the vocabulary of the README's graphic:
   a node is a column, a VIP a blue pill, an app an amber block and a VM an
   outlined one, a volume green, and dashed for whatever is moving, empty or
   not there yet. Each is drawn from live data as SVG in the page's own
   colours, so it follows the theme; colour still means state. */
(function (root) {
  "use strict";
  const e = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  // Drawn at up to a quarter over its own size, never blown up across a wide dialog.
  const svg = (w, h, body, label) => `<svg class="dg" viewBox="0 0 ${w} ${h}" style="max-width:${Math.round(w * 1.25)}px" role="img" aria-label="${e(label)}"><title>${e(label)}</title>`
    + `<defs><marker id="dg-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M1 1 9 5 1 9z" class="dg-arrowhead"/></marker></defs>${body}</svg>`;
  const text = (x, y, s, cls = "", anchor = "start") => `<text x="${x}" y="${y}" class="dg-t ${cls}" text-anchor="${anchor}">${e(s)}</text>`;
  const box = (x, y, w, h, cls, r = 6) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" class="${cls}"/>`;
  const line = (d, cls = "dg-line", arrow = true) => `<path d="${d}" class="${cls}"${arrow ? ' marker-end="url(#dg-arrow)"' : ""}/>`;
  const clip = (s, n) => (s = String(s ?? "")).length > n ? s.slice(0, n - 1) + "…" : s;
  const app = (x, y, w, name) => box(x, y, w, 20, "dg-app", 5) + text(x + 8, y + 14, clip(name, Math.floor(w / 6.4)), "dg-s");
  const vm = (x, y, w, name) => box(x, y, w, 20, "dg-vm", 5) + text(x + 8, y + 14, clip(name, Math.floor((w - 26) / 6.4)), "dg-s")
    + box(x + w - 24, y + 5, 18, 10, "dg-vm-tag", 3) + text(x + w - 15, y + 13, "VM", "dg-xs dg-on-amber", "middle");
  const pill = (cx, y, ip, w, dashed) => box(cx - w / 2, y, w, 24, dashed ? "dg-vip dg-dash" : "dg-vip", 12)
    + `<circle cx="${cx - w / 2 + 13}" cy="${y + 12}" r="4" class="dg-vip-dot"/>` + text(cx - w / 2 + 23, y + 16, "VIP", "dg-xs dg-blue dg-b")
    + text(cx - w / 2 + 47, y + 16.5, ip, "dg-s dg-mono");
  const figure = (inner, caption) => `<figure class="diagram">${inner}${caption ? `<figcaption>${e(caption)}</figcaption>` : ""}</figure>`;

  /* A node: the VIPs it answers for, what runs on it, and its drives. */
  function node(n) {
    const duties = n.duties || {};
    const vips = [...new Set([...(duties.management_vip || []), ...(duties.vips || [])])];
    const apps = (n.workloads || []).slice(0, 6), more = Math.max(0, (n.workloads || []).length - apps.length);
    const vms = +n.vms || 0;
    const drives = (n.disks || []).filter(d => d.device !== "longhorn" || d.lh_size_gb).slice(0, 4);
    const colH = 44 + Math.max(1, apps.length + (more ? 1 : 0) + (vms ? 1 : 0)) * 24;
    let b = "", top = vips.length ? 40 : 6;
    // The first VIP it answers for; any others are counted beside it.
    if (vips.length) {
      const words = `${vips[0]}${(duties.management_vip || []).includes(vips[0]) ? " · management" : ""}${vips.length > 1 ? ` +${vips.length - 1}` : ""}`;
      const w = Math.min(300, 56 + words.length * 6.9);
      b += pill(Math.max(110, w / 2 + 4), 6, words, w);
    }
    if (vips.length) b += line(`M110 30V${top}`, "dg-line dg-blue-line", false);
    b += box(16, top, 190, colH, n.status === "Ready" ? "dg-node" : "dg-node dg-bad", 10) + text(30, top + 22, clip(n.name, 22), "dg-b");
    b += `<circle cx="192" cy="${top + 18}" r="4" class="${vips.length ? "dg-vip-dot" : "dg-ring"}"/>`;
    let y = top + 36;
    apps.forEach(a => { b += app(30, y, 162, a); y += 24; });
    if (more) { b += text(30, y + 14, `+${more} more`, "dg-xs dg-dim"); y += 24; }
    if (vms) { b += vm(30, y, 162, vms === 1 ? "1 virtual machine" : `${vms} virtual machines`); y += 24; }
    if (!apps.length && !vms) b += text(30, y + 14, "nothing of yours runs here", "dg-xs dg-dim");
    // drives
    let dy = top + 22;
    b += text(236, dy - 8 + 0, "Drives", "dg-xs dg-dim dg-b");
    drives.forEach(d => {
      const usage = root.diskUsage(n, d), size = usage.physical, w = 180;
      const sys = usage.host, lh = usage.longhorn, room = usage.room;
      const px = gb => usage.capacity ? gb / usage.capacity * w : 0;
      b += text(236, dy + 12, `${clip(d.name || d.device, 16)} · ${size >= 1000 ? (size / 1000).toFixed(1) + " TB" : Math.round(size) + " GB"}`, "dg-s dg-mono");
      if (d.role === "unused") b += box(236, dy + 18, w, 9, "dg-empty dg-dash", 3) + text(236, dy + 40, "unused", "dg-xs dg-dim");
      else if (usage.capacity) {
        b += box(236, dy + 18, w, 9, "dg-track", 3) + (sys ? box(236, dy + 18, px(sys), 9, "dg-sys", 0) : "")
          + (lh ? box(236 + px(sys), dy + 18, px(lh), 9, "dg-vol", 0) : "") + (room ? box(236 + px(sys) + px(lh), dy + 18, px(room), 9, "dg-vol-room", 0) : "");
        b += text(236, dy + 40, `Filesystem ${Math.round(usage.used)}/${Math.round(usage.capacity)} GB · ${usage.pct}%`, "dg-xs dg-dim");
      } else b += text(236, dy + 40, "usage unavailable", "dg-xs dg-dim");
      dy += 52;
    });
    if (!drives.length) b += text(236, dy + 12, "no drive details yet", "dg-xs dg-dim");
    const h = Math.max(top + colH, dy) + 8;
    const label = `${n.name}${vips.length ? ` answers for ${vips.join(", ")}` : ""}; runs ${(n.workloads || []).length} apps and ${vms} VMs; ${drives.length} drives`;
    return figure(svg(430, h, b, label));
  }

  /* A VIP: the LAN, the address, and each port to its app on its node.
     rows: [{ port, app, node }]; an address not chosen yet is dashed. */
  function vip(ip, rows, opts = {}) {
    const list = (rows || []).slice(0, 5), n = Math.max(list.length, 1);
    const h = Math.max(96, 24 + n * 32);
    const mid = h / 2;
    let b = box(6, mid - 22, 74, 44, "dg-lan", 8) + text(43, mid - 3, "your LAN", "dg-xs dg-dim", "middle") + text(43, mid + 11, "phones, TVs", "dg-xs dg-dim", "middle");
    b += line(`M82 ${mid}H106`);
    b += pill(176, mid - 12, ip || "not chosen", 136, !ip);
    if (!list.length) {
      b += line(`M246 ${mid}H284`, "dg-line dg-dash-line");
      b += box(288, mid - 12, 170, 24, "dg-app dg-dash", 5) + text(298, mid + 4, opts.empty || "apps you put here, a port each", "dg-xs dg-dim");
    }
    list.forEach((r, i) => {
      const y = mid - (list.length - 1) * 16 + i * 32;
      b += `<path d="M246 ${mid}C270 ${mid} 266 ${y} 290 ${y}" class="dg-line dg-blue-line"/>`;
      b += box(290, y - 11, 52, 22, "dg-port", 11) + text(316, y + 4, `:${r.port}`, "dg-xs dg-mono dg-blue", "middle");
      b += line(`M344 ${y}H356`);
      b += app(360, y - 10, 120, r.app) + (r.node ? text(486, y + 4, clip(r.node, 18), "dg-xs dg-dim dg-mono") : "");
    });
    const label = ip ? `VIP ${ip}: ${list.map(r => `port ${r.port} to ${r.app}${r.node ? " on " + r.node : ""}`).join(", ") || "nothing on it yet"}` : "A VIP not chosen yet";
    return figure(svg(610, h, b, label), opts.caption || "");
  }

  /* Where each folder goes: source, what happens to it, volume, path inside.
     rows: [{ source, how: copy|empty|skip|ram, volume, path }] */
  function mapping(rows, opts = {}) {
    const list = (rows || []).slice(0, 8);
    const h = 24 + Math.max(1, list.length) * 30;
    let b = text(4, 12, opts.from || "on the source", "dg-xs dg-dim") + text(244, 12, "volumes", "dg-xs dg-dim") + text(392, 12, "inside the container", "dg-xs dg-dim");
    if (!list.length) b += text(4, 36, "nothing mapped yet", "dg-xs dg-dim");
    list.forEach((r, i) => {
      const y = 20 + i * 30, skip = r.how === "skip", ram = r.how === "ram";
      b += box(4, y, 160, 22, skip ? "dg-src dg-faint" : "dg-src", 5) + text(11, y + 15, clip(ram ? "RAM" : r.source || "—", 25), `dg-xs dg-mono${skip ? " dg-dim" : ""}`);
      b += text(203, y + 9, skip ? "leave out" : ram ? "scratch" : r.how === "empty" ? "mount empty" : "copy", "dg-xxs dg-dim", "middle");
      if (skip) { b += line(`M166 ${y + 14}H236`, "dg-line dg-dash-line dg-faint", false); return; }
      b += line(`M166 ${y + 14}H236`, r.how === "copy" ? "dg-line" : "dg-line dg-dash-line");
      b += box(240, y, 138, 22, r.how === "copy" ? "dg-vol-box" : "dg-vol-box dg-dash", 5) + text(248, y + 15, clip(ram ? `${r.volume || "memory"}` : r.volume || "no volume", 22), "dg-xs");
      b += line(`M380 ${y + 11}H388`) + box(392, y, 120, 22, "dg-app", 5) + text(400, y + 15, clip(r.path || "—", 18), "dg-xs dg-mono");
    });
    const label = list.map(r => r.how === "skip" ? `${r.source} left out` : `${r.how === "ram" ? "RAM" : r.source} ${r.how === "copy" ? "copied" : r.how === "empty" ? "mounted empty" : "as scratch"} into ${r.volume || "memory"} at ${r.path}`).join("; ") || "Nothing mapped";
    return figure(svg(516, h, b, label), opts.caption || "");
  }

  /* A VM brought from another host: each of its settings on the left, what
     it becomes here on the right, and what stays behind struck through.
     rows: [{ what, from, to, drop }] */
  function vmImport(rows, opts = {}) {
    const list = (rows || []).slice(0, 12);
    const h = 24 + Math.max(1, list.length) * 28;
    let b = text(4, 12, opts.from || "on the source", "dg-xs dg-dim") + text(300, 12, opts.to || "in Homestead", "dg-xs dg-dim");
    list.forEach((r, i) => {
      const y = 20 + i * 28;
      b += box(4, y, 256, 22, r.drop ? "dg-src dg-faint" : "dg-src", 5)
        + text(11, y + 15, clip(r.what, 12), `dg-xs dg-b${r.drop ? " dg-dim" : ""}`)
        + text(90, y + 15, clip(r.from, 27), `dg-xs dg-mono${r.drop ? " dg-dim" : ""}`);
      if (r.drop) {
        b += line(`M262 ${y + 11}H292`, "dg-line dg-dash-line dg-faint", false) + text(300, y + 15, "stays behind", "dg-xs dg-dim");
        return;
      }
      b += line(`M262 ${y + 11}H292`) + box(296, y, 216, 22, r.what === "Disk" || /^Disk/.test(r.what) ? "dg-vol-box" : "dg-vm", 5)
        + text(304, y + 15, clip(r.to, 33), "dg-xs dg-mono");
    });
    const label = list.map(r => r.drop ? `${r.what} ${r.from} stays behind` : `${r.what} ${r.from} becomes ${r.to}`).join("; ") || "Nothing to bring across";
    return figure(svg(516, h, b, label), opts.caption || "");
  }

  /* Quorum: each server a column with its vote; the cluster carries on while
     more than half agree. servers: [{ name, ready }]. With two, a third is
     drawn dashed - the one that would make losing either survivable. */
  function quorum(servers) {
    const list = (servers || []).slice(0, 5);
    const ghost = list.length === 2;
    const cols = list.length + (ghost ? 1 : 0), w = 150, gap = 14;
    const width = Math.max(320, cols * (w + gap) + 8);
    let b = "";
    list.forEach((s, i) => {
      const x = 6 + i * (w + gap);
      b += box(x, 8, w, 60, s.ready ? "dg-node" : "dg-node dg-bad", 8) + text(x + 12, 28, clip(s.name, 20), "dg-b")
        + text(x + 12, 44, s.ready ? "server · ready" : "server · down", "dg-xs dg-dim")
        + box(x + w - 58, 40, 48, 18, s.ready ? "dg-vol-box" : "dg-vol-box dg-dash", 5) + text(x + w - 34, 53, "vote", "dg-xs", "middle");
    });
    if (ghost) {
      const x = 6 + 2 * (w + gap);
      b += box(x, 8, w, 60, "dg-node dg-dash", 8) + text(x + 12, 28, "a third server", "dg-dim")
        + box(x + w - 58, 40, 48, 18, "dg-vol-box dg-dash", 5) + text(x + w - 34, 53, "vote", "dg-xs dg-dim", "middle");
    }
    const n = list.length, need = Math.floor(n / 2) + 1, lose = Math.max(0, n - need);
    const said = n <= 1 ? "one server: simple, and nothing to fail over to"
      : lose ? `${n} servers: ${need} must agree, so ${lose} can fail and the cluster carries on`
      : `${n} servers: ${need} must agree, so losing either stops the cluster`;
    b += text(6, 92, said, "dg-s") + (ghost ? text(6, 108, "with a third: lose one and 2 of 3 carry on", "dg-xs dg-dim") : "");
    return figure(svg(width, ghost ? 116 : 100, b, said));
  }

  /* A volume's copies across nodes: one per node, the ones no node can hold
     dashed. nodes: [names]; copies: how many the default class keeps. */
  function copies(nodes, count) {
    const names = (nodes || []).slice(0, 5), want = Math.max(1, +count || 1);
    const cols = Math.max(names.length, Math.min(5, want)), w = 110, gap = 12;
    let b = "";
    for (let i = 0; i < cols; i++) {
      const x = 6 + i * (w + gap), has = i < names.length, held = has && i < want;
      b += box(x, 8, w, 64, has ? "dg-node" : "dg-node dg-dash", 8) + text(x + 10, 26, has ? clip(names[i], 15) : "no node", has ? "dg-b" : "dg-dim");
      if (i < want) b += box(x + 10, 38, w - 20, 22, held ? "dg-vol-box" : "dg-vol-box dg-dash", 5)
        + text(x + 18, 53, `copy ${i + 1}`, held ? "dg-xs" : "dg-xs dg-dim");
    }
    const placed = Math.min(want, names.length);
    const said = placed < want ? `${want} copies wanted, ${names.length} node${names.length === 1 ? "" : "s"} to hold them: ${want - placed} never placed`
      : `${want} cop${want === 1 ? "y" : "ies"} of each volume, each on a node of its own`;
    return figure(svg(Math.max(320, cols * (w + gap) + 8), 96, b + text(6, 90, said, "dg-s"), said));
  }

  /* From a phone, over HTTPS, to Homestead: through a tunnel or Tailscale,
     with nothing opened at home. via: "Cloudflare Tunnel" | "Tailscale" | "". */
  function remote(via, url, address) {
    let b = box(6, 20, 46, 70, "dg-node", 9) + text(29, 60, "phone", "dg-xs", "middle");
    b += line("M54 55H118", url ? "dg-line" : "dg-line dg-dash-line") + text(86, 46, url ? "HTTPS" : "no HTTPS yet", "dg-xxs dg-dim", "middle");
    b += box(122, 32, 112, 46, via ? "dg-vip" : "dg-vip dg-dash", 12) + text(178, 52, via || "a tunnel", "dg-s", "middle")
      + text(178, 67, via ? "outbound only" : "or Tailscale", "dg-xxs dg-dim", "middle");
    b += line("M236 55H262") + box(266, 24, 120, 62, "dg-node", 8) + text(276, 44, "Homestead", "dg-b") + text(276, 62, clip(address || "", 20), "dg-xs dg-mono dg-dim");
    const said = url ? `Your phone reaches ${url} over HTTPS, so it can install Homestead and get notifications`
      : "Without HTTPS a phone can open Homestead on your network, but not install it as an app with notifications";
    return figure(svg(392, 112, b + text(6, 106, clip(url ? url : "needed for notifications on a phone", 64), "dg-xs dg-dim"), said));
  }

  root.Diagram = Object.freeze({ node, vip, mapping, vmImport, quorum, copies, remote });
})(typeof window !== "undefined" ? window : globalThis);

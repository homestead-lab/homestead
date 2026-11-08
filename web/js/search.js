/* The top bar's search (#510): what it finds, in what order, and where each
   result goes. It only reads what it is given, so tests load it with require;
   app.js fetches the lists, draws the results and opens the one picked. */
const HomesteadSearch = (() => {
  // Ties keep this order: what people run first, places to go last.
  const KINDS = ["Container", "VM", "Host", "Volume", "Share", "App Store", "Page", "Settings"];

  // 0 the name is the search, 1 it starts with it, 2 it contains it, 3 only another field does.
  const rank = (name, s) => {
    const n = String(name || "").toLowerCase();
    return n === s ? 0 : n.startsWith(s) ? 1 : n.includes(s) ? 2 : 3;
  };

  function hits(q, data = {}, limit = 12) {
    const s = String(q || "").trim().toLowerCase();
    if (!s) return [];
    const found = [];
    const add = (kind, name, others, entry, matched = false) => {
      if (!matched && ![name, ...others].filter(Boolean).join(" ").toLowerCase().includes(s)) return;
      found.push({ kind, name, rank: rank(name, s), ...entry });
    };
    for (const x of data.workloads || [])
      add("Container", x.name, [x.ns, ...(x.images || []), ...(x.nodes || [])],
        { sub: `${x.ns} · ${(x.nodes || []).join(", ") || "unscheduled"}`, icon: x.icon, open: { type: "workload", name: x.name } });
    for (const x of data.vms || [])
      add("VM", x.name, [x.ns, x.os, x.ip, x.node, x.description],
        { sub: [x.ns, x.status, x.node].filter(Boolean).join(" · "), open: { type: "vm", ns: x.ns, name: x.name } });
    for (const x of data.nodes || [])
      add("Host", x.name, [x.os, ...(x.roles || []), ...(x.workloads || [])],
        { sub: `${x.status} · ${x.pods_wl || 0} app copies`, open: { type: "node", name: x.name } });
    for (const x of data.volumes || [])
      add("Volume", x.pvc_name || x.name, [x.name, x.namespace, x.attached_to],
        { sub: `${x.size_gb} GiB · ${x.state}`, open: { type: "volume", name: x.pvc_name || x.name } });
    for (const x of data.shares || [])
      add("Share", x.name, [x.pvc, x.path], { sub: `${x.size_gb} GiB network share`, open: { type: "share", name: x.name } });
    // The App Store has searched already, on more than these fields.
    for (const x of data.apps || [])
      add("App Store", x.name, [x.summary, x.description, x.repo],
        { sub: x.summary || x.description || "From the App Store", icon: x.icon, open: { type: "store", app: x } }, true);
    for (const x of data.pages || [])
      add("Page", x.label, [x.view, x.section], { sub: x.tab ? `${x.section} › ${x.label}` : `Go to ${x.label}`,
        open: { type: "page", view: x.view, ...(x.tab ? { tab: x.tab } : {}) } });
    for (const [tab, label, description, topics] of data.settings || [])
      add("Settings", label, [tab, description, ...(topics || [])], { sub: description || label, open: { type: "settings", tab } });
    return found
      .sort((a, b) => a.rank - b.rank || KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind) || a.name.localeCompare(b.name))
      .slice(0, limit)
      .map(({ rank: _, ...hit }) => hit);
  }

  return Object.freeze({ hits, rank });
})();
if (typeof window !== "undefined") window.HomesteadSearch = HomesteadSearch;
if (typeof module !== "undefined") module.exports = HomesteadSearch;

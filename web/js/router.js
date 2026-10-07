/* Homestead route definitions and pure URL helpers.
   Kept independent of the DOM so the browser and Node tests exercise the same
   canonical route behavior. */
(function (root, factory) {
  const router = factory(root);
  if (typeof module === "object" && module.exports) module.exports = router;
  root.HomesteadRouter = router;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  /* Where the app sits: "/" when Homestead serves it, "/homestead/" for the
     demo on GitHub Pages. Paths inside the app stay the same either way. */
  function base() {
    const value = String((root && root.HOMESTEAD_BASE) || "/");
    return value.endsWith("/") ? value : value + "/";
  }

  function href(path) {
    return base().slice(0, -1) + (String(path || "/").startsWith("/") ? path : "/" + path);
  }

  /* A page with a parent is not in the sidebar: it is a tab of its parent's
     page, and the sidebar marks the parent while it is open. */
  const ROUTES = Object.freeze({
    dash:      Object.freeze({ path: "/",                label: "Dashboard",       section: "Overview" }),
    flow:      Object.freeze({ path: "/architecture",    label: "Architecture",    section: "Overview", parent: "dash" }),
    nodes:     Object.freeze({ path: "/nodes",           label: "Nodes",           section: "Overview" }),
    portal:    Object.freeze({ path: "/portal",          label: "Portal",          section: "Overview" }),
    workloads: Object.freeze({ path: "/containers",      label: "Containers",      section: "Apps" }),
    deploy:    Object.freeze({ path: "/deploy",          label: "Deploy",          section: "Apps", parent: "workloads" }),
    images:    Object.freeze({ path: "/image-cache",     label: "Image Cache",     section: "Apps", parent: "workloads" }),
    schedules: Object.freeze({ path: "/schedules",       label: "Schedules",       section: "Apps", parent: "workloads" }),
    monitoring: Object.freeze({ path: "/monitoring",     label: "Monitoring",      section: "Apps", parent: "workloads" }),
    imports:   Object.freeze({ path: "/import",          label: "Import",          section: "Apps", parent: "workloads" }),
    vms:       Object.freeze({ path: "/vms",             label: "Virtual Machines", section: "Apps" }),
    vmimport:  Object.freeze({ path: "/vms/import",      label: "Import",          section: "Apps", parent: "vms" }),
    store:     Object.freeze({ path: "/app-store",       label: "App Store",       section: "Apps" }),
    helm:      Object.freeze({ path: "/helm",            label: "Helm",            section: "Apps", parent: "store" }),
    storage:   Object.freeze({ path: "/volumes",         label: "Volumes",         section: "Storage" }),
    shares:    Object.freeze({ path: "/shares",          label: "Network Shares",  section: "Storage" }),
    protect:   Object.freeze({ path: "/data-protection", label: "Data Protection", section: "Storage" }),
    network:   Object.freeze({ path: "/networking",      label: "Networking",      section: "System" }),
    cluster:   Object.freeze({ path: "/system/cluster",  label: "Cluster",         section: "System" }),
    events:    Object.freeze({ path: "/events",          label: "Events",          section: "System", parent: "cluster" }),
    resources: Object.freeze({ path: "/resources",       label: "Resources",       section: "System", parent: "cluster" }),
    settings:  Object.freeze({ path: "/settings",        label: "Settings",        section: "System" }),
    setup:     Object.freeze({ path: "/setup",           label: "Setup",           section: "System", parent: "settings" }),
  });

  /* The tabs across the top of a page and the pages folded into it. */
  const TABS = Object.freeze({
    dash: [["dash", "Overview"], ["flow", "Architecture"]],
    workloads: [["workloads", "Running"], ["monitoring", "Monitoring"], ["images", "Images"], ["schedules", "Schedules"], ["imports", "Import"]],
    vms: [["vms", "Machines"], ["vmimport", "Import"]],
    store: [["store", "Apps"], ["helm", "Helm"]],
    cluster: [["cluster", "Health"], ["events", "Events"], ["resources", "Resources"]],
  });

  /* The sidebar entry a page belongs to. */
  function navView(view) {
    return (ROUTES[view] && ROUTES[view].parent) || view;
  }

  /* The tabs to show on a page: its family's, when it is one of them. */
  function tabsFor(view) {
    const tabs = TABS[navView(view)] || [];
    return tabs.some(([id]) => id === view) ? tabs : [];
  }

  const BY_PATH = Object.freeze(Object.fromEntries(
    Object.entries(ROUTES).map(([view, route]) => [route.path, view])));

  function normalizePath(pathname) {
    let path = String(pathname || "/").split(/[?#]/, 1)[0] || "/";
    try { path = decodeURI(path); } catch (_) { /* preserve malformed input */ }
    if (!path.startsWith("/")) path = "/" + path;
    path = path.replace(/\/{2,}/g, "/");
    const prefix = base();
    if (prefix !== "/" && (path + "/").startsWith(prefix)) path = "/" + path.slice(prefix.length);
    if (path.length > 1) path = path.replace(/\/+$/, "");
    return path;
  }

  function resolve(pathname) {
    const path = normalizePath(pathname);
    const view = BY_PATH[path];
    return view
      ? { view, path, known: true, route: ROUTES[view] }
      : { view: "dash", path: "/", known: false, route: ROUTES.dash };
  }

  function urlFor(view, params) {
    const route = ROUTES[view] || ROUTES.dash;
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === "") return;
      if (Array.isArray(value)) value.forEach(item => query.append(key, String(item)));
      else query.set(key, String(value));
    });
    const suffix = query.toString();
    return href(route.path) + (suffix ? "?" + suffix : "");
  }

  function queryParams(search) {
    const query = new URLSearchParams(String(search || "").replace(/^\?/, ""));
    const out = {};
    for (const [key, value] of query.entries()) {
      if (Object.prototype.hasOwnProperty.call(out, key)) {
        out[key] = Array.isArray(out[key]) ? [...out[key], value] : [out[key], value];
      } else out[key] = value;
    }
    return out;
  }

  /* Where a page sits is its section in the sidebar - Containers lives under
     Workloads, not under the Dashboard, which is a sibling page and never a
     parent of anything. A section has no page of its own, so it is a label
     rather than a link. */
  function breadcrumbs(view, detail) {
    const route = ROUTES[view] || ROUTES.dash;
    const items = [];
    if (route.section) items.push({ label: route.section, url: "", current: false });
    if (route.parent && ROUTES[route.parent]) items.push({ label: ROUTES[route.parent].label, url: href(ROUTES[route.parent].path), current: false });
    items.push({ label: route.label, url: href(route.path), current: !detail });
    if (detail) items.push({ label: String(detail), url: "", current: true });
    return items;
  }

  return Object.freeze({ ROUTES, TABS, normalizePath, resolve, urlFor, queryParams, breadcrumbs, href, navView, tabsFor });
});

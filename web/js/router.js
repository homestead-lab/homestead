/* Homestead route definitions and pure URL helpers.
   Kept independent of the DOM so the browser and Node tests exercise the same
   canonical route behavior. */
(function (root, factory) {
  const router = factory();
  if (typeof module === "object" && module.exports) module.exports = router;
  root.HomesteadRouter = router;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ROUTES = Object.freeze({
    dash:      Object.freeze({ path: "/",                label: "Dashboard",       section: "Overview" }),
    flow:      Object.freeze({ path: "/architecture",    label: "Architecture",    section: "Overview" }),
    nodes:     Object.freeze({ path: "/nodes",           label: "Nodes",           section: "Overview" }),
    network:   Object.freeze({ path: "/networking",      label: "Networking",      section: "System" }),
    portal:    Object.freeze({ path: "/portal",          label: "Portal",          section: "Overview" }),
    deploy:    Object.freeze({ path: "/deploy",          label: "Deploy",          section: "Workloads" }),
    workloads: Object.freeze({ path: "/containers",      label: "Containers",      section: "Workloads" }),
    vms:       Object.freeze({ path: "/vms",             label: "Virtual Machines", section: "Workloads" }),
    store:     Object.freeze({ path: "/app-store",       label: "App Store",       section: "Workloads" }),
    helm:      Object.freeze({ path: "/helm",            label: "Helm",            section: "Workloads" }),
    shares:    Object.freeze({ path: "/shares",          label: "Network Shares",  section: "Storage" }),
    storage:   Object.freeze({ path: "/volumes",         label: "Volumes",         section: "Storage" }),
    images:    Object.freeze({ path: "/image-cache",     label: "Image Cache",     section: "Storage" }),
    protect:   Object.freeze({ path: "/data-protection", label: "Data Protection", section: "Storage" }),
    schedules: Object.freeze({ path: "/schedules",       label: "Schedules",       section: "System" }),
    imports:   Object.freeze({ path: "/import",          label: "Import",          section: "System" }),
    events:    Object.freeze({ path: "/events",          label: "Events",          section: "System" }),
    resources: Object.freeze({ path: "/resources",       label: "Resources",       section: "System" }),
    cluster:   Object.freeze({ path: "/system/cluster",  label: "Cluster",         section: "System" }),
    settings:  Object.freeze({ path: "/settings",        label: "Settings",        section: "System" }),
  });

  const BY_PATH = Object.freeze(Object.fromEntries(
    Object.entries(ROUTES).map(([view, route]) => [route.path, view])));

  function normalizePath(pathname) {
    let path = String(pathname || "/").split(/[?#]/, 1)[0] || "/";
    try { path = decodeURI(path); } catch (_) { /* preserve malformed input */ }
    if (!path.startsWith("/")) path = "/" + path;
    path = path.replace(/\/{2,}/g, "/");
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
    return route.path + (suffix ? "?" + suffix : "");
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
    items.push({ label: route.label, url: route.path, current: !detail });
    if (detail) items.push({ label: String(detail), url: "", current: true });
    return items;
  }

  return Object.freeze({ ROUTES, normalizePath, resolve, urlFor, queryParams, breadcrumbs });
});

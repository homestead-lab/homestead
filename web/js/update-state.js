(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.HomesteadUpdateState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function isStale(existing, incoming) {
    if (existing?.channel !== incoming?.channel) return false;
    if (!existing || !existing.checked_at || !incoming || !incoming.checked_at) return false;
    const current = Date.parse(existing.checked_at);
    const next = Date.parse(incoming.checked_at);
    return Number.isFinite(current) && Number.isFinite(next) && next < current;
  }

  // Apps' updates. Homestead's own - itself and the helpers it runs - are
  // updated apart, from the top bar's Homestead button and Settings › Updates.
  function availableWorkloads(report) {
    // An app something else updates (Flux, Argo CD, or marked so) keeps its
    // notice but is not one Homestead offers to update.
    return (report?.workloads || []).filter(workload => workload?.available && !workload.homestead && !workload.managed?.by);
  }

  // Homestead and its helpers, Homestead first: what each runs and whether a
  // newer release or image is waiting.
  function homesteadWorkloads(report) {
    return (report?.workloads || []).filter(workload => workload?.homestead)
      .sort((left, right) => Number(right.homestead === "self") - Number(left.homestead === "self"));
  }

  function orderApply(workloads, controlPlaneNames = ["homestead"]) {
    const names = new Set(controlPlaneNames);
    const items = [...(workloads || [])];
    const clusters = new Map();
    for (const item of items) if (item?.cluster && !clusters.has(item.cluster)) clusters.set(item.cluster, clusters.size);
    const clusterOrder = item => item?.cluster ? clusters.get(item.cluster) : clusters.size;
    const homestead = item => item?.part === "self" || item?.homestead === "self" || names.has(item?.name);
    // Keep each linked cluster together, helpers before its Homestead. The
    // cluster serving this page goes last so its restart cannot cut the relay.
    return items.sort((left, right) => clusterOrder(left) - clusterOrder(right)
      || Number(homestead(left)) - Number(homestead(right)));
  }

  return { isStale, availableWorkloads, homesteadWorkloads, orderApply };
});

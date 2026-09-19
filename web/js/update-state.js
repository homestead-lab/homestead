(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.HarvUpdateState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function isStale(existing, incoming) {
    if (!existing || !existing.checked_at || !incoming || !incoming.checked_at) return false;
    const current = Date.parse(existing.checked_at);
    const next = Date.parse(incoming.checked_at);
    return Number.isFinite(current) && Number.isFinite(next) && next < current;
  }

  return { isStale };
});

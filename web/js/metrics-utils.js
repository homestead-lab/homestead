"use strict";

(function (global) {
  function workloadCpuPercent(cores) {
    const value = Number(cores);
    if (!Number.isFinite(value) || value <= 0) return "0%";
    return `${Number((value * 100).toFixed(1))}%`;
  }

  global.workloadCpuPercent = workloadCpuPercent;
})(typeof window !== "undefined" ? window : globalThis);

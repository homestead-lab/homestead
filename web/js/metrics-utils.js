"use strict";

(function (global) {
  function workloadCpuPercent(cores) {
    const value = Number(cores);
    if (!Number.isFinite(value) || value <= 0) return "0%";
    const percent = value * 100;
    // A tenth of a percent is worth showing on an idle container, not on one
    // using several cores, where it only makes the number too wide to fit.
    return `${percent >= 100 ? Math.round(percent) : Number(percent.toFixed(1))}%`;
  }

  function workloadMemory(mb) {
    const value = Number(mb);
    if (!Number.isFinite(value) || value <= 0) return "0 MB";
    if (value < 1024) return `${Math.round(value)} MB`;
    const gb = value / 1024;
    return `${gb < 10 ? Number(gb.toFixed(1)) : Math.round(gb)} GB`;
  }

  global.workloadCpuPercent = workloadCpuPercent;
  global.workloadMemory = workloadMemory;
})(typeof window !== "undefined" ? window : globalThis);

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

  // Figures on cards get a bigger unit rather than more digits, so a large
  // cluster reads "12.1 TB" where it used to push "12345.6 GB" off the card.
  function short(value) {
    const n = Number(value) || 0;
    return String(n >= 100 ? Math.round(n) : Number(n.toFixed(1)));
  }

  // Four digits of GB still fit; five do not, and 0.5 TB reads worse than 486 GB.
  const TB_FROM = 10240;

  function sizeParts(gb) {
    const n = Number(gb) || 0;
    return n >= TB_FROM ? [short(n / 1024), "TB"] : [short(n), "GB"];
  }

  function sizeText(gb) {
    return sizeParts(gb).join(" ");
  }

  // Both halves in the unit the larger one needs: "12.1/13.7 TB", never "12345.6/13.7".
  function sizePair(used, cap) {
    const big = Math.max(Number(used) || 0, Number(cap) || 0) >= TB_FROM;
    const scale = n => short(big ? (Number(n) || 0) / 1024 : n);
    return `${scale(used)}/${scale(cap)} ${big ? "TB" : "GB"}`;
  }

  function rateParts(mbps) {
    const n = Number(mbps) || 0;
    return n >= 1000 ? [short(n / 1000), "Gb/s"] : [short(n), "Mb/s"];
  }

  function ratePair(rx, tx) {
    const big = Math.max(Number(rx) || 0, Number(tx) || 0) >= 1000;
    const scale = n => short(big ? (Number(n) || 0) / 1000 : n);
    return [`↓${scale(rx)} ↑${scale(tx)}`, big ? "Gb/s" : "Mb/s"];
  }

  // Usage is measured against mounted filesystems, not the physical drive.
  // Space outside a root LV/partition is not automatically available to it.
  function diskUsage(node, disk) {
    const number = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
    const root = disk.node_fs ?? disk.root_fs ?? (disk.system || disk.role === "system");
    const reported = Array.isArray(disk.lh_filesystems);
    const filesystems = reported ? disk.lh_filesystems : (disk.lh_size_gb > 0 ? [{
      capacity_gb: disk.lh_size_gb, used_gb: disk.lh_used_gb,
      data_gb: disk.lh_used_gb, available_gb: 0, reserved_gb: 0,
      on_root: disk.lh_root_used_gb > 0 || (disk.lh_paths || []).some(p => /^\/var\/lib\/longhorn\/?$/.test(p)),
    }] : []);
    const sharedFilesystem = f => f.on_node_fs ?? f.on_root;
    const shared = filesystems.filter(sharedFilesystem);
    const rootCap = root ? number(node.fs_cap_gb) || Math.max(0,...shared.map(f => number(f.capacity_gb))) : 0;
    const rootUsed = Math.min(rootCap, number(node.fs_cap_gb) ? number(node.fs_used_gb)
      : Math.max(0,...shared.map(f => number(f.used_gb))));
    const rootData = Math.min(rootUsed, shared.reduce((sum,f) => sum + number(f.data_gb),0));
    let capacity = rootCap, host = rootUsed - rootData, longhorn = rootData;
    let room = Math.min(Math.max(0,rootCap-rootUsed),shared.reduce((sum,f) =>
      sum + Math.max(0,number(f.available_gb)-number(f.reserved_gb)),0));
    for (const fs of filesystems.filter(f => !sharedFilesystem(f))) {
      const cap = number(fs.capacity_gb), used = Math.min(cap,number(fs.used_gb));
      capacity += cap;longhorn += used;
      room += Math.min(cap-used,Math.max(0,number(fs.available_gb)-number(fs.reserved_gb)));
    }
    const used = host + longhorn;
    return {capacity,host,longhorn,room,used,remaining:Math.max(0,capacity-used-room),
      pct:capacity ? Math.round(used/capacity*100) : null,physical:number(disk.size_gb)};
  }

  global.workloadCpuPercent = workloadCpuPercent;
  global.sizeParts = sizeParts;
  global.sizeText = sizeText;
  global.sizePair = sizePair;
  global.rateParts = rateParts;
  global.ratePair = ratePair;
  global.workloadMemory = workloadMemory;
  global.diskUsage = diskUsage;
})(typeof window !== "undefined" ? window : globalThis);

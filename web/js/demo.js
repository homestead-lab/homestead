/* Deterministic, read-only demo transport: the release screenshots (?demo=1)
   and the live demo on GitHub Pages (window.HOMESTEAD_DEMO, set by
   scripts/build_demo_site.py). It never contacts a cluster. */
(function () {
  const live = window.HOMESTEAD_DEMO === true;
  if (!live && new URLSearchParams(location.search).get("demo") !== "1") return;
  const requestedScenario = new URLSearchParams(location.search).get("demo-scenario");
  const scenario = ["incidents", "critical"].includes(requestedScenario) ? requestedScenario : "healthy";
  window.HOMESTEAD_DEMO_SCENARIO = scenario;

  /* What the counters add up to, the way the server computes it. */
  const diskHealth = smart => {
    const issues = [];
    if (smart.health === "failed") issues.push({ severity: "critical", reason: "SMART overall-health check failed" });
    if (smart.temperature_c >= 65) issues.push({ severity: "critical", reason: `drive temperature is ${smart.temperature_c}°C` });
    else if (smart.temperature_c >= 55) issues.push({ severity: "degraded", reason: `drive temperature is ${smart.temperature_c}°C` });
    if (smart.reallocated) issues.push({ severity: "degraded", reason: `${smart.reallocated} reallocated sector(s)` });
    if (smart.pending) issues.push({ severity: "critical", reason: `${smart.pending} pending sector(s)` });
    if (smart.uncorrectable) issues.push({ severity: "critical", reason: `${smart.uncorrectable} uncorrectable sector(s)` });
    if (smart.media_errors) issues.push({ severity: "critical", reason: `${smart.media_errors} NVMe media error(s)` });
    const life = smart.wear?.life_pct;
    if (life != null && life <= 10) issues.push({ severity: "critical", reason: `only ${life}% of rated life remains` });
    else if (life != null && life <= 25) issues.push({ severity: "degraded", reason: `${life}% of rated life remains` });
    const state = issues.some(i => i.severity === "critical") ? "critical" : issues.length ? "attention" : "healthy";
    return { state, issues, life_pct: life ?? null, life_basis: smart.wear?.basis || "",
      stale_probe: false,
      spare_pct: smart.wear?.spare_pct ?? null,
      summary: issues.length ? issues.map(i => i.reason).join("; ") : "passed, with no reported defects" };
  };

  const smartDisk = (name, model, serial, temperature, powerHours, wear = {}) => ({
    name, path: `/dev/${name}`, available: true, protocol: name.startsWith("nvme") ? "NVMe" : "ATA",
    model, serial, firmware: "1.0", capacity_gb: name.startsWith("nvme") ? 465.8 : 931.5,
    smart_enabled: true, health: "passed", temperature_c: temperature, power_on_hours: powerHours,
    reallocated: name.startsWith("nvme") ? null : (wear.reallocated ?? 0),
    pending: name.startsWith("nvme") ? null : (wear.pending ?? 0),
    uncorrectable: name.startsWith("nvme") ? null : (wear.uncorrectable ?? 0), error_count: 0,
    media_errors: name.startsWith("nvme") ? (wear.media_errors ?? 0) : null,
    nvme: name.startsWith("nvme")
      ? { available_spare: 100, available_spare_threshold: 10,
          percentage_used: 100 - (wear.life_pct ?? 94), unsafe_shutdowns: 12,
          data_units_written: 41_235_700, critical_warning: 0 }
      : null,
    wear: name.startsWith("nvme")
      ? { life_pct: wear.life_pct ?? 94, basis: "NVMe endurance used",
          spare_pct: wear.spare_pct ?? 100, spare_floor_pct: 10 }
      : { life_pct: wear.life_pct ?? 72, basis: "worst pre-failure attribute",
          spare_pct: null, spare_floor_pct: null },
    supported_tests: ["short", "long"],
    test: { active: false, status: "No self-test running", remaining_percent: null },
    self_tests: [{ type: "Short offline", status: "Completed without error", lifetime_hours: powerHours - 12,
      signature: `short|ok|${powerHours - 12}` }],
  });

  const withHealth = disk => Object.assign(disk, { health: diskHealth(disk.smart) });

  function demoUplinks() {
    const nic = (name, used_by = "", link = "up", speed_mbps = 2500) => ({ name, link, speed_mbps: link === "up" ? speed_mbps : null, master: "", used_by });
    const ready = (...names) => Object.fromEntries(names.map(n => [n, { ready: true, message: "" }]));
    return { applies: true, modes: ["active-backup", "802.3ad", "balance-tlb", "balance-alb", "balance-xor", "balance-rr", "broadcast"],
      networks: [
        { name: "mgmt", mgmt: true, ready: true, message: "", configs: [], uncovered: [], lan_networks: ["lab/untagged"] },
        { name: "data", mgmt: false, ready: true, message: "", uncovered: ["harvester-node3"], lan_networks: ["lab/vlan20"], configs: [
          { name: "data-harvester-node1", cluster_network: "data", nodes: ["harvester-node1"], nics: ["enp2s0", "enp3s0"], mode: "active-backup",
            miimon: 100, mtu: null, homestead: true, status: ready("harvester-node1") },
          { name: "data-uplink", cluster_network: "data", nodes: ["harvester-node2"], nics: ["enp4s0"], mode: "active-backup",
            miimon: 100, mtu: 9000, homestead: false, status: { "harvester-node2": { ready: false, message: "nic enp4s0 has no carrier" } } }] }],
      hosts: {
        "harvester-node1": [nic("eno1", "mgmt"), nic("enp2s0", "data"), nic("enp3s0", "data"), nic("enp4s0", "", "down")],
        "harvester-node2": [nic("enp1s0", "mgmt"), nic("enp2s0", "mgmt"), nic("enp4s0", "data", "down"), nic("enp5s0")],
        "harvester-node3": [nic("ens5", "mgmt"), nic("ens6", "mgmt"), nic("ens7"), nic("ens8", "", "up", 1000)] } };
  }
  function demoPorts() {
    const nic = (name, extra = {}) => ({ name, kind: "nic", link: "up", speed_mbps: 2500, duplex: "full", mtu: 1500, driver: "igc",
      master: "", carries: [], uplink: false, errors: 0, flaps: 0, drops: 0, window_s: 3600, crc: 0, was_mbps: null, bond: null, bond_member: null, ...extra });
    const up = ["host address"], lan = ["host address", "lab/iot"];
    const hosts = {
      "harvester-node1": { node: "harvester-node1", available: true, uplink: "mgmt-br", conditions: [], ports: [
        nic("eno1", { master: "mgmt-br", carries: up, uplink: true }),
        nic("enp1s0", { link: "down", speed_mbps: null, duplex: null, driver: "r8169" }),
        { name: "mgmt-br", kind: "bridge", link: "up", carries: up, uplink: true, mtu: 1500 }] },
      "harvester-node2": { node: "harvester-node2", available: true, uplink: "mgmt-br", ports: [
        nic("enp1s0", { master: "mgmt-bo", carries: lan, uplink: true, bond_member: { state: "active", mii_status: "up", link_failure_count: 0 } }),
        nic("enp2s0", { master: "mgmt-bo", carries: lan, uplink: true, speed_mbps: 1000, was_mbps: 2500, errors: 142, crc: 139,
          bond_member: { state: "backup", mii_status: "up", link_failure_count: 2 } }),
        nic("enp3s0", { link: "down", speed_mbps: null, duplex: null, driver: "r8169" }),
        { name: "mgmt-bo", kind: "bond", link: "up", master: "mgmt-br", carries: lan, uplink: true, mtu: 1500,
          bond: { mode: "active-backup", slaves: ["enp1s0", "enp2s0"], active_slave: "enp1s0", mii_status: "up" } },
        { name: "mgmt-br", kind: "bridge", link: "up", carries: lan, uplink: true, mtu: 1500 }],
        conditions: [
          { key: "harvester-node2:enp2s0:slower", node: "harvester-node2", iface: "enp2s0", kind: "slower", severity: "degraded",
            title: "enp2s0 on harvester-node2 runs slower than the rest of mgmt-bo", body: "It negotiated 1 Gb/s; its bond peers run at 2.5 Gb/s. Usually a cable or a switch port." },
          { key: "harvester-node2:enp2s0:errors", node: "harvester-node2", iface: "enp2s0", kind: "errors", severity: "degraded",
            title: "enp2s0 on harvester-node2 is seeing errors", body: "142 errors in the last 60 minutes, 139 of them CRC errors. Usually a cable, a switch port or a failing NIC." }] },
      "harvester-node3": { node: "harvester-node3", available: true, uplink: "mgmt-br", ports: [
        nic("ens5", { master: "mgmt-bo", carries: up, uplink: true, bond_member: { state: "active", mii_status: "up" } }),
        nic("ens6", { master: "mgmt-bo", carries: up, uplink: true, bond_member: { state: "active", mii_status: "up" } }),
        { name: "mgmt-bo", kind: "bond", link: "up", master: "mgmt-br", carries: up, uplink: true, mtu: 1500,
          bond: { mode: "802.3ad", slaves: ["ens5", "ens6"], mii_status: "up", ad_partner_mac: "00:00:00:00:00:00" } },
        { name: "mgmt-br", kind: "bridge", link: "up", carries: up, uplink: true, mtu: 1500 }],
        conditions: [{ key: "harvester-node3:mgmt-bo:lacp", node: "harvester-node3", iface: "mgmt-bo", kind: "lacp", severity: "degraded",
          title: "The switch is not aggregating mgmt-bo on harvester-node3",
          body: "This 802.3ad bond has had no LACP partner for over two minutes: the switch ports are not one LACP group, so the bond uses one member at a time. Group the ports on the switch, or change the bond to active-backup." }] },
    };
    return { hosts, conditions: Object.values(hosts).flatMap(h => h.conditions), counts: { hosts: 3, ports: 7 } };
  }
  const nodes = [
    { name: "harvester-node1", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 22.4, cpu_used: 1.79, cpu_cap: 8, mem_pct: 61.7, mem_used_gb: 9.6, mem_cap_gb: 15.6,
      fs_pct: 48.3, fs_used_gb: 168, fs_cap_gb: 348, rx_mbps: 8.4, tx_mbps: 3.1,
      pods: 54, pods_sys: 46, pods_wl: 8, vms: 1, workloads: ["home-assistant", "mosquitto", "homestead-smb"],
      hardware: { igpu: true }, temps: { cpu_c: 39, max_c: 51, max_source: "NVMe nvme0", cpu_model: "Intel(R) Core(TM) i5-12500T", sensors: 4, smart_helper: { available: true },
        disks: [withHealth({ name: "nvme0n1", model: "Samsung SSD 970 EVO Plus", serial: "DEMO-NVME-01", kind: "NVMe", size_gb: 465.8,
          read_mbps: 18.42, write_mbps: 6.17, smart: smartDisk("nvme0n1", "Samsung SSD 970 EVO Plus", "DEMO-NVME-01", 41, 8421) })] } },
    { name: "harvester-node2", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 41.8, cpu_used: 3.34, cpu_cap: 8, mem_pct: 54.1, mem_used_gb: 8.4, mem_cap_gb: 15.6,
      fs_pct: 28.4, fs_used_gb: 66, fs_cap_gb: 232, rx_mbps: 21.9, tx_mbps: 12.6,
      pods: 47, pods_sys: 42, pods_wl: 5, vms: 0, workloads: ["frigate", "homestead"],
      hardware: { igpu: true, coral_usb: true }, temps: { cpu_c: 34, max_c: 47, max_source: "CPU package", cpu_model: "Intel(R) N100", sensors: 5, smart_helper: { available: true },
        disks: [withHealth({ name: "sda", model: "WDC WD100EFAX", serial: "DEMO-SATA-02", kind: "HDD", size_gb: 931.5,
          read_mbps: 3.26, write_mbps: 12.91,
          smart: smartDisk("sda", "WDC WD100EFAX", "DEMO-SATA-02", 36, 16420,
            { reallocated: 24, life_pct: 61 }) })] } },
    { name: "harvester-node3", status: "Ready", roles: ["worker"], schedulable: true,
      cpu_pct: 16.3, cpu_used: 0.65, cpu_cap: 4, mem_pct: 46.2, mem_used_gb: 7.2, mem_cap_gb: 15.6,
      fs_pct: 41.4, fs_used_gb: 144, fs_cap_gb: 348, rx_mbps: 5.8, tx_mbps: 2.4,
      pods: 31, pods_sys: 28, pods_wl: 3, vms: 0, workloads: ["paperless"],
      hardware: {}, temps: { cpu_c: 36, max_c: 45, max_source: "Motherboard", cpu_model: "AMD Ryzen 5 5600G with Radeon Graphics", sensors: 3, smart_helper: { available: true },
        disks: [withHealth({ name: "nvme0n1", model: "Kingston NV2", serial: "DEMO-NVME-03", kind: "NVMe", size_gb: 465.8,
          read_mbps: 0.74, write_mbps: 1.15, smart: smartDisk("nvme0n1", "Kingston NV2", "DEMO-NVME-03", 38, 3912) })] } },
  ];
  // How long each has been up, and what the history says of the last 90 days.
  [[41 * 86400 + 5 * 3600], [9 * 86400 + 2 * 3600], [2 * 86400 + 7 * 3600]].forEach(([up], i) => {
    nodes[i].uptime_s = up;
    nodes[i].ready_since = new Date(Date.now() - up * 1000 + 95000).toISOString();
  });
  const demoUptime = (() => {
    const now = Math.floor(Date.now() / 1000), today = now - now % 86400;
    const plan = {
      "harvester-node1": { outages: [], reboots: [now - nodes[0].uptime_s] },
      "harvester-node2": { outages: [{ back: 9 * 86400 + 2 * 3600, down: 780, exact: true }, { back: 52 * 86400, down: 3 * 3600, exact: false }],
                           reboots: [now - nodes[1].uptime_s] },
      "harvester-node3": { outages: [{ back: 2 * 86400 + 7 * 3600 + 1500, down: 1500, exact: true }, { back: 23 * 86400, down: 600, exact: true }],
                           reboots: [now - nodes[2].uptime_s, now - 23 * 86400 + 600] },
    };
    const out = {};
    for (const [name, p] of Object.entries(plan)) {
      const outages = p.outages.map(o => ({ start: now - o.back, end: now - o.back + o.down, down_s: o.down, exact: o.exact, ongoing: false }))
        .sort((a, b) => a.start - b.start);
      const downIn = (from, to) => outages.reduce((s, o) => s + Math.max(0, Math.min(to, o.end) - Math.max(from, o.start)), 0);
      const pct = (from, to) => Math.round(1e5 * (1 - downIn(from, to) / (to - from))) / 1e3;
      out[name] = {
        windows: { "24h": pct(now - 86400, now), "7d": pct(now - 7 * 86400, now), "30d": pct(now - 30 * 86400, now), "90d": pct(now - 90 * 86400, now) },
        days: Array.from({ length: 90 }, (_, i) => { const day = today - (89 - i) * 86400; return { day, up: pct(day, Math.min(now, day + 86400)) }; }),
        outages, reboots: p.reboots.sort((a, b) => a - b), since: now - 90 * 86400,
      };
    }
    return { nodes: out, step: 300 };
  })();
  // Every disk on each node: the system disk with Longhorn's default folder,
  // a second disk given to Longhorn, and one nothing uses yet.
  const lhDisk = (id, path, size, used, alloc, replicas, tags = []) => ({ id, path, type: "filesystem", scheduling: true, evicting: false,
    size_gb: size, used_gb: used, allocated_gb: alloc, free_gb: size - used, replicas, ready: true, problem: "", tags });
  const demoDisks = {
    "harvester-node1": [
      { device: "nvme0n1", path: "/dev/nvme0n1", size_gb: 465.8, model: "Samsung SSD 970 EVO Plus", kind: "NVMe", serial: "", system: true, role: "longhorn",
        mounts: ["/", "/var/lib/harvester/defaultdisk"], blockdevice: null, can_add: false, needs_wipe: false,
        longhorn: [lhDisk("default-disk-1", "/var/lib/harvester/defaultdisk", 116.8, 26.3, 99, 12, ["ssd", "nvme"])] },
      { device: "sdb", path: "/dev/sdb", size_gb: 1863, model: "Seagate IronWolf", kind: "HDD", serial: "", system: false, role: "unused",
        mounts: [], can_add: true, needs_wipe: true, longhorn: [],
        blockdevice: { name: "bd-node1-sdb", path: "/dev/sdb", provisioned: false, fstype: "ext4", state: "Active" } }],
    "harvester-node2": [
      { device: "nvme0n1", path: "/dev/nvme0n1", size_gb: 238.5, model: "WD SN570", kind: "NVMe", serial: "", system: true, role: "system",
        mounts: ["/"], blockdevice: null, can_add: false, needs_wipe: false, longhorn: [] },
      { device: "sda", path: "/dev/sda", size_gb: 931.5, model: "WDC WD100EFAX", kind: "HDD", serial: "", system: false, role: "longhorn",
        mounts: ["/var/lib/harvester/extra-disks/abc"], can_add: false, needs_wipe: false,
        blockdevice: { name: "bd-node2-sda", path: "/dev/sda", provisioned: true, fstype: "ext4", state: "Active" },
        longhorn: [lhDisk("bd-node2-sda", "/var/lib/harvester/extra-disks/abc", 396.5, 14.8, 160.2, 15, ["hdd"])] }],
    "harvester-node3": [
      { device: "nvme0n1", path: "/dev/nvme0n1", size_gb: 465.8, model: "Kingston NV2", kind: "NVMe", serial: "", system: true, role: "longhorn",
        mounts: ["/", "/var/lib/harvester/defaultdisk"], blockdevice: null, can_add: false, needs_wipe: false,
        longhorn: [lhDisk("default-disk-3", "/var/lib/harvester/defaultdisk", 116.8, 21.1, 80, 9, ["ssd"])] },
      // A drive that died: Harvester has lost it, Longhorn still lists it with
      // its replicas, and a new drive sits beside it waiting to be added.
      { device: "", path: "", size_gb: 931.5, model: "", kind: "", serial: "", system: false, role: "longhorn",
        mounts: [], can_add: false, needs_wipe: false, blockdevice: null,
        longhorn: [{ ...lhDisk("bd-node3-sdb", "/var/lib/harvester/extra-disks/7f2c", 931.5, 0, 240, 6),
          ready: false, failed: true, problem: "Disk bd-node3-sdb(/var/lib/harvester/extra-disks/7f2c) on node harvester-node3 is not ready: failed to get disk config",
          missing: "Harvester no longer finds this drive (/dev/sdb): it is missing or dead" }] },
      { device: "sdc", path: "/dev/sdc", size_gb: 1863, model: "WDC WD20EFZX", kind: "HDD", serial: "", system: false, role: "unused",
        mounts: [], can_add: true, needs_wipe: false, longhorn: [],
        blockdevice: { name: "bd-node3-sdc", path: "/dev/sdc", provisioned: false, fstype: "", state: "Active" } }],
  };
  // Which node answers for the management VIP and serves shared volumes.
  const demoDuties = {
    "harvester-node1": { vips: ["192.0.2.210", "192.0.2.214", "192.0.2.215", "192.0.2.216"], management_vip: ["192.0.2.210"], rwx: [], control_plane_vip: false },
    "harvester-node2": { vips: [], management_vip: [], rwx: ["share-media", "frigate-config"], control_plane_vip: false },
  };
  nodes.forEach(n => { n.duties = demoDuties[n.name] || { vips: [], management_vip: [], rwx: [], control_plane_vip: false }; });
  const demoNodeIps = { "harvester-node1": "192.0.2.207", "harvester-node2": "192.0.2.208", "harvester-node3": "192.0.2.209" };
  nodes.forEach(n => { n.addresses = { InternalIP: demoNodeIps[n.name], Hostname: n.name }; });
  // One drive named, as someone would from the node's detail.
  const demoDiskNames = { "harvester-node1": { sdb: "Media 2TB" } };
  nodes.forEach(n => { n.disks = demoDisks[n.name].map(d => ({ device: d.device, size_gb: d.size_gb, role: d.role,
    name: (demoDiskNames[n.name] || {})[d.device] || "", system: !!d.system || d.role === "system", model: d.model || "",
    lh_paths: d.longhorn.map(x => x.path || ""), lh_root_used_gb: 0, root_fs: d.mounts.includes("/"), node_fs: d.mounts.includes("/"),
    lh_filesystems: d.longhorn.map(x => ({ capacity_gb: x.size_gb, used_gb: x.used_gb,
      data_gb: x.used_gb, available_gb: x.free_gb, reserved_gb: 0, on_root: false, on_node_fs: false })),
    lh_used_gb: d.longhorn.reduce((s, x) => s + x.used_gb, 0), lh_size_gb: d.longhorn.reduce((s, x) => s + x.size_gb, 0) })); });
  const pod = (name, node, image) => ({ name: `${name}-7d8f6d4c9-demo`, node, phase: "Running",
    ready: true, restarts: 0, container_count: 1,
    containers: [{ name, image, kind: "app", state: "running", ready: true, restarts: 0 }] });
  // Harvester unless ?platform=k3s: a plain k3s cluster, for the pages that differ.
  const demoPlatform = new URLSearchParams(location.search).get("platform") || "harvester";
  // The IP addresses page with UniFi connected, until Settings disconnects it.
  let demoUnifi = new URLSearchParams(location.search).get("unifi") !== "0";
  const vmDisk = (claim, size, extra = {}) => ({ name: "disk-0", kind: "disk", claim, boot: 1, bus: "virtio", size, storage_class: "harvester-longhorn", ...extra });
  const demoVms = [
    { ns: "default", name: "home-assistant-os", status: "Running", run_strategy: "RerunOnFailure", running: true, node: "harvester-node1",
      cores: 2, memory: "4Gi", ip: "192.0.2.60", ips: ["192.0.2.60"], network: "default/vlan1",
      usage: { cpu: 0.46, cpu_pct: 23, mem: 2.9 * 1024 ** 3, mem_pct: 72.5, read_bps: 184320, write_bps: 1.6 * 1024 ** 2 },
      os: "Home Assistant OS 13.2", description: "HAOS with the Zigbee stick passed through",
      nics: [{ name: "default", model: "virtio", network: "default/vlan1", mac: "52:54:00:6a:11:02", ips: ["192.0.2.60"] }],
      disks: [vmDisk("haos-disk-0", "32Gi")], migratable: false, restart_required: false, problem: "", created: "2026-08-02T10:00:00Z",
      actions: ["console", "stop", "restart", "pause"] },
    // Two nodes of a k3s cluster made here: an address each, and k3s's own on the server.
    ...[["server", "192.0.2.231", ["192.0.2.231", "10.42.0.1"]], ["agent", "192.0.2.232", ["192.0.2.232"]]].map(([role, ip, ips]) => ({
      ns: "lab", name: `k3s-demo-${role}-1`, status: "Running", run_strategy: "RerunOnFailure", running: true, node: "harvester-node2",
      cores: 2, memory: "1Gi", ip, ips, network: "default/lan", os: "Ubuntu 26.04.1 LTS", description: "",
      cluster: "k3s-demo", cluster_role: role,
      usage: role === "server" ? { cpu: 0.71, cpu_pct: 35.5, mem: 0.84 * 1024 ** 3, mem_pct: 84, read_bps: 40960, write_bps: 2.4 * 1024 ** 2 }
        : { cpu: 0.12, cpu_pct: 6, mem: 0.52 * 1024 ** 3, mem_pct: 52, read_bps: 0, write_bps: 120 * 1024 },
      nics: [{ name: "default", model: "virtio", network: "default/lan", mac: "52:54:00:12:34:" + (role === "server" ? "01" : "02"), ips }],
      disks: [vmDisk(`k3s-demo-${role}-1-disk`, "10Gi")], migratable: true, restart_required: false, problem: "",
      created: "2026-09-25T12:00:00Z", actions: ["console", "stop", "restart", "pause", "migrate"] })),
    { ns: "default", name: "win11", status: "Stopped", run_strategy: "Halted", running: false, node: "", cores: 4, memory: "8Gi", ip: "",
      os: "windows", description: "", nics: [{ name: "default", model: "e1000", network: "default/vlan1", mac: "52:54:00:aa:bb:cc", ips: [] }],
      disks: [vmDisk("win11-disk-0", "80Gi"), { name: "cdrom", kind: "cd-rom", claim: "win11-iso", boot: 2, bus: "sata", size: "6Gi", storage_class: "" }],
      migratable: false, restart_required: false, problem: "", created: "2026-09-10T10:00:00Z", actions: ["start"] },
    { ns: "lab", name: "ubuntu-test", status: "ErrorUnschedulable", run_strategy: "RerunOnFailure", running: false, node: "", cores: 16, memory: "64Gi", ip: "",
      os: "ubuntu", description: "", nics: [{ name: "default", model: "virtio", network: "pod network", mac: "", ips: [] }],
      disks: [vmDisk("ubuntu-test-disk-0", "40Gi")], migratable: false, restart_required: true,
      problem: "0/3 nodes are available: 3 Insufficient memory.", created: "2026-09-23T10:00:00Z", actions: ["stop", "force-stop"] },
  ];
  // Each VM's logo as the server works it out (homestead_logos.os_logo).
  demoVms.forEach(v => { v.os_logo = /home\s*assistant/i.test(v.os) ? "homeassistant" : /windows/i.test(v.os) ? "windows" : /ubuntu/i.test(v.os) ? "ubuntu" : ""; });

  let portalLinks = [
    { id: "demo0", title: "Home Assistant", url: "http://192.0.2.215:8123", section: "Home", icon: "workload:lab/home-assistant", note: "", shown: { kind: "letter" } },
    { id: "demo1", title: "Frigate", url: "http://192.0.2.214:5000", section: "Home", icon: "workload:lab/frigate", note: "cameras", shown: { kind: "letter" } },
    { id: "demo2", title: "Gateway", url: "https://192.0.2.1", section: "Network", icon: "builtin:router", note: "UniFi gateway", shown: { kind: "builtin", src: "router" } },
    { id: "demo3", title: "Core switch", url: "http://192.0.2.2", section: "Network", icon: "builtin:switch", note: "", shown: { kind: "builtin", src: "switch" } },
    { id: "demo4", title: "Office AP", url: "http://192.0.2.3", section: "Network", icon: "builtin:wifi", note: "", shown: { kind: "builtin", src: "wifi" } },
    { id: "demo5", title: "NAS-01", url: "http://192.0.2.10", section: "Storage", icon: "builtin:nas", note: "Unraid", shown: { kind: "builtin", src: "nas" } },
  ];
  // A logo the demo can show without fetching anything: the app's initial on a colour.
  const demoLogo = (letter, hue) => `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="hsl(${hue} 52% 42%)"/><text x="32" y="43" font-family="sans-serif" font-size="30" font-weight="700" fill="#fff" text-anchor="middle">${letter}</text></svg>`)}`;
  const demoLogoCatalogue = () => workloads.filter(w => !w.platform && (w.images || [])[0]).flatMap((w, i) => [
    { name: w.name.replace(/(^|-)([a-z])/g, (_, dash, c) => (dash ? " " : "") + c.toUpperCase()), icon: demoLogo(w.name[0].toUpperCase(), (i * 47) % 360), repo: w.images[0], key: `${w.name}|${w.images[0]}` },
    { name: `${w.name}-alt`, icon: demoLogo(w.name[0].toUpperCase(), (i * 47 + 180) % 360), repo: `example/${w.name}-alt`, key: `${w.name}-alt|example` }]);
  const workloads = [
    { name: "frigate", ns: "lab", kind: "Deployment", group: "Home", failover: "wait", desired: 1, ready: 1, uptime: 472221,
      cpu: 0.84, mem_mb: 1840, nodes: ["harvester-node2"], hardware: ["igpu", "coral_usb"],
      images: ["ghcr.io/blakeblackshear/frigate:stable"], ports: [{ port: 5000, ip: "192.0.2.214" }], claims: ["frigate-config"],
      pod_count: 1, container_count: 1, pods: [pod("frigate", "harvester-node2", "ghcr.io/blakeblackshear/frigate:stable")] },
    { name: "home-assistant", ns: "lab", kind: "Deployment", failover: "move", group: "Home", desired: 1, ready: 1, uptime: 912400,
      cpu: 0.31, mem_mb: 738, nodes: ["harvester-node1"], hardware: [],
      images: ["ghcr.io/home-assistant/home-assistant:stable"], ports: [{ port: 8123, ip: "192.0.2.215" }],
      pod_count: 1, container_count: 1, pods: [pod("home-assistant", "harvester-node1", "ghcr.io/home-assistant/home-assistant:stable")] },
    { name: "paperless", ns: "lab", kind: "Deployment", failover: "move", desired: 1, ready: 1, uptime: 220190,
      cpu: 0.18, mem_mb: 512, nodes: ["harvester-node3"], hardware: [],
      images: ["ghcr.io/paperless-ngx/paperless-ngx:latest"], ports: [{ port: 8000, ip: "192.0.2.216" }],
      pod_count: 1, container_count: 1, pods: [pod("paperless", "harvester-node3", "ghcr.io/paperless-ngx/paperless-ngx:latest")] },
    // A first start part-way through its image, and a pod from before still stopping.
    { name: "doublecommander", ns: "lab", kind: "Deployment", failover: "move", desired: 1, ready: 0, uptime: 0,
      cpu: 0, mem_mb: 0, nodes: ["harvester-node1"], hardware: [], images: ["lscr.io/linuxserver/doublecommander:latest"],
      ports: [{ port: 3010, ip: "192.0.2.242" }], pod_count: 2, container_count: 2,
      pods: [
        { name: "doublecommander-796b957c77-g6sps", node: "harvester-node1", phase: "Pending", ready: false, restarts: 0,
          container_count: 1, pull: { state: "pulling", image: "lscr.io/linuxserver/doublecommander:latest", node: "harvester-node1",
            seconds: 48, percent: 37, done_bytes: 311 * 1024 ** 2, total_bytes: 842 * 1024 ** 2 },
          containers: [{ name: "doublecommander", image: "lscr.io/linuxserver/doublecommander:latest", kind: "app",
            state: "ContainerCreating", ready: false, restarts: 0 }] },
        { name: "doublecommander-796b957c77-cvfnm", node: "harvester-node1", phase: "Pending", ready: false, restarts: 0,
          terminating: true, container_count: 1,
          containers: [{ name: "doublecommander", image: "lscr.io/linuxserver/doublecommander:latest", kind: "app",
            state: "ImagePullBackOff", ready: false, restarts: 0,
            message: "Back-off pulling image \"lscr.io/linuxserver/doublecommander:latest\": failed to resolve reference: dial tcp: lookup lscr.io: i/o timeout" }] }] },
    // Homestead itself: its Stop asks first, since it takes this page with it.
    { name: "homestead", ns: "lab", kind: "Deployment", group: "Homestead", self: true, platform: "Homestead", homestead: "self", desired: 1, ready: 1, uptime: 86400,
      cpu: 0.04, mem_mb: 88, nodes: ["harvester-node1"], hardware: [],
      images: ["ghcr.io/homestead-lab/homestead:2.8.318-dev.6"], ports: [{ port: 8088, ip: "192.0.2.242" }],
      pod_count: 1, container_count: 1, pods: [pod("homestead", "harvester-node1", "ghcr.io/homestead-lab/homestead:2.8.318-dev.6")] },
    { name: "homestead-smb", ns: "lab", kind: "Deployment", group: "Homestead", managed_smb: true, platform: "Homestead", homestead: "smb",
      desired: 1, ready: 1, uptime: 86400, cpu: 0.01, mem_mb: 40, nodes: ["harvester-node2"], hardware: [],
      images: ["dperson/samba:latest"], ports: [{ port: 445, ip: "192.0.2.245" }],
      pod_count: 1, container_count: 1, pods: [pod("homestead-smb", "harvester-node2", "dperson/samba:latest")] },
  ];
  const storage = { cap_gb: 1392, avail_gb: 906, used_gb: 486, used_pct: 34.9,
    provisioned_gb: 670, actual_gb: 224, volumes: 8, healthy: 6, degraded: 1,
    faulted: 0, detached: 1, unknown: 1, attached: 7,
    disks: [{ node: "harvester-node1", cap_gb: 464, avail_gb: 312, sched_gb: 190 },
      { node: "harvester-node2", cap_gb: 464, avail_gb: 298, sched_gb: 210 },
      { node: "harvester-node3", cap_gb: 464, avail_gb: 296, sched_gb: 205 }],
    reasons: [{ name: "arr-dashboard-data", robustness: "degraded",
      reason: "no disk space to create the replicas required: 1 of 2 replicas scheduled" }] };
  const history = {
    cpu: [18,21,19,26,24,31,27,29,33,30,35,28,31,27,29,32,30,27,26,27,25,28,27,27],
    mem: [47,48,49,50,50,51,52,52,53,54,54,55,55,55,56,56,57,58,57,58,59,59,60,61],
    net_rx: [8,12,10,16,14,25,19,31,22,18,30,21,26,24,38,29,20,34,28,25,33,29,31,36],
    net_tx: [3,4,5,6,5,8,7,12,9,7,11,8,9,10,15,11,8,13,9,10,12,11,13,15],
  };
  const hardware = [
    { id: "igpu", name: "Intel/AMD iGPU", host_path: "/dev/dri", container_path: "/dev/dri", builtin: true },
    { id: "coral_usb", name: "Google Coral USB", host_path: "/dev/bus/usb", container_path: "/dev/bus/usb", builtin: false, usb_ids: ["18d1:9302"] },
  ];
  const volumes = [
    { name: "pvc-demo-frigate", pvc_name: "frigate-config", namespace: "lab", attached_to: "frigate, homestead-smb",
      attached: ["frigate", "homestead-smb"],
      pod_status: "Running", state: "attached", robustness: "healthy", node: "harvester-node2",
      size_gb: 20, actual_gb: 27.8, used_pct: 19, replicas: 2,
      copies: [{ node: "harvester-node2", disk: "OS disk", os: true, healthy: true, state: "running" },
        { node: "harvester-node3", disk: "nvme1n1", healthy: true, state: "running" }],
      filesystem: { used_gb: 3.8, capacity_gb: 19.6, used_pct: 19.4, source: "kubelet" },
      access_modes: ["ReadWriteOnce"], storage_class: "longhorn-r2", last_used_secs: 0 },
    { name: "pvc-demo-degraded", pvc_name: "arr-dashboard-data", namespace: "lab", attached_to: "arr-dashboard",
      attached: ["arr-dashboard"], node: "harvester-node1", state: "attached", robustness: "degraded",
      size_gb: 5, actual_gb: 3.1, used_pct: 62, replicas: 2, access_modes: ["ReadWriteOnce"],
      copies: [{ node: "harvester-node1", disk: "OS disk", os: true, healthy: true, state: "running" }],
      filesystem: { used_gb: 2.8, capacity_gb: 4.8, used_pct: 58.3, source: "kubelet" },
      storage_class: "longhorn-r2", last_used_secs: 0, created: "2026-09-19T08:00:00Z",
      health_reason: "no disk space to create the replicas required: 1 of 2 replicas scheduled",
      conditions: [{ type: "Scheduled", status: "False", reason: "ReplicaSchedulingFailure",
        message: "no disk space to create the replicas required: 1 of 2 replicas scheduled" }],
      scheduling_error: "" },
    // A replica catching up after its node came back, and a backup being
    // restored into a new volume: both show Longhorn's live percentage.
    { name: "pvc-demo-rebuild", pvc_name: "jellyfin-config", namespace: "lab", attached_to: "jellyfin",
      attached: ["jellyfin"], node: "harvester-node3", state: "attached", robustness: "degraded",
      size_gb: 10, actual_gb: 4.6, used_pct: 46, replicas: 3, access_modes: ["ReadWriteOnce"],
      filesystem: { used_gb: 4.2, capacity_gb: 9.8, used_pct: 42.9, source: "kubelet" },
      storage_class: "longhorn-r3", last_used_secs: 0, pod_status: "Running",
      health_reason: "a replica is rebuilding; the volume is readable and writable meanwhile",
      rebuild: { pct: 63, replicas: 1, error: "" } },
    { name: "pvc-demo-restore", pvc_name: "paperless-data-restored", namespace: "lab", attached_to: "",
      attached: [], node: "harvester-node1", state: "attached", robustness: "degraded",
      size_gb: 20, actual_gb: 7.4, used_pct: 37, replicas: 2, access_modes: ["ReadWriteOnce"],
      storage_class: "longhorn-r2", last_used_secs: 0, pod_status: "",
      restore: { pct: 41, error: "" } },
    { name: "pvc-demo-scratch", pvc_name: "scratch-test", namespace: "lab", attached_to: "",
      pod_status: "", state: "detached", robustness: "unknown", node: "",
      // Longhorn reports this on a resting volume; it is not a fault.
      health_reason: "", size_gb: 5, actual_gb: 0.2, used_pct: 4, replicas: 2,
      access_modes: ["ReadWriteOnce"], storage_class: "longhorn-r2", last_used_secs: 2400,
      // Nothing refers to it: the kind to think about deleting.
      used_by: [] },
    // A stopped container's volume: detached, and its data waiting for it.
    { name: "pvc-demo-nextcloud", pvc_name: "nextcloud-data", namespace: "lab", attached_to: "",
      pod_status: "", state: "detached", robustness: "unknown", node: "", health_reason: "",
      size_gb: 100, actual_gb: 38.2, used_pct: 38, replicas: 2, access_modes: ["ReadWriteOnce"],
      storage_class: "longhorn-r2", last_used_secs: 86400 * 6, used_by: ["Deployment/nextcloud"],
      // Detached a copy short: Longhorn will not rebuild it until something attaches it.
      copies_short: { whole: 1, wanted: 2, offline: "ignored" } },
    // An original kept after a storage class change: its claim is the copy now.
    { name: "pvc-7f3e9c1a-2b44-4d1b-9a55-0c1f2e3d4a5b", pvc_name: "mosquitto-appdata", namespace: "lab", attached_to: "",
      pod_status: "", state: "detached", robustness: "unknown", node: "", health_reason: "",
      size_gb: 10, actual_gb: 0.3, used_pct: 3, replicas: 2, access_modes: ["ReadWriteOnce"],
      storage_class: "longhorn-r2", last_used_secs: 86400 * 2, used_by: null, unclaimed: true },
  ];
  const shares = [
    { name: "media", pvc: "share-media", path: "/shares/media", size_gb: 250,
      actual_size_gb: 250, pvc_status: "Bound", access_modes: ["ReadWriteMany"],
      nfs_clients: "192.0.2.0/24", nfs_read_only: true, user: "lab", public: true,
      read_only: false, has_password: false, created: "2026-09-18 22:26" },
    { name: "secure", pvc: "share-secure", path: "/shares/secure", size_gb: 20,
      actual_size_gb: 20, pvc_status: "Bound", user: "lab", public: false, owned: true,
      read_only: true, has_password: true, created: "2026-09-18 22:39" },
    { name: "photos", pvc: "frigate-config", path: "/shares/photos", sub_path: "clips",
      size_gb: 20, actual_size_gb: 20, pvc_status: "Bound", user: "lab", public: false,
      owned: false, read_only: true, has_password: true, created: "2026-09-19 08:12" },
  ];
  const lhBackups = [{ name: "backup-demo-frigate-20260919", volume: "pvc-demo-frigate",
    state: "Completed", progress: 100, size_mb: 1842.6, volume_size_gb: 20,
    created: "2026-09-19T02:14:32Z", error: "", target: "default", restorable: true }];
  const vmDisks = [
    { namespace: "lab", name: "ubuntu-2404", pvc: "ubuntu-2404", phase: "Succeeded",
      progress: 100, capacity: "40Gi", storage_class: "longhorn-r2",
      access_modes: ["ReadWriteOnce"], message: "", in_use: false, used_by: [] },
    { namespace: "lab", name: "router-migration", pvc: "router-migration", phase: "ImportInProgress",
      progress: 63.4, capacity: "16Gi", storage_class: "longhorn-r2",
      access_modes: ["ReadWriteOnce"], message: "", in_use: false, used_by: [] },
  ];
  const lhOverview = {
    total: 3, protected: 2, backed_up: 2, unprotected: ["homeassistant-config"], groups: ["critical", "default", "media"],
    group_rows: [{ name: "critical", volumes: ["pvc-demo-frigate"], jobs: ["hourly-snapshot"] },
      { name: "default", volumes: ["pvc-demo-frigate", "pvc-demo-scratch"], jobs: ["nightly-backup"] },
      { name: "media", volumes: ["pvc-demo-hass"], jobs: ["media-weekly-trim"] }],
    target: { configured: true, available: true, name: "default",
      url: "s3://homestead-backups@us-east-1/", reason: "", interval: "5m",
      secret: "homestead-backup-credentials" },
    tasks: { snapshot: "Snapshot — point-in-time, stored on the volume",
      backup: "Backup — snapshot then upload to the backup target",
      "snapshot-cleanup": "Cleanup — purge system snapshots",
      "filesystem-trim": "Trim — reclaim space the guest has freed" },
    jobs: [{ name: "nightly-backup", task: "backup", cron: "0 2 * * *", retain: 7,
      concurrency: 1, groups: ["default"], covers: 2,
      volumes: ["pvc-demo-frigate", "pvc-demo-scratch"], desc: "Nightly external backup",
      last_run: "2026-05-11T02:00:00Z", last_success: "2026-05-11T02:04:12Z", running: 0, last_failed: false },
      { name: "hourly-snapshot", task: "snapshot", cron: "0 * * * *", retain: 24,
      concurrency: 2, groups: ["critical"], covers: 1, volumes: ["pvc-demo-frigate"],
      desc: "Snapshot — point-in-time, stored on the volume", last_run: "", last_success: "", running: 0, last_failed: false },
      { name: "media-weekly-trim", task: "filesystem-trim", cron: "0 4 * * 6", retain: 0,
      concurrency: 1, groups: ["media"], covers: 1, volumes: ["pvc-demo-hass"],
      desc: "Trim — reclaim space the guest has freed", last_run: "", last_success: "", running: 0, last_failed: false }],
    volumes: [
      { name: "pvc-demo-frigate", pvc: "frigate-config", namespace: "lab", size_gb: 20,
        robustness: "healthy", state: "attached", labels: {}, jobs: [], groups: ["critical", "default"],
        last_backup: lhBackups[0].name, last_backup_at: lhBackups[0].created,
        protected_by: ["hourly-snapshot", "nightly-backup"], snapshotted: true, backed_up: true },
      { name: "pvc-demo-scratch", pvc: "scratch-test", namespace: "lab", size_gb: 5,
        robustness: "healthy", state: "detached", labels: {}, jobs: [], groups: ["default"],
        last_backup: "", last_backup_at: "", protected_by: ["nightly-backup"], snapshotted: false, backed_up: true },
      { name: "pvc-demo-hass", pvc: "homeassistant-config", namespace: "lab", size_gb: 2,
        robustness: "healthy", state: "attached", labels: {}, jobs: [], groups: ["media"],
        last_backup: "", last_backup_at: "", protected_by: [], snapshotted: false, backed_up: false },
    ],
  };
  const lhBackupVolumes = [
    { name: "pvc-demo-frigate", id: "pvc-demo-frigate", pvc: "frigate-config", exists: true,
      last_backup: lhBackups[0].name, last_backup_at: lhBackups[0].created, size_mb: 1842.6, count: 1, target: "default" },
    { name: "pvc-demo-paperless", id: "pvc-demo-paperless", pvc: "paperless-data", exists: false,
      last_backup: "backup-demo-paperless-20260901", last_backup_at: "2026-09-01T02:31:07Z", size_mb: 3420.2, count: 6, target: "default" },
  ];
  const network = {
    controller: { name: "kube-vip", installed: true, desired: 3, ready: 3, healthy: true,
      mode: "ARP Service controller · explicit VIP allocation" },
    summary: { services: 8, app_services: 3, load_balancers: 3, vips: 3,
      listeners: 3, unhealthy: 0, ready_endpoints: 3 },
    available_vips: ["192.0.2.230", "192.0.2.231", "192.0.2.217", "192.0.2.218"], available_vip_count: 4,
    registered_vips: [{ ip: "192.0.2.214", label: "Frigate", free: false, used_by: ["lab/frigate"] },
      { ip: "192.0.2.230", label: "Shares", free: true, used_by: [] },
      { ip: "192.0.2.231", label: "Spare", free: true, used_by: [] }],
    vip_labels: { "192.0.2.214": "Frigate", "192.0.2.230": "Shares", "192.0.2.231": "Spare" },
    platform_addresses: { "192.0.2.210": "kube-system/ingress-expose" }, foreign_addresses: {},
    platform_clashes: [], shared_vip: { ip: "192.0.2.242", problem: "" },
    node_ips: ["192.0.2.207", "192.0.2.208", "192.0.2.210"], conflicts: [],
    pools: [{ name: "lab-pool", ready: true, total: 6, reported_available: 2,
      ranges: [{ start: "192.0.2.214", end: "192.0.2.219", candidate_count: 6 }] }],
    workloads: workloads.map(row => ({ namespace: row.ns, name: row.name, replicas: row.desired,
      ports: row.ports.map(port => ({ name: "web", port: port.port, protocol: "TCP" })) })),
    vips: workloads.map(row => ({ ip: row.ports[0].ip, shared: false, services: 1,
      listeners: [{ namespace: row.ns, service: row.name, port: row.ports[0].port,
        protocol: "TCP", access: `http://${row.ports[0].ip}:${row.ports[0].port}`,
        browser: true, health: "healthy" }] })),
    services: workloads.map(row => ({ namespace: row.ns, name: row.name, type: "LoadBalancer",
      system: false, managed: true, cluster_ip: `10.43.0.${20 + workloads.indexOf(row)}`,
      external_ips: [row.ports[0].ip], assigned_ips: [row.ports[0].ip], requested_ips: [row.ports[0].ip],
      vip_host: "harvester-node1", selector: { app: row.name }, targets: [row.name],
      ports: [{ name: "web", port: row.ports[0].port, target_port: row.ports[0].port,
        protocol: "TCP", access: `http://${row.ports[0].ip}:${row.ports[0].port}`, browser: true }],
      endpoints: { ready: [{ addresses: [`10.42.0.${30 + workloads.indexOf(row)}`],
        node: row.nodes[0], target_kind: "Pod", target: row.pods[0].name }], not_ready: [], ports: [] },
      ready_endpoints: 1, not_ready_endpoints: 0, health: "healthy", reason: "1 ready endpoint",
      orphaned: false })).concat([{ namespace: "lab", name: "sonarr-old", type: "LoadBalancer",
      system: false, managed: true, cluster_ip: "10.43.0.44", external_ips: ["192.0.2.246"],
      assigned_ips: ["192.0.2.246"], requested_ips: ["192.0.2.246"], vip_host: "harvester-node1",
      selector: { app: "sonarr-old" }, targets: [], orphaned: true,
      ports: [{ name: "web", port: 8989, target_port: 8989, protocol: "TCP",
        access: "http://192.0.2.246:8989", browser: true }],
      endpoints: { ready: [], not_ready: [], ports: [] }, ready_endpoints: 0,
      not_ready_endpoints: 0, health: "unavailable",
      reason: "No ready endpoints match the Service selector" }]),
    ingresses: [],
  };
  /* Nodes & addresses: node1 answers for the management address and the app
     VIPs; the file shares' address is answered for and left off its Service,
     the fault Homestead records for kube-vip. */
  const listener = (port, service, workloads, ready = 1) => ({ port, protocol: "TCP", namespace: service === "ingress-expose" ? "kube-system" : "lab", service, workloads, ready });
  const place = (ip, node, listeners, state = "ok", reason = "") => ({ ip, kind: "vip", node, controller: "kube-vip", listeners,
    services: [], unrouted: state === "unrouted" ? [{ namespace: "lab", name: "homestead-smb" }] : [], ready: 1, announced: !!node, state, reason });
  network.addresses = {
    nodes: nodes.map(n => ({ name: n.name, ips: [demoNodeIps[n.name]], ready: n.status === "Ready", control_plane: n.roles.includes("control-plane"),
      vips: n.name === "harvester-node1" ? ["192.0.2.210", "192.0.2.214", "192.0.2.215", "192.0.2.216", "192.0.2.242", "192.0.2.245"]
        : n.name === "harvester-node3" ? ["192.0.2.217"] : [] })),
    addresses: [
      ...Object.entries(demoNodeIps).map(([node, ip]) => ({ ip, kind: "node", node, controller: "", listeners: [], services: [], unrouted: [], ready: 0, announced: false, state: "ok", reason: "" })),
      place("192.0.2.210", "harvester-node1", [listener(443, "ingress-expose", [])]),
      place("192.0.2.214", "harvester-node1", [listener(5000, "frigate", ["frigate"])]),
      place("192.0.2.215", "harvester-node1", [listener(8123, "home-assistant", ["home-assistant"])]),
      place("192.0.2.216", "harvester-node1", [listener(8000, "paperless", ["paperless"])]),
      place("192.0.2.217", "harvester-node3", [listener(22, "ubuntu-ssh", ["VirtualMachine/ubuntu"])]),
      place("192.0.2.242", "harvester-node1", [listener(3010, "doublecommander", ["doublecommander"]), listener(8088, "homestead", ["homestead"])]),
      place("192.0.2.245", "harvester-node1", [listener(445, "homestead-smb", ["homestead-smb"])], "unrouted",
        "harvester-node1 answers for 192.0.2.245, but lab/homestead-smb does not carry it, so connections to its ports are refused"),
    ],
    problems: 1,
    kept: [{ at: Math.floor(Date.now() / 1000) - 3600, namespace: "lab", name: "homestead-objectstore", ips: ["192.0.2.242"], node: "harvester-node1" }],
  };

  const restorePlan = url => {
    const ns = url.searchParams.get("ns") || "";
    const name = url.searchParams.get("name") || "";
    const conflict = name === "frigate-config" ? { kind: "PersistentVolumeClaim", name,
      message: `PVC ${ns}/${name} already exists; choose a new name` } : null;
    return { backup: lhBackups[0].name, source_volume: "pvc-demo-frigate",
      created: lhBackups[0].created, backup_size_mb: lhBackups[0].size_mb,
      volume_size_bytes: 21474836480, minimum_size_gb: 20,
      suggested_name: "pvc-demo-frigate-restore", namespace: ns, pvc_name: name,
      conflict, ready: !conflict, target: "default", storage_class: "longhorn-r2",
      storage_classes: ["longhorn-r2", "longhorn-fast"] };
  };
  const volumeDeletePlan = url => {
    const name = url.searchParams.get("name") || "scratch-test";
    const attached = name === "frigate-config";
    return {
      namespace: "lab", name, uid: `demo-${name}`, resource_version: "42", phase: "Bound",
      storage_class: "longhorn-r2", access_modes: ["ReadWriteOnce"],
      requested_storage: attached ? "20Gi" : "5Gi",
      pv: { name: `pvc-demo-${name}`, reclaim_policy: "Delete", driver: "driver.longhorn.io" },
      longhorn: { name: `pvc-demo-${name}`, state: attached ? "attached" : "detached",
        robustness: attached ? "healthy" : "unknown", attached_node: attached ? "harvester-node2" : "",
        replicas: 2, actual_bytes: attached ? 4080218931 : 214748364, actual_gb: attached ? 3.8 : 0.2 },
      consumers: attached ? [
        { kind: "Pod", name: "frigate-7d8f6d4c9-demo", namespace: "lab", active: true,
          detail: "Running on harvester-node2", mounts: [{ container: "frigate", container_kind: "app", path: "/config", read_only: false }] },
        { kind: "Deployment", name: "frigate", namespace: "lab", active: true,
          detail: "1 desired replica", mounts: [{ container: "frigate", container_kind: "app", path: "/config", read_only: false }] },
      ] : [{ kind: "Job", name: "homestead-import-frigate", namespace: "lab", active: false,
        detail: "not running", mounts: [{ container: "copy", container_kind: "app", path: "/appdata" }] }],
      active_consumers: attached ? 2 : 0,
      snapshots: { count: attached ? 3 : 1, names: ["daily"] },
      backups: { count: 1, names: ["nightly"] },
      data_present: true, inventory_complete: true, warnings: [],
      blocked: attached,
      blocking_reasons: attached ? [
        "2 active workload reference(s) must be stopped and unmounted first",
        "Longhorn still reports the volume attached to harvester-node2",
      ] : [],
      stale_consumers: attached ? [] : [{ kind: "Job", name: "homestead-import-frigate", namespace: "lab",
        active: false, detail: "not running", mounts: [{ container: "copy", path: "/appdata" }] }],
      removable_jobs: attached ? [] : ["homestead-import-frigate"],
      actions: {
        detach: { complete: !attached, description: "Stop/unmount consumers while keeping the claim and all data." },
        delete_claim: { enabled: !attached, description: "Delete the PVC and retain backing data." },
        delete_data: { enabled: !attached, description: "Delete the PVC and backing data." },
      },
    };
  };
  const deployOptions = {
    deployments: workloads.filter(w => w.ns === "lab").map(w => ({ name: w.name,
      containers: w.pods[0].containers.map(c => c.name),
      volumes: w.name === "frigate" ? [{ name: "config", kind: "pvc", source: "frigate-config" }] : [] })),
    pvcs: volumes.map(v => ({ name: v.pvc_name, size: `${v.size_gb}Gi`, status: "Bound",
      access_modes: v.access_modes, storage_class: v.storage_class,
      robustness: v.robustness, node: v.node, migratable: v.storage_class === "longhorn-r2",
      workloads: v.attached || [] })),
    storage_classes: ["harvester-longhorn", "longhorn", "longhorn-r2"],
    shared_storage_classes: ["longhorn"],
    storage_class_facts: {
      "harvester-longhorn": { replicas: "3", migratable: true, encrypted: false, expandable: true, reclaim: "Delete", default: true },
      longhorn: { replicas: "3", migratable: false, encrypted: false, expandable: true, reclaim: "Delete", default: false },
      "longhorn-r2": { replicas: "2", migratable: true, encrypted: false, expandable: true, reclaim: "Retain", default: false },
    },
  };
  const demoApp = { name: "Frigate", repo: "ghcr.io/blakeblackshear/frigate:stable", icon: "", cat: "HomeAutomation",
    desc: "Network video recorder with local AI object detection.", downloads: 24800000, stars: 42000,
    trending: 7.8, top_trending: 5.4, top_performing: 7.8, first_seen: 1640995200,
    maintainer: "blakeblackshear", official: true, categories: ["HomeAutomation", "Security"],
    spotlight: { date: 1785556800, month: "Aug 2026", reason: "Local AI object detection for every camera you own.", who: "Homestead demo" },
    deploy: {
      name: "frigate", image: "ghcr.io/blakeblackshear/frigate:stable", icon: "",
      ports: [{ container: 8971, host: 8971, expose: true, protocol: "TCP" },
              { container: 8555, host: 8555, expose: true, protocol: "TCP" },
              { container: 8555, host: 8555, expose: true, protocol: "UDP" }],
      env: { FRIGATE_RTSP_PASSWORD: "change-me", LIBVA_DRIVER_NAME: "iHD" },
      env_meta: [{ key: "FRIGATE_RTSP_PASSWORD", label: "Frigate RTSP password", required: true, masked: true }],
      volumes: [{ path: "/config", source: "frigate-data", type: "pvc", create: true, size_gb: 5,
        access_mode: "ReadWriteOnce", label: "Config path", required: true, template_source: "/mnt/user/appdata/frigate" },
        { path: "/media/frigate", source: "frigate-data2", type: "pvc", create: true, size_gb: 5,
          access_mode: "ReadWriteOnce", label: "Media path", required: true, template_source: "/mnt/user/Media/frigate" }],
      template_devices: [{ host_path: "/dev/bus/usb", container_path: "/dev/bus/usb", label: "Coral TPU" },
                         { host_path: "/dev/dri/renderD128", container_path: "/dev/dri/renderD128", label: "iGPU" }],
      app_profile: { family: "frigate", level: "guided", label: "Hardware review", notes: [
        "Imported device paths match the reusable Coral TPU and iGPU hardware features.",
        "Choose existing storage for retained recordings or create a suitably sized Longhorn claim.",
      ], dependencies: [] },
    } };
  const responses = {
    "/api/auth/state": { setup: false, user: "demo", role: "admin", remember: true,
      session_started: Math.floor(Date.now() / 1000) - 86400 * 3,
      session_expires: Math.floor(Date.now() / 1000) + 86400 * 27,
      session_max_days: 90 },
    "/api/node/probe/install": { state: "installed",
      detail: "homestead-nodeprobe installed; each node reports once its pod is ready" },
    "/api/node/probe/remove": { state: "absent", detail: "the node probe was removed" },
    "/api/node/probe/allocation": {installed: true, enabled: false, managed: false, directory: "",
      uid: "demo-probe", resource_version: "1", detail: "Placement checks are disabled (demo; no host changes)",
      capacity: {blocked:false, blockers:[], warnings:[], nodes:[], fingerprint:"demo"}},
    "/api/settings": { thresholds: { cpu: { warning: 70, critical: 88 }, memory: { warning: 70, critical: 88 }, disk: { warning: 75, critical: 90 }, temperature: { warning: 70, critical: 85 } }, smart: { temperature: { warning: 55, critical: 65 }, reallocated_warning: 1, pending_critical: 1, uncorrectable_critical: 1, notify_failures: true }, updates: { policy: "approval_required", notify_available: true, notify_failures: true }, site_name: "Main site",
      info: { version: "2.8.318-dev.6", namespace: "lab", storage_class: "longhorn-r2", vip: "192.0.2.242",
        kubernetes: "v1.32.4+rke2r1",
        node_probe: { state: "updated", detail: "homestead-nodeprobe updated to this release's scripts" },
        permissions: { state: "current", detail: "homestead has everything this release uses" } } },
    "/api/overview": { health: "healthy", health_state: "healthy", health_summary: "All cluster services are healthy", health_issues: [],
      cpu_pct: 27.2, cpu_used: 5.4, cpu_cap: 20, mem_pct: 54.0, mem_used_gb: 25.2, mem_cap_gb: 46.8,
      nodes_ready: 3, nodes_total: 3, workload_pods: 16, system_pods: 116, lb_ip: "192.0.2.242", nodes,
      top_cpu: [{ name: "frigate", ns: "lab", nodes: ["harvester-node2"], cpu: .84 }, { name: "home-assistant", ns: "lab", nodes: ["harvester-node1"], cpu: .31 }, { name: "paperless", ns: "lab", nodes: ["harvester-node3"], cpu: .18 }],
      top_mem: [{ name: "frigate", ns: "lab", nodes: ["harvester-node2"], mem_mb: 1840 }, { name: "home-assistant", ns: "lab", nodes: ["harvester-node1"], mem_mb: 738 }, { name: "paperless", ns: "lab", nodes: ["harvester-node3"], mem_mb: 512 }] },
    "/api/history": history, "/api/storage": storage, "/api/volumes": volumes,
    "/api/nodes": nodes, "/api/nodes/uptime": demoUptime,
    // Each host's ports (host-ports.js): one plain uplink with a spare, one
    // bond running on a slow member with CRC errors, one LACP bond the switch
    // is not answering.
    // Harvester uplinks (host-ports.js): mgmt shown only, a data network
    // bonded on two hosts and missing on the third.
    // Bonding a k3s host's NICs (host-ports.js): a host on enp1s0 with a spare
    // enp2s0 at the same speed and enp3s0 without a cable.
    "/api/node/bond/inspect": (url, init) => ({ node: JSON.parse(init.body).node, address: "192.0.2.12/24", dhcp: true, problem: "",
      modes: ["active-backup", "802.3ad", "balance-alb", "balance-tlb"], rollback_seconds: 240,
      shape: { iface: "enp1s0", shape: "nic", bond: "", members: [], mode: "", carrier_nic: "enp1s0", bridge: "", file: "/etc/netplan/50-cloud-init.yaml" },
      nics: [{ name: "enp1s0", mac: "52:54:00:0a:00:01", carrier: true, speed: 2500, master: "" },
             { name: "enp2s0", mac: "52:54:00:0a:00:02", carrier: true, speed: 2500, master: "" },
             { name: "enp3s0", mac: "52:54:00:0a:00:03", carrier: false, speed: null, master: "" }] }),
    "/api/node/bond/preview": (url, init) => {
      const req = JSON.parse(init.body), members = req.members || [];
      const refusals = [];
      if (members.length < 2) refusals.push("a bond needs two or more NICs");
      if (members.includes("enp3s0") && !req.allow_down) refusals.push("enp3s0 has no link: plug it in, or confirm you want it in the bond anyway");
      if (req.mode === "802.3ad" && !req.lacp_confirmed) refusals.push("802.3ad needs the switch ports to be one LACP group: confirm they are, or choose active-backup, which works on any switch");
      return { node: req.node, action: req.action, bond: "bond0", members, mode: req.mode || "active-backup", primary: req.primary, keep: "",
        address: "192.0.2.12/24", carries_on: "bond0", renamed: true, refusals, digest: "demo", rollback_seconds: 240,
        shape: { iface: "enp1s0", shape: "nic", bridge: "", carrier_nic: "enp1s0" },
        warnings: ["the host's address moves to bond0: kube-vip restarts there, and on a cluster of several hosts k3s restarts so flannel follows. Containers keep running"] };
    },
    "/api/node/bond": (url, init) => ({ ok: true, detail: `${JSON.parse(init.body).node}'s network is changing; follow it in the job tray` }),
    "/api/network/uplinks": () => demoUplinks(),
    "/api/network/uplinks/preview": (url, init) => {
      const req = JSON.parse(init.body), inv = demoUplinks();
      const config = inv.networks.flatMap(n => n.configs).find(c => c.name === req.config);
      const cn = config?.cluster_network || req.cluster_network, nodes = config?.nodes || req.nodes || [];
      const nics = req.action === "remove" ? config.nics : req.nics || [];
      const refusals = [];
      if (cn === "mgmt") refusals.push("mgmt's uplink is set when Harvester installs, and Harvester does not change it afterwards.");
      if (req.action !== "remove" && !nics.length) refusals.push("choose at least one NIC");
      if (req.action === "remove") refusals.push("Harvester will not take the uplink away while these VMs on data's networks run on harvester-node1, harvester-node2: lab/nas. Stop or move them first.");
      if (req.mode === "802.3ad" && nics.length > 1 && !req.lacp_confirmed) refusals.push("802.3ad needs the switch ports to be one LACP group: confirm they are, or choose active-backup, which works on any switch");
      return { action: req.action, cluster_network: cn, config: req.config || "", nodes, nics, mode: req.mode || config?.mode || "active-backup",
        mtu: req.mtu || null, new_network: !!req.new_network, lan_networks: cn === "data" ? ["lab/vlan20"] : [], refusals,
        warnings: config && config.nodes.length > 1 && req.action === "change" ? [`${config.name} is the uplink of ${config.nodes.length} hosts (${config.nodes.join(", ")}): they all change`] : [],
        digest: "demo" };
    },
    "/api/network/uplinks/apply": () => ({ ok: true, detail: "Sent to Harvester; follow each host in the job tray" }),
    "/api/nodes/ports": url => {
      const report = demoPorts();
      const node = url.searchParams.get("node");
      return node ? report.hosts[node] || { node, available: false, ports: [], conditions: [], reason: "No node probe answers on this host." } : report;
    }, "/api/node": url => nodes.find(n => n.name === url.searchParams.get("name")) || {},
    // A host's own OS, as the leader reads it on k3s and RKE2 (host-os.js).
    // Homestead's own services, on node addresses until put on a VIP (views-network.js).
    "/api/self/address": { on_vip: false, url: "", shared_vip: "192.0.2.242", components: [
      { id: "web", label: "Homestead's web page", namespace: "lab", workload: "homestead", present: true, vip: "", vip_service: "",
        node_addresses: ["192.0.2.207", "192.0.2.208"], ports: [{ name: "http", port: 8088, target_port: 8080, protocol: "TCP" }] },
      { id: "objectstore", label: "Backup storage (S3)", namespace: "lab", workload: "homestead-objectstore", present: true, vip: "192.0.2.242", vip_service: "homestead-objectstore",
        node_addresses: [], ports: [{ name: "s3", port: 9000, target_port: "s3", protocol: "TCP" }, { name: "console", port: 9001, target_port: "console", protocol: "TCP" }] },
      { id: "smb", label: "Network shares (SMB)", namespace: "lab", workload: "homestead-smb", present: true, vip: "", vip_service: "",
        node_addresses: ["192.0.2.207"], ports: [{ name: "smb", port: 445, target_port: 445, protocol: "TCP" }] }] },
    "/api/self/address/plan": { vip: "192.0.2.242", steps: [
      { id: "web", label: "Homestead's web page", action: "add", detail: "8088 on 192.0.2.242, as homestead-vip" },
      { id: "objectstore", label: "Backup storage (S3)", action: "kept", detail: "already on 192.0.2.242" },
      { id: "smb", label: "Network shares (SMB)", action: "add", detail: "445 on 192.0.2.242, as homestead-smb-vip" }] },
    "/api/network/vips/change": { old: "192.0.2.242", new: "192.0.2.210", default: true, label: "Main VIP",
      services: [{ namespace: "lab", name: "homestead-vip", ports: ["8088/TCP"], targets: ["homestead"] },
        { namespace: "lab", name: "frigate", ports: ["5000/TCP"], targets: ["frigate"] }], detail: "192.0.2.242 is now 192.0.2.210; 2 Services moved with it" },
    "/api/welcome": { show: false, done: false, harvester: false, load_balancer: "kube-vip", steps: {
      address: { done: false, url: "", shared_vip: "192.0.2.242", vips: 2 }, probe: { done: true }, backups: { done: false },
      updates: { applies: true, done: false } } },
    // A host's devices for VMs (passthrough.js): a GPU handed over, a NIC the host needs.
    "/api/passthrough/inspect": { node: "harvester-node1", harvester: false, iommu: true, cmdline_iommu: true, cpu: "intel", complete: true, kubevirt: true,
      pci: [
        { address: "0000:01:00.0", vendor: "10de", device: "1e87", class: "0300", class_name: "VGA compatible controller", name: "NVIDIA Corporation TU104 [GeForce RTX 2080]",
          driver: "vfio-pci", group: "12", boot_vga: false, nets: [], vfio: true, listed: true, permitted: true, problems: [], group_members: ["0000:01:00.1"], offered: true, resource: "homestead.io/pci-10de-1e87" },
        { address: "0000:01:00.1", vendor: "10de", device: "10f8", class: "0403", class_name: "Audio device", name: "NVIDIA Corporation TU104 HD Audio",
          driver: "vfio-pci", group: "12", boot_vga: false, nets: [], vfio: true, listed: true, permitted: true, problems: [], group_members: ["0000:01:00.0"], offered: true, resource: "homestead.io/pci-10de-10f8" },
        { address: "0000:03:00.0", vendor: "8086", device: "1533", class: "0200", class_name: "Ethernet controller", name: "Intel Corporation I210 Gigabit",
          driver: "igb", group: "15", boot_vga: false, nets: ["enp3s0"], vfio: false, listed: false, permitted: false, problems: ["it carries this host's network"], group_members: [], offered: true, resource: "homestead.io/pci-8086-1533" },
        { address: "0000:00:00.0", vendor: "8086", device: "3e30", class: "0600", class_name: "Host bridge", name: "Intel Corporation 8th Gen Core Host Bridge",
          driver: "skl_uncore", group: "0", boot_vga: false, nets: [], vfio: false, listed: false, permitted: false, problems: [], group_members: [], offered: false, resource: "" }],
      usb: [{ vendor: "1a6e", product: "089a", name: "Google Coral TPU (unflashed)", port: "1-4", resource: "homestead.io/usb-1a6e-089a", permitted: true },
        { vendor: "1cf1", product: "0030", name: "dresden elektronik ConBee II", port: "1-2", resource: "homestead.io/usb-1cf1-0030", permitted: false }], listed: [] },
    "/api/passthrough/resources": { sidecar: true, resources: [
      { resource: "homestead.io/pci-10de-1e87", kind: "pci", label: "NVIDIA Corporation TU104 [GeForce RTX 2080]", selector: "10DE:1E87", devices: [{node: "harvester-node1", address: "0000:01:00.0", group: "12"}], nodes: ["harvester-node1"] },
      { resource: "homestead.io/usb-1a6e-089a", kind: "usb", label: "Google Coral TPU (unflashed)", selector: "1a6e:089a", nodes: ["harvester-node1", "harvester-node3"] }] },
    // Every host's OS, one at a time (host-os.js): one host done, one restarting.
    "/api/os-updates": () => {
      const host = name => ({ os: "Ubuntu 24.04.3 LTS", updates: name === "harvester-node3" ? [{ name: "openssl", security: true }] : [],
        security: name === "harvester-node3" ? 1 : 0, reboot: false,
        auto: { tool: "unattended-upgrades", on: name !== "harvester-node1", reboots: name === "harvester-node3", held: name === "harvester-node1" } });
      return { applies: true, hosts: Object.fromEntries(["harvester-node1", "harvester-node2", "harvester-node3"].map(n => [n, host(n)])),
        settings: { schedule: { enabled: true, days: ["sun"], hour: 3, tz: "Europe/London", offset_min: 60 }, reboot: "when-needed", single_copy: false, manage: "ubuntu" },
        rollout: { id: "os-1", status: "running", nodes: ["harvester-node2", "harvester-node3", "harvester-node1"], index: 1,
          message: "harvester-node3: draining and restarting",
          results: [{ node: "harvester-node2", ok: true, note: "updates installed", updates: 4, security: 2, restarted: false }] },
        last: null };
    },
    "/api/node/os": url => {
      const name = url.searchParams.get("name") || "harvester-node1", at = Math.floor(Date.now() / 1000) - 5400, gb = 1024 ** 3;
      const updates = [["libc6", true], ["openssl", true], ["linux-image-6.8.0-86-generic", true], ["tzdata", false], ["curl", false], ["python3.12", false]]
        .map(([pkg, security]) => ({ name: pkg, security }));
      const facts = { os: "Ubuntu 24.04.3 LTS", id: "ubuntu", version: "24.04", kernel: "6.8.0-85-generic", uptime_s: 1728000,
        package_manager: "apt", lists_at: at - 30000, updates, security: 3, reboot: name === "harvester-node2",
        reboot_for: name === "harvester-node2" ? "linux-image-6.8.0-85-generic" : "", failed_units: [], ntp: true,
        root_total_gb: 97.9, root_used_pct: 41, upgrading: false,
        auto: { tool: "unattended-upgrades", on: true, reboots: name === "harvester-node2", held: false }, last_upgrade: { ok: true, code: "0", at: at - 86400 * 9 }, at,
        disks: [{ name: "nvme0n1", size: 512 * gb, table: "gpt", fstype: "", mount: "", free: 0, partitions: [
            { name: "nvme0n1p1", start: 1024 ** 2, size: 1.05 * gb, fstype: "vfat", label: "", mounts: ["/boot/efi"], holds: [] },
            { name: "nvme0n1p2", start: 1.05 * gb, size: 2 * gb, fstype: "ext4", label: "", mounts: ["/boot"], holds: [] },
            { name: "nvme0n1p3", start: 3.05 * gb, size: 508.9 * gb, fstype: "LVM2_member", label: "", mounts: ["/"], holds: ["lvm"] }] },
          { name: "sda", size: 4000 * gb, table: "gpt", fstype: "", mount: "", free: 0, partitions: [
            { name: "sda1", start: 1024 ** 2, size: 3999 * gb, fstype: "ext4", label: "", mounts: ["/var/lib/longhorn"], holds: [] }] },
          { name: "sdb", size: 2000 * gb, table: "", fstype: "", mount: "", free: 0, partitions: [] }] };
      return { applies: true, every_s: 21600, hosts: { [name]: { ...facts,
        summary: { tone: "warn", text: `3 security updates${facts.reboot ? ", restart needed" : ""}` } } } };
    },
    "/api/node/smart": url => {
      const node = nodes.find(n => n.name === url.searchParams.get("node"));
      const disk = node?.temps?.disks?.find(d => d.name === url.searchParams.get("disk"));
      if (!disk?.smart) return { error: "Demo disk not found" };
      return { ...disk.smart, health_assessment: disk.health };
    },
    "/api/volumes/edit-options": url => {
      const volume = volumes.find(v => v.namespace === (url.searchParams.get("ns") || "lab") && v.pvc_name === url.searchParams.get("name"));
      return { can_expand: true, requested_gb: Math.ceil(volume?.size_gb || 5), storage_class: volume?.storage_class || "longhorn", reason: "" };
    },
    "/api/volumes/delete-plan": volumeDeletePlan, "/api/hardware/features": hardware,
    "/api/namespaces": ["default", "lab", "monitoring"],
    "/api/namespaces/manage": { default: "lab", system_hidden: 31, namespaces: [
      { name: "default", created: "2026-01-04T10:00:00Z", protected: "Kubernetes' own default namespace", deployments: 0, statefulsets: 0, volumes: 0, vms: 0, empty: true },
      { name: "lab", created: "2026-01-04T10:20:00Z", protected: "new workloads go here by default", deployments: 12, statefulsets: 0, volumes: 11, vms: 1, empty: false },
      { name: "monitoring", created: "2026-03-11T08:00:00Z", homestead: true, protected: "", deployments: 0, statefulsets: 0, volumes: 0, vms: 0, empty: true }] },
    "/api/deploy/options": deployOptions,
    "/api/appstore": url => (url.searchParams.get("q") || !["", "home"].includes(url.searchParams.get("sort") || "")
      ? { total: 1, apps: [demoApp], sort: url.searchParams.get("sort") || "search", spotlight: null }
      : { total: 1, sort: "home", sections: { spotlight: [demoApp], recent: [demoApp], trending: [demoApp], popular: [demoApp] } }),
    "/api/appstore/app": () => Object.assign({}, demoApp, {
      overview: ["Frigate is a complete, local network video recorder with realtime AI object detection for IP cameras.", "",
        "Uses OpenCV and TensorFlow to detect people, cars and more, locally.", "Integrates with Home Assistant."].join("\n"),
      links: { project: "https://frigate.video", support: "https://github.com/blakeblackshear/frigate/discussions",
        registry: "https://github.com/blakeblackshear/frigate/pkgs/container/frigate" },
      screenshots: [], comment: "", requires: "", license: "MIT" }),
    "/api/preview": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const joining = body.target_mode === "existing";
      const workloadName = body.workload_name || body.name;
      const containerName = body.container_name || body.name;
      return { deployment: { apiVersion: "apps/v1", kind: "Deployment",
          metadata: { name: joining ? body.target_workload : workloadName, namespace: body.namespace },
          spec: { template: { spec: { containers: [{ name: containerName, image: body.image }] } } } },
        service: null, capacity: { additional: 1, pod_request_gb: 0.25, pod_memory_gb: 0.5,
          pod_cpu_request_percent: 10, blocked: false, requires_confirmation: joining,
          ...(joining ? { rollout: { strategy: "Recreate", replicas: 1, ownership_known: true, owned_pods: ["demo-pod"], release_request_gb: 0.25 } } : {}),
          warnings: joining ? ["Recreate stops old pods before replacements start; every container will be unavailable during the restart"] : [],
          candidates: [{ name: "harvester-node1", eligible: true, metrics_available: true, used_gb: 6,
            projected_gb: 6.5, capacity_gb: 16, projected_percent: 40.6, reservations_known: true, reserved_gb: 4, request_slots: 1 }] },
        capacity_token: "demo-review", impact: { mode: joining ? "existing" : "new", workload: joining ? body.target_workload : workloadName,
          message: joining ? "Saving updates the Deployment template and restarts every container in its pods." : "Creates a new independently managed Deployment." } };
    },
    "/api/vm-disks": vmDisks,
    "/api/vm-disks/import-plan": url => {
      const namespace = url.searchParams.get("ns") || "lab";
      const name = url.searchParams.get("name") || "";
      const conflict = vmDisks.some(d => d.namespace === namespace && d.name === name);
      return { namespace, name, ready: !conflict,
        conflicts: conflict ? [{ kind: "DataVolume", name }] : [],
        message: conflict ? `${namespace}/${name} already exists; choose a new disk name` : "Ready to create a new CDI DataVolume and PVC" };
    },
    "/api/vm-disks/import": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, namespace: body.namespace || "lab", name: body.name,
        pvc: body.name, size_gb: body.size_gb,
        message: `CDI import into ${body.namespace || "lab"}/${body.name} started` };
    },
    // An Unraid server's VMs, as homestead_unraid_vms.plan() maps them.
    "/api/sources/vms": (url, init) => {
      const disk = (path, gb, format, bus, usedGb) => ({ index: 0, path, target: "hdc", unraid_bus: bus, bus: bus === "ide" ? "sata" : bus,
        format, virtual: gb * 2 ** 30, size: format === "raw" ? gb * 2 ** 30 : usedGb * 2 ** 30, used: usedGb * 2 ** 30, size_gb: gb, found: true });
      const vm = (name, state, os, cores, memory, firmware, disks, nic, extra = {}) => ({
        name, slug: name.toLowerCase().replace(/[^a-z0-9]+/g, "-"), state, shut_off: state === "shut off", os, cores, memory, firmware,
        secure_boot: false, tpm: false, hyperv: false, cpu_model: "host-passthrough", machine: "pc-q35-9.2",
        disks: disks.map((d, index) => ({ ...d, index })), nic: { bridge: "br0", model: "virtio-net", nic_model: "virtio", ...nic },
        dropped: [], notes: [], ready: true, problem: "", ...extra });
      return { source: JSON.parse(init?.body || "{}").name || "unraid", virsh: true, vms: [
        vm("home-assistant", "shut off", "linux", 2, "4Gi", "uefi", [disk("/mnt/user/domains/home-assistant/haos_ova-16.2.qcow2", 32, "qcow2", "virtio", 6)],
          { mac: "52:54:00:4b:21:9e" }),
        vm("ubuntu-dev", "shut off", "ubuntu", 4, "6Gi", "bios", [disk("/mnt/user/domains/ubuntu-dev/vdisk1.img", 40, "raw", "virtio", 11)],
          { mac: "52:54:00:18:c3:5d" }),
        vm("win11-desk", "running", "windows11", 4, "8Gi", "uefi",
          [disk("/mnt/user/domains/win11-desk/vdisk1.img", 80, "raw", "sata", 18), disk("/mnt/user/domains/win11-desk/vdisk2.img", 100, "qcow2", "virtio", 2)],
          { mac: "52:54:00:3a:1c:07" },
          { tpm: true, hyperv: true,
            dropped: [{ what: "GPU or PCI device", detail: "0000:01:00.0", hardware: true, reason: "passthrough is tied to Unraid's hardware; add it from Homestead's hardware devices in Edit VM" },
              { what: "CD-ROM", detail: "virtio-win-0.1.262.iso", reason: "an ISO is only needed to install; add one in Edit VM if you still need it" },
              { what: "CPU pinning", detail: "vcpupin", reason: "Unraid's own tuning for its cores; the core count comes across" }],
            notes: ["its TPM comes across as a new one - Unraid keeps the old one's contents - so BitLocker, if it is on, asks once for its recovery key"] }),
      ] };
    },
    "/api/sources/vms/shutdown": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, message: `Asked ${body.source} to shut ${body.vm} down; it shows as shut off once the guest has stopped` };
    },
    "/api/vms/import-unraid": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, operation: { id: "demo-uvm", kind: "unraid-vm-import", title: `Import ${body.vm} from ${body.source}`,
        status: "running", progress: 3, message: `Copying from ${body.source}`, href: "/vms/import" } };
    },
    "/api/images": () => ({ distinct: 4, protected: 3, retained: 3, complete: true, scanning: [],
      node_names: ["harvester-node1", "harvester-node2", "harvester-node3"],
      nodes: [{ node: "harvester-node1", total_gb: 14.2, count: 38, scanned_at: Date.now() / 1000 - 240 },
        { node: "harvester-node2", total_gb: 11.9, count: 31, scanned_at: Date.now() / 1000 - 240 },
        { node: "harvester-node3", total_gb: 9.4, count: 27, scanned_at: Date.now() / 1000 - 250 }],
      pulls: [{ name: "homestead-pull-frigate", image: "ghcr.io/blakeblackshear/frigate:0.18.0-rc1",
        desired: 3, ready: 1, complete: false }],
      pulls_finished: [],
      images: [
        { name: "ghcr.io/blakeblackshear/frigate:stable", names: [], digest: "sha256:" + "a".repeat(64),
          size_mb: 2480, nodes: ["harvester-node2"], system: false, protected: true,
          retained_by: [{ reason: "active", namespace: "lab", workload: "frigate", container: "frigate" }] },
        { name: "ghcr.io/home-assistant/home-assistant:stable", names: [], digest: "sha256:" + "b".repeat(64),
          size_mb: 1720, nodes: ["harvester-node1", "harvester-node3"], system: false, protected: true,
          retained_by: [{ reason: "rollback", namespace: "lab", workload: "home-assistant", container: "home-assistant" }] },
        // A container scaled to zero still starts from its image.
        { name: "ghcr.io/esphome/esphome:2025.9.0", names: [], digest: "sha256:" + "e".repeat(64),
          size_mb: 612, nodes: ["harvester-node1"], system: false, protected: true,
          retained_by: [{ reason: "stopped", namespace: "lab", workload: "esphome", container: "esphome" }] },
        { name: "docker.io/library/redis:7.2", names: [], digest: "sha256:" + "c".repeat(64),
          size_mb: 41, nodes: ["harvester-node1", "harvester-node2", "harvester-node3"],
          system: false, protected: false, retained_by: [] }] }),
    // Harvester's VM images: kept once, copied to the nodes their disks run on.
    "/api/images/vm": { harvester: true, note: "", images: [
      { name: "image-4f2a1c", namespace: "lab", display: "noble-server-cloudimg-amd64.img", source: "cloud-images.ubuntu.com",
        size_mb: 598.2, virtual_size_gb: 3.5, state: "ready", progress: 100, message: "", storage_class: "longhorn-image-4f2a1c",
        nodes: ["harvester-node1", "harvester-node2"], copies: 2, disks: ["lab/pihole-disk", "lab/k3s-lab-server-1-disk"],
        used_by: ["lab/pihole", "lab/k3s-lab-server-1"], deleting: false },
      { name: "image-9b07e3", namespace: "lab", display: "debian-12-genericcloud-amd64.qcow2", source: "cloud.debian.org",
        size_mb: 412.7, virtual_size_gb: 2, state: "ready", progress: 100, message: "", storage_class: "longhorn-image-9b07e3",
        nodes: ["harvester-node3"], copies: 1, disks: [], used_by: [], deleting: false },
      { name: "image-c1d8e0", namespace: "lab", display: "haos_ova-16.2.qcow2", source: "github.com",
        size_mb: 0, virtual_size_gb: 0, state: "downloading", progress: 42, message: "", storage_class: "",
        nodes: [], copies: 0, disks: [], used_by: [], deleting: false }] },
    "/api/images/vm/delete": { ok: true, detail: "debian-12-genericcloud-amd64.qcow2 is being deleted, with its copies on 1 node" },
    "/api/images/prepull": { ok: true, daemonset: "homestead-pull-redis",
      nodes: ["harvester-node1", "harvester-node3"], skipped: ["harvester-node2"],
      message: "Pulling onto 2 nodes; skipped harvester-node2 (cordoned or not ready)" },
    "/api/images/prepull/stop": { ok: true, message: "Pre-pull homestead-pull-frigate stopped" },
    "/api/network/vm-networks": { ok: true, name: "default/lan", detail: "VM network default/lan made, on the untagged LAN of mgmt; VMs and containers can join it now" },
    "/api/node/bridge/inspect": (url, init) => ({ node: JSON.parse(init.body).node, interface: "eth0", mac: "18:60:24:f5:e5:09",
      address: "192.0.2.109/24", dhcp: true, gateway: "192.0.2.1", file: "/etc/netplan/00-installer-config.yaml", netplan_id: "eth0",
      services: ["k3s"], bridge: "br0", problem: "", rollback_seconds: 240 }),
    "/api/node/bridge": (url, init) => ({ ok: true, detail: `${JSON.parse(init.body).node} is moving to br0; follow it in the job tray` }),
    "/api/images/scan": { ok: true, nodes: ["harvester-node1", "harvester-node2", "harvester-node3"], detail: "asking containerd on 3 nodes for every image" },
    "/api/images/forget-rollback": { ok: true, detail: "home-assistant no longer keeps its previous image; it can be cleaned up now" },
    // Two linked clusters (branch answers, dr-site is off) and staging, added for
    // moves before linking existed.
    "/api/move/clusters": [
      { name: "branch", label: "Branch office", url: "http://192.0.2.250:8088", user: "", fleet: true, id: "b2c0de" },
      { name: "dr-site", label: "DR site", url: "http://192.0.2.251:8088", user: "", fleet: true, id: "c3beef" },
      { name: "staging", url: "http://192.0.2.252:8088", user: "admin", added: "2026-05-02 18:40" }],
    "/api/move/clusters/check": (url, init) => {
      const name = JSON.parse(init?.body || "{}").name;
      if (name === "dr-site") return { name, version: "", protocol: null, local_version: "2.8.318-dev.6", local_protocol: 1,
        state: "unreachable", message: "could not reach dr-site: no answer from http://192.0.2.251:8088" };
      if (name === "staging") return { name, version: "2.8.190", protocol: 1, local_version: "2.8.318-dev.6",
        local_protocol: 1, state: "differs", compatible: true,
        message: "staging runs 2.8.190 and this one 2.8.318-dev.6. Moves work between them; this Homestead is the newer of the two." };
      return { name, version: "2.8.318-dev.6", protocol: 1, local_version: "2.8.318-dev.6", local_protocol: 1,
        state: "same", compatible: true, message: "Both run Homestead 2.8.318-dev.6." };
    },
    "/api/move/clusters/add": [], "/api/move/clusters/remove": [],
    // branch is ready to move from; staging has no backup storage yet.
    "/api/move/clusters/readiness": (url, init) => JSON.parse(init?.body || "{}").name === "dr-site"
      ? { version: { state: "unreachable", message: "could not reach dr-site" }, storage: {}, target: {}, ready: false }
      : JSON.parse(init?.body || "{}").name === "staging"
      ? { version: { compatible: true }, storage: { deployed: false }, target: { configured: false, error: "no backup target" }, ready: false,
          shared_vip: "192.0.2.245", free_vips: [{ ip: "192.0.2.246", label: "spare", from: "vips" }, { ip: "192.0.2.230", label: "", from: "pool" }] }
      : { version: { compatible: true }, storage: { deployed: true, ready: true, reachable_off_cluster: true },
          target: { configured: true, reachable_off_cluster: true, answers: true, url: "s3://homestead-backups@us-east-1/",
            endpoint: "http://192.0.2.250:9000" }, ready: true },
    "/api/move/clusters/storage": { ok: true, detail: "backup storage is starting on staging at http://192.0.2.244:9000" },
    "/api/move/inventory": { namespace: "lab", movable: 2, workloads: [] },
    "/api/move/remote": { cluster: "branch", url: "http://192.0.2.250:8088",
      namespace: "lab", version: "2.8.318-dev.6", protocol: 1, movable: 2, workloads: [
        { name: "frigate", namespace: "lab", kind: "container", image: "ghcr.io/blakeblackshear/frigate:stable",
          replicas: 1, running: true, containers: ["frigate"], hardware: ["igpu"],
          ports: [{ container: 5000, protocol: "TCP" }], movable: true, blockers: [],
          volumes: [{ claim: "frigate-config", path: "/config", sub_path: "", read_only: false,
            size_gb: 10, storage_class: "longhorn-r2", access_modes: ["ReadWriteOnce"] }] },
        { name: "mosquitto", namespace: "lab", kind: "container", image: "eclipse-mosquitto:2",
          replicas: 1, running: false, containers: ["mosquitto"], hardware: [],
          ports: [{ container: 1883, protocol: "TCP" }], movable: true, blockers: [],
          volumes: [{ claim: "mosquitto-appdata", path: "/mosquitto/data", sub_path: "", read_only: false,
            size_gb: 10, storage_class: "longhorn-r2", access_modes: ["ReadWriteOnce"] }] },
        { name: "legacy-app", namespace: "lab", kind: "container", image: "legacy:1",
          replicas: 1, running: true, containers: ["legacy-app"], hardware: [], ports: [],
          movable: false, blockers: ["/etc/app comes from a configMap Homestead did not create"],
          volumes: [] }],
      vms: [{ name: "home-assistant-os", namespace: "lab", kind: "vm", running: true, cores: 2,
        memory: "4Gi", replicas: 1, containers: [], ports: [], hardware: [], movable: true, blockers: [],
        warnings: ["uses the lab/vlan20 network, which must exist on the destination"],
        volumes: [{ claim: "haos-disk-0", path: "rootdisk", sub_path: "", read_only: false,
          size_gb: 32, storage_class: "longhorn-haos", access_modes: ["ReadWriteMany"] }] }],
      volumes: [
        { name: "frigate-config", namespace: "lab", kind: "volume", size_gb: 10, storage_class: "longhorn-r2",
          access_modes: ["ReadWriteOnce"], volume_mode: "Filesystem", used_by: ["frigate"], movable: false,
          blockers: ["in use by frigate; stop it, or move it instead, which brings this volume with it"], warnings: [] },
        { name: "media-archive", namespace: "lab", kind: "volume", size_gb: 500, storage_class: "longhorn-r2",
          access_modes: ["ReadWriteOnce"], volume_mode: "Filesystem", used_by: [], movable: true, blockers: [], warnings: [] },
        { name: "mosquitto-appdata", namespace: "lab", kind: "volume", size_gb: 10, storage_class: "longhorn-r2",
          access_modes: ["ReadWriteOnce"], volume_mode: "Filesystem", used_by: ["mosquitto"], movable: true, blockers: [],
          warnings: ["mosquitto uses it and stays here, stopped"] }] },
    // One path, two questions: where this can move within the cluster (GET),
    // and what bringing it from another cluster involves (POST).
    "/api/move/hello": { protocol: 1, namespace: "lab", capabilities: ["copy-source-lease", "copy-destination"] },
    "/api/move/plan": (url, init) => (init?.method || "GET") !== "GET" && JSON.parse(init.body || "{}").cluster === "dr-site"
      ? { ok: false, blockers: ["dr-site: this cluster has no Longhorn backup target; set up backup storage under Data protection first"],
          warnings: [], claims: [], fixes: [{ kind: "source-storage", cluster: "dr-site" }] }
      : (init?.method || "GET") === "GET" ? {
      current: "harvester-node2", recommended: "harvester-node1",
      requirements: { devices: [{ id: "igpu", label: "Intel/AMD iGPU" }], features: ["igpu"], labels: {}, resources: {} },
      candidates: [
        { name: "harvester-node2", ok: true, current: true, pods_wl: 3, score: 71, cpu_after: 42, mem_after: 54,
          hardware: { igpu: true, coral_usb: true }, temp_c: 34, why: [] },
        { name: "harvester-node1", ok: true, current: false, pods_wl: 2, score: 88, cpu_after: 31, mem_after: 66,
          hardware: { igpu: true }, temp_c: 39, why: [] },
        { name: "harvester-node3", ok: false, current: false, pods_wl: 1, score: 0, cpu_after: 24, mem_after: 49,
          hardware: {}, temp_c: 36, why: ["no Intel/AMD iGPU on this host"] }] } : (() => {
      const body = JSON.parse(init.body || "{}"), isVm = body.kind === "vm", copy = body.transfer_mode === "copy";
      const hardware = isVm && body.name === "gpu-desktop", mapping = body.host_devices?.display;
      const deviceReady = !hardware || mapping && "resource" in mapping;
      return {
      ok: !!deviceReady, blockers: deviceReady ? [] : ["Choose a destination device or Leave out for display"], cluster: body.cluster || "branch", kind: body.kind || "container", name: body.name || "frigate",
      host_devices: hardware ? [{name:"display",resource:"example.test/source-gpu",gpu:true,rom:true}] : [],
      device_resources: hardware ? [{resource:"homestead.io/pci-10de-1e87",label:"10DE:1E87",kind:"pci",nodes:["harvester-node1"]}] : [],
      device_hosts: hardware && mapping?.resource ? ["harvester-node1"] : [],
      transfer_mode: copy ? "copy" : "move",
      storage_class: body.storage_class || "longhorn-r2", storage_classes: ["longhorn-r2", "longhorn-r3"],
      all_storage_classes: ["longhorn-r2", "longhorn-r3", "local-path"],
      namespace: body.namespace || "lab", joined: false, will_run: !copy, addresses: isVm ? [] : ["frigate on 192.0.2.242"],
      warnings: [...(copy && isVm ? ["The VM copy gets new MAC addresses and a firmware UUID. Review guest static IP and network settings before starting it"] : []),
        "this cluster's Longhorn backup target changes from (none) to s3://homestead-backups@us-east-1/; backups already written to the old one stay there"],
      claims: [{ claim: isVm ? "haos-disk-0" : "frigate-config", size_gb: isVm ? 32 : 10, access_mode: isVm ? "ReadWriteMany" : "ReadWriteOnce",
        volume_mode: isVm ? "Block" : "Filesystem", backing_image: "" }], total_gb: isVm ? 32 : 10 };
      })(),
    "/api/move/start": { id: "d1", status: "running" },
    "/api/host-console": { version: "2.8.318-dev.6", enabled: true, hosts: 2, installed: 2, current: 1, settled: false, harvester: false, nodes: [
      { name: "node-1", ready: true, enabled: true, version: "2.8.243", current: false, detail: "Installed 2.8.243; update available" },
      { name: "node-2", ready: true, enabled: true, current: true, version: "2.8.318-dev.6", detail: "Installed 2.8.318-dev.6; matches this release" },
      { name: "node-3", ready: true, native: true, detail: "Native Harvester console" }] },
    "/api/compose/preview": () => ({ capacity_token: "demo-compose-review", capacity: {
      status: "fits", blocked: false, requires_confirmation: true, pods: 3,
      services: ["broker", "db", "webserver"].map(name => ({ name, replicas: 1, pod_request_gb: 0.25, pod_memory_gb: 0.5, pod_cpu_request_percent: 10 })),
      nodes: [], example: [], warnings: ["Demo batch preview; no workloads are created."], reasons: []
    } }),
    "/api/compose/parse": () => ({ ok: true, project: "paperless", errors: [], warnings: [],
      variables: { used: ["DB_PASSWORD"], missing: [] }, order: ["broker", "db", "webserver"],
      services: [
        { name: "broker", source_name: "broker", line: 4, image: "docker.io/library/redis:7", errors: [], warnings: [],
          notes: [{ line: 4, message: "webserver reaches it by name, so it gets an address inside the cluster on port 6379, its usual port" }],
          summary: { ports: ["6379→6379/tcp"], lan: false, network: "internal", env: 0, hardware: [], command: "",
            volumes: [{ path: "/data", kind: "new-rwo", source: "redisdata", template_source: "" }] },
          config: { name: "broker", workload_name: "broker", container_name: "broker", image: "docker.io/library/redis:7",
            namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi", network_mode: "internal", vip_mode: "shared",
            ports: [{ container: 6379, host: 6379, protocol: "TCP", expose: true }], env: {}, hardware: [], template_devices: [],
            volumes: [{ path: "/data", source: "redisdata", kind: "new-rwo", type: "pvc", create: true, size_gb: 5, access_mode: "ReadWriteOnce" }] } },
        { name: "db", source_name: "db", line: 9, image: "docker.io/library/postgres:16", errors: [], warnings: [],
          notes: [{ line: 9, message: "webserver reaches it by name, so it gets an address inside the cluster on port 5432, its usual port" }],
          summary: { ports: ["5432→5432/tcp"], lan: false, network: "internal", env: 3, hardware: [], command: "",
            volumes: [{ path: "/var/lib/postgresql/data", kind: "new-rwo", source: "pgdata", template_source: "" }] },
          config: { name: "db", workload_name: "db", container_name: "db", image: "docker.io/library/postgres:16",
            namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi", network_mode: "internal", vip_mode: "shared",
            ports: [{ container: 5432, host: 5432, protocol: "TCP", expose: true }], hardware: [], template_devices: [],
            env: { POSTGRES_DB: "paperless", POSTGRES_USER: "paperless", POSTGRES_PASSWORD: "paperless" },
            volumes: [{ path: "/var/lib/postgresql/data", source: "pgdata", kind: "new-rwo", type: "pvc", create: true, size_gb: 5, access_mode: "ReadWriteOnce" }] } },
        { name: "webserver", source_name: "webserver", line: 18, image: "ghcr.io/paperless-ngx/paperless-ngx:latest", errors: [],
          warnings: [{ line: 26, message: "set TZ in environment (for example TZ: Europe/London) for the local time zone" }],
          notes: [{ line: 27, message: "./consume becomes new volume webserver-consume; its current contents are not copied. Import › Container source can bring them across" }],
          summary: { ports: ["8000→8000/tcp"], lan: true, network: "loadbalancer", env: 3, hardware: [], command: "",
            volumes: [{ path: "/usr/src/paperless/data", kind: "new-rwo", source: "data", template_source: "" },
              { path: "/usr/src/paperless/consume", kind: "new-rwo", source: "webserver-consume", template_source: "./consume" }] },
          config: { name: "webserver", workload_name: "webserver", container_name: "webserver",
            image: "ghcr.io/paperless-ngx/paperless-ngx:latest", namespace: "lab", replicas: 1, cpu: "50m", memory: "128Mi",
            network_mode: "loadbalancer", vip_mode: "shared", hardware: [], template_devices: [],
            ports: [{ container: 8000, host: 8000, protocol: "TCP", expose: true }],
            env: { PAPERLESS_REDIS: "redis://broker:6379", PAPERLESS_DBHOST: "db", USERMAP_UID: "1000" },
            volumes: [{ path: "/usr/src/paperless/data", source: "data", kind: "new-rwo", type: "pvc", create: true, size_gb: 5, access_mode: "ReadWriteOnce" },
              { path: "/usr/src/paperless/consume", source: "webserver-consume", kind: "new-rwo", type: "pvc", create: true, size_gb: 5,
                access_mode: "ReadWriteOnce", template_source: "./consume", template_origin: "Compose" }],
            app_profile: { label: "Imported from Docker Compose", level: "review", intent: "compose",
              notes: ["./consume becomes new volume webserver-consume; its current contents are not copied. Import › Container source can bring them across"] } } }] }),
    "/api/compose/apply": { ok: true, created: ["broker", "db", "webserver"] },
    "/api/onboard/guide": { version: "1.4.1", arch: "amd64",
      iso: "https://releases.rancher.com/harvester/v1.4.1/harvester-v1.4.1-amd64.iso",
      checksums: "https://releases.rancher.com/harvester/v1.4.1/harvester-v1.4.1-amd64.sha512",
      vip: "192.0.2.240", ntp: ["0.suse.pool.ntp.org"], proxy: "", hostname: "harvester-node4",
      nodes: [{ name: "harvester-node1", ip: "192.0.2.210", ready: true, management: true },
        { name: "harvester-node2", ip: "192.0.2.211", ready: true, management: true },
        { name: "harvester-node3", ip: "192.0.2.212", ready: true, management: true }],
      management_count: 3, token_file: "/etc/rancher/rancherd/config.yaml",
      token_command: "sudo grep '^token:' /etc/rancher/rancherd/config.yaml", token_host: "192.0.2.210" },
    "/api/cluster/cleanup": {
      dead_nodes: [{ name: "harvester-node5", roles: [], since: "2026-09-21T02:14:00Z" }],
      stale_machines: [{ name: "custom-5f2c81a9e0d4", node: "harvester-node4", phase: "Deleting", stuck: true, created: "2026-08-01T10:00:00Z" }],
      stale_longhorn: [{ name: "harvester-node4", replicas: 0 }],
      passwords: [{ name: "harvester-node4.node-password.rke2", node: "harvester-node4" }],
      pinned_volumes: [], pinned_workloads: [{ kind: "Deployment", namespace: "lab", name: "zigbee2mqtt", node: "harvester-node4" }],
      attachments: [] },
    "/api/cluster/cleanup/run": { ok: true, message: "Deleted" },
    "/api/cluster/removal": url => url.searchParams.get("node") === "harvester-node5" ? {
      node: "harvester-node5", ready: false, roles: [], since: "2026-09-21T02:14:00Z", ok: true,
      blockers: [], warnings: ["2 volumes will rebuild the copy harvester-node5 held, on the remaining nodes."],
      lost_volumes: ["pvc-3f1e"], rebuilt_volumes: ["frigate-config", "paperless-data"], distribution: "harvester",
      lost_detail: [{ volume: "pvc-3f1e", namespace: "lab", claim: "scratch-cache", users: ["tdarr"] }],
      pinned_volumes: [], pinned_workloads: [{ kind: "Deployment", namespace: "lab", name: "zigbee2mqtt" }],
      stuck: { pods: 7, vms: ["home-assistant-os"], attachments: 2, replicas: 2 },
      steps: ["Stop Longhorn scheduling new replicas to harvester-node5",
        "Delete the Kubernetes node harvester-node5 (RKE2 removes its etcd membership)",
        "Delete its Cluster API machine custom-8a1b2c3d", "Delete Longhorn's record of harvester-node5 once it holds no replicas"],
      gone_steps: ["Force-delete the 7 pods still bound to it, so their workloads start elsewhere",
        "Force-stop the 1 VM it was running (home-assistant-os), so each restarts on another host",
        "Release 2 volume attachments, so those volumes can attach on another node",
        "Delete the 2 replica records Longhorn keeps for it, so it rebuilds them from the remaining copies",
        "Finish its Cluster API machine's deletion if finalizers hold it",
        "Let 1 app pinned to it run on any host", "No volumes were kept on the host itself"],
      machine: { name: "custom-8a1b2c3d", namespace: "fleet-local" } } : ({ node: url.searchParams.get("node"), ready: true, roles: ["control-plane", "etcd"],
      since: "", ok: false, lost_volumes: [], rebuilt_volumes: ["frigate-config"],
      blockers: [`${url.searchParams.get("node")} is Ready. A running node re-registers itself, so it is not removed from here: put it in maintenance mode in Harvester, run /opt/rke2/bin/rke2-uninstall.sh on it, power it off, and come back when it shows Not ready.`],
      warnings: ["1 volume will rebuild the copy it held, on the remaining nodes."],
      steps: ["Stop Longhorn scheduling new replicas to it", "Delete the Kubernetes node (RKE2 removes its etcd membership)",
        "Delete its Cluster API machine", "Delete Longhorn's record of it once it holds no replicas"],
      gone_steps: [], stuck: { pods: 0, vms: [], attachments: 0, replicas: 0 },
      machine: { name: "custom-1", namespace: "fleet-local" } }),
    "/api/cluster/remove-node": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, node: body.node, log: [
        `Longhorn stopped scheduling to ${body.node}`,
        ...(body.gone ? ["Force-stopped the 1 VM it was running", `Force-deleted 7 pods bound to ${body.node}`, "Released 2 volume attachments"] : []),
        `Deleted node ${body.node}`, "Deleted Cluster API machine custom-8a1b2c3d",
        ...(body.gone ? ["Cleared the finalizers holding machine custom-8a1b2c3d", "Deleted 2 replica records; Longhorn rebuilds them from the remaining copies"] : []),
        `Deleted Longhorn's record of ${body.node}`] };
    },
    "/api/schedules": [
      { name: "nightly-db-dump", namespace: "lab", schedule: "0 3 * * *", image: "docker.io/library/postgres:16",
        command: "pg_dumpall -h db -U paperless > /backup/all.sql", last: "2026-09-22T03:00:04Z", suspend: false, active: 0 },
      { name: "prune-recordings-older-than-thirty-days", namespace: "lab", schedule: "*/30 * * * *",
        image: "ghcr.io/example/long-image-name-for-cleanup-tasks:2026.09.1", command: "find /media -mtime +30 -delete",
        last: "2026-09-22T13:30:00Z", suspend: true, active: 0 }],
    "/api/events": () => [
      { obj: "frigate-7d9f8c6b5-x2abc", ns: "lab", kind: "Pod", type: "Normal", reason: "Pulled",
        msg: "Successfully pulled image \"ghcr.io/blakeblackshear/frigate:stable\" in 12.4s", count: 1,
        time: new Date(Date.now() - 4 * 60e3).toISOString() },
      { obj: "arr-dashboard-data", ns: "lab", kind: "PersistentVolumeClaim", type: "Warning", reason: "ProvisioningFailed",
        msg: "failed to provision volume with StorageClass \"longhorn-r2\": no disk space to create the replicas required: 1 of 2 replicas scheduled on nodes with enough free space",
        count: 14, time: new Date(Date.now() - 11 * 60e3).toISOString() },
      { obj: "home-assistant", ns: "lab", kind: "Deployment", type: "Normal", reason: "ScalingReplicaSet",
        msg: "Scaled up replica set home-assistant-6c8d9 to 1", count: 1, time: new Date(Date.now() - 20 * 60e3).toISOString() }],
    "/api/move/moves/retry": { ok: true }, "/api/move/moves/abandon": { ok: true },
    "/api/move/moves/finish": { ok: true, message: "mosquitto lives here now; removed workload mosquitto on branch" },
    "/api/move/moves": () => [
      { id: "d2", cluster: "branch", kind: "container", name: "grafana", source_namespace: "lab",
        namespace: "lab", status: "failed", phase: "joining", phase_index: 0, source_stopped: false,
        phases: ["joining", "quiescing", "backing-up", "syncing", "restoring", "creating", "starting", "done"],
        progress: 1, message: "this cluster cannot reach branch's backup storage at http://192.0.2.108:9000. Give it an address this cluster can reach - branch's Migration button under Linked clusters - then retry",
        source_removed: false, created_at: new Date(Date.now() - 2 * 60e3).toISOString(), claims: [] },
      { id: "d1", cluster: "branch", kind: "container", name: "frigate", source_namespace: "lab",
        namespace: "lab", status: "running", phase: "restoring", phase_index: 4,
        phases: ["joining", "quiescing", "backing-up", "syncing", "restoring", "creating", "starting", "done"],
        progress: 71, message: "Restoring 1 volume here: 54%", source_removed: false,
        created_at: new Date(Date.now() - 8 * 60e3).toISOString(), claims: [] },
      { id: "d0", cluster: "branch", kind: "container", name: "mosquitto", source_namespace: "lab",
        namespace: "lab", status: "succeeded", phase: "done", phase_index: 7,
        phases: ["joining", "quiescing", "backing-up", "syncing", "restoring", "creating", "starting", "done"],
        progress: 100, message: "mosquitto is running here; still stopped on branch until you remove it there",
        source_removed: false, created_at: new Date(Date.now() - 50 * 60e3).toISOString(), claims: [] }],
    "/api/objectstore": { deployed: true, ready: true, endpoint: "http://192.0.2.244:9000",
      reachable_off_cluster: true, bucket: "homestead-backups", size_gb: 100,
      backup_url: "s3://homestead-backups@us-east-1/",
      longhorn: { pointed: true, state: "complete", detail: "Longhorn backs up to this store" },
      image: "quay.io/minio/minio:RELEASE.2024-09-22T00-33-43Z" },
    "/api/objectstore/deploy": { ok: true, endpoint: "http://192.0.2.244:9000",
      bucket: "homestead-backups", access_key: "homestead" },
    "/api/objectstore/longhorn": { url: "s3://homestead-backups@us-east-1/",
      secret: "homestead-backup-credentials", endpoint: "http://192.0.2.244:9000",
      reachable_off_cluster: true, detail: "Longhorn will back up here" },
    "/api/objectstore/remove": { ok: true, detail: "object storage removed" },
    "/api/move/clusters/target": { pending: true, detail: "Backup target switch queued; Homestead will retry when storage is ready" },
    "/api/vmimages": [],
    "/api/vms": demoVms,
    "/api/vm": url => {
      const v = demoVms.find(x => x.name === url.searchParams.get("name")) || demoVms[0];
      const unmade = v.status === "ErrorUnschedulable";
      return { ...v, node_selector: "", cloud_init: { user_data: `#cloud-config
hostname: ${v.name}
ssh_pwauth: true
`, network_data: "", source: "secret" },
        disks: v.disks.map(d => ({ ...d, made: !unmade || d.kind !== "disk", template: unmade && d.kind === "disk" ? "datavolume" : "",
          source: unmade && d.kind === "disk" ? { url: "ubuntu-26.04-minimal-cloudimg-amd64.img" } : {}, template_size: unmade ? "40Gi" : "" })),
        guest: { prettyName: v.os, kernelRelease: v.status === "Running" ? "6.8.0-45-generic" : "" },
        conditions: v.status === "Running" ? [{ type: "Ready", status: "True", reason: "", message: "" }, { type: "LiveMigratable", status: "True", reason: "", message: "" }]
          : [{ type: "Ready", status: "False", reason: v.problem ? "Unschedulable" : "", message: v.problem }],
        events: [{ type: "Normal", reason: "SuccessfulCreate", message: `Created virtual machine pod virt-launcher-${v.name}-x7k2p`, count: 1, last: new Date().toISOString() }],
        hardware: { cpu: { sockets: 1, cores: v.cores || 2, threads: 1, model: "host-model", dedicated: false, isolate_emulator: false },
          firmware: "bios", secure_boot: false, efi_persistent: false, tpm: "off", machine: "q35", hyperv: false, kvm_hidden: false,
          timezone: "", graphics: true, serial: true, tablet: false, rng: false, balloon: true, sound: false, hugepages: "",
          eviction: "LiveMigrate" } };
    },
    // The ISO library: two folders on the demo's shares, one ISO ready and in use.
    "/api/vm/isos": {
      folders: [{ share: "media", path: "isos" }, { share: "backups", path: "installers/windows" }],
      files: [{ share: "media", folder: "isos", name: "debian-13.1.0-amd64-netinst.iso", path: "isos/debian-13.1.0-amd64-netinst.iso",
          size: 783286272, volume: "iso-debian-13-1-0-amd64-netinst-3f2a9c1d", state: "ready" },
        { share: "media", folder: "isos", name: "ubuntu-24.04.3-live-server-amd64.iso", path: "isos/ubuntu-24.04.3-live-server-amd64.iso",
          size: 3213064192, volume: "iso-ubuntu-24-04-3-live-server-amd64-7b1e0a44", state: "copying" },
        { share: "backups", folder: "installers/windows", name: "Win11_24H2_EnglishInternational_x64.iso",
          path: "installers/windows/Win11_24H2_EnglishInternational_x64.iso", size: 5819484160, volume: "", state: "" },
        { share: "backups", folder: "installers/windows", name: "virtio-win-0.1.271.iso",
          path: "installers/windows/virtio-win-0.1.271.iso", size: 739246080, volume: "", state: "" }],
      problems: [],
      volumes: [{ name: "iso-debian-13-1-0-amd64-netinst-3f2a9c1d", namespace: "lab", file: "debian-13.1.0-amd64-netinst.iso",
          source: "media/isos/debian-13.1.0-amd64-netinst.iso", size: 783286272, state: "ready", problem: "", used_by: ["router"], rwx: true },
        { name: "iso-ubuntu-24-04-3-live-server-amd64-7b1e0a44", namespace: "lab", file: "ubuntu-24.04.3-live-server-amd64.iso",
          source: "media/isos/ubuntu-24.04.3-live-server-amd64.iso", size: 3213064192, state: "copying", problem: "", used_by: [], rwx: true },
        { name: "iso-alpine-3-20-3-x86-64-5c2d7e10", namespace: "lab", file: "alpine-3.20.3-x86_64.iso", source: "media/isos/alpine-3.20.3-x86_64.iso",
          size: 219152384, state: "ready", problem: "", used_by: [], rwx: true, unused_since: Math.floor(Date.now() / 1000) - 5 * 86400 }],
      keep_days: 7,
      shares: [{ name: "media", pvc: "share-media", sub_path: "" }, { name: "backups", pvc: "share-backups", sub_path: "" }] },
    "/api/vm/isos/browse": url => ({ share: url.searchParams.get("share"), path: url.searchParams.get("path") || "",
      folders: url.searchParams.get("path") ? [] : ["isos", "installers", "photos"], isos: url.searchParams.get("path") === "isos" ? 2 : 0 }),
    "/api/vm/isos/prepare": { ok: true, name: "iso-win11", detail: "Copying Win11_24H2_EnglishInternational_x64.iso into a volume; it can go in a CD-ROM drive once ready" },
    "/api/vm/isos/delete": { ok: true, detail: "The ISO's volume deleted; the file on the share is kept" },
    "/api/vm/isos/keep": { ok: true, keep_days: 7, detail: "ISO copies no VM uses are removed after 7 days" },
    "/api/vm/isos/folders": { ok: true, folders: [], detail: "ISO folders saved" },
    "/api/vm/power/preview": (url, init) => {
      const body = JSON.parse(init.body), v = demoVms.find(x => x.name === body.name) || demoVms[0];
      const guest = parseFloat(v.memory) || 4;
      const policy = v.run_strategy || "Halted";
      return {capacity_token:"demo-vm-power-review", capacity:{blocked:false, requires_confirmation:true,
        additional:1, pod_request_gb:guest, pod_memory_gb:guest + 0.25, pod_cpu_request_percent:20,
        placement:{pinned:null,preferred:null,resident:body.action === "unpause" ? v.node || "homestead-01" : null},
        vm:{action:body.action, guest_memory_gb:guest, request_is_lower_bound:body.action !== "unpause",
          policy_before:policy, policy_after:body.action === "start" && policy === "Halted" ? "Always" : policy},
        warnings:["Demo estimates only: launcher overhead, storage attachment and actual guest readiness need live checks."],
        candidates:[{name:v.node || "homestead-01", eligible:true, metrics_available:true, used_gb:10, capacity_gb:32,
          projected_gb:10 + guest + 0.25, projected_percent:Math.round((10 + guest + 0.25) / 32 * 100),
          reservations_known:true, reserved_gb:8, request_slots:1}]}};
    },
    "/api/vm/power": (url, init) => {
      const body=JSON.parse(init.body);
      if (!["start","restart","unpause"].includes(body.action)) return {ok:true,detail:`${body.name}: ${body.action} requested`};
      const operation={id:`demo-power-${Date.now()}`,kind:"vm-power",title:`${body.action} VM ${body.name}`,
        resource:{kind:"VirtualMachine",namespace:body.ns,name:body.name},href:"/vms",status:"running",progress:25,
        message:"KubeVirt accepted the request; waiting for the expected VM instance to become ready",
        started_at:new Date().toISOString(),cancellable:true,dismissible:false};
      (window.__demoOps ??=[]).unshift(operation);
      return {ok:true,detail:`Power request accepted for ${body.name}; follow its job for readiness`,operation};
    },
    "/api/vm/edit": { ok: true, detail: "saved; no Restart request was sent" },
    "/api/vm/edit/preview": (url, init) => {
      const b = JSON.parse(init.body || "{}"), memory = parseFloat(b.memory) || 4;
      return {capacity_token:"demo-vm-edit", volumes:[], capacity:{blocked:false, blockers:[], requires_confirmation:true,
        additional:1, pod_request_gb:memory, pod_memory_gb:memory+0.25, pod_cpu_request_percent:20,
        warnings:["Template changes may take effect immediately through KubeVirt. Save sends no Restart request.", "If saving fails, inspect retained disks and Secrets before retrying."],
        candidates:[{name:"harvester-node1",eligible:true,metrics_available:true,used_gb:8,capacity_gb:32,projected_gb:8+memory+0.25,
          projected_percent:Math.round((8+memory+0.25)/32*100),reservations_known:true,reserved_gb:6,request_slots:1}],
        vm:{action:"edit", admission_needed:true, guest_memory_gb:memory, request_is_lower_bound:true,
          policy_before:"RerunOnFailure", policy_after:b.run_strategy || "RerunOnFailure"}}};
    },
    "/api/vm/delete": { ok: true, detail: "deleted; its disks are kept" },
    "/api/sources": (url, init) => {
      const source={name:"unraid",host:"192.0.2.10",port:22,user:"root",kind:"unraid",base_path:"/mnt/user/appdata",ssh_trust:{fingerprint:"SHA256:demo-previous-key"}};
      return init?.method === "POST" ? {ok:true,sources:[{...source,...JSON.parse(init.body),password:undefined,ssh_trust:undefined}]} : [source];
    },
    "/api/sources/scan": (url, init) => ({name:JSON.parse(init.body).name,key:"ssh-ed25519 DEMO-ONLY-PUBLIC-KEY",algorithm:"ssh-ed25519",
      fingerprint:"SHA256:DEMOonly4xWbYyR7bTfSYJyaJs6vzc87vQY8Gk5kCbo",connection:{host:"192.0.2.10",port:22},
      changed:!!window.__demoSourceKeyChanged,previous:window.__demoSourceKeyChanged?"SHA256:demo-previous-key":"",capacity_token:"demo-source-review"}),
    "/api/sources/trust": {ok:true},
    "/api/sources/containers": { containers: [{ name: "media-server", image: "example/media-server:latest", state: "running" }] },
    "/api/sources/browse": { entries: ["media-server", "home-automation"] },
    "/api/sources/inspect": { name: "media-server", image: "example/media-server:latest", remote_path: "/mnt/user/appdata/media-server",
      source_container_id:"a".repeat(64), source_running:true,
      mount_path: "/config", ports: [{ container: 8096, host: 8096, protocol: "TCP", expose: true }],
      env: { PUID: "1000", PGID: "1000" }, hardware: [], network_mode: "loadbalancer",
      guessed_path: false, shm_mb: 512,
      mounts: [{ source: "/mnt/user/appdata/media-server", path: "/config", type: "bind" },
        { source: "/mnt/user/appdata/media-server/transcode", path: "/transcode", type: "bind" },
        { source: "/mnt/user/media", path: "/media", type: "bind" },
        { source: "", path: "/tmp/cache", type: "tmpfs", size_mb: 1000 }] },
    "/api/import/preview": (url, init) => {
      const cfg = JSON.parse(init?.body || "{}");
      const capacity = {blocked:false, requires_confirmation:true, additional:1,
        pod_request_gb:.25, pod_cpu_request_percent:5, pod_memory_gb:1,
        warnings:["Projected RAM reaches 91% on lab-node-1. This capacity warning may be overridden."],
        candidates:[{name:"lab-node-1",eligible:true,used_gb:6.3,capacity_gb:8,projected_gb:7.3,projected_percent:91,
          metrics_available:true,reservations_known:true,reserved_gb:5,request_slots:8,reasons:[],warnings:[]}]};
      return {capacity:{blocked:false,requires_confirmation:true,warnings:[
        "Imported files may replace existing files. Stop the source application or use a consistent backup.",
        "The application stays stopped. Review its start after the copy completes."]},
        capacity_token:"demo-import-review",volumes:cfg.volumes || [],
        phases:[{title:"Copy files",capacity:{...capacity,pod_request_gb:.12,pod_memory_gb:.5}},
          {title:"Imported application",capacity}]};
    },
    "/api/import": { ok: true, job: "homestead-import-media-server", pvc: "media-server-appdata",
      deployment: "media-server", note: "Deployment created stopped; start it once the copy job finishes." },
    "/api/imports/delete": { ok: true, message: "Import removed", removed: [] },
    "/api/imports/cleanup-plan": (url, init) => {
      if (JSON.parse(init.body).name === "homestead-import-photos") return {journalled:true,known:true,volumes:[]};
      const name = JSON.parse(init?.body || "{}").name || "";
      return name.includes("obsidian")
        ? { job: name, namespace: "lab", workload: "obsidian", volume: "obsidian-appdata",
            volume_created: true, known: true,
            volumes: [{ name: "obsidian-appdata", created: true },
                      { name: "obsidian-vault", created: true }] }
        : { job: name, namespace: "lab", workload: "plex", volume: "plexmedia",
            volume_created: false, known: true,
            volumes: [{ name: "plexmedia", created: false },
                      { name: "plex-config", created: true }] };
    },
    "/api/snapshot-files/plan": url => ({volume:url.searchParams.get('volume'), snapshot:url.searchParams.get('snapshot'), namespace:'lab', claim:'frigate-config', storage_class:'longhorn', size:'10737418240', method:window.__demoSnapshotV2 ? 'linked-clone' : 'full-copy', engine:window.__demoSnapshotV2 ? 'v2' : 'v1', review_token:'demo'}),
    "/api/snapshot-files/start": (url, opts) => {
      const body = JSON.parse(opts.body || '{}');
      return window.__demoSnapshotFiles = {namespace:'lab', session:'homestead-snapshot-files-' + body.request_id, state:'preparing', stage:'clone', percent:42,
        source:{claim:'frigate-config', snapshot:body.snapshot, method:window.__demoSnapshotV2 ? 'linked-clone' : 'full-copy'}, message:'Preparing the selected snapshot; the live volume stays online'};
    },
    "/api/snapshot-files/status": () => ({...window.__demoSnapshotFiles, state:window.__demoSnapshotWaiting ? 'preparing' : 'ready'}),
    "/api/snapshot-files/list": url => ({path:url.searchParams.get('path') || '', read_only:true, entries:[
      {name:'config',kind:'dir',size:0,editable:false}, {name:'frigate.db',kind:'file',size:5242880,editable:false}, {name:'config.yml',kind:'file',size:4210,editable:false}]}),
    "/api/snapshot-files/close": {state:'closing'},
    "/api/files/list": url => {
      const path = url.searchParams.get("path") || "";
      if (path === "config") {
        return { path: "config", truncated: false, pod: "homestead-files-frigate-config",
          entries: [{ name: "config.yml", kind: "file", size: 4210, editable: true },
            { name: "secrets.yaml", kind: "file", size: 180, editable: true }] };
      }
      return { path: "", truncated: false, pod: "homestead-files-frigate-config",
        entries: [{ name: "config", kind: "dir", size: 0, editable: false },
          { name: "clips", kind: "dir", size: 0, editable: false },
          { name: "frigate.db", kind: "file", size: 5242880, editable: false },
          { name: "notes.txt", kind: "file", size: 96, editable: true }] };
    },
    "/api/files/read": url => ({ path: url.searchParams.get("path") || "notes.txt", size: 96,
      content: "detectors:\n  coral:\n    type: edgetpu\n\nmqtt:\n  host: mqtt\n" }),
    "/api/files/write": { ok: true, path: "config/config.yml", bytes: 96,
      message: "Saved config/config.yml (96 bytes); previous contents kept as config.yml.homestead-bak" },
    "/api/files/close": { ok: true },
    "/api/volumes/ownership": { uid: 1000, gid: 1000, known: true, workload: "frigate",
      image: "ghcr.io/blakeblackshear/frigate:stable", source: "PUID/PGID on frigate" },
    "/api/volumes/chown": { ok: true, job: "homestead-chown-frigate-config", uid: 1883, gid: 1883,
      message: "Setting ownership of frigate-config to 1883:1883" },
    "/api/sources/measure": { total_bytes: 9663676416, complete: false, suggested_gb: 12,
      timeout_seconds: 25, missing: ["/mnt/user/appdata/media-server/old-config"], paths: [
        { path: "/mnt/user/appdata/media-server", bytes: 1073741824, measured: true,
          exists: true, timed_out: false, gross_bytes: 9663676416 },
        { path: "/mnt/user/appdata/media-server/transcode", bytes: 8589934592, measured: true,
          exists: true, timed_out: false },
        { path: "/mnt/user/appdata/media-server/old-config", bytes: null, measured: false,
          exists: false, timed_out: false },
        { path: "/mnt/user/media", bytes: null, measured: false, exists: true,
          timed_out: true } ] },
    "/api/image-updates/progress": { ns: "lab", name: "plex", uid: "demo-rollout", phase: "progressing", desired: 1,
      replicas: 1, updated: 1, ready: 0, available: 0, unavailable: 1, generation: 4,
      observed_generation: 4, problems: [], can_rollback: true,
      pull: { state: "pulling", image: "ghcr.io/hotio/plex:latest", node: "harvester-node1",
        seconds: 135, pod: "plex-5cc965d5f7-d7x7b" },
      pods: [{ name: "plex-5cc965d5f7-d7x7b", phase: "Pending", node: "harvester-node1",
        waiting: [{ container: "plex", reason: "ContainerCreating", message: "" }],
        pull: { state: "pulling", image: "ghcr.io/hotio/plex:latest", seconds: 135 } }],
      images: { plex: "ghcr.io/hotio/plex:latest" } },
    "/api/imports": [
      { name: "homestead-import-plex", app: "plex", state: "running", start: "2026-09-21T08:40:00Z",
        active: 1, succeeded: 0, failed: 0, step: 2, steps: 4, folder: "transcode",
        detail: "nas-01:/mnt/user/appdata/plex/transcode -> /transcode",
        step_percent: 50, percent: 37.5, rate: "22.10MB/s" },
      { name: "homestead-import-obsidian", app: "obsidian", state: "failed", percent: 12,
        start: "2026-09-21T07:55:00Z", active: 0, succeeded: 0, failed: 1, step: 1, steps: 3,
        folder: "config", step_percent: 36, rate: "", error: "ran out of space on the volume",
        error_detail: 'rsync: [receiver] write failed on "/appdata/home-assistant_v2.db": No space left on device (28)' },
      { name: "homestead-import-krusader", app: "binhex-krusader", state: "done", percent: 100,
        start: "2026-09-21T08:12:00Z", end: "2026-09-21T08:19:00Z", active: 0, succeeded: 1, failed: 0 },
    ],
    "/api/lh/overview": lhOverview,
    "/api/lh/job/run": (url, init) => ({ ok: true, job: `${JSON.parse(init?.body || "{}").name}-now-000001`, namespace: "longhorn-system" }),
    "/api/lh/snapshot-progress": {known: true, active: false, percent: null, replicas: [], errors: []},
    "/api/lh/snapshots": [{ name: "homestead-1758765600-a1b2c3", volume: "pvc-demo", created: new Date(Date.now() - 86400000).toISOString(),
        size_mb: 212.4, ready: true, user_created: false, source: "system", children: ["volume-head"] },
      { name: "homestead-1758679200-d4e5f6", volume: "pvc-demo", created: new Date(Date.now() - 2 * 86400000).toISOString(),
        size_mb: 48.1, ready: true, user_created: true, source: "user" }],
    "/api/lh/snapshot/revert/plan": { volume: "pvc-demo", snapshot: "homestead-1758765600-a1b2c3", namespace: "lab", claim: "paperless-data",
      created: new Date(Date.now() - 86400000).toISOString(), ready: true, blockers: [],
      consumers: [{ kind: "Deployment", name: "paperless", replicas: 1, running: true }] },
    "/api/lh/snapshot/revert": { ok: true, detail: "Rolling back: what uses it stops first, then starts again" },
    "/api/lh/backups": lhBackups, "/api/lh/backupvolumes": lhBackupVolumes,
    "/api/lh/group": (url, init) => ({ ok: true, name: JSON.parse(init?.body || "{}").name, added: [], removed: [],
      left_default: [], back_to_default: [], kept_in_default: [] }),
    "/api/lh/group/delete": { ok: true, back_to_default: ["homeassistant-config"], idle_jobs: ["media-weekly-trim"] },
    "/api/lh/backup/delete": { ok: true },
    "/api/lh/restore/plan": restorePlan,
    "/api/lh/restore": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, backup: body.backup || lhBackups[0].name,
        namespace: body.namespace || "lab", name: body.name || "pvc-demo-frigate-restore",
        size_gb: body.size_gb || 20,
        message: `Restore of ${body.backup || lhBackups[0].name} into ${body.namespace || "lab"}/${body.name || "pvc-demo-frigate-restore"} started` };
    },
    "/api/storageclasses": ["harvester-longhorn", "longhorn", "longhorn-r2"],
    "/api/storage/classes": [
      { name: "harvester-longhorn", provisioner: "driver.longhorn.io", engine: "v1", replicas: "3", migratable: true,
        expandable: true, reclaim: "Delete", default: true, internal: false, in_use: 2 },
      { name: "longhorn", provisioner: "driver.longhorn.io", engine: "v1", replicas: "3", migratable: false,
        encrypted: true, expandable: true, reclaim: "Delete", default: false, internal: false, in_use: 0 },
      { name: "longhorn-r2", provisioner: "driver.longhorn.io", engine: "v1", replicas: "2", migratable: true,
        expandable: true, reclaim: "Retain", default: false, internal: false, in_use: 7 },
      { name: "longhorn-ssd", provisioner: "driver.longhorn.io", engine: "v1", replicas: "2", migratable: false,
        expandable: true, reclaim: "Delete", default: false, internal: false, in_use: 1, disk_tags: ["ssd"], node_tags: [] },
      { name: "longhorn-static", provisioner: "driver.longhorn.io", engine: "v1", replicas: "", migratable: false,
        expandable: false, reclaim: "Delete", default: false, internal: true, in_use: 0 },
      { name: "longhorn-v2", provisioner: "driver.longhorn.io", engine: "v2", replicas: "2", migratable: false,
        expandable: true, reclaim: "Delete", default: false, internal: false, in_use: 1 },
    ],
    "/api/storage/v2": { enabled: true, harvester_setting: true, ready_nodes: 2, total_nodes: 3, nodes: [
      { name: "harvester-node1", block_disks: 1, hugepages_mb: 2048, ready: true, missing: [], missing_modules: [],
        checks: { cpu: true, modules: true, hugepages: true, disk: true } },
      { name: "harvester-node2", block_disks: 1, hugepages_mb: 2048, ready: true, missing: [], missing_modules: [],
        checks: { cpu: true, modules: true, hugepages: true, disk: true } },
      { name: "harvester-node3", block_disks: 0, hugepages_mb: 2048, ready: false, missing: ["a V2 (block) disk"], missing_modules: [],
        checks: { cpu: true, modules: true, hugepages: true, disk: false } }],
      distribution: "harvester", longhorn_version: "v1.8.1", longhorn_ok: true },
    "/api/network/service/delete": { ok: true, freed: ["192.0.2.246:8989/TCP"],
      message: "Service lab/sonarr-old deleted, releasing 192.0.2.246:8989/TCP" },
    "/api/shares": shares,
    "/api/shares/users": [{ user: "lab", has_password: true, shares: ["secure"] },
      { user: "backup", has_password: true, shares: [] }],
    "/api/shares/users/delete": { ok: true, message: "Unused SMB user removed" },
    "/api/shares/server": { installed: true, enabled: true, desired: 1, ready: 1,
      name: "homestead-smb", address: "192.0.2.245", shares: 3,
      served_shares: ["media", "photos", "secure"], in_sync: true, image: "dperson/samba:latest" },
    "/api/shares/nfs/server": { installed: true, enabled: true, desired: 1, ready: 1,
      name: "homestead-nfs", address: "192.0.2.246", exports: ["media"], image: "pedroetb/nfs-server:v2.4.0",
      recovery: { level: "limited", detail: "Reconnect recovery prerequisites checked; lock recovery is unsupported",
        eligible_hosts: ["harvester-node1", "harvester-node2", "harvester-node3"], blockers: [],
        warnings: ["Longhorn RWX re-exports do not support NFS lock recovery. Use this gateway for ordinary files."], lock_recovery: false } },
    "/api/shares/options": { namespace: "lab", samba_installed: true, node: "harvester-node2",
      pvcs: volumes.map(v => ({ name: v.pvc_name, size: `${v.size_gb}Gi`, status: "Bound",
        access_modes: v.access_modes, storage_class: v.storage_class,
        robustness: v.robustness, node: v.node, migratable: v.storage_class === "longhorn-r2",
        workloads: v.attached || [] })),
      storage_classes: ["harvester-longhorn", "longhorn", "longhorn-r2"],
      shared_storage_classes: ["longhorn"], storage_class_facts: deployOptions.storage_class_facts },
    "/api/shares/edit": { ok: true, shares, deployment_updated: true,
      message: "Share secure updated; Samba is restarting" },
    // A finished batch plus one still running, which is the case the clear
    // button exists for and the one it must not touch.
    "/api/operations": () => (window.__demoOps ??= [
      { id: "op1", kind: "import", title: "Import frigate", status: "succeeded", progress: 100,
        message: "copied 3 folders", started_at: new Date(Date.now() - 9e5).toISOString(),
        finished_at: new Date(Date.now() - 6e5).toISOString(), href: "/import",
        resource: { kind: "Job", name: "frigate", namespace: "lab" } },
      { id: "op2", kind: "image-pull", title: "Pull plex", status: "failed", progress: 40,
        message: "registry returned HTTP 429", started_at: new Date(Date.now() - 6e5).toISOString(),
        finished_at: new Date(Date.now() - 5e5).toISOString(), href: "/image-cache",
        resource: { kind: "Image", name: "plex", namespace: "lab" } },
      { id: "op3", kind: "image-update", title: "Update home-assistant", status: "running", progress: 62, cancellable: true,
        message: "rolling out", started_at: new Date(Date.now() - 6e4).toISOString(),
        href: "/containers?q=home-assistant", resource: { kind: "Deployment", name: "home-assistant", namespace: "lab" } },
      { id: "op4", kind: "reclass", title: "Move paperless-data to longhorn-r3", status: "running", progress: 41, cancellable: false, storage_recovery: true,
        message: "Copying 52% at 96.4MB/s", started_at: new Date(Date.now() - 3e5).toISOString(),
        href: "/volumes?q=paperless-data", resource: { kind: "PersistentVolumeClaim", name: "paperless-data", namespace: "lab" },
        copy: { percent: 52, speed: "96.4MB/s", verifying: false },
        steps: [["stop", "Stop what uses it", "done"], ["create", "Make the new volume", "done"], ["copy", "Copy the data", "active"],
          ["verify", "Check the copy", "todo"], ["swap", "Swap the new volume in", "todo"], ["start", "Start everything again", "todo"]]
          .map(([id, label, state]) => ({ id, label, state })) },
      { id: "op5", kind: "k3s-cluster", title: "k3s cluster k3s-lab", status: "running", progress: 60, cancellable: true,
        message: "VMs running; installing k3s on k3s-lab-server-1 (192.0.2.60) - a few minutes",
        started_at: new Date(Date.now() - 4e5).toISOString(), href: "/vms",
        resource: { kind: "VirtualMachine", name: "k3s-lab-server-1", namespace: "lab" } },
    ]),
    // What cancelling each running job above would do, as the server says it.
    "/api/operations/storage-recovery/preview": (url, init) => {
      const id=JSON.parse(init.body).id, state=window.__demoStorageState || "running";
      const held=state==="ready", uncertain=state==="uncertain", running=state==="running";
      return {tokens:held?{continue:"demo-storage-ready"}:running?{pause:"demo-storage-running"}:{},plan:{
        id,claim:"paperless-data",namespace:"lab",phase:"copy",status:running?"running":"failed",from_class:"longhorn-r2",to_class:"longhorn-r3",
        can_continue:held,can_pause:running,requires_confirmation:true,
        message:uncertain?"The response to the copy job request was lost.":held?"Copy volume created. Review current capacity before starting the copy.":"Copying and checking data; the original remains protected.",
        blockers:uncertain?["The copy request has no confirmed outcome. Inspect the Kubernetes audit trail and retained resources; this screen cannot safely retry it."]:[],
        warnings:["This review does not delete data. Continuing may restart workloads and never rolls back or repeats an uncertain request.",...(held?["Projected RAM on lab-node-2 is above its configured warning threshold."]:[])],
        workloads:[{kind:"Deployment",name:"paperless"}],
        resources:[{resource:{kind:"Deployment",name:"paperless"},receipt:"accepted",relationship:"same identity"},
          {resource:{kind:"PersistentVolumeClaim",name:"paperless-data-reclass"},receipt:"accepted",relationship:"same identity"},
          ...(uncertain?[{resource:{kind:"Job",name:"paperless-data-reclass-copy"},receipt:"uncertain",relationship:"identity unproven"}]:[])],
        next_write:held?{step:"copy-job",method:"POST",target:{kind:"Job",name:"paperless-data-reclass-copy"}}:null}};
    },
    "/api/operations/storage-recovery/act": (url, init) => {
      const b=JSON.parse(init.body), state=window.__demoStorageState || "running";
      if(!b.confirm_capacity || (b.action==="continue"?state!=="ready" || b.capacity_token!=="demo-storage-ready":
          b.action!=="pause" || state!=="running" || b.capacity_token!=="demo-storage-running")) throw new Error("Refresh the storage review first");
      window.__demoStorageState=b.action==="continue"?"running":"ready";
      return {ok:true,detail:b.action==="continue"?"Move queued to continue with fresh safety checks":"Move paused; data and running copy jobs retained"};
    },
    "/api/operations/power-recovery/preview": (url, init) => {
      const id=JSON.parse(init.body).id, op=(window.__demoOps || []).find(row=>row.id===id);
      if (!op?.power_recovery) throw new Error("This job has no uncertain power outcome");
      const blocked=!!op.demo_dispatching;
      return {capacity_token:blocked?null:"demo-recovery-review",plan:{id,blocked,requires_confirmation:true,
        blockers:blocked?["A Homestead dispatcher is still active. Wait before resolving this job."]:[],
        resource:{...op.resource,original_uid:"demo-original-vm"},action:"restart",dispatch_phase:"uncertain",confirm:op.resource.name,
        observed:{vm:{uid:"demo-original-vm"},instance:{uid:"demo-current-instance"},same_vm:true,run_strategy:"RerunOnFailure",
          vm_status:"Running",instance_phase:"Running",ready:true,paused:false,queued_changes:0},
        warnings:["Tracking only: no VM, disk, Secret or restart policy is changed.",
          "The local dispatcher is inactive, but Kubernetes may still apply the old request late. A running guest does not prove which request caused it.",
          "Resolving this record permits a new, separately reviewed action. The original outcome stays unknown; nothing is retried."]}};
    },
    "/api/operations/power-recovery/resolve": (url, init) => {
      const body=JSON.parse(init.body), op=(window.__demoOps || []).find(row=>row.id===body.id);
      if (!op?.power_recovery || op.demo_dispatching || body.capacity_token!=="demo-recovery-review" || body.confirm!==op.resource.name || !body.confirm_capacity || !body.acknowledge_unknown)
        throw new Error("Review the uncertain outcome and confirm the VM name first");
      Object.assign(op,{status:"failed",power_recovery:false,cancellable:false,dismissible:false,
        message:"Admin acknowledged an unknown outcome. No retry or rollback was sent; a late effect is still possible.",finished_at:new Date().toISOString()});
      return {ok:true,detail:"Tracking resolved as unknown; no cluster change was sent",operation:op};
    },
    "/api/operations/vm-recovery/preview": (url, init) => {
      const id=JSON.parse(init.body).id, op=(window.__demoOps || []).find(row=>row.id===id);
      if (!op?.mutation_recovery) throw new Error("This job has no incomplete save");
      if (op.kind === "import-create") return {capacity_token:op.demo_dispatching?null:"demo-vm-recovery",plan:{id,blocked:!!op.demo_dispatching,
        blockers:op.demo_dispatching?["The import dispatcher is active. Wait before inspecting its outcome."]:[],requires_confirmation:true,
        resource:op.resource,action:op.kind,dispatch_phase:"failed",confirm:op.resource.name,
        resources:[{resource:{kind:"PersistentVolumeClaim",namespace:"lab",name:"photos-data"},last_write:"accepted",relationship:"same identity",expected:{uid:"demo-volume"},current:{uid:"demo-volume"}},
          {resource:{kind:"Deployment",namespace:"lab",name:"photos"},last_write:"uncertain",relationship:"identity unproven",current:{uid:"demo-app"}},
          {resource:{kind:"Job",namespace:"lab",name:"homestead-import-photos"},last_write:"not dispatched",relationship:"not found"}],
        warnings:["All data and resources are retained. This does not retry the import or remove its app-start hold.","Inspect partial files before any new review. A sent request may still finish late."]}};
      const batch=op.kind==="k3s-cluster", name=batch?op.batch_name:op.resource.name;
      return {capacity_token:op.demo_dispatching?null:"demo-vm-recovery",plan:{id,blocked:!!op.demo_dispatching,requires_confirmation:true,
        blockers:op.demo_dispatching?["The VM configuration dispatcher is active; wait before inspecting its outcome."]:[],
        resource:{...op.resource,name},action:op.kind,dispatch_phase:"failed",confirm:name,
        resources:[{resource:{apiVersion:"kubevirt.io/v1",kind:"VirtualMachine",namespace:op.resource.namespace,name:op.resource.name},
          current:{uid:"demo-vm-identity"},expected:{uid:"demo-vm-identity"},relationship:"same identity",last_write:"uncertain"},
          {resource:{apiVersion:"v1",kind:"Secret",namespace:op.resource.namespace,name:op.resource.name+"-login"},
          current:{uid:"demo-secret-identity"},expected:{uid:"demo-secret-identity"},relationship:"same identity",last_write:"accepted"},
          ...(batch?[{resource:{apiVersion:"kubevirt.io/v1",kind:"VirtualMachine",namespace:op.resource.namespace,name:name+"-agent-1"},
            current:null,expected:null,relationship:"not found",last_write:"not dispatched"}]:[])],
        warnings:["All VM, disk, image and Secret resources are retained. No retry, rollback or deletion is sent.",
          "The earlier request may still take effect late. Accepted receipts do not prove a complete save or guest health.",
          "Resolving releases this tracking block, but the old approval stays consumed. A new action requires a fresh review."]}};
    },
    "/api/operations/vm-recovery/resolve": (url, init) => {
      const body=JSON.parse(init.body),op=(window.__demoOps || []).find(row=>row.id===body.id);
      if (!op?.mutation_recovery || op.demo_dispatching || body.capacity_token!=="demo-vm-recovery" || body.confirm!==(op.kind==="k3s-cluster"?op.batch_name:op.resource.name) || !body.confirm_capacity || !body.acknowledge_unknown)
        throw new Error("Review the retained resources and confirm the VM name first");
      Object.assign(op,{status:"failed",mutation_recovery:false,cancellable:false,dismissible:false,
        message:"Admin inspected the incomplete save. Resources retained, outcome unknown; no retry or rollback.",finished_at:new Date().toISOString()});
      if(op.kind==="k3s-cluster")op.tracking_stopped=true;
      return {ok:true,detail:"Tracking resolved as unknown; no cluster change was sent",operation:op};
    },
    "/api/operations/cancel-plan": (url, init) => {
      const id = JSON.parse(init?.body || "{}").id;
      const op = (window.__demoOps || []).find(item => item.id === id) || {};
      const base = { id, kind: op.kind, title: op.title, status: op.status, progress: op.progress,
        message: op.message, resource: op.resource, can: true, why_not: "", severity: "high",
        confirm: "", needs: "operator", options: [] };
      if (op.kind === "vm-power") return {...base,mode:"forget",action:"Stop tracking it",severity:"low",confirm:op.resource.name,
        can:op.cancellable,why_not:"An uncertain request cannot be forgotten",undo:[],
        keeps:["The accepted power request still runs in KubeVirt. Stopping tracking does not undo it.","Its approval stays consumed."]};
      if (op.kind === "k3s-cluster") return { ...base, mode: "forget", tracking_only:true, action: "Stop tracking it", needs: "admin",
        cleanup:op.status==="failed",severity:"low",confirm:"k3s-lab",undo:[],
        keeps:["This older job has no creation identity receipts. A matching name or label cannot prove it is the original VM; automatic cleanup is disabled.",
          "All VMs, disks, Secrets and IP-address records remain. Guest installation may continue.",
          "Inspect each VM separately before removing it. No addresses are freed and readiness is not verified.",
          "After acknowledgement, the finished tracking record can be cleared. Save any recovery details you still need."] };
      if (op.kind === "reclass") return { ...base, can: false, needs: "admin",
        why_not: "Use Review storage move to pause safely; retained data is not rolled back or deleted." };
      return { ...base, mode: "rollback", action: "Cancel and put back",
        undo: ["home-assistant's home-assistant goes back to ghcr.io/home-assistant/home-assistant@sha256:4be1…"],
        keeps: ["home-assistant's pods restart once more, onto that image"] };
    },
    // A job's log: its steps, and for a k3s cluster each node's console.
    "/api/operations/log": url => {
      const op = (window.__demoOps || []).find(item => item.id === url.searchParams.get("id")) || {};
      const at = s => new Date(Date.now() - s * 1000).toISOString();
      const k3s = op.kind === "k3s-cluster";
      return { ...op,
        history: k3s ? [{ t: at(400), s: "queued", p: 0, m: "Starting 3 VMs" }, { t: at(380), s: "running", p: 10, m: "0 of 3 VMs running" },
          { t: at(300), s: "running", p: 36, m: "2 of 3 VMs running" }, { t: at(240), s: "running", p: 50, m: "3 of 3 VMs running" },
          { t: at(230), s: "running", p: 60, m: op.message }]
          : [{ t: at(120), s: "queued", p: 0, m: "Waiting for Kubernetes" }, { t: at(60), s: op.status, p: op.progress, m: op.message || op.status }],
        sources: k3s ? [
          { title: "k3s-lab-server-1 · server · 192.0.2.60", text: "[  OK  ] Started cloud-final.service - Cloud-init: Final Stage.\n[INFO]  Finding release for channel stable\n[INFO]  Using v1.33.4+k3s1 as release\n[INFO]  Downloading hash https://github.com/k3s-io/k3s/releases/download/v1.33.4+k3s1/sha256sum-amd64.txt\n[INFO]  Downloading binary https://github.com/k3s-io/k3s/releases/download/v1.33.4+k3s1/k3s\n[INFO]  Verifying binary download\n[INFO]  Installing k3s to /usr/local/bin/k3s\n[INFO]  systemd: Starting k3s\n==> waiting for the API server\n==> installing Longhorn (this takes a few minutes)", note: "" },
          { title: "k3s-lab-agent-1 · agent · 192.0.2.61", text: "[  OK  ] Started cloud-final.service - Cloud-init: Final Stage.\n[INFO]  Finding release for channel stable\n==> waiting for https://192.0.2.60:6443 to answer", note: "" },
          { title: "k3s-lab-agent-2 · agent · 192.0.2.62", text: "", note: "the VM is not running yet" }]
          : op.kind === "reclass" ? [{ title: "Copy and check", text: "==> copying 20.0 GiB\n  10,737,418,240  52%   96.40MB/s    0:01:50", note: "" }]
          : op.kind === "unraid-vm-import" ? [{ title: "Disk 1 · copy", kind:"disk-copy",
            progress:{percent:0,bytes:0,total_bytes:20000000000,bytes_per_second:null,eta_seconds:null},
            text: "curl: (22) The requested URL returned error: 413\n0.0% · 0.0 / 20.0 GB · ETA estimating\nHSVM-FAILED Disk stream or CDI upload exited with status 22", note: "Saved before cleanup" },
            { title: "Disk 1 · CDI upload", text: "Upload rejected: No space left on device", note: "Saved before cleanup" }] : [] };
    },
    "/api/operations/cancel": (url, init) => {
      const id = JSON.parse(init?.body || "{}").id;
      const op = (window.__demoOps || []).find(item => item.id === id);
      if(op?.kind==="k3s-cluster") {
        if(JSON.parse(init.body).confirm!=="k3s-lab")throw new Error("Type k3s-lab to confirm");
        Object.assign(op,{status:op.status==="failed"?"failed":"cancelled",cancellable:false,cleanable:false,
          tracking_stopped:true,dismissible:true,finished_at:new Date().toISOString(),
          message:"Tracking stopped. All VMs, disks, Secrets and IP-address records are retained; no cleanup or readiness verification was performed."});
        return {ok:true,id,detail:op.message,operation:op};
      }
      if (op) Object.assign(op, { status: "cancelled", cancellable: false, finished_at: new Date().toISOString(),
        message: op.kind === "vm-power" ? "Stopped tracking; KubeVirt is unchanged and the approval remains consumed"
          : "Cancelled and put back" });
      return { ok: true, id, detail: op?.message || "cancelled", operation: op };
    },
    "/api/volumes/reclass/plan": { ok: true, blockers: [], capacity_token:"demo-reclass-review", namespace: "lab", claim: "frigate-config",
      warnings: ["If a step cannot be verified, the move pauses for review. Data is kept; there is no automatic retry or rollback."], from_class: "longhorn-r2", to_class: "longhorn-r3", volume_mode: "Filesystem", access_modes: ["ReadWriteOnce"],
      consumers: [{ kind: "Deployment", name: "frigate", replicas: 1, running: true }, { kind: "Deployment", name: "homestead-smb", replicas: 1, running: true }],
      space: { size_gb: 20, used_gb: 6.4, replicas: 3, allocated_gb: 60, written_gb: 19.2, longhorn: true, room_gb: 36.8 },
      minutes: 3, downtime: true },
    "/api/volumes/reclass/start": { ok: true, operation: { id: "op4" } },
    "/api/self/health": () => {
      const now = Date.now() / 1000;
      return { version: "2.8.318-dev.6", leader: true, identity: "homestead-6d9f-abcde",
        api: { ok: true, ms: 38 },
        replicas: { desired: 1, pods: [{ name: "homestead-6d9f-abcde", node: "harvester-node1", ready: true, leader: true, this: true }] },
        loops: [{ name: "sampler", label: "Live charts", state: "ok", last_ok: now - 12, error: "", every: 30 },
          { name: "alerts", label: "Alerts and notifications", state: "ok", last_ok: now - 8, error: "", every: 20 },
          { name: "history", label: "Long-term stats", state: "ok", last_ok: now - 140, error: "", every: 300 },
          { name: "hardware", label: "Hardware detection", state: "ok", last_ok: now - 20, error: "", every: 30 },
          { name: "moves", label: "Cluster moves", state: "failing", last_ok: now - 900, error: "could not reach branch: timed out", every: 10 }],
        probe: { installed: true, desired: 3, ready: 3, reporting: 3, smart: 2, state: "current", detail: "homestead-nodeprobe is running this release's scripts" },
        samba: { installed: true, enabled: true, desired: 1, ready: 1, name: "homestead-smb",
          address: "192.0.2.245", shares: 3, served_shares: ["media", "photos", "secure"],
          in_sync: true, image: "dperson/samba:latest" },
        permissions: { state: "current", detail: "Homestead's permissions match this release" },
        backups: { deployed: true, ready: true, endpoint: "http://192.0.2.244:9000" },
        addresses: { lb_ip: "192.0.2.242", problem: "", clashes: [], platform: ["192.0.2.210"] },
        mqtt: { state: "publishing", detail: "publishing to 192.0.2.177:1883 every 60s", error: "", last_publish: now - 20 } };
    },
    "/api/network/vips/add": { ok: true, added: ["192.0.2.232"], skipped: [], detail: "1 address added" },
    "/api/network/vips/remove": { ok: true, detail: "192.0.2.231 is no longer reserved for Homestead" },
    "/api/network/vips/label": { ok: true },
    "/api/self/samba": { ok: true, detail: "Samba is stopping; the shares, their volumes and passwords are kept" },
    "/api/self/nfs": { ok: true, detail: "NFS stopped; exports, shares and every PVC were kept" },
    "/api/addons/nfs/remove": { ok: true, detail: "NFS server removed. Export settings and PVCs were kept." },
    "/api/shares/nfs": { ok: true, detail: "NFS export saved" },
    "/api/volumes/other": [{ namespace: "lab", name: "scratch-cache", volume: "pvc-demo-local", storage_class: "local-path",
      phase: "Bound", access_modes: ["ReadWriteOnce"], size: "5Gi", reason: "" }],
    "/api/volumes/old-copies": [{ pv: "pvc-7f3a9c1e-2b44-4d1b-9a55-0c1f2e3d4a5b", was: "lab/mosquitto-appdata",
      storage_class: "longhorn-r2", size: "10Gi", since: "2026-09-24T12:00:00Z" }],
    "/api/volumes/old-copies/remove": { ok: true, detail: "removing the old copy" },
    "/api/operations/dismiss": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const before = (window.__demoOps || []).length;
      window.__demoOps = (window.__demoOps || []).filter(op => op.dismissible === false || (body.all
        ? !["succeeded", "failed", "cancelled"].includes(op.status)
        : op.id !== body.id));
      const gone = before - window.__demoOps.length;
      return { ok: true, dismissed: gone, remaining: window.__demoOps.length,
        detail: gone ? `cleared ${gone} finished job${gone === 1 ? "" : "s"}; 1 still running`
          : "nothing finished to clear" };
    },
    "/api/cluster/shutdown/plan": () => ({ready: !window.__demoShutdownBlocked, blockers: window.__demoShutdownBlocked ? ['Gracefully stop these VMs first: default/home-assistant-os', 'lab/paperless: disruption budget permits no verified eviction'] : [],
      confirm:'SHUT DOWN CLUSTER', review_token:'demo-shutdown', nodes: nodes.map(n => ({name:n.name, cordoned:false})), pods:12, volumes:8, homestead_node:nodes[0].name, local_storage:[]}),
    "/api/cluster/shutdown": (url, init) => {
      if (init?.method === 'POST') window.__demoShutdown = {run:'demo-shutdown', phase:'draining', progress:55, deadline:Date.now()/1000+1800,
        hosts:nodes.map(n=>({name:n.name,state:'Helper ready'})),
        drain:{done:9, total:12, pods:[{pod:'lab/paperless', node:nodes[0].name, state:'stopping'},
          {pod:'lab/frigate', node:nodes[1 % nodes.length].name, state:'evicting'},
          {pod:'kubevirt/virt-controller-7d66d7487-sbqc4', node:nodes[2 % nodes.length].name, state:'budget'}]},
        message:'Stopping applications: 9 of 12 pods stopped. Evicting: lab/frigate; stopping: lab/paperless; held by a disruption budget, stopped directly within 30 s: kubevirt/virt-controller-7d66d7487-sbqc4. Homestead stays online',
        plan:{own:['lab','homestead','demo'], own_node:nodes[0].name, nodes:nodes.map(n=>({name:n.name}))}};
      return {state:window.__demoShutdown || null};
    },
    "/api/cluster/shutdown/cancel": () => {
      if (window.__demoShutdown) Object.assign(window.__demoShutdown, {phase:'failed', message:'Shutdown cancelled. Original scheduling restored; check workloads.'});
      return {ok:true};
    },
    "/api/cluster/shutdown/recover": () => { window.__demoShutdown = null; return {ok:true}; },
    "/api/workloads": workloads, "/api/network": network,
    // Rebooting a host: one app has nowhere else to go, one volume keeps a
    // single copy elsewhere while the host is down.
    // A reviewed reboot or shutdown: a job that walks through the real phases.
    "/api/node/power": (url, init) => {
      const body = JSON.parse(init?.body || "{}"), reboot = body.action === "reboot";
      const op = { id: "demo-power-" + Date.now(), kind: "node-power", title: `${body.action} ${body.node}`, status: "running",
        progress: 5, message: "Cordoning host; power has not been sent", started_at: new Date().toISOString(), finished_at: "",
        href: "/nodes?node=" + encodeURIComponent(body.node), resource: { kind: "Node", name: body.node, namespace: "" },
        power: { phase: "cordoning", action: body.action, node: body.node, direct: !!(body.force || window.__demoSingleHostOutage),
          holds: !body.force && Object.values(body.choices || {}).length > 0, held: Object.values(body.choices || {}).filter(c => c === "wait").length } };
      const phases = op.power.direct
        ? [["verifying", 15, "Rechecking the host"], ["sending", 20, "Submitting power helper"], ["observing", 60, reboot ? "Waiting for the host to return" : "Waiting for the host to leave Ready"]]
        : [...(op.power.holds ? [["holding", 8, "Waiting to stop or move: lab/frigate, vms/win11; power has not been sent"]] : []),
           ["draining", 10, "Evicting workload and system pods: 9 left"], ["draining", 12, "Evicting workload and system pods: 3 left"],
           ["verifying", 15, "Drain completed; rechecking quorum, VMs, pods and volume replicas before power"],
           ["sending", 20, "Submitting power helper"], ["observing", 60, reboot ? "Host is Ready; waiting for 2 volume(s) to become healthy" : "Host is NotReady; waiting to confirm shutdown"]];
      (window.__demoOps ??= responses["/api/operations"]()).unshift(op);
      phases.forEach(([phase, progress, message], i) => setTimeout(() => Object.assign(op, { progress, message, power: { ...op.power, phase } }), (i + 1) * 2500));
      setTimeout(() => Object.assign(op, { status: "succeeded", progress: 100, finished_at: new Date().toISOString(),
        message: reboot ? "Host rebooted and its volumes are healthy. The host stays cordoned." : "Host is powered off." }), (phases.length + 1) * 2500);
      return { operation: op, steps: [], background: true };
    },
    "/api/quorum": { members: ["harvester-node1", "harvester-node2", "harvester-node3"], ready: ["harvester-node1", "harvester-node2", "harvester-node3"],
      total: 3, quorum_needs: 2, can_lose: 1, power_enabled: true },
    "/api/node/impact": url => ({ node: url.searchParams.get("node"),
      workloads: [{ ns: "lab", name: "frigate", hardware: ["coral"], eligible: [], stranded: true, blocked: [{ name: "harvester-node2", why: ["no coral"] }] },
        { ns: "lab", name: "home-assistant", hardware: [], eligible: ["harvester-node2", "harvester-node3"], stranded: false },
        { ns: "lab", name: "paperless", hardware: [], eligible: ["harvester-node2"], stranded: false }],
      stranded: [{ ns: "lab", name: "frigate" }] }),
    "/api/node/power/plan": url => {
      const plannedOutage = !!window.__demoSingleHostOutage && url.searchParams.get("force") !== "1";
      const plan = { node: url.searchParams.get("node"), action: url.searchParams.get("action"), review_token: "demo-power",
      boot_id: "demo", pods: 14, vms: [], storage_unknown: false, ready: true, requires_data_ack: true, blockers: [],
      overridable: [], hard_blockers: [], force: url.searchParams.get("force") === "1",
      planned_outage: plannedOutage,
      workloads: [{ ns: "lab", name: "frigate", stranded: true, eligible: [] },
        { ns: "lab", name: "home-assistant", stranded: false, eligible: ["harvester-node2", "harvester-node3"] },
        { ns: "lab", name: "paperless", stranded: false, eligible: ["harvester-node2"] }],
      stranded: [{ ns: "lab", name: "frigate" }],
      // What each app and VM on the host does while it is down.
      hold: [
        { id: "Deployment/lab/frigate", kind: "Deployment", ns: "lab", name: "frigate", here: 1, hosts: [], options: ["wait"], default: "wait", why: "no other host it can run on" },
        { id: "Deployment/lab/home-assistant", kind: "Deployment", ns: "lab", name: "home-assistant", here: 1, hosts: ["harvester-node2", "harvester-node3"], options: ["move", "wait"], default: "move", why: "" },
        { id: "Deployment/lab/paperless", kind: "Deployment", ns: "lab", name: "paperless", here: 1, hosts: ["harvester-node2"], options: ["move", "wait"], default: "move", why: "" },
        { id: "VirtualMachine/vms/win11", kind: "VirtualMachine", ns: "vms", name: "win11", here: 1, hosts: ["harvester-node2", "harvester-node3"], options: ["move", "wait"], default: "move", why: "" },
        { id: "VirtualMachine/vms/gpu-desktop", kind: "VirtualMachine", ns: "vms", name: "gpu-desktop", here: 1, hosts: [], options: ["wait"], default: "wait", why: "it cannot live-migrate: a host device is passed through" }],
      volumes: [{ name: "pvc-demo-frigate", claim: "lab/frigate-config", healthy_elsewhere: 1, risk: "single-copy" },
        { name: "pvc-demo-ha", claim: "lab/homeassistant-config", healthy_elsewhere: 2, risk: "resync" }],
      maintenance: { budgets: [{ pod: "lab/paperless-5c9d", budget: "minAvailable 1", allowed: 1 }], local_storage: [] },
      warnings: ["DaemonSets and static pods remain on the host; their services stop during the outage.",
        "1 workload(s) have no eligible failover host", "2 volume(s) lose a replica until this host returns or Longhorn rebuilds"] };
      if (plannedOutage) {
        plan.workloads = [{ns:"lab",name:"homestead",stranded:true,eligible:[]}];
        plan.stranded = [{ns:"lab",name:"homestead"}];
        plan.hold = plan.hold.map(h => ({ ...h, hosts: [], options: ["wait"], default: "wait", why: "the only host" }));
        plan.volumes = [{name:"pvc-demo-homestead",claim:"lab/homestead-data",healthy_elsewhere:0,risk:"unavailable"}];
        plan.maintenance.budgets[0].allowed = 0;
        plan.warnings = ["All applications, storage and Homestead are unavailable while this host is down."];
      }
      return plan;
    },
    // Starting a stopped app that only just fits: one host near its memory
    // warning, one ruled out by placement.
    "/api/workloads/start-plan": url => ({ namespace: url.searchParams.get("ns"), name: url.searchParams.get("name"),
      current: 0, requested: 1, additional: 1, pod_memory_gb: 1.5, pod_request_gb: 0.5, pod_cpu_request_percent: 25,
      reservations_known: true, resource_slots: 3, topology_status: "not-needed", unbounded: [], warning_percent: 88,
      warnings: ["projected RAM reaches 91% (warning at 88%)"], requires_confirmation: true, blocked: false,
      candidates: [
        { name: "harvester-node1", eligible: true, reasons: [], used_gb: 12.6, capacity_gb: 15.6, projected_gb: 14.2, projected_percent: 91,
          metrics_available: true, warnings: ["projected RAM reaches 91% (warning at 88%)"], reservations_known: true,
          reserved_gb: 9.8, allocatable_gb: 15.1, reserved_cpu_percent: 42, request_slots: 2, max_additional_pods: 2, projected_pods: 1 },
        { name: "harvester-node2", eligible: true, reasons: [], used_gb: 8.4, capacity_gb: 15.6, projected_gb: 9.9, projected_percent: 63,
          metrics_available: true, warnings: [], reservations_known: true, reserved_gb: 6.1, allocatable_gb: 15.1,
          reserved_cpu_percent: 31, request_slots: 1, max_additional_pods: 1, projected_pods: 0 },
        { name: "harvester-node3", eligible: false, reasons: ["does not match the node selector (hardware: coral)"], used_gb: 5.2,
          capacity_gb: 7.7, projected_gb: 5.2, projected_percent: 68, metrics_available: true, warnings: [], reservations_known: true,
          reserved_gb: 3.0, allocatable_gb: 7.3, reserved_cpu_percent: 18, request_slots: 0, max_additional_pods: 0, projected_pods: 0 },
      ] }),
    "/api/cluster": { generated_at: 1789891200, state: "attention",
      summary: "The platform is online, with resilience or warning items to review.",
      versions: { harvester: "1.6.0", kubernetes: "1.34.1+rke2r1" },
      control_plane: { total: 2, ready: 2, etcd_total: 2, etcd_ready: 2, quorum_needed: 2, quorum_margin: 0, state: "attention" },
      nodes: nodes.map(n => ({ name: n.name, ready: true, status: "Ready", roles: n.roles,
        schedulable: n.schedulable, pressure: [], cpu_pct: n.cpu_pct, memory_pct: n.mem_pct,
        disk_pct: n.fs_pct, pods: n.pods, version: "v1.34.1+rke2r1", os: "Harvester v1.6.0" })),
      capacity: { pressure: [], unready: [], cordoned: [] },
      services: [
        { id: "api", name: "Kubernetes API", pods: 2, ready: 2, state: "healthy", required: true },
        { id: "etcd", name: "etcd", pods: 2, ready: 2, state: "healthy", required: true },
        { id: "controller", name: "Controller manager", pods: 2, ready: 2, state: "healthy", required: true },
        { id: "scheduler", name: "Scheduler", pods: 2, ready: 2, state: "healthy", required: true },
        { id: "dns", name: "Cluster DNS", pods: 2, ready: 2, state: "healthy", required: true },
        { id: "harvester", name: "Harvester", pods: 3, ready: 3, state: "healthy", required: true },
        { id: "longhorn", name: "Longhorn", pods: 3, ready: 3, state: "healthy", required: true },
        { id: "kubevirt", name: "KubeVirt", pods: 5, ready: 5, state: "healthy", required: false }],
      certificates: { state: "healthy", total: 4, pending: 0, failed: 0, expiring: 0,
        entries: [{ name: "csr-node-3", signer: "kubernetes.io/kube-apiserver-client-kubelet", state: "approved", age_seconds: 8120 }],
        note: "Issued certificate lifetime is estimated from each CSR request. Private keys and certificate bodies are never returned." },
      warnings: [{ namespace: "longhorn-system", object: "longhorn-manager", kind: "Pod", reason: "Unhealthy",
        message: "Readiness probe recovered after one retry", count: 1, age_seconds: 820 }], unavailable: [],
      onboarding: { recommended_role: "Control plane + etcd",
        reason: "The cluster has 2 etcd members. An odd three-member control plane provides a useful one-node failure margin.",
        checks: ["Reserve a unique hostname and management-network address.", "Verify DNS, gateway, and time synchronization from the new host.",
          "Match the running Harvester release before joining it.", "Confirm the install disk is empty and data disks are intentionally assigned.",
          "Review hardware features after join so workloads can use the new host.", "Run drain and failover preflight before relying on the node for resilience."],
        pxe: { enabled: false, status: "Not configured", reason: "PXE can affect DHCP and boot traffic, so it remains a separately designed managed add-on." }} },
    "/api/workload": url => {
      const name = url.searchParams.get("name") || "frigate";
      const found = workloads.find(item => item.name === name) || workloads[0];
      const base = found.pods[0]?.containers || [{ name: found.name, image: found.images[0] }];
      const source = found.name === "home-assistant" ? base.concat([{ name: "mqtt-sidecar", image: "eclipse-mosquitto:2" }]) : base;
      const containers = source.map((container, index) => ({ original_name: container.name, name: container.name,
        image: container.image || found.images[index] || found.images[0], cpu: index ? "20m" : "50m", memory: index ? "64Mi" : "128Mi",
        env: index ? { LOG_LEVEL: "info" } : {}, env_refs: index ? [] : [{ name: "APP_TOKEN", source: "Secret homestead-demo · token" }],
        ports: index ? [{ name: "mqtt", container: 1883, protocol: "TCP", host: 1883, expose: false }]
          : [{ name: "web", container: 8123, protocol: "TCP", host: 8123, expose: true }],
        hardware: index ? [] : (found.hardware || []), volumes: index ? [] : [{ name: "config",
          source: `${found.name}-config`, path: "/config", read_only: false, kind: "existing",
          value: `${found.name}-config`, managed: false }] }));
      return { ns: found.ns, name: found.name, container_name: containers[0].name,
        pod_volumes: [{ name: "config", kind: "pvc", source: `${found.name}-config` }], has_service: true,
        pod_hostname: found.name === "frigate" ? "frigate-core" : "", image: found.images[0], replicas: found.desired,
        cpu: "50m", memory: "128Mi", env: {}, ports: [], hardware: found.hardware || [], icon: "", node: found.nodes[0] || "",
        seed_configs: [], volumes: containers[0].volumes, containers };
    },
    "/api/portal": (url, init) => {
      if (init?.method === "POST") {
        const body = JSON.parse(init.body || "{}");
        portalLinks = (body.links || []).map((link, i) => ({ ...link, id: link.id || `demo${i}`,
          shown: link.icon.startsWith("builtin:") ? { kind: "builtin", src: link.icon.slice(8) } : { kind: "letter" } }));
        return { ok: true, links: portalLinks };
      }
      return { links: portalLinks, icons: ["router", "switch", "wifi", "firewall", "nas", "server", "printer", "camera", "ups", "globe"] };
    },
    "/api/cluster/components": demoPlatform === "harvester"
      ? { distribution: "harvester", harvester: true, checked: Math.floor(Date.now() / 1000), components: [
          { id: "cluster", name: "Harvester", how: "harvester", installed: "" },
          { id: "longhorn", name: "Longhorn", installed: "v1.8.1", newest: "v1.9.1", next: "", behind: true, how: "harvester",
            note: "Comes with Harvester, and is upgraded with it.", notes_url: "https://github.com/longhorn/longhorn/releases/tag/v1.9.1" },
          { id: "kubevirt", name: "KubeVirt", installed: "v1.4.0", newest: "v1.6.0", next: "", behind: true, how: "harvester", phase: "Deployed",
            note: "Comes with Harvester, and is upgraded with it.", notes_url: "https://github.com/kubevirt/kubevirt/releases/tag/v1.6.0" },
          { id: "cdi", name: "CDI", installed: "v1.61.0", newest: "v1.62.0", next: "", behind: true, how: "harvester", phase: "Deployed",
            note: "Comes with Harvester, and is upgraded with it." }] }
      : { distribution: "k3s", harvester: false, checked: Math.floor(Date.now() / 1000), components: [
          { id: "cluster", name: "k3s", installed: "v1.31.4+k3s1", newest: "v1.33.4+k3s1", next: "v1.31.12+k3s1", behind: true, how: "suc",
            steps_left: true, note: "Upgraded by Rancher's system-upgrade-controller: servers one at a time, then agents.",
            notes_url: "https://github.com/k3s-io/k3s/releases/tag/v1.31.12+k3s1", nodes: { "node-1": "v1.31.4+k3s1" } },
          ...(demoPlatform === "kubevirt" ? [{ id: "kubevirt", name: "KubeVirt", installed: "v1.6.0", newest: "v1.6.0", next: "", behind: false,
            how: "helmchart", phase: "Deployed", note: "" }] : [])] },
    "/api/longhorn/v2/upgrade": () => {
      const state=window.__demoV2UpgradeState || 'ready', running=['running','failed'].includes(state), blocked=state==='blocked';
      return {installed:running?'v1.13.0':'v1.12.2',target:'v1.13.0',v2_volumes:4,harvester:false,live_ready:!blocked,
        offline_ready:false,live_blockers:blocked?['disk-data: needs healthy RW replicas on at least two eligible hosts.','Upgrade Kubernetes to 1.34 or newer on every host first.']:[],
        offline_blockers:[...(blocked?['Upgrade Kubernetes to 1.34 or newer on every host first.']:[]),'Stop workloads, detach V2 volumes and wait for replicas to stop.'],
        upgrade:{enabled:running,timeout:60,current_node:state==='running'?'node-2':'',active:state==='running',pending:running,nodes:running?[
          {node:'node-1',state:'completed',stage:'completed',error:'',retries:0},
          {node:'node-2',state:state==='failed'?'failed':'in-progress',stage:state==='failed'?'failed':'waiting-for-healthy-volumes',error:state==='failed'?'Not enough space to rebuild a replica':'',retries:state==='failed'?5:0},
          {node:'node-3',state:'pending',stage:'pending',error:'',retries:0}]:[]}};
    },
    "/api/longhorn/v2/upgrade/review": () => ({capacity_token:'demo-v2-upgrade-review'}),
    "/api/longhorn/v2/upgrade/settings": () => ({ok:true,detail:'V2 upgrade settings updated in this demo'}),
    "/api/cluster/components/upgrade": { ok: true, component: "cluster", name: "k3s", from: "v1.31.4+k3s1", to: "v1.31.12+k3s1",
      detail: "Installing Rancher's system-upgrade-controller first; then the servers move to v1.31.12+k3s1 one at a time, and the agents after them" },
    "/api/cluster/upgrades/start": { ok: true, upgrade: "hvst-upgrade-demo", detail: "Harvester is upgrading to v1.9.0" },
    "/api/cluster/upgrades": { current: "1.8.2", error: "", offered: [{ version: "v1.9.0", released: "20260812", tags: [] }],
      stable: { tag: "v1.9.0", name: "Harvester v1.9.0", channel: "stable", published: "2026-08-12T09:00:00Z", url: "https://github.com/harvester/harvester/releases", offered: true },
      test: { tag: "v1.9.1-rc2", name: "Harvester v1.9.1-rc2", channel: "rc", published: "2026-09-18T09:00:00Z", url: "https://github.com/harvester/harvester/releases", offered: false },
      active: null, history: [], recent: [],
      last: { name: "hvst-upgrade-demo", version: "v1.8.2", previous: "v1.8.1", started: "2026-06-02T08:00:00Z", latest: true,
        state: "succeeded", message: "", progress: 100, nodes: [],
        steps: [["ImageReady", "Upgrade image"], ["RepoReady", "Package repository"], ["NodesPrepared", "Nodes prepared"],
          ["SystemServicesUpgraded", "System services"], ["NodesUpgraded", "Nodes upgraded"]].map(([key, label]) => ({ key, label, state: "done", message: "" })) } },
    "/api/ipam": () => {
      const r = (ip, extra) => ({ ip, name: "", mac: "", kind: "", category: "", note: "", owner: "", tags: [], sources: [],
        cluster: "", scan: null, unifi: null, flags: [], in_dhcp: false, pool: "", ...extra });
      const rows = [
        r("192.0.2.1", { kind: "infrastructure", category: "router", mac: "74:ac:b9:00:00:01", gateway: true,
          unifi: { type: "device", model: "UDM-Pro", name: "UDM Pro", hostname: "" }, scan: { up: true, ports: [22, 53, 443] } }),
        r("192.0.2.2", { kind: "infrastructure", category: "switch", mac: "74:ac:b9:00:00:02", name: "Core switch",
          unifi: { type: "device", model: "USW-Pro-24", name: "USW Pro 24", hostname: "" } }),
        r("192.0.2.3", { kind: "infrastructure", category: "access-point", mac: "74:ac:b9:00:00:03",
          unifi: { type: "device", model: "U6-Lite", name: "Hallway AP", hostname: "u6-lite-hall" } }),
        r("192.0.2.10", { kind: "static", category: "nas", name: "NAS-01", mac: "d0:50:99:00:00:10", tags: ["storage"], note: "Unraid server in the rack - web UI on port 80, parity check runs Sunday nights, UPS on the second shelf",
          scan: { up: true, ports: [22, 80, 445], rdns: "nas-01.lan" } }),
        r("192.0.2.21", { cluster: "node", scan: { up: true, ports: [22, 443] } }),
        r("192.0.2.22", { cluster: "node", scan: { up: true, ports: [22, 443] } }),
        r("192.0.2.40", { kind: "reservation", category: "media", mac: "a4:83:e7:00:00:40",
          unifi: { type: "wired", online: true, reserved: true, name: "Living room TV", hostname: "LGwebOSTV" } }),
        r("192.0.2.41", { kind: "reservation", category: "cctv", mac: "9c:8e:cd:00:00:41",
          unifi: { type: "reservation", online: false, reserved: true, name: "Driveway camera", hostname: "" } }),
        r("192.0.2.60", { scan: { up: true, ports: [80] }, flags: [{ level: "info", text: "answers on the network but is not documented" }] }),
        r("192.0.2.120", { cluster: "vip", services: ["lab/plex"], in_dhcp: true,
          flags: [{ level: "warn", text: "inside the DHCP range: the DHCP server may hand this address to something else" }] }),
        r("192.0.2.131", { kind: "dhcp", category: "phone", mac: "f2:11:00:00:01:31", in_dhcp: true,
          unifi: { type: "wireless", online: true, name: "Pixel 8", hostname: "pixel-8" } }),
        r("192.0.2.242", { cluster: "vip", services: ["lab/homestead"], pool: "lan" }),
      ];
      return { kinds: ["static", "reservation", "dhcp", "reserved", "infrastructure"], suggested: [],
        unifi: demoUnifi ? { configured: true, url: "https://192.0.2.1", site: "default", has_key: true, last_sync: Math.floor(Date.now() / 1000) - 600, site_name: "Default" } : {},
        unifi_networks: [{ cidr: "198.51.100.0/24", name: "IoT", vlan: 20, gateway: "198.51.100.1", dhcp_start: "198.51.100.10", dhcp_end: "198.51.100.250" }],
        subnets: [{ id: "192.0.2.0/24", cidr: "192.0.2.0/24", name: "LAN", vlan: null, gateway: "192.0.2.1",
          dhcp_start: "192.0.2.100", dhcp_end: "192.0.2.199", note: "", rows, usable: 254, used: rows.length,
          dhcp_size: 100, free_static: 118, next_free: ["192.0.2.4", "192.0.2.5", "192.0.2.6"], pool_clash: [],
          scan: { at: Math.floor(Date.now() / 1000) - 3600, state: "done", progress: 100 } }] };
    },
    "/api/ipam/unifi": (url, init) => { demoUnifi = !JSON.parse(init?.body || "{}").forget; return { ok: true }; },
    "/api/helm": [
      { name: "grafana", namespace: "monitoring", chart: "grafana", chart_version: "8.5.2", app_version: "11.3.0", status: "deployed",
        revision: 3, updated: new Date(Date.now() - 86400000 * 2).toISOString(), managed: "homestead", system: false, icon: "" },
      { name: "cert-manager", namespace: "cert-manager", chart: "cert-manager", chart_version: "v1.16.1", app_version: "v1.16.1",
        status: "deployed", revision: 1, updated: new Date(Date.now() - 86400000 * 30).toISOString(), managed: "", system: false, icon: "" },
      { name: "immich", namespace: "media", chart: "immich", chart_version: "0.9.3", app_version: "v1.119.0", status: "pending-install",
        revision: 0, updated: "", managed: "homestead", system: false, icon: "" },
      { name: "rancher-monitoring", namespace: "cattle-monitoring-system", chart: "rancher-monitoring", chart_version: "103.1.1",
        app_version: "45.31.1", status: "deployed", revision: 2, updated: new Date(Date.now() - 86400000 * 90).toISOString(), managed: "", system: true, icon: "" }],
    "/api/helm/release": () => ({ name: "grafana", namespace: "monitoring", chart: "grafana", chart_version: "8.5.2", app_version: "11.3.0",
      status: "deployed", revision: 3, managed: "homestead", system: false, description: "The leading tool for querying and visualizing time series and metrics.",
      notes: "1. Get your 'admin' user password by running:\n   kubectl get secret --namespace monitoring grafana -o jsonpath=\"{.data.admin-password}\" | base64 --decode",
      values: "persistence:\n  enabled: true\n  size: 10Gi\nservice:\n  type: LoadBalancer",
      history: [3, 2, 1].map(revision => ({ revision, status: revision === 3 ? "deployed" : "superseded", chart_version: `8.${revision + 2}.0`,
        app_version: "11.3.0", updated: new Date(Date.now() - 86400000 * (5 - revision)).toISOString(), description: revision === 1 ? "Install complete" : "Upgrade complete" })),
      objects: [["ServiceAccount", "grafana"], ["Secret", "grafana"], ["ConfigMap", "grafana"], ["PersistentVolumeClaim", "grafana"],
        ["Service", "grafana"], ["Deployment", "grafana"]].map(([kind, name]) => ({ kind, name, namespace: "monitoring" })),
      source: { repo: "https://grafana.github.io/helm-charts", chart: "grafana", version: "8.5.2",
        values: "persistence:\n  enabled: true\n  size: 10Gi\nservice:\n  type: LoadBalancer\n" } }),
    "/api/helm/search": [
      { name: "grafana", version: "8.5.2", app_version: "11.3.0", description: "The leading tool for querying and visualizing time series and metrics.",
        repo: "https://grafana.github.io/helm-charts", repo_name: "grafana", publisher: "Grafana", verified: true, official: true, logo: "" },
      { name: "grafana-operator", version: "5.15.1", app_version: "v5.15.1", description: "Helm chart for the Grafana Operator",
        repo: "https://grafana.github.io/helm-charts", repo_name: "grafana", publisher: "Grafana", verified: true, official: false, logo: "" }],
    "/api/helm/chart": { name: "grafana", version: "8.5.2", versions: ["8.5.2", "8.5.1", "8.4.0"], repo: "https://grafana.github.io/helm-charts",
      values: "replicas: 1\npersistence:\n  enabled: false\n  size: 10Gi\nservice:\n  type: ClusterIP\n  port: 80\n", readme_url: "https://artifacthub.io/packages/helm/grafana/grafana" },
    "/api/helm/install": { ok: true, name: "grafana", detail: "grafana is being installed as grafana in lab by the Helm controller" },
    "/api/mqtt": (url, init) => init?.method === "POST" ? { ok: true } : {
      enabled: true, host: "192.0.2.177", port: 1883, tls: false, username: "", has_password: false, base: "harvester",
      discovery: "homeassistant", interval: 60, device_name: "Harvester Cluster", model: "Harvester",
      sensors: { cluster: 15, node: 9 },
      status: { state: "publishing", detail: "publishing to 192.0.2.177:1883 every 60s", last_publish: Math.floor(Date.now() / 1000) - 20, published: 3120, error: "" } },
    "/api/mqtt/test": { ok: true, detail: "192.0.2.177:1883 accepted the connection" },
    "/api/mqtt/preview": { states: [
      { topic: "harvester/cluster/state", payload: { nodes_ready: 3, nodes_total: 3, nodes_notready: 0, vol_total: 8, vol_degraded: 1, vol_faulted: 0,
        pods_system: 96, pods_workload: 12, pods_sys_bad: 0, pods_wl_bad: 0, vms_running: 1, health: "degraded", wl_summary: "lab:12", cpu_pct: 18.2, mem_pct: 41.7 } },
      { topic: "harvester/node/harvester_node1/state", payload: { cpu_pct: 21.3, mem_pct: 44.1, mem_gb: 27.6, rx_mbps: 12.4, tx_mbps: 3.1, pods: 41, vms: 1, wl: "home-assistant", status: "Ready" } }] },
    "/api/history/long": url => {
      const requested = new URL(url, location.origin).searchParams.get("range") || "24h";
      const spans = { "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400 };
      const range = Object.hasOwn(spans, requested) ? requested : "24h", span = spans[range];
      const step = range === "24h" ? 300 : 3600, points = span / step;
      const now = Math.floor(Date.now() / 1000 / step) * step;
      // Gentle trends across the selected window, rather than per-sample
      // sawtooth noise that overwhelms a phone-sized long-term chart. Anchor
      // values to timestamps so refreshing keeps previously shown buckets.
      const wave = (time, base, amp, cycles, offset = 0) => {
        const phase = time / span * cycles * 2 * Math.PI + offset;
        return +(base + amp * (Math.sin(phase) + 0.16 * Math.sin(phase * 0.37 + 0.8))).toFixed(2);
      };
      const t = Array.from({ length: points }, (_, i) => now - (points - 1 - i) * step);
      const cpu = t.map(time => wave(time, 18, 8, 3)), mem = t.map(time => wave(time, 42, 3, 1.5));
      return { range, step, t, samples: points, since: t[0], cpu, mem,
        rx: t.map(time => wave(time, 14, 9, 4)), tx: t.map(time => wave(time, 4, 2, 4, 0.4)), pods: t.map(() => 12),
        vol_bad: t.map((_, i) => (scenario !== "healthy" && i > points * 0.6 && i < points * 0.62 ? 1 : 0)), nodes_ready: t.map(() => 3), nodes_total: t.map(() => 3),
        cpu_max: Math.max(...cpu), mem_max: Math.max(...mem),
        nodes: [{ name: "harvester-node1", cpu: 21.4, mem: 44.1, availability: 100 }, { name: "harvester-node2", cpu: 17.9, mem: 39.8, availability: scenario === "healthy" ? 100 : 99.31 },
          { name: "harvester-node3", cpu: 12.2, mem: 35.0, availability: 100 }] };
    },
    // ?platform=k3s is a bare k3s; ?platform=kubevirt is k3s with KubeVirt but no CDI.
    "/api/platform": demoPlatform === "k3s" || demoPlatform === "kubevirt"
      ? { distribution: "k3s", version: "1.31.4+k3s1", harvester: false, longhorn: false, kubevirt: demoPlatform === "kubevirt", cdi: false,
          helm_controller: true, metrics: true, load_balancer: "servicelb", control_plane: ["192.0.2.50"], arch: ["amd64"] }
      : { distribution: "harvester", version: "1.31.4+rke2r1", harvester: true, longhorn: true, kubevirt: true, cdi: true, helm_controller: true,
          metrics: true, load_balancer: "kube-vip", control_plane: ["192.0.2.207", "192.0.2.208"], arch: ["amd64"] },
    "/api/addons": demoPlatform === "harvester" ? { harvester: true }
      : { distribution: "k3s", harvester: false, helm_controller: true,
          longhorn: { installed: false, installing: false },
          kubevirt: { installed: demoPlatform === "kubevirt", installing: false, cdi: false },
          multus: { installed: false, installing: false },
          kube_vip: { installed: false, installing: false, interface: "eth0", beside_servicelb: true },
          kvm: { "k3s-server-1": true, "k3s-agent-1": false }, kvm_known: true, kvm_everywhere: false, kvm_nowhere: false },
    "/api/addons/longhorn": { ok: true, name: "longhorn", job: "helm-install-longhorn", copies: 2,
      detail: "Longhorn is being installed, keeping 2 copies of each volume." },
    "/api/addons/kube-vip": { ok: true, name: "kube-vip", job: "helm-install-kube-vip", interface: "eth0", class_only: true,
      detail: "kube-vip is being installed, announcing on eth0. Add the addresses it may hand out under Networking > Your VIPs" },
    "/api/addons/multus": { ok: true, name: "multus", job: "helm-install-multus",
      detail: "Multus is being installed on every node; pods already running are left as they are. LAN networks can be made once it is up" },
    "/api/addons/kubevirt": { ok: true, name: "homestead-kubevirt", job: "helm-install-homestead-kubevirt",
      kubevirt: "v1.9.0", cdi: "v1.62.0", emulation: false, detail: "KubeVirt v1.9.0 and CDI v1.62.0 are being installed" },
    "/api/vm/create-options": demoPlatform === "harvester"
      ? { harvester: true, cdi: true, distribution: "harvester", default_class: "longhorn-r2",
          storage_classes: ["harvester-longhorn", "longhorn-r2", "longhorn-r3"],
          storage_class_facts: { "harvester-longhorn": { replicas: "3" }, "longhorn-r2": { replicas: "2", default: true }, "longhorn-r3": { replicas: "3" } },
          images: [{ namespace: "default", name: "image-ubuntu", display: "ubuntu-24.04-server-cloudimg-amd64.img", size_gb: 3.5, storage_class: "longhorn-image-ubuntu" }],
          store: [{ id: "ubuntu-24.04", distro: "Ubuntu", name: "Ubuntu 24.04 LTS", variant: "Server", user: "ubuntu", min_gb: 10, kept: true, ready: true },
            { id: "ubuntu-24.04-minimal", distro: "Ubuntu", name: "Ubuntu 24.04 LTS", variant: "Minimal", user: "ubuntu", min_gb: 10, kept: false, ready: false },
            { id: "fedora", distro: "Fedora", name: "Fedora Cloud", variant: "Base", user: "fedora", min_gb: 10, kept: false, ready: false }],
          network_details: [{ name: "default/vlan1", type: "bridge", vlan: 1, bridge: "mgmt-br", kind: "L2VlanNetwork", lan: true }],
          vm_network_options: { harvester: true, cluster_networks: ["mgmt"] },
          subnets: [{ cidr: "192.0.2.0/24", name: "LAN", gateway: "192.0.2.1", dhcp_start: "192.0.2.100", dhcp_end: "192.0.2.199",
            free: ["192.0.2.60", "192.0.2.61", "192.0.2.62", "192.0.2.63", "192.0.2.64", "192.0.2.65"] }],
          networks: ["pod", "default/vlan1", "default/vlan20-iot"], nodes: ["harvester-node1", "harvester-node2", "harvester-node3"],
          cpu_models: ["Cascadelake-Server", "Skylake-Client-IBRS", "Skylake-Server"], kubevirt_gates: ["VMPersistentState"],
          isos: [{ name: "iso-debian-13-1-0-amd64-netinst-3f2a9c1d", file: "debian-13.1.0-amd64-netinst.iso" }],
          hardware_base: { cpu: { sockets: 1, cores: 1, threads: 1, model: "", dedicated: false, isolate_emulator: false }, firmware: "bios", secure_boot: false, efi_persistent: false, tpm: "off", machine: "", hyperv: false, kvm_hidden: false, timezone: "", graphics: true, serial: true, tablet: false, rng: false, balloon: true, sound: false, hugepages: "", eviction: "LiveMigrate" } }
      : { harvester: false, cdi: false, distribution: "k3s", default_class: "local-path", storage_classes: ["local-path"],
          storage_class_facts: { "local-path": { default: true } }, images: [], networks: ["pod"], nodes: ["node-1"],
          network_details: [], hardware_base: { cpu: { sockets: 1, cores: 1, threads: 1, model: "", dedicated: false, isolate_emulator: false }, firmware: "bios", secure_boot: false, efi_persistent: false, tpm: "off", machine: "", hyperv: false, kvm_hidden: false, timezone: "", graphics: true, serial: true, tablet: false, rng: false, balloon: true, sound: false, hugepages: "", eviction: "" },
          vm_network_options: { harvester: false, cluster_networks: [], multus: true, macvtap: true,
            interfaces: [{ name: "eth0", kind: "nic", master: "", nodes: ["node-1"], everywhere: true },
              { name: "cni0", kind: "bridge", master: "", nodes: ["node-1"], everywhere: true }] } },
    "/api/vm/create/preview": (url, init) => {
      const body=JSON.parse(init.body), memory=parseFloat(body.memory)||2;
      return {config:{...body,mac:body.mac||"52:54:00:12:34:56"},capacity_token:"demo-vm-create-review",
        volumes:body.disk_import ? [] : [{name:`${body.name}-disk`,size:`${body.disk_gb||20}Gi`,access_mode:"ReadWriteMany",volume_mode:"Block",storage_class:body.storage_class||"longhorn"}],
        capacity:{blocked:false,requires_confirmation:true,additional:1,pod_request_gb:memory,pod_memory_gb:memory+0.25,pod_cpu_request_percent:20,
          vm:{action:"create",guest_memory_gb:memory,request_is_lower_bound:true},
          warnings:["Demo: storage provisioning and image importer overhead need live checks. Partial resources are retained if creation stops."],
          candidates:[{name:"harvester-node1",eligible:true,metrics_available:true,used_gb:8,capacity_gb:32,projected_gb:8+memory+0.25,
            projected_percent:Math.round((8+memory+0.25)/32*100),reservations_known:true,reserved_gb:6,request_slots:1}]}};
    },
    "/api/vm/create": { ok: true, vm: "demo", datavolume: "demo-disk" },
    "/api/vm/store": (() => {
      const now = Math.floor(Date.now() / 1000), H = demoPlatform === "harvester";
      const row = (id, distro, name, variant, about, user, mb, extra = {}) => ({ id, distro, name, variant, about, user, min_gb: 10,
        available: true, arch: "amd64", size: mb * 1024 * 1024, kept: false, auto: false, versions: [], ready: !H ? true : false, update: false, ...extra });
      const here = (image, days, extra = {}) => ({ image, added: now - days * 86400, ready: true, failed: false, progress: 100, display: image, ...extra });
      return { harvester: H, arch: "amd64", note: H ? "" : "CDI fills each VM's disk straight from the publisher, so a new VM always starts from the newest build and there is nothing to keep.",
        own: H ? [{ image: "default/image-win", display: "win2022-eval.qcow2", ready: true, failed: false, progress: 100, size_gb: 11.2, created: now - 86400 * 40, from: "nas.local" }] : [],
        images: [
          row("ubuntu-24.04", "Ubuntu", "Ubuntu 24.04 LTS", "Server", "The standard server image", "ubuntu", 597,
            H ? { kept: true, auto: true, ready: true, update: true, versions: [here("default/image-7f3a2c", 3)] } : {}),
          row("ubuntu-24.04-minimal", "Ubuntu", "Ubuntu 24.04 LTS", "Minimal", "Smaller: fewer packages and no manuals, for a server you set up yourself", "ubuntu", 252),
          row("ubuntu-22.04", "Ubuntu", "Ubuntu 22.04 LTS", "Server", "The standard server image", "ubuntu", 701),
          row("debian-13", "Debian", "Debian 13 (trixie)", "Cloud", "A kernel slimmed for VMs - the usual choice", "debian", 325,
            H ? { kept: true, auto: true, versions: [{ image: "default/image-91bd0e", added: now, ready: false, failed: false, progress: 42 }] } : {}),
          row("debian-13-generic", "Debian", "Debian 13 (trixie)", "Generic", "The full kernel with every driver - for hardware passed through to the VM", "debian", 413),
          row("fedora", "Fedora", "Fedora Cloud", "Base", "The newest Fedora release's cloud image", "fedora", 541),
          row("rocky-10", "Rocky Linux", "Rocky Linux 10", "Base", "The standard cloud image", "rocky", 520),
          row("rocky-10-lvm", "Rocky Linux", "Rocky Linux 10", "LVM", "Its disk under LVM, to grow or split later", "rocky", 519),
          row("alpine", "Alpine", "Alpine Linux", "Cloud-init", "Tiny - musl and OpenRC rather than glibc and systemd", "alpine", 148)] };
    })(),
    "/api/vm/store/keep": { ok: true, detail: "Fedora Cloud is downloading as a Harvester image; it keeps itself current" },
    "/api/vm/store/refresh": { ok: true, updated: ["Ubuntu 24.04 LTS"], tidied: [] },
    // The numbers from a real two-disk-heavy cluster: node1 is nearly full.
    "/api/longhorn/capacity": { node_down: "do-nothing", rebuild_limit: 5, over_provisioning: 100, minimal_available: 25, warn_pct: 80, crit_pct: 95,
      largest: { 1: 236.3, 2: 36.8, 3: 17.8 },
      nodes: [
        { name: "harvester-node1", size_gb: 116.8, allocated_gb: 99, limit_gb: 116.8, used_gb: 26.3, pct: 84.8, room_gb: 17.8, physical_room_gb: 61.3, level: "warn", blocked: "",
          disks: [{ id: "d1", size_gb: 116.8, allocated_gb: 99, limit_gb: 116.8, used_gb: 26.3, room_gb: 17.8, pct: 84.8, blocked: "" }] },
        { name: "harvester-node2", size_gb: 396.5, allocated_gb: 160.2, limit_gb: 396.5, used_gb: 14.8, pct: 40.4, room_gb: 236.3, physical_room_gb: 282.6, level: "ok", blocked: "",
          disks: [{ id: "d1", size_gb: 396.5, allocated_gb: 160.2, limit_gb: 396.5, used_gb: 14.8, room_gb: 236.3, pct: 40.4, blocked: "" }] },
        { name: "harvester-node3", size_gb: 116.8, allocated_gb: 80, limit_gb: 116.8, used_gb: 21.1, pct: 68.5, room_gb: 36.8, physical_room_gb: 66.5, level: "ok", blocked: "",
          disks: [{ id: "d1", size_gb: 116.8, allocated_gb: 80, limit_gb: 116.8, used_gb: 21.1, room_gb: 36.8, pct: 68.5, blocked: "" }] }],
      v2: { enabled: false, harvester_setting: false, ready_nodes: 0, total_nodes: 3,
        nodes: ["harvester-node1", "harvester-node2", "harvester-node3"].map(name => ({ name, ready: false, block_disks: 0, hugepages_mb: 0,
          missing: ["a V2 (block) disk", "2 GiB of hugepages (has 0 MiB)"] })) } },
    "/api/disks/v2/plan": (url,init) => {
      const body=JSON.parse(init?.body||'{}');
      return {node:body.node||'node-1',disk:body.disk||'disk-data',device:'/dev/sdb',size_bytes:1073741824000,
        request_id:'a'.repeat(24),capacity_token:'demo-evacuate',volumes:[{volume:'volume-media',size_bytes:107374182400,replicas:3,destination:'node-1 / spare-v1'}],
        blockers:window.__demoDiskV2State==='blocked'?['No other eligible V1 disk has room. Add a spare V1 disk on this host or another eligible V1 host.']:[]};
    },
    "/api/disks/v2/start": () => {window.__demoDiskV2State='evacuating';return responses['/api/disks/v2/status']();},
    "/api/disks/v2/status": () => {
      const phase=window.__demoDiskV2State||'awaiting-erase',done=phase==='complete',failed=phase==='failed';
      return {id:'demo-disk-v2',kind:'disk-v2-convert',title:'Prepare /dev/sdb for V2',node:'node-1',disk:'disk-data',device:'/dev/sdb',
        phase,status:done?'succeeded':failed?'failed':'running',progress:done?100:phase==='evacuating'?35:phase==='awaiting-erase'?70:80,
        message:done?'The V2 block disk is ready.':failed?'Preparation helper failed. Inspect the saved log.':phase==='awaiting-erase'?'Replicas are healthy elsewhere. Review the device erase to continue.':phase==='evacuating'?'Longhorn is moving replicas to other V1 disks.':'Preparing the evacuated device.',
        remaining:phase==='evacuating'?1:0,needs_erase_review:phase==='awaiting-erase',cancellable:['evacuating','awaiting-erase'].includes(phase),dismissible:done,
        volumes:[{volume:'volume-media',size_bytes:107374182400,replicas:3,destination:'node-1 / spare-v1'}]};
    },
    "/api/disks/v2/prepare-review": {operation_id:'demo-disk-v2',node:'node-1',device:'/dev/sdb',size_bytes:1073741824000,request_id:'b'.repeat(24),capacity_token:'demo-prepare'},
    "/api/disks/v2/prepare": () => {window.__demoDiskV2State='preparing';return responses['/api/disks/v2/status']();},
    "/api/longhorn/v2/plan": () => {
      const state=window.__demoV2State || 'missing', harvester=state==='harvester';
      const configured=['reboot','ready','enabled','complete'].includes(state), capacity=['ready','enabled','complete'].includes(state)?2048:0;
      const nodes=['k3s-test','k3s-server-2','k3s-server-3'].map((node,i)=>({node, required_mib:2048,target_pages:1024,
        review_token:'host-review-'+i,capacity_mib:capacity,allocatable_mib:capacity,configured,
        can_prepare:!harvester&&!configured&&state!=='running',needs_reboot:state==='reboot',engine_ready:state==='complete',
        problems:configured&&capacity?[]:[capacity?'':'Kubernetes reports 0 MiB capacity; V2 needs 2048 MiB',configured?'':'Run host preparation to verify tools and persistent configuration'].filter(Boolean),
        job:state==='missing'||harvester?null:{name:'homestead-v2-demo-'+i,state:state==='running'?'running':state==='failed'?'failed':'succeeded'}}));
      return {namespace:'lab',enabled:['enabled','complete'].includes(state),harvester,harvester_requested:false,
        distribution:harvester?'harvester':'k3s',required_mib:2048,nodes,review_token:'enable-review',
        blockers:harvester?[]:nodes.flatMap(n=>n.problems.map(p=>n.node+': '+p)),can_enable:harvester||state==='ready',engine_ready:state==='complete'};
    },
    "/api/longhorn/v2/prepare": () => {window.__demoV2State='running';return {operation:{id:'demo-v2',kind:'longhorn-v2-prepare',title:'Prepare Longhorn V2',status:'running',progress:25,message:'Preparing host prerequisites'}};},
    "/api/longhorn/v2/enable": () => {window.__demoV2State='enabled';return {ok:true};},
    "/api/longhorn/settings": { ok: true, detail: "Saved: over-provisioning 150%" },
    "/api/longhorn/offline-rebuilding": { supported: true, enabled: false, short: [{ name: "pvc-demo-nextcloud", claim: "lab/nextcloud-data", whole: 1, wanted: 2, hosts: ["node-2"], offline: "ignored" }] },
    "/api/workloads/rebalance/plan": url => {
      const exclude = (url.searchParams.get("exclude") || "").split(",").filter(Boolean);
      const all = [{ ns: "lab", name: "frigate", id: "lab/frigate", from: "harvester-node2", to: "harvester-node1", cpu_m: 1400, mem_gb: 1.8, near: true },
        { ns: "lab", name: "home-assistant", id: "lab/home-assistant", from: "harvester-node2", to: "harvester-node3", cpu_m: 310, mem_gb: 0.74, near: false }];
      const moves = all.filter(m => !exclude.includes(m.id)), off = id => exclude.includes(id);
      return { moves, apps: all.map(m => m.id), excluded: exclude, metrics: true, review_token: "demo-crebalance-" + exclude.join("."),
        hosts: [{ name: "harvester-node1", takes: true, cpu_before: 22, cpu_after: off("lab/frigate") ? 22 : 40, mem_before: 44, mem_after: off("lab/frigate") ? 44 : 55 },
          { name: "harvester-node2", takes: true, cpu_before: 78, cpu_after: 78 - (off("lab/frigate") ? 0 : 18) - (off("lab/home-assistant") ? 0 : 4), mem_before: 81, mem_after: 81 - (off("lab/frigate") ? 0 : 11) - (off("lab/home-assistant") ? 0 : 5) },
          { name: "harvester-node3", takes: true, cpu_before: 16, cpu_after: off("lab/home-assistant") ? 16 : 20, mem_before: 35, mem_after: off("lab/home-assistant") ? 35 : 40 }],
        skipped: [{ id: "lab/plex", why: "it is pinned to its host" }, { id: "lab/immich", why: "it runs more than one copy, which the scheduler spreads itself" }] };
    },
    "/api/workloads/rebalance": { operation: { id: "demo-crebalance", kind: "container-rebalance", title: "Rebalance 2 containers", status: "running", progress: 0, message: "Starting with the first container", cancellable: true } },
    "/api/longhorn/rebalance/plan": url => {
      const exclude = (url.searchParams.get("exclude") || "").split(",").filter(Boolean);
      const all = [{ volume: "pvc-demo-frigate", claim: "lab/frigate-recordings", app: "lab/frigate", from: "harvester-node2", to: "harvester-node1", size_gb: 120, attached: true },
        { volume: "pvc-demo-media", claim: "lab/jellyfin-media", app: "lab/jellyfin", from: "harvester-node3", to: "harvester-node1", size_gb: 64, attached: true },
        { volume: "pvc-demo-nextcloud", claim: "lab/nextcloud-data", app: "lab/nextcloud", from: "harvester-node2", to: "harvester-node1", size_gb: 38, attached: false }];
      const moves = all.filter(m => !exclude.includes(m.app)), moved = n => moves.filter(m => m.from === n).reduce((s, m) => s + m.size_gb, 0);
      return { moves, apps: all.map(m => m.app), excluded: exclude, review_token: "demo-rebalance-" + exclude.join("."),
        hosts: [{ name: "harvester-node1", before_gb: 40, after_gb: 40 + moves.reduce((s, m) => s + m.size_gb, 0), capacity_gb: 900, takes: true },
          { name: "harvester-node2", before_gb: 410, after_gb: 410 - moved("harvester-node2"), capacity_gb: 900, takes: true },
          { name: "harvester-node3", before_gb: 380, after_gb: 380 - moved("harvester-node3"), capacity_gb: 900, takes: true }],
        skipped: [{ claim: "lab/nas-backup", why: "its copies are being rebuilt or changed" }] };
    },
    "/api/longhorn/rebalance": { operation: { id: "demo-rebalance", kind: "volume-rebalance", title: "Rebalance 3 volume copies", status: "running", progress: 0, message: "Starting with the first copy", cancellable: true } },
    "/api/longhorn/rebuild": { ok: true, detail: "Longhorn is rebuilding lab/nextcloud-data while it is detached" },
    "/api/disks": { harvester: true, nodes: demoDisks, disk_tags: ["hdd", "nvme", "ssd"], all_node_tags: ["rack-a"],
      node_tags: { "harvester-node1": ["rack-a"], "harvester-node2": [], "harvester-node3": ["rack-a"] } },
    "/api/disks/tags": (url, init) => ({ ok: true, detail: `tagged ${JSON.parse(init?.body || "{}").tags.join(", ")}` }),
    "/api/disks/node-tags": (url, init) => ({ ok: true, detail: `tagged ${JSON.parse(init?.body || "{}").tags.join(", ")}` }),
    "/api/disks/inspect": (url, init) => {
      const device = JSON.parse(init.body).device;
      return { device, size_gb: 931.5, partitions: [], mounts: [], fstype: device.endsWith("c") ? "" : "ext4", uuid: "",
        by_id: "/dev/disk/by-id/ata-WDC_WD10EZEX-00BN5A0_WD-WCC3F0123456", longhorn: device.endsWith("c") ? null : '{"diskName":"disk-1","diskUUID":"8f1c"}',
        replicas: device.endsWith("c") ? 0 : 3, entries: 2, tools: ["mkfs.ext4", "mkfs.xfs", "wipefs", "chattr", "findmnt"],
        error: "", system: false, state: device.endsWith("c") ? "blank" : "longhorn", mount_point: `/mnt/${device.split("/").pop()}`,
        choices: device.endsWith("c") ? ["format"] : ["import", "erase"] };
    },
    "/api/disks/setup": { ok: true, path: "/mnt/sdc", tags: ["hdd"], detail: "/dev/sdc is formatted and mounted at /mnt/sdc; Longhorn is adding it on harvester-node3, tagged hdd" },
    "/api/disks/os-space": { root: "/dev/mapper/ubuntu--vg-ubuntu--lv", root_free_gb: 71.2, vg: "ubuntu-vg", size_gb: 235.4, free_gb: 135.4,
      lvs: { "ubuntu-lv": 100 }, pvs: ["/dev/sda3"], tools: ["lvcreate", "mkfs.ext4", "chattr", "findmnt"], lvm_tools: true,
      reserve_gb: 24, usable_gb: 111, exists: false, taken: { v1: false, v2: false }, mount_point: "/mnt/longhorn-os",
      regions: [{ disk: "sdb", start: 419432448, sectors: 1534019584, sector: 512, size_gb: 731 }],
      lvm_problems: { v1: "", v2: "" }, problem: "" },
    "/api/disks/os-space/use": { ok: true, path: "/mnt/longhorn-os", tags: ["os"], detail: "A 111 GB volume in ubuntu-vg is mounted at /mnt/longhorn-os; Longhorn is adding it on node-1, tagged os" },
    "/api/disks/add": { ok: true, detail: "Harvester is wiping and adding /dev/sdb on harvester-node1 to Longhorn" },
    "/api/disks/scheduling": { ok: true, detail: "done" }, "/api/disks/evict": { ok: true, detail: "moving replicas off" },
    "/api/disks/remove": { ok: true, detail: "released" },
    "/api/vm/k3s-cluster/plan": (url, init) => {
      const cfg = JSON.parse(init.body || "{}"), count = (+cfg.servers || 1) + (+cfg.agents || 0);
      const memory = (parseFloat(cfg.memory) || 4) / (String(cfg.memory).endsWith("Mi") ? 1024 : 1);
      const upper = 8 + count * (memory + Math.max(0.25, memory * 0.05));
      const nodes = Array.from({length:count}, (_,i) => ({name:`${cfg.name}-${i < cfg.servers ? "server" : "agent"}-${i < cfg.servers ? i+1 : i-cfg.servers+1}`,
        role:i < cfg.servers ? "server" : "agent", address:cfg.addresses?.[i] || `192.0.2.${20+i}`, problem:""}));
      return {name:cfg.name, setup:cfg.setup, first:nodes[0].address, url:`http://${nodes[0].address}:8088`, ok:true, nodes,
        config:{...cfg,review_id:"demo-review",macs:Object.fromEntries(nodes.map((n,i)=>[n.name,`52:54:00:11:22:${String(i+10).padStart(2,"0")}`]))}, capacity_token:"demo-batch",
        capacity:{status:"fits",blocked:false,vm_count:count,warnings:["Snapshot only: placement is not reserved. Partial VMs, disks and Secrets are retained after failure.","Guest quorum does not guarantee independent physical hosts or storage."],
          nodes:[{name:"harvester-node1",baseline_gb:8,upper_gb:Math.round(upper*100)/100,upper_percent:Math.round(upper/32*100),metrics_available:true}],
          example:nodes.map(n=>({service:n.name,host:"harvester-node1"})),blockers:[],reasons:[]}};
    },
    "/api/vm/k3s-cluster": { ok: true, operation: { id: "op-k3s" } },
    "/api/workloads/failover": { ok: true, changed: ["paperless"], detail: "1 container changed and restarting" },
    "/api/disks/retire/plan": { node: "harvester-node3", disk: "bd-node3-sdb", path: "/var/lib/harvester/extra-disks/7f2c",
      problem: "failed to get disk config", harvester_device: { name: "bd-node3-sdb", path: "/dev/sdb", state: "Inactive" }, only_copies: 1,
      volumes: [
        { replica: "r1", volume: "pvc-scratch", claim: "lab/scratch-test", copies: 1, healthy_elsewhere: 0, outcome: "only-copy",
          why: "its only copy was on this disk: reconnect the drive, or restore it from a backup" },
        { replica: "r2", volume: "pvc-demo-frigate", claim: "lab/frigate-config", copies: 3, healthy_elsewhere: 2, outcome: "waits",
          why: "every other node already has a copy: it rebuilds on harvester-node3 once a new disk is added there" },
        { replica: "r3", volume: "pvc-demo-rebuild", claim: "lab/jellyfin-config", copies: 2, healthy_elsewhere: 1, outcome: "elsewhere",
          why: "rebuilds on harvester-node2 from its healthy copy" }] },
    "/api/disks/retire": { ok: true, operation: { id: "op-retire" } },
    "/api/platform/join": { distribution: "k3s", server: "192.0.2.50", version: "1.31.4+k3s1", token_file: "/var/lib/rancher/k3s/server/node-token",
      agent: 'curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="v1.31.4+k3s1" K3S_URL=https://192.0.2.50:6443 K3S_TOKEN=<token> sh -',
      server_join: 'curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="v1.31.4+k3s1" K3S_TOKEN=<token> sh -s - server --server https://192.0.2.50:6443',
      longhorn: "sudo apt-get install -y open-iscsi nfs-common   # or: sudo dnf install -y iscsi-initiator-utils nfs-utils" },
    "/api/resources/kinds": [
      ["Workloads", "", "v1", "pods", "Pod", true], ["Workloads", "apps", "v1", "deployments", "Deployment", true],
      ["Workloads", "apps", "v1", "statefulsets", "StatefulSet", true], ["Workloads", "apps", "v1", "daemonsets", "DaemonSet", true],
      ["Workloads", "batch", "v1", "jobs", "Job", true], ["Workloads", "batch", "v1", "cronjobs", "CronJob", true],
      ["Network", "", "v1", "services", "Service", true], ["Network", "networking.k8s.io", "v1", "ingresses", "Ingress", true],
      ["Storage", "", "v1", "persistentvolumeclaims", "PersistentVolumeClaim", true], ["Storage", "storage.k8s.io", "v1", "storageclasses", "StorageClass", false],
      ["Configuration", "", "v1", "configmaps", "ConfigMap", true], ["Configuration", "", "v1", "secrets", "Secret", true],
      ["Access", "", "v1", "serviceaccounts", "ServiceAccount", true], ["Access", "rbac.authorization.k8s.io", "v1", "clusterroles", "ClusterRole", false],
      ["Cluster", "", "v1", "nodes", "Node", false], ["Cluster", "", "v1", "namespaces", "Namespace", false],
      ["Cluster", "apiextensions.k8s.io", "v1", "customresourcedefinitions", "CustomResourceDefinition", false],
      ["Custom resources", "longhorn.io", "v1beta2", "volumes", "Volume", true], ["Custom resources", "kubevirt.io", "v1", "virtualmachines", "VirtualMachine", true],
      ["Custom resources", "harvesterhci.io", "v1beta1", "settings", "Setting", false],
    ].map(([category, group, version, resource, kind, namespaced]) => ({ category, group, version, resource, kind, namespaced, verbs: ["get", "list"], short: [] })),
    "/api/resources/list": url => {
      const resource = url.searchParams.get("resource");
      if (resource === "namespaces") return { columns: [{ name: "Name" }], rows: ["default", "lab", "longhorn-system", "harvester-system"].map(name => ({ name, namespace: "", cells: [name] })) };
      if (resource === "pods") return { columns: [{ name: "Name", description: "" }, { name: "Ready", description: "" }, { name: "Status", description: "" }, { name: "Restarts", description: "" }, { name: "Age", description: "" }],
        rows: [["frigate-7d8f6d4c9-demo", "1/1", "Running", 0, "5d"], ["home-assistant-6b9c-demo", "2/2", "Running", 1, "10d"], ["paperless-5c8d-demo", "1/1", "Running", 0, "2d"]]
          .map(([name, ...rest]) => ({ name, namespace: "lab", cells: [name, ...rest] })) };
      return { columns: [{ name: "Name", description: "" }, { name: "Age", description: "" }], rows: [{ name: "example", namespace: "lab", cells: ["example", "3d"] }] };
    },
    "/api/resources/object": url => ({ secret_hidden: false, object: { metadata: { uid: "u1" } },
      yaml: `apiVersion: v1\nkind: Pod\nmetadata:\n  name: ${url.searchParams.get("name")}\n  namespace: lab\n  labels:\n    app: frigate\n  resourceVersion: "48121"\nspec:\n  containers:\n  - name: frigate\n    image: ghcr.io/blakeblackshear/frigate:stable\n    ports:\n    - containerPort: 5000\nstatus:\n  phase: Running\n` }),
    "/api/resources/events": [{ type: "Normal", reason: "Pulled", message: "Container image already present on machine", count: 1, last: new Date().toISOString() }],
    "/api/ipam/import": { ok: true, created: 2, updated: 1, detail: "2 addresses added, 1 updated" },
    "/api/ipam/record": { ok: true },
    "/api/ipam/bulk": { ok: true, detail: "updated" },
    "/api/ipam/scan": { ok: true, detail: "scanning 254 addresses in 192.0.2.0/24" },
    "/api/ipam/unifi/sync": { ok: true, detail: "11 addresses from UniFi, 2 reserved" },
    "/api/self/replicas": (url, init) => init?.method === "POST"
      ? { ok: true, desired: JSON.parse(init.body || "{}").replicas, detail: "Homestead runs as 2 copies, spread over different nodes" }
      : { desired: 1, max: 3, leader: "homestead-6f9c-a1", spread_nodes: 1, pods: [
        { name: "homestead-6f9c-a1", node: "harvester-node1", ready: true, leader: true, this: true, terminating: false }],
        data: { pvc: "homestead-data", storage_class: "longhorn-r2", access_modes: ["ReadWriteMany"], size: "2Gi", shareable: false,
          reason: "homestead-data is on longhorn-r2, a migratable class: Longhorn gives it a VM-disk volume that only one node can mount, so a copy on a second node would never start",
          candidates: ["longhorn"], classes: [{ name: "longhorn", shareable: true }] } },
    "/api/self/data/prepare": (url, init) => {
      if (init?.method === "POST") {
        window.__demoDataPrepared = true;
        return { operation: { id: "demo-data-prepare", title: "Prepare Homestead data volume", kind: "self-data-prepare", status: "succeeded", progress: 100, href: "/settings" }, destination: "homestead-data-prepared" };
      }
      let blocker;
      if (window.__demoDataBatchRecovery) {
        window.__demoOps ||= [];
        blocker = window.__demoOps.find(op => op.id === "data-batch-recovery");
        if (!blocker) {
          blocker = {id:"data-batch-recovery",title:"k3s cluster k3s-demo",kind:"k3s-cluster",batch_name:"k3s-demo",
            status:"failed",progress:10,mutation_recovery:true,recovery:true,dismissible:false,href:"/vms?find=k3s-demo",
            resource:{namespace:"lab",name:"k3s-demo-server-1"},message:"Guest verification timed out (0/1 ready). Resources are retained; inspect the batch outcome."};
          window.__demoOps.push(blocker);
        }
      }
      return { source: "homestead-data", classes: [{ name: "longhorn", shareable: true }], execution_ready: false,
        blocking_jobs: blocker?.mutation_recovery ? [blocker] : [],
        nodes: [{ name: "harvester-node1", ready: true }, { name: "harvester-node2", ready: true }],
        preparations: window.__demoDataPrepared ? [{ id: "demo-data-prepare", operation: "a".repeat(24), destination: "homestead-data-prepared", node: "harvester-node1", status: "succeeded", progress: 100, prepared: true, archivable: true,
          message: "Destination prepared. Review the move when you are ready for downtime." }] : [] };
    },
    "/api/self/data/prepare/archive/preview": (url, init) => ({ id: JSON.parse(init.body).id,
      destination: "homestead-data-prepared", source: "homestead-data", capacity_token: "demo-only",
      detail: "Hides this completed preparation from Move data and Jobs. Both volumes are retained; no data is copied or deleted." }),
    "/api/self/data/prepare/archive": () => { window.__demoDataPrepared = false; return {ok: true}; },
    "/api/self/data/prepare/preview": { size: "2Gi", storage_class: "longhorn", capacity_token: "demo-only", capacity: {
      blocked: false, warnings: ["Storage capacity is not reserved until provisioning completes."], candidates: [] } },
    "/api/self/data/move/preview": { capacity_token: "demo-only", downtime: "Homestead will be unavailable while data is copied and checked. Both volumes are retained.",
      stages: ["Start move coordinator", "Copy and verify", "Restart Homestead"].map((label, i) => ({ label, detail: i ? "Conditional on stopping Homestead first. Capacity is checked again before acting." : "Starts alongside Homestead.", capacity: { blocked: false, warnings: [], candidates: [] } })) },
    "/api/self/data/move": { ok: true, detail: "copying homestead-data to homestead-data-shared on longhorn; Homestead restarts onto it when done" },
    // App links carry the app's Answering state, as the server's portal_status gives it.
    "/api/portal/status": () => Object.fromEntries(portalLinks.map((link, i) => [link.id,
      link.icon === "workload:lab/home-assistant" ? { up: true, ms: 2840, state: "slow", source: "monitoring", error: "", code: 200 }
        : link.icon === "workload:lab/frigate" ? { up: true, ms: 38, state: "up", source: "monitoring", error: "", code: 200 }
        : i === 3 ? { up: false, ms: null } : { up: true, ms: 3 + i }])),
    "/api/portal/candidates": [
      { title: "frigate", ns: "lab", name: "frigate", url: "http://192.0.2.214:5000", port: 5000, port_name: "http", icon: "workload:lab/frigate", has_logo: false, group: "Home" },
      { title: "home-assistant", ns: "lab", name: "home-assistant", url: "http://192.0.2.215:8123", port: 8123, port_name: "", icon: "workload:lab/home-assistant", has_logo: false, group: "Home" },
      { title: "paperless", ns: "lab", name: "paperless", url: "http://192.0.2.216:8000", port: 8000, port_name: "", icon: "workload:lab/paperless", has_logo: false, group: "" }],
    // Answering (uptime.js): frigate healthy, home-assistant slow this hour,
    // paperless down since a quarter of an hour ago, the last one not checked.
    "/api/uptime": () => {
      const now = Date.now() / 1000, day = (bad = []) => Array.from({ length: 24 }, (_, i) => bad.includes(i) ? "down" : i < 3 ? null : "up");
      const apps = {};
      workloads.forEach((w, i) => {
        const key = `${w.ns}/${w.name}`, ip = (w.ports || [])[0]?.ip || "192.0.2.10", port = (w.ports || [])[0]?.port || 80;
        const target = `http://${ip}:${port}/`;
        apps[key] = [
          { state: "up", since: now - 86400 * 4, target, last: { ok: true, ms: 38, code: 200, error: "" }, strip: day(), uptime_24h: 100, uptime_30d: 99.98 },
          { state: "slow", since: now - 900, target, last: { ok: true, ms: 2840, code: 200, error: "" }, strip: [...day([9]).slice(0, 23), "slow"], uptime_24h: 99.4, uptime_30d: 99.71 },
          { state: "down", since: now - 960, target, last: { ok: false, ms: null, code: 502, error: "HTTP 502" }, strip: [...day([14]).slice(0, 23), "down"], uptime_24h: 96.5, uptime_30d: 99.2 },
        ][i] || { state: "off", why: "checks are off for this app", target: "", last: {}, strip: [], uptime_24h: null, uptime_30d: null };
      });
      const month = (bad = [], dips = []) => Array.from({ length: 30 }, (_, i) => i < 4 ? null : bad.includes(i) ? "down" : dips.includes(i) ? "dip" : "up");
      Object.values(apps).forEach((a, i) => { a.days = a.state === "off" ? [] : month(i === 2 ? [29] : [], i === 1 ? [12, 25] : i === 2 ? [18] : []); });
      return { apps, every: 60, down_after: 3, slow_ms: 2000 };
    },
    "/api/image-updates/mode": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const w = workloads.find(x => x.ns === body.ns && x.name === body.name);
      if (w) w.update_mode = body.mode;
      return { ok: true, mode: body.mode, detail: body.mode === "auto" ? `${body.name} updates itself in the maintenance window` : `${body.name} waits for you to update it` };
    },
    "/api/uptime/setting": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, detail: `${body.name} is ${({ auto: "checked automatically", off: "not checked", tcp: "checked by TCP connection" })[body.mode] || `checked at ${body.path || "/"}`}` };
    },
    // Logos (logos.js): a small catalogue drawn from the demo's own apps.
    // Most match their image; paperless matches by name only; the last has none.
    "/api/logos": url => {
      const w = workloads.find(x => x.ns === url.searchParams.get("ns") && x.name === url.searchParams.get("name")) || {};
      const q = (url.searchParams.get("q") ?? w.name ?? "").toLowerCase();
      const own = demoLogoCatalogue().filter(t => t.repo === (w.images || [])[0]).map(t => ({ ...t, match: "image" }));
      const named = demoLogoCatalogue().filter(t => q && t.name.toLowerCase().includes(q) && !own.some(o => o.icon === t.icon)).map(t => ({ ...t, match: "name" }));
      return { tiles: [...own, ...named], images: w.images || [] };
    },
    "/api/logos/missing": () => ({ apps: workloads.filter(w => !w.icon && !w.logo_skipped && !w.platform && !w.self && !w.homestead && !w.managed_smb && !w.managed_nfs)
      .map((w, i, rows) => {
        const t = demoLogoCatalogue().find(c => c.repo === w.images[0]);
        const suggestion = i === rows.length - 1 ? null : w.name === "paperless" ? { ...t, name: "Paperless-ngx", match: "name" } : t ? { ...t, match: "image" } : null;
        return { ns: w.ns, name: w.name, images: w.images, suggestion };
      }).sort((a, b) => ({ image: 0, name: 1 }[a.suggestion?.match] ?? 2) - ({ image: 0, name: 1 }[b.suggestion?.match] ?? 2)) }),
    "/api/vms/logo": (url, init) => {
      const items = JSON.parse(init?.body || "{}").items || [];
      for (const item of items) {
        const v = demoVms.find(x => x.ns === item.ns && x.name === item.name);
        if (!v) continue;
        v.icon = item.icon || "";
        v.has_logo = !!item.icon;
        v.logo_os_set = !!item.os;
        if (item.os) v.os_logo = item.os;
      }
      return { ok: true, done: items.length, failed: [], detail: items.some(i => i.icon || i.os) ? "1 logo saved" : "back to its OS's logo" };
    },
    "/api/workloads/logo": (url, init) => {
      const items = JSON.parse(init?.body || "{}").items || [];
      for (const item of items) {
        const w = workloads.find(x => x.ns === item.ns && x.name === item.name);
        if (!w) continue;
        if (item.skip) w.logo_skipped = true; else w.icon = item.icon || "";
      }
      const saved = items.filter(i => i.icon && !i.skip).length, skipped = items.filter(i => i.skip).length;
      return { ok: true, done: items.length, failed: [], detail: [saved && `${saved} logo${saved === 1 ? "" : "s"} saved`, skipped && `${skipped} left without one`].filter(Boolean).join(", ") || "logo removed" };
    },
    "/api/workloads/group": (url, init) => {
      const body = JSON.parse(init?.body || "{}"), group = String(body.group || "").trim();
      return { ok: true, group, detail: `${(body.items || []).length} moved` };
    },
    "/api/move/preview": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { capacity_token: "demo-host-move", capacity: {
        blocked: false, requires_confirmation: true, additional: 1, candidates: [],
        pod_request_gb: 0.5, pod_memory_gb: 1, pod_cpu_request_percent: 10,
        warnings: ["Demo snapshot: all containers stop before replacements start. Placement is not reserved."],
        rollout: { strategy: "Recreate", replicas: 1, ownership_known: true, owned_pods: [], release_request_gb: 0.5, max_surge: 0, max_unavailable: 1 },
        move: { node: body.node || null, mode: body.node ? body.pin ? "pinned" : "preferred" : "unpinned", target: null }
      } };
    },
    "/api/move": { ok: true },
    "/api/edit/preview": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const rename = body.workload_name && body.workload_name !== body.name;
      return { capacity_token: "demo-review", capacity: {
      blocked: false, requires_confirmation: true, additional: 1,
      candidates: [
        { name: "harvester-node2", eligible: true, reasons: [], used_gb: 8.4, capacity_gb: 15.6, projected_gb: 10.9, projected_percent: 70,
          metrics_available: true, warnings: [], reservations_known: true, reserved_gb: 6.1, allocatable_gb: 15.1, reserved_cpu_percent: 31,
          request_slots: 2, projected_pods: 1 },
        { name: "harvester-node1", eligible: true, reasons: [], used_gb: 12.6, capacity_gb: 15.6, projected_gb: 12.6, projected_percent: 81,
          metrics_available: true, warnings: [], reservations_known: true, reserved_gb: 9.8, allocatable_gb: 15.1, reserved_cpu_percent: 42,
          request_slots: 1, projected_pods: 0 },
        { name: "harvester-node3", eligible: false, reasons: ["does not have the Google Coral USB this workload asks for"] },
      ],
      pod_request_gb: 0.5, pod_memory_gb: 1, pod_cpu_request_percent: 10,
      ...(rename ? { rename: { from: body.name, to: body.workload_name } } : {}),
      ...((body.containers || []).some(c => (c.volumes || []).some(v => v.copy_from)) ? {
        copy_helper: { blocked: false, requires_confirmation: true, additional: 1, candidates: [],
          pod_request_gb: 0.0625, pod_memory_gb: 0.25, pod_cpu_request_percent: 10, warnings: ["Demo placement snapshot; rechecked before copying."] }
      } : {}),
      warnings: [rename ? "Old pods stop before the replacement starts. A failed or uncertain step keeps resources for inspection, without automatic rollback." : "Editing restarts all containers in the pod. This is a demo capacity snapshot."],
      rollout: { strategy: "Recreate", replicas: 1, ownership_known: true, owned_pods: [], release_request_gb: 0.5, max_surge: 0, max_unavailable: 1 }
    } }; },
    "/api/edit": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, name: body.workload_name || body.name, renamed: body.workload_name && body.workload_name !== body.name,
        renamed_from: body.name };
    },
    "/api/network/plan": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const vip = body.type === "ClusterIP" ? "" : body.vip_mode === "shared" ? "192.0.2.242" : body.vip || "192.0.2.217";
      return { ready: true, namespace: body.namespace, name: body.name, workload: body.workload,
        type: body.type, vip_mode: body.vip_mode, vip, ports: (body.ports || []).map((port, index) => ({
          name: `port-${index + 1}`, port: +port.port, targetPort: +port.target_port, protocol: port.protocol })),
        warnings: [], path: { vip: vip || "cluster only", service: `${body.namespace}/${body.name}`,
          workload: `Deployment/${body.workload}`, endpoints: 1 }, available_vips: network.available_vips };
    },
    "/api/network/services": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      return { ok: true, name: body.name, namespace: body.namespace,
        message: `Service ${body.namespace}/${body.name} created` };
    },
    // Paced so the progress readout is visible rather than a flash.
    "/api/image-updates/preview": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const review = {capacity_token: "demo-image-review", action: body.action || "update",
        // As the server sends them: images pinned to their digests, which
        // carry no tag, and the releases Homestead tracks for them beside.
        images: [{container: body.name, before: "ghcr.io/example/app@sha256:" + "a".repeat(64),
          after: "ghcr.io/example/app@sha256:" + "b".repeat(64), before_tag: "1.0.0", after_tag: "1.1.0",
          rollback: "ghcr.io/example/app@sha256:" + "a".repeat(64)}],
        capacity: {blocked: false, requires_confirmation: true, additional: 1,
          pod_request_gb: 0.5, pod_memory_gb: 2, pod_cpu_request_percent: 10,
          candidates: [{name: "h-node1", eligible: true, metrics_available: true,
            used_gb: 12, projected_gb: 14, capacity_gb: 16, projected_percent: 87.5,
            reservations_known: true, reserved_gb: 5, request_slots: 4}],
          warnings: ["Recreate stops the old pod before its replacement starts.", "Projected RAM is above the configured warning level."],
          rollout: {strategy: "Recreate", replicas: 1, ownership_known: true, owned_pods: ["app-old"], release_request_gb: 0.5, max_surge: 0, max_unavailable: 1}}};
      if (window.__demoImageReviewCordoned) {
        window.__demoImageReviewCordoned = false;
        review.capacity.blocked = true;
        review.capacity.candidates = [{...review.capacity.candidates[0], name:"node-1", eligible:false, reasons:["cordoned"]}];
        review.capacity.warnings = ["no ready host satisfies this workload's placement requirements",
          "no scheduling order fits all requested replicas under the observed pod affinity and topology spread rules",
          "updated pod estimates include every container, not only the added container",
          "post-stop capacity assumes old pods have fully terminated and released ports and volumes; termination and storage detach are not guaranteed",
          "memory is not limited for data-permissions"];
      }
      return review;
    },
    "/api/image-updates/apply": {uid: "demo-rollout", generation: 4, phase: "progressing", desired: 1, ready: 0},
    "/api/image-updates/rollback": {uid: "demo-rollout", generation: 4, phase: "progressing", desired: 1, ready: 0},
    // An administrator marking an app as updated elsewhere, or clearing it.
    "/api/image-updates/managed": (url, init) => {
      const body = JSON.parse(init?.body || "{}"), by = String(body.by || "").trim();
      const row = (responses["/api/image-updates"].workloads || []).find(w => w.ns === body.ns && w.name === body.name);
      if (row) row.managed = by ? {by, source: "", detected: false} : {};
      return {ok: true, ns: body.ns, name: body.name, managed: row?.managed || {}};
    },
    "/api/image-updates/scan-progress": () => {
      const at = (window.__demoScan = (window.__demoScan || 0) + 1);
      const total = 8, done = Math.min(total, at * 2);
      return { running: done < total, done, total, updates: Math.floor(done / 3),
        current: ["frigate", "home-assistant", "paperless", "homestead-smb"][at % 4],
        started_at: 0, finished_at: 0, elapsed: at * 0.5 };
    },
    "/api/image-updates": { checked_at: new Date().toISOString(), updates: 3, errors: 1, homestead: { updates: 1, errors: 0 },
      policy: { policy: "approval_required", allows_install: true, reason: "Explicit operator approval is required before rollout." },
      workloads: [{ ns: "lab", name: "frigate", available: true, can_rollback: true,
        images: [{ container: "frigate", deployed: "ghcr.io/blakeblackshear/frigate:stable", candidate: "ghcr.io/blakeblackshear/frigate:stable", candidate_tag: "stable", remote_digest: "sha256:abc", available: true }] },
      { ns: "lab", name: "home-assistant", available: true, can_rollback: false,
        images: [{ container: "home-assistant", deployed: "ghcr.io/home-assistant/home-assistant:2026.8", candidate: "ghcr.io/home-assistant/home-assistant:2026.9", candidate_tag: "2026.9", remote_digest: "sha256:def", available: true }] },
      // Homestead's own release, offered on the top bar and under Settings › Updates.
      { ns: "lab", name: "homestead", homestead: "self", available: true, can_rollback: true,
        images: [{ container: "homestead", deployed: `ghcr.io/homestead-lab/homestead:${typeof HOMESTEAD_VERSION === "string" ? HOMESTEAD_VERSION : "2.8.318-dev.6"}`, candidate: "ghcr.io/homestead-lab/homestead:2.9.0", candidate_tag: "2.9.0", remote_digest: "sha256:ghi", available: true }] },
      // Deployed by Flux from git: its update is a notice, made in git (#296).
      { ns: "monitoring", name: "loki", available: true, can_rollback: false,
        managed: { by: "Flux", source: "Kustomization flux-system/monitoring", detected: true },
        images: [{ container: "loki", deployed: "grafana/loki:3.4.2", candidate: "grafana/loki:3.5.0", candidate_tag: "3.5.0", remote_digest: "sha256:aaa", available: true }] },
      { ns: "lab", name: "paperless", available: false, can_rollback: false,
        images: [{ container: "paperless", deployed: "registry.lan/paperless-ngx:2.11", candidate: "registry.lan/paperless-ngx:2.11", available: false, error: "registry authentication required" }] }] },
    // The demo is a Harvester cluster: kube-vip and Multus come with it.
    "/api/platform/baseline": { applies: false, parts: [], missing: [], distribution: "harvester", harvester: true, done: {} },
    "/api/flow": {
      nodes: nodes.map((n, i) => ({ id: `n:${n.name}`, name: n.name, copies: i === 0
        ? [{ vid: "v:home", vol: "home-assistant", running: true }, { vid: "v:paperless", vol: "paperless-data", running: true }]
        : i === 1 ? [{ vid: "v:frigate", vol: "frigate-config", running: true }, { vid: "v:home", vol: "home-assistant", running: true }]
        : [{ vid: "v:paperless", vol: "paperless-data", running: true }, { vid: "v:frigate", vol: "frigate-config", running: true },
          { vid: "v:ubuntu", vol: "ubuntu-2404", running: true }, { vid: "v:router", vol: "router-disk", running: false },
          { vid: "v:orphan", vol: "paperless-old-copy", running: false }],
        ips: [demoNodeIps[n.name]], vips: network.addresses.nodes[i].vips })),
      volumes: [{ id: "v:frigate", name: "frigate-config", replicas: 2, size_gb: 20, robustness: "healthy", attached: "harvester-node2" },
        { id: "v:home", name: "home-assistant", replicas: 2, size_gb: 10, robustness: "healthy", attached: "harvester-node1" },
        { id: "v:paperless", name: "paperless-data", replicas: 2, size_gb: 100, robustness: "healthy", attached: "harvester-node3" },
        { id: "v:ubuntu", name: "ubuntu-2404", replicas: 1, size_gb: 40, robustness: "healthy", attached: "harvester-node3" },
        { id: "v:router", name: "router-disk", replicas: 1, size_gb: 16, robustness: "unknown", attached: "" },
        { id: "v:orphan", name: "paperless-old-copy", replicas: 1, size_gb: 100, robustness: "unknown", attached: "" }],
      workloads: [{ id: "w:frigate", name: "frigate", ns: "lab", kind: "container", node: "harvester-node2", hardware: ["igpu", "coral_usb"], uptime: 472221, cpu: .84, mem_mb: 1840, claims: [{ pvc: "frigate-config", vid: "v:frigate" }], ports: [{ name: "web", port: 5000, vip: "192.0.2.214" }] },
        { id: "w:home", name: "home-assistant", ns: "lab", kind: "container", node: "harvester-node1", hardware: [], uptime: 912400, cpu: .31, mem_mb: 738, claims: [{ pvc: "home-assistant", vid: "v:home" }], ports: [{ name: "web", port: 8123, vip: "192.0.2.215" }] },
        { id: "w:paperless", name: "paperless", ns: "lab", kind: "container", node: "harvester-node3", hardware: [], uptime: 220190, cpu: .18, mem_mb: 512, claims: [{ pvc: "paperless-data", vid: "v:paperless" }], ports: [{ name: "web", port: 8000, vip: "192.0.2.216" }] },
        { id: "w:vm-ubuntu", name: "ubuntu", ns: "lab", kind: "vm", node: "harvester-node3", running: true, state: "Running", ip: "192.0.2.61", hardware: [], uptime: 86400, cpu: .22, mem_mb: 1540, claims: [{ pvc: "ubuntu-2404", vid: "v:ubuntu" }], ports: [{ name: "ssh", port: 22, vip: "192.0.2.217" }] },
        { id: "w:vm-router", name: "router", ns: "lab", kind: "vm", node: "", running: false, state: "Stopped", ip: "", hardware: [], uptime: 0, cpu: 0, mem_mb: 0, claims: [{ pvc: "router-disk", vid: "v:router" }], ports: [] }],
      vips: [{ id: "i:192.0.2.214", ip: "192.0.2.214", kind: "vip", node: "harvester-node1", state: "ok", ports: [{ app: "frigate", port: 5000 }] },
        { id: "i:192.0.2.215", ip: "192.0.2.215", kind: "vip", node: "harvester-node1", state: "ok", ports: [{ app: "home-assistant", port: 8123 }] },
        { id: "i:192.0.2.216", ip: "192.0.2.216", kind: "vip", node: "harvester-node1", state: "ok", ports: [{ app: "paperless", port: 8000 }] },
        { id: "i:192.0.2.217", ip: "192.0.2.217", kind: "vip", node: "harvester-node3", state: "ok", ports: [{ app: "ubuntu", port: 22 }] }],
    },
  };

  const diagnosticDemoId = "0123456789abcdef0123456789abcdef";
  const diagnosticReports = [{ id: diagnosticDemoId, title: "Container restart fails", comment: "Restart did not return the container to Ready.",
    version: "2.8.294-dev.3", created: Date.now() / 1000, updated: Date.now() / 1000, expires: Date.now() / 1000 + 86400,
    status: "ready", truncated: false, events: [{ kind: "click", at: 8000, action: "restart", target: "button:2" },
      { kind: "request", at: 9000, method: "POST", path: "/api/restart", status: 503, duration: 824 }],
    sources: { "homestead.log": "workload=frigate host=harvester-node1 address=192.0.2.207" },
    manifest: [{ source: "homestead.log", state: "included" }, { source: "homestead-previous.log", state: "unavailable" }] }];
  const passthroughExample = responses["/api/passthrough/inspect"];
  const inspectedDevices = {};
  responses["/api/passthrough/inventory"] = url => ({facts: inspectedDevices[url.searchParams.get("node")] || null});
  responses["/api/passthrough/inspect"] = (url, init) => {
    const node = JSON.parse(init?.body || "{}").node || "harvester-node1";
    return inspectedDevices[node] = {...passthroughExample, node, inspected_at: Math.floor(Date.now() / 1000)};
  };
  responses["/api/passthrough/vbios/capture"] = () => {
    const rom = new Uint8Array(512);
    rom.set([0x55, 0xaa]); rom[24] = 32;
    rom.set([0x50, 0x43, 0x49, 0x52, 0xde, 0x10, 0x87, 0x1e], 32);
    rom[48] = 1; rom[53] = 128;
    return {ok: true, data: btoa(String.fromCharCode(...rom)), size: rom.length, filename: "demo-gpu-vbios.rom"};
  };
  responses["/api/diagnostics"] = () => diagnosticReports.map(row => ({ ...row, events: row.events.length }));
  responses["/api/diagnostics/start"] = (url, init) => {
    const input = JSON.parse(init.body || "{}");
    const row = { ...structuredClone(diagnosticReports[0]), id: crypto.randomUUID().replaceAll("-", ""), title: input.package ? "Logs package" : "Bug report",
      comment: "", status: input.package ? "draft" : "recording", created: Date.now() / 1000, events: [] };
    diagnosticReports.push(row); return { ...row, events: 0 };
  };
  responses["/api/diagnostics/report"] = url => {
    const row = structuredClone(diagnosticReports.find(row => row.id === url.searchParams.get("id")) || diagnosticReports[0]);
    row.format = url.searchParams.get("format") || "anonymised";
    if (row.format === "anonymised") row.sources = { "homestead.log": "workload=<identifier-1> host=<identifier-2> address=<identifier-3>" };
    return row;
  };
  for (const path of ["events", "stop", "draft", "prepare", "delete"]) responses[`/api/diagnostics/${path}`] = (url, init) => {
    const input = JSON.parse(init.body || "{}"), row = diagnosticReports.find(row => row.id === input.id);
    if (!row) return { error: "Report not found" };
    if (path === "events") { if (input.batch > (row.lastBatch || 0)) row.events.push(...input.events); row.lastBatch = input.batch; return { batch: input.batch, status: row.status }; }
    if (path === "delete") { diagnosticReports.splice(diagnosticReports.indexOf(row), 1); return { ok: true }; }
    if (path === "stop") row.status = "draft";
    if (path === "draft" || path === "prepare") { row.title = input.title || "Bug report"; row.comment = input.comment || ""; }
    if (path === "prepare") row.status = "ready";
    return { ...row, events: row.events.length };
  };
  responses["/api/diagnostics/issue"] = () => ({ title: "Container restart fails", body: "### What happened\nRestart did not return the container to Ready.\n\n### Diagnostics\nHomestead demo. Identifiers anonymised. No logs attached.", url: "https://github.com/homestead-lab/homestead/issues/new?title=Container%20restart%20fails", comment_shortened: false });
  const original = window.fetch.bind(window);
  /* Linked clusters: this one, a branch office that answers, and a DR site that is off. */
  const demoSites = [
    { id: "a1f00d", handle: "main-site", name: "Main site", url: "http://192.0.2.242:8088", self: true, version: "2.8.200", reachable: true, compatible: true, error: "" },
    { id: "b2c0de", handle: "branch", name: "Branch office", url: "http://192.0.2.250:8088", self: false, version: "2.8.200", reachable: true, compatible: true, error: "" },
    { id: "c3beef", handle: "dr-site", name: "DR site", url: "http://192.0.2.251:8088", self: false, version: "", reachable: false, compatible: true,
      error: "no answer from http://192.0.2.251:8088" }];
  const siteTag = id => { const s = demoSites.find(x => x.id === id); return { id: s.id, name: s.name, handle: s.handle, self: s.self }; };
  const branchNodes = ["branch-node1", "branch-node2"];
  const branchWorkloads = [
    { ...workloads[0], name: "jellyfin", group: "", nodes: [branchNodes[0]], icon: "", images: ["jellyfin/jellyfin:10.9.11"], cpu: 0.41, mem_mb: 1210, ports: [{ port: 8096, ip: "192.0.2.250" }] },
    { ...workloads[0], name: "unifi", group: "", nodes: [branchNodes[1]], icon: "", images: ["jacobalberty/unifi:v8.4"], cpu: 0.06, mem_mb: 690, ports: [{ port: 8443, ip: "192.0.2.250" }] }];
  const mine = rows => rows.map(row => ({ ...row, site: siteTag("a1f00d") }));
  const branch = rows => rows.map(row => ({ ...row, site: siteTag("b2c0de") }));
  Object.assign(responses, {
    "/api/fleet": { self: "a1f00d", protocol: 1, linked: true, members: demoSites, via: "", via_id: "",
      address: "http://192.0.2.242:8088", suggested_address: "http://192.0.2.242:8088" },
    "/api/fleet/switch": { ok: true }, "/api/fleet/address": { ok: true, missed: [] },
    "/api/fleet/join": { ok: true, member: { name: "DR site" }, missed: [] },
    "/api/fleet/remove": { ok: true, told: true, missed: [] },
    "/api/objectstore/transfers": (url, init) => init?.method === "POST"
      ? { allowed: JSON.parse(init.body || "{}").allow, deployed: true, detail: JSON.parse(init.body || "{}").allow ? "moves out are on" : "moves out are off: backup storage is stopped, its volume kept" }
      : { allowed: true, deployed: true, ready: true, stopped: false, endpoint: "http://192.0.2.242:9000", reachable_off_cluster: true,
          size_gb: 100, backups_here: true },
    "/api/move/clusters/transfers": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      if (body.allow !== undefined) return { allowed: body.allow, deployed: true, detail: body.allow ? "moves out are on" : "moves out are off" };
      return body.name === "branch" ? { allowed: true, deployed: true, ready: true, stopped: false, endpoint: "http://192.0.2.250:9000",
          reachable_off_cluster: true, size_gb: 100, backups_here: true }
        : { allowed: false, deployed: false, ready: false, stopped: false };
    },
    "/api/fleet/legacy": [{ name: "staging", url: "http://192.0.2.252:8088", user: "admin", added: "2026-05-02 18:40", linked_as: null }],
    "/api/fleet/link-legacy": { ok: true, member: { name: "Staging" }, missed: [] }, "/api/fleet/leave": { ok: true, missed: [] },
    "/api/fleet/all/flow": () => ({clusters:[{...responses["/api/flow"],site:siteTag("a1f00d")},{...responses["/api/flow"],site:siteTag("b2c0de")}],missing:[{id:"c3beef",name:"DR site",error:"Not answering"}]}),
    "/api/fleet/all/workloads": () => [...mine(workloads), ...branch(branchWorkloads)],
    "/api/fleet/all/nodes": () => [...mine(nodes), ...branch(branchNodes.map((name, i) => ({ ...nodes[i], name })))],
    "/api/fleet/all/vms": () => [...mine(demoVms), ...branch([{ ...demoVms[0], name: "pfsense", node: branchNodes[0], ip: "192.0.2.1", ips: ["192.0.2.1"] }])],
    "/api/fleet/all/volumes": () => [...mine(volumes), ...branch(volumes.slice(0, 2).map((v, i) => ({ ...v, name: `pvc-branch-${i}`, pvc_name: ["jellyfin-config", "unifi-data"][i] })))],
  });
  const firewallTargets = workloads.map(row => ({namespace:row.ns, name:row.name, kind:"Deployment",
    uid:`demo-${row.name}`, selector:{matchLabels:{app:row.name}}, labels:{app:row.name}, blocked:"", warnings:[]}));
  const firewallSpec = cfg => {
    const spec = {podSelector:{matchLabels:{app:cfg.target.name}}, policyTypes:[]};
    for (const direction of ["ingress", "egress"]) {
      if (cfg[direction] !== "restricted") continue;
      spec.policyTypes.push(direction === "ingress" ? "Ingress" : "Egress");
      spec[direction] = (cfg[direction + "_rules"] || []).map(row => {
        const rule = {};
        if (row.peer !== "any") rule[direction === "ingress" ? "from" : "to"] = [row.peer === "cidr"
          ? {ipBlock:{cidr:row.value}} : {namespaceSelector:{matchLabels:{"kubernetes.io/metadata.name":row.value}}}];
        if (row.protocol !== "Any") rule.ports = row.ports ? row.ports.split(",").map(p => ({protocol:row.protocol,port:Number(p.trim())})) : [{protocol:row.protocol}];
        return rule;
      });
    }
    if (cfg.allow_dns && spec.egress) spec.egress.push({to:[{namespaceSelector:{matchLabels:{"kubernetes.io/metadata.name":"kube-system"}},
      podSelector:{matchLabels:{"k8s-app":"kube-dns"}}}],ports:[{protocol:"UDP",port:53},{protocol:"TCP",port:53}]});
    return spec;
  };
  const firewallConfig = {namespace:firewallTargets[0].namespace, name:`homestead-fw-${firewallTargets[0].name}`,
    target:Object.fromEntries(["namespace","name","kind","uid"].map(k => [k,firewallTargets[0][k]])),
    ingress:"restricted", egress:"unchanged", ingress_rules:[{peer:"any",value:"",protocol:"TCP",ports:"80,443"}],egress_rules:[],allow_dns:false};
  const firewall = {provider:{name:"K3s",detail:"Demo cluster: network-policy enforcement is not tested."},targets:firewallTargets,
    namespaces:["lab","default","kube-system"],policies:[{namespace:firewallConfig.namespace,name:firewallConfig.name,
      uid:"demo-firewall",resource_version:"1",managed:true,config:firewallConfig,spec:firewallSpec(firewallConfig)}]};
  responses["/api/firewall"] = () => firewall;
  responses["/api/firewall/preview"] = (url, init) => {
    const cfg = JSON.parse(init?.body || "{}");
    return {review:"demo-reviewed-policy",pods:[`${cfg.target.name}-demo`],overlapping:[],update:!!cfg.uid,
      warnings:["Demo only: no cluster traffic changes.","Policies add allowed traffic together; other policies can permit additional connections.","Pod-network traffic only. Host and Multus/LAN traffic need a separate firewall."],
      manifest:{apiVersion:"networking.k8s.io/v1",kind:"NetworkPolicy",metadata:{namespace:cfg.namespace,name:cfg.name},spec:firewallSpec(cfg)}};
  };
  responses["/api/firewall/save"] = (url, init) => {
    const cfg = JSON.parse(init?.body || "{}");
    firewall.policies = firewall.policies.filter(p => p.namespace !== cfg.namespace || p.name !== cfg.name);
    firewall.policies.push({namespace:cfg.namespace,name:cfg.name,uid:cfg.uid || `demo-${Date.now()}`,resource_version:String(Date.now()),
      managed:true,config:cfg,spec:firewallSpec(cfg)});
    return {ok:true,message:"Demo firewall policy saved."};
  };
  responses["/api/firewall/delete"] = (url, init) => {
    const cfg = JSON.parse(init?.body || "{}");
    firewall.policies = firewall.policies.filter(p => p.namespace !== cfg.namespace || p.name !== cfg.name);
    return {ok:true,message:"Demo policy removed."};
  };
  responses["/api/ipam/free"] = [
    { cidr: "192.0.2.0/24", name: "Home LAN", free: ["192.0.2.231", "192.0.2.232", "192.0.2.233", "192.0.2.236",
      "192.0.2.237", "192.0.2.238", "192.0.2.241", "192.0.2.247", "192.0.2.248", "192.0.2.249", "192.0.2.251", "192.0.2.253"] },
    { cidr: "10.20.0.0/24", name: "Lab VLAN", free: ["10.20.0.10", "10.20.0.11", "10.20.0.12", "10.20.0.13"] }];
  const demoConfigParts = [
    ["settings", "Settings", "Site name, health thresholds, update policy, App Store feed", true],
    ["users", "Users and roles", "Every account, its role and password", false, "Replaces every account and password with the backup's, and signs everyone out - sign in again with an account from the backup."],
    ["hardware", "Hardware features", "Device mappings: iGPU, Coral, USB and the rest", true],
    ["vips", "VIPs", "Your saved VIPs, their labels and the default workload VIP", true],
    ["ipam", "IP addresses", "Subnets, documented addresses, and the UniFi connection", true],
    ["mqtt", "MQTT", "The broker, its credentials and what is published", true],
    ["portal", "Portal", "Its sections and tiles", true],
    ["shares", "Network shares", "Shares, their options, and SMB users", true, "Brings back share definitions and SMB users; the volumes they point at must still exist."],
    ["sources", "Import sources", "Unraid and Docker hosts to import from", true],
    ["vmstore", "VM image store", "The cloud images kept, and whether they refresh", false]];
  Object.assign(responses, {
    "/api/config/parts": demoConfigParts.map(([id, label, detail, dflt, caution]) => ({ id, label, detail, caution: caution || "",
      default: id !== "users", present: id !== "vmstore" })),
    "/api/config/backup": { format: "homestead-config-backup", version: 1, homestead: "2.8.318-dev.6", site: "Main site",
      created: new Date().toISOString(), parts: [] },
    "/api/config/inspect": { homestead: "2.8.209", site: "Main site", created: "2026-09-26T21:40:00Z",
      parts: demoConfigParts.map(([id, label, detail, , caution], i) => ({ id, label, detail, caution: caution || "", default: id !== "users",
        state: id === "vmstore" ? "empty" : ["ipam", "vips", "portal"].includes(id) ? "differs" : "same", restorable: id !== "vmstore" })) },
    "/api/config/restore": { ok: true, restored: ["ipam", "vips", "portal"], skipped: [], detail: "restored IP addresses, VIPs, Portal" },
  });
  const ago = minutes => Math.floor(Date.now() / 1000) - minutes * 60;
  const demoKeys = { scopes: { read: "Read status: the cluster, nodes, containers, VMs, alerts and jobs",
      "containers:control": "Start, stop and restart containers", "vms:control": "Start, stop and restart virtual machines" },
    min_ttl: 3600, max_ttl: 366 * 86400,
    keys: [{ id: "3f9a1c0b7d2e", name: "Home Assistant", owner: "demo", scopes: ["read", "containers:control"], networks: ["192.0.2.20"],
             created: ago(60 * 24 * 12), expires: ago(-60 * 24 * 78), expired: false, last_used: ago(2), last_ip: "192.0.2.20" },
           { id: "8c41e07a9b55", name: "Ops agent", owner: "demo", scopes: ["read"], networks: [],
             created: ago(60 * 24 * 40), expires: ago(60 * 24), expired: true, last_used: ago(60 * 30), last_ip: "198.51.100.7" }] };
  responses["/api/auth/keys"] = (url, init) => {
    if (init?.method !== "POST") return demoKeys;
    const body = JSON.parse(init.body || "{}");
    return { ok: true, token: "hsk_0123456789ab_DEMO-ONLY-not-a-real-key-it-works-nowhere-x",
      key: { id: "0123456789ab", name: body.name || "New key", owner: "demo", scopes: body.scopes || ["read"], networks: body.networks || [],
             created: ago(0), expires: ago(-(body.ttl_seconds || 86400) / 60), expired: false, last_used: null, last_ip: "" } };
  };
  responses["/api/auth/keys/revoke"] = { ok: true, name: "Home Assistant" };
  // The setup guide, part way through, as a cluster a few weeks in would be.
  const demoSetup = { skips: [], hidden: false, opened: true, admin: true, personal: ["appearance", "phone", "notifications"], steps: {
    health: { done: false, applies: true, summary: "One workload is not ready",
      issues: [{ severity: "degraded", kind: "Workload", name: "lab/doublecommander", reason: "only 0/1 replicas ready after 12m" }] },
    quorum: { done: true, applies: true, servers: 3, members: ["harvester-node1", "harvester-node2", "harvester-node3"],
      ready: ["harvester-node1", "harvester-node2", "harvester-node3"], can_lose: 1, nodes: [] },
    clocks: { done: true, applies: false },
    address: { done: true, applies: true, url: "http://192.0.2.245:8088", service_url: "http://homestead.lab.svc:8088", vips: 3, load_balancer: "kube-vip", harvester: true },
    lan: { done: demoPlatform === "harvester", applies: true,
      vms: demoPlatform === "harvester" ? ["default/vlan1"] : [], containers: demoPlatform === "harvester" ? ["default/vlan1"] : [], networks: demoPlatform === "harvester"
      ? [{ name: "default/vlan1", type: "bridge", vms: true, containers: true }] : [] },
    smb: { done: true, applies: true, installed: true, enabled: true, address: "192.0.2.245", shares: 3 },
    https: { done: false, applies: true, url: "", tunnels: [] }, hostname: { done: false, applies: true },
    disks: { done: false, applies: true, unused: [{ node: "harvester-node2", device: "sdb", size_gb: 4000, kind: "HDD" }] },
    storage: { done: true, applies: true, default: "harvester-longhorn", copies: 3, provisioner: "driver.longhorn.io", nodes: 3, target: 3, candidates: [] },
    backups: { done: true, applies: true }, config: { done: false, applies: true, at: null }, osupdates: { done: false, applies: false },
    notifications: { done: false, applies: true }, people: { done: false, applies: true, users: 3 },
    unifi: { done: true, applies: true }, unraid: { done: true, applies: true }, homeassistant: { done: true, applies: true },
    ipam: { done: true, applies: true, unifi: true, synced: Math.floor(Date.now() / 1000) - 3600,
      subnets: [{ id: "192.0.2.0/24", cidr: "192.0.2.0/24", name: "LAN", scanned: Math.floor(Date.now() / 1000) - 7200 }] },
    linked: { done: true, applies: true }, starter: { done: true, applies: true }, console: { done: false, applies: false } } };
  responses["/api/setup"] = () => ({ ...demoSetup, skips: [...demoSetup.skips] });
  responses["/api/setup/skip"] = (url, init) => {
    const body = JSON.parse(init?.body || "{}");
    demoSetup.skips = body.skip ? [...new Set([...demoSetup.skips, body.step])] : demoSetup.skips.filter(x => x !== body.step);
    return { ok: true, skips: demoSetup.skips };
  };
  responses["/api/setup/hide"] = (url, init) => { demoSetup.hidden = !!JSON.parse(init?.body || "{}").hidden; return { ok: true, hidden: demoSetup.hidden }; };
  responses["/api/setup/complete"] = (url, init) => { demoSetup.completed = !!JSON.parse(init?.body || "{}").completed; return { ok: true, completed: demoSetup.completed }; };
  responses["/api/setup/https-check"] = (url, init) => ({ ok: true, url: JSON.parse(init?.body || "{}").url || "https://homestead.example.com", at: Math.floor(Date.now() / 1000) });
  responses["/api/setup/opened"] = { ok: true };
  responses["/api/auth/users"] = [{ name: "demo", role: "admin", last_login: "2026-09-28 07:40" },
    { name: "alex", role: "operator", last_login: "2026-09-28 07:06" }, { name: "kiosk", role: "viewer", last_login: "" }];
  responses["/api/auth/history"] = [
    { at: ago(4), event: "signin", user: "demo", ok: true, ip: "192.0.2.20", device: "Chrome on Windows", via: "", detail: "kept signed in" },
    { at: ago(38), event: "signin", user: "alex", ok: true, ip: "172.70.4.18", device: "Safari on iOS", via: "Cloudflare", detail: "" },
    { at: ago(41), event: "signin-failed", user: "alex", ok: false, ip: "172.70.4.18", device: "Safari on iOS", via: "Cloudflare", detail: "incorrect username or password" },
    { at: ago(180), event: "role", user: "alex", ok: true, ip: "192.0.2.20", device: "Chrome on Windows", via: "", detail: "now operator, by demo" },
    { at: ago(600), event: "signin-blocked", user: "admin", ok: false, ip: "203.0.113.7", device: "a script", via: "", detail: "too many attempts — wait a few minutes" },
    { at: ago(1440), event: "password", user: "demo", ok: true, ip: "192.0.2.20", device: "Chrome on Windows", via: "", detail: "" }];
  // Public demos start healthy. Incident fixtures remain explicit and deterministic
  // for UI audits and evaluations; action previews still enforce their constraints.
  if (scenario === "healthy") {
    Object.assign(demoVms.find(v => v.name === "ubuntu-test"), {
      status: "Stopped", run_strategy: "Halted", cores: 2, memory: "4Gi",
      restart_required: false, problem: "", actions: ["start"],
    });
    nodes[2].roles = ["control-plane", "etcd"];
    for (const node of nodes) for (const disk of node.temps.disks) {
      const nvme = disk.kind === "NVMe";
      Object.assign(disk.smart, { reallocated: nvme ? null : 0, pending: nvme ? null : 0,
        uncorrectable: nvme ? null : 0, media_errors: nvme ? 0 : null });
      withHealth(disk);
    }
    for (const workload of workloads.filter(row => row.desired > 0 && row.ready < row.desired)) {
      workload.ready = workload.desired;
      workload.uptime = 3600;
      workload.pods = [pod(workload.name, workload.nodes[0], workload.images[0])];
      workload.pod_count = workload.container_count = 1;
    }
    const imageUpdates = responses["/api/image-updates"];
    imageUpdates.errors = 0;
    for (const workload of imageUpdates.workloads) for (const image of workload.images) {
      delete image.error;
    }
    for (const volume of volumes.filter(row => row.state === "attached")) {
      Object.assign(volume, { robustness: "healthy", health_reason: "", conditions: [], scheduling_error: "" });
      delete volume.rebuild; delete volume.restore;
      const existing = new Set((volume.copies || []).map(row => row.node));
      volume.copies = (volume.copies || []).map(row => ({ ...row, healthy: true, state: "running" }));
      for (const node of nodes) if (volume.copies.length < volume.replicas && !existing.has(node.name)) {
        volume.copies.push({ node: node.name, disk: "nvme1n1", healthy: true, state: "running" });
        existing.add(node.name);
      }
    }
    Object.assign(storage, { volumes: volumes.length, healthy: volumes.filter(row => row.robustness === "healthy").length,
      degraded: 0, faulted: 0, unknown: volumes.filter(row => row.robustness === "unknown").length,
      detached: volumes.filter(row => row.state === "detached").length,
      attached: volumes.filter(row => row.state === "attached").length, reasons: [] });
    const cluster = responses["/api/cluster"];
    Object.assign(cluster, { state: "healthy", summary: "Cluster services and all three control-plane nodes are healthy", warnings: [] });
    Object.assign(cluster.control_plane, { total: 3, ready: 3, etcd_total: 3, etcd_ready: 3, quorum_needed: 2, quorum_margin: 1, state: "healthy" });
    cluster.nodes[2].roles = [...nodes[2].roles];
    cluster.onboarding.reason = "Three control-plane nodes provide a one-node failure margin.";
    demoSetup.steps.health = { done: true, applies: true, summary: cluster.summary, issues: [] };
    const capacity = responses["/api/longhorn/capacity"], first = capacity.nodes[0];
    Object.assign(first, { allocated_gb: 60, pct: 51.4, room_gb: 56.8, level: "ok" });
    Object.assign(first.disks[0], { allocated_gb: 60, pct: 51.4, room_gb: 56.8 });
    capacity.largest = { 1: 236.3, 2: 56.8, 3: 36.8 };
    network.services = network.services.filter(row => !row.orphaned);
    network.addresses.problems = 0;
    network.addresses.nodes[2].control_plane = true;
    for (const address of network.addresses.addresses.filter(row => row.state === "unrouted")) {
      Object.assign(address, { state: "ok", reason: "", unrouted: [] });
    }
    const events = responses["/api/events"];
    responses["/api/events"] = (...args) => events(...args).filter(row => row.type !== "Warning");
    const operations = responses["/api/operations"];
    responses["/api/operations"] = (...args) => operations(...args).map(row => row.id === "op2"
      ? { ...row, status: "succeeded", progress: 100, message: "Image cached" } : row);
    const moves = responses["/api/move/moves"];
    responses["/api/move/moves"] = (...args) => moves(...args).filter(row => row.status !== "failed");
    for (const site of demoSites.filter(row => !row.reachable)) Object.assign(site, { reachable: true, version: "2.8.318-dev.6", error: "" });
    const mqtt = responses["/api/mqtt/preview"].states[0].payload;
    Object.assign(mqtt, { health: "healthy", vol_total: storage.volumes, vol_degraded: 0, vol_faulted: 0 });
  } else {
    const overview = responses["/api/overview"];
    Object.assign(overview, { health: "degraded", health_state: "degraded", health_summary: "A workload and storage replicas need attention",
      health_issues: [...demoSetup.steps.health.issues, { kind: "Volume", name: "lab/arr-dashboard-data", severity: "degraded", reason: storage.reasons[0].reason }] });
    if (scenario === "critical") {
      nodes[2].status = "NotReady";
      Object.assign(overview, { health: "critical", health_state: "critical", nodes_ready: 2, health_summary: "One node is unavailable" });
      overview.health_issues.unshift({ kind: "Node", name: nodes[2].name, severity: "critical", reason: "Node is not ready" });
      Object.assign(responses["/api/cluster"], { state: "critical", summary: overview.health_summary });
      Object.assign(responses["/api/cluster"].nodes[2], { status: "NotReady", ready: false });
      network.addresses.nodes[2].ready = false;
      demoSetup.steps.health = { done: false, applies: true, summary: overview.health_summary, issues: overview.health_issues };
    }
  }
  responses["/api/alerts"] = {devices:[],log:[],active:scenario === "healthy" ? [] : [
    {key:"health:Disk:harvester-node2/sda",version:"demo-drive-24",severity:"degraded",category:"degraded",title:"Drive harvester-node2/sda needs attention",body:"24 reallocated sectors",href:"/nodes",acknowledged:false},
    {key:"health:Node:harvester-node3",version:"demo-node-down",severity:"critical",category:"outage",title:"Host harvester-node3 is not ready",body:"Kubernetes reports NotReady. Review this host.",href:"/nodes",acknowledged:false}
  ]};
  responses["/api/alerts/acknowledge"] = (url,opts) => {const body=JSON.parse(opts.body || "{}");const row=responses["/api/alerts"].active.find(a=>a.key===body.key && a.version===body.version);if(!row)throw new Error("This alert changed. Refresh and review its current state.");row.acknowledged=!body.undo;return {ok:true};};
  // On the live demo, say so on every page.
  if (live) {
    const banner = () => {
      if (document.getElementById("demoBanner")) return;
      const bar = document.createElement("div");
      bar.id = "demoBanner";
      bar.className = "demobanner";
      bar.innerHTML = `Live demo · ${scenario === "healthy" ? "healthy cluster" : scenario + " scenario"}: made-up data, and nothing you do here is saved. `
        + '<a href="https://github.com/homestead-lab/homestead" target="_blank" rel="noopener">Homestead on GitHub</a>';
      document.body.prepend(bar);
    };
    if (document.body) banner(); else document.addEventListener("DOMContentLoaded", banner);
  }
  window.fetch = async function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.origin);
    if (!url.pathname.startsWith("/api/")) return original(input, init);
    const key = url.pathname === "/api/image-updates" ? "/api/image-updates" : url.pathname;
    if (key === "/api/auth/preferences/dashboard") {
      const storageKey = `homestead.demo.dashboard.${typeof ME === "string" ? ME : "demo"}`;
      let saved;try {saved=JSON.parse(localStorage.getItem(storageKey));}catch{}
      saved ||= {revision:null,layout:null};
      if(init?.method === "POST") {
        const body=JSON.parse(init.body);
        if(body.revision!==saved.revision)return new Response(JSON.stringify({error:"Your dashboard changed in another session. Cancel and reopen the editor to load the latest layout."}),{status:409,headers:{"Content-Type":"application/json"}});
        saved={revision:crypto.randomUUID(),layout:body.layout};localStorage.setItem(storageKey,JSON.stringify(saved));
      }
      return new Response(JSON.stringify(saved),{status:200,headers:{"Content-Type":"application/json"}});
    }
    if (key === "/api/diagnostics/download") return new Response("Homestead demo diagnostics; no cluster logs were collected.\n", { headers: { "Content-Type": "text/plain" } });
    // Asked of a linked cluster: the branch office runs an older Homestead, which does
    // not yet tag its own parts, with a release waiting.
    const cluster = init?.headers?.["X-Homestead-Cluster"];
    if (cluster && key === "/api/image-updates") {
      const branchReport = { checked_at: new Date().toISOString(), updates: 0, errors: 0, workloads: [
        { ns: "lab", name: "homestead", available: true, can_rollback: true, images: [{ container: "homestead",
          deployed: "ghcr.io/homestead-lab/homestead:2.8.200", candidate: "ghcr.io/homestead-lab/homestead:2.9.0", candidate_tag: "2.9.0", available: true }] },
        { ns: "lab", name: "jellyfin", available: false, can_rollback: false, images: [] }] };
      return new Response(JSON.stringify(branchReport), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (key === "/api/image-updates/channel" && init?.method === "POST") {
      const channel = JSON.parse(init.body).channel;
      if (!["prod", "dev"].includes(channel)) return new Response(JSON.stringify({ error: "update channel must be prod or dev" }), { status: 400 });
      responses["/api/settings"].updates.channel = channel;
      const report = responses["/api/image-updates"];
      report.channel = channel;
      const image = report.workloads.find(w => w.homestead === "self").images[0];
      image.candidate_tag = channel === "dev" ? "2.9.0-dev.1" : "2.9.0";
      image.candidate = "ghcr.io/homestead-lab/homestead:" + image.candidate_tag;
      return new Response(JSON.stringify(responses["/api/settings"]), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    const configured = responses[key];
    let value = typeof configured === "function" ? configured(url, init) : configured;
    // The live demo: a change the demo has no answer for is taken, and kept nowhere.
    if (value === undefined && live && (init?.method || "GET") !== "GET") value = { ok: true, detail: "Demo: nothing is saved" };
    if (value === undefined) return new Response(JSON.stringify({ error: `Demo endpoint not available: ${url.pathname}` }), { status: 404, headers: { "Content-Type": "application/json" } });
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  };
})();

/* Deterministic, read-only demo transport used only by release screenshot CI.
   It never contacts a cluster and is inert unless ?demo=1 is present. */
(function () {
  if (new URLSearchParams(location.search).get("demo") !== "1") return;

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

  const nodes = [
    { name: "harvester-node1", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 22.4, cpu_used: 1.79, cpu_cap: 8, mem_pct: 61.7, mem_used_gb: 9.6, mem_cap_gb: 15.6,
      fs_pct: 36.2, fs_used_gb: 168, fs_cap_gb: 464, rx_mbps: 8.4, tx_mbps: 3.1,
      pods: 54, pods_sys: 46, pods_wl: 8, vms: 1, workloads: ["home-assistant", "mosquitto", "samba"],
      hardware: { igpu: true }, temps: { cpu_c: 39, max_c: 51, sensors: 4, smart_helper: { available: true },
        disks: [withHealth({ name: "nvme0n1", model: "Samsung SSD 970 EVO Plus", serial: "DEMO-NVME-01", kind: "NVMe", size_gb: 465.8,
          read_mbps: 18.42, write_mbps: 6.17, smart: smartDisk("nvme0n1", "Samsung SSD 970 EVO Plus", "DEMO-NVME-01", 41, 8421) })] } },
    { name: "harvester-node2", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 41.8, cpu_used: 3.34, cpu_cap: 8, mem_pct: 54.1, mem_used_gb: 8.4, mem_cap_gb: 15.6,
      fs_pct: 28.5, fs_used_gb: 132, fs_cap_gb: 464, rx_mbps: 21.9, tx_mbps: 12.6,
      pods: 47, pods_sys: 42, pods_wl: 5, vms: 0, workloads: ["frigate", "homestead"],
      hardware: { igpu: true, coral_usb: true }, temps: { cpu_c: 34, max_c: 47, sensors: 5, smart_helper: { available: true },
        disks: [withHealth({ name: "sda", model: "WDC WD100EFAX", serial: "DEMO-SATA-02", kind: "HDD", size_gb: 931.5,
          read_mbps: 3.26, write_mbps: 12.91,
          smart: smartDisk("sda", "WDC WD100EFAX", "DEMO-SATA-02", 36, 16420,
            { reallocated: 24, life_pct: 61 }) })] } },
    { name: "harvester-node3", status: "Ready", roles: ["worker"], schedulable: true,
      cpu_pct: 16.3, cpu_used: 0.65, cpu_cap: 4, mem_pct: 46.2, mem_used_gb: 7.2, mem_cap_gb: 15.6,
      fs_pct: 31.1, fs_used_gb: 144, fs_cap_gb: 464, rx_mbps: 5.8, tx_mbps: 2.4,
      pods: 31, pods_sys: 28, pods_wl: 3, vms: 0, workloads: ["paperless"],
      hardware: {}, temps: { cpu_c: 36, max_c: 45, sensors: 3, smart_helper: { available: true },
        disks: [withHealth({ name: "nvme0n1", model: "Kingston NV2", serial: "DEMO-NVME-03", kind: "NVMe", size_gb: 465.8,
          read_mbps: 0.74, write_mbps: 1.15, smart: smartDisk("nvme0n1", "Kingston NV2", "DEMO-NVME-03", 38, 3912) })] } },
  ];
  const pod = (name, node, image) => ({ name: `${name}-7d8f6d4c9-demo`, node, phase: "Running",
    ready: true, restarts: 0, container_count: 1,
    containers: [{ name, image, kind: "app", state: "running", ready: true, restarts: 0 }] });
  const workloads = [
    { name: "frigate", ns: "lab", kind: "Deployment", desired: 1, ready: 1, uptime: 472221,
      cpu: 0.84, mem_mb: 1840, nodes: ["harvester-node2"], hardware: ["igpu", "coral_usb"],
      images: ["ghcr.io/blakeblackshear/frigate:stable"], ports: [{ port: 5000, ip: "192.0.2.214" }],
      pod_count: 1, container_count: 1, pods: [pod("frigate", "harvester-node2", "ghcr.io/blakeblackshear/frigate:stable")] },
    { name: "home-assistant", ns: "lab", kind: "Deployment", desired: 1, ready: 1, uptime: 912400,
      cpu: 0.31, mem_mb: 738, nodes: ["harvester-node1"], hardware: [],
      images: ["ghcr.io/home-assistant/home-assistant:stable"], ports: [{ port: 8123, ip: "192.0.2.215" }],
      pod_count: 1, container_count: 1, pods: [pod("home-assistant", "harvester-node1", "ghcr.io/home-assistant/home-assistant:stable")] },
    { name: "paperless", ns: "lab", kind: "Deployment", desired: 1, ready: 1, uptime: 220190,
      cpu: 0.18, mem_mb: 512, nodes: ["harvester-node3"], hardware: [],
      images: ["ghcr.io/paperless-ngx/paperless-ngx:latest"], ports: [{ port: 8000, ip: "192.0.2.216" }],
      pod_count: 1, container_count: 1, pods: [pod("paperless", "harvester-node3", "ghcr.io/paperless-ngx/paperless-ngx:latest")] },
  ];
  const storage = { cap_gb: 1392, avail_gb: 906, used_gb: 486, used_pct: 34.9,
    provisioned_gb: 670, actual_gb: 224, volumes: 8, healthy: 6, degraded: 1,
    faulted: 0, detached: 1, unknown: 1, attached: 7, disks: [],
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
    { name: "pvc-demo-frigate", pvc_name: "frigate-config", namespace: "lab", attached_to: "frigate, samba",
      attached: ["frigate", "samba"],
      pod_status: "Running", state: "attached", robustness: "healthy", node: "harvester-node2",
      size_gb: 20, actual_gb: 3.8, used_pct: 19, replicas: 2,
      access_modes: ["ReadWriteOnce"], storage_class: "longhorn-r2", last_used_secs: 0 },
    { name: "pvc-demo-degraded", pvc_name: "arr-dashboard-data", namespace: "lab", attached_to: "arr-dashboard",
      attached: ["arr-dashboard"], node: "harvester-node1", state: "attached", robustness: "degraded",
      size_gb: 5, actual_gb: 3.1, used_pct: 62, replicas: 2, access_modes: ["ReadWriteOnce"],
      storage_class: "longhorn-r2", last_used_secs: 0, created: "2026-09-19T08:00:00Z",
      health_reason: "no disk space to create the replicas required: 1 of 2 replicas scheduled",
      conditions: [{ type: "Scheduled", status: "False", reason: "ReplicaSchedulingFailure",
        message: "no disk space to create the replicas required: 1 of 2 replicas scheduled" }],
      scheduling_error: "" },
    { name: "pvc-demo-scratch", pvc_name: "scratch-test", namespace: "lab", attached_to: "",
      pod_status: "", state: "detached", robustness: "unknown", node: "",
      // Longhorn reports this on a resting volume; it is not a fault.
      health_reason: "", size_gb: 5, actual_gb: 0.2, used_pct: 4, replicas: 2,
      access_modes: ["ReadWriteOnce"], storage_class: "longhorn-r2", last_used_secs: 2400 },
  ];
  const shares = [
    { name: "media", pvc: "share-media", path: "/shares/media", size_gb: 250,
      actual_size_gb: 250, pvc_status: "Bound", user: "lab", public: true,
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
    total: 2, protected: 2, unprotected: [], groups: ["default", "critical"],
    target: { configured: true, available: true, name: "default",
      url: "s3://homestead-backups@us-east-1/", reason: "", interval: "5m",
      secret: "homestead-backup-credentials" },
    tasks: { snapshot: "Snapshot — point-in-time, stored on the volume",
      backup: "Backup — snapshot then upload to the backup target" },
    jobs: [{ name: "nightly-backup", task: "backup", cron: "0 2 * * *", retain: 7,
      concurrency: 1, groups: ["default"], covers: 2,
      volumes: ["pvc-demo-frigate", "pvc-demo-scratch"], desc: "Nightly external backup" }],
    volumes: [
      { name: "pvc-demo-frigate", pvc: "frigate-config", namespace: "lab", size_gb: 20,
        robustness: "healthy", state: "attached", labels: {}, jobs: [], groups: ["default", "critical"],
        last_backup: lhBackups[0].name, last_backup_at: lhBackups[0].created },
      { name: "pvc-demo-scratch", pvc: "scratch-test", namespace: "lab", size_gb: 5,
        robustness: "healthy", state: "detached", labels: {}, jobs: [], groups: ["default"],
        last_backup: "", last_backup_at: "" },
    ],
  };
  const network = {
    controller: { name: "kube-vip", installed: true, desired: 3, ready: 3, healthy: true,
      mode: "ARP Service controller · explicit VIP allocation" },
    summary: { services: 8, app_services: 3, load_balancers: 3, vips: 3,
      listeners: 3, unhealthy: 0, ready_endpoints: 3 },
    available_vips: ["192.0.2.217", "192.0.2.218"], available_vip_count: 2,
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
  const restorePlan = url => {
    const ns = url.searchParams.get("ns") || "";
    const name = url.searchParams.get("name") || "";
    const conflict = name === "frigate-config" ? { kind: "PersistentVolumeClaim", name,
      message: `PVC ${ns}/${name} already exists; choose a new name` } : null;
    return { backup: lhBackups[0].name, source_volume: "pvc-demo-frigate",
      created: lhBackups[0].created, backup_size_mb: lhBackups[0].size_mb,
      volume_size_bytes: 21474836480, minimum_size_gb: 20,
      suggested_name: "pvc-demo-frigate-restore", namespace: ns, pvc_name: name,
      conflict, ready: !conflict, target: "default" };
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
    trending: 7.8, top_trending: 5.4, top_performing: 7.8, first_seen: 1640995200, deploy: {
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
    "/api/settings": { thresholds: { cpu: { warning: 70, critical: 88 }, memory: { warning: 70, critical: 88 }, disk: { warning: 75, critical: 90 }, temperature: { warning: 70, critical: 85 } }, smart: { temperature: { warning: 55, critical: 65 }, reallocated_warning: 1, pending_critical: 1, uncorrectable_critical: 1, notify_failures: true }, updates: { policy: "approval_required", notify_available: true, notify_failures: true }, site_name: "Loft rack",
      info: { version: "2.8.60", namespace: "lab", storage_class: "longhorn-r2", vip: "192.0.2.242",
        kubernetes: "v1.32.4+rke2r1",
        node_probe: { state: "updated", detail: "homestead-nodeprobe updated to this release's scripts" } } },
    "/api/overview": { health: "healthy", health_state: "healthy", health_summary: "All cluster services are healthy", health_issues: [],
      cpu_pct: 27.2, cpu_used: 5.4, cpu_cap: 20, mem_pct: 54.0, mem_used_gb: 25.2, mem_cap_gb: 46.8,
      nodes_ready: 3, nodes_total: 3, workload_pods: 16, system_pods: 116, lb_ip: "192.0.2.242", nodes,
      top_cpu: [{ name: "frigate", ns: "lab", nodes: ["harvester-node2"], cpu: .84 }, { name: "home-assistant", ns: "lab", nodes: ["harvester-node1"], cpu: .31 }, { name: "paperless", ns: "lab", nodes: ["harvester-node3"], cpu: .18 }],
      top_mem: [{ name: "frigate", ns: "lab", nodes: ["harvester-node2"], mem_mb: 1840 }, { name: "home-assistant", ns: "lab", nodes: ["harvester-node1"], mem_mb: 738 }, { name: "paperless", ns: "lab", nodes: ["harvester-node3"], mem_mb: 512 }] },
    "/api/history": history, "/api/storage": storage, "/api/volumes": volumes,
    "/api/nodes": nodes, "/api/node": url => nodes.find(n => n.name === url.searchParams.get("name")) || {},
    "/api/node/smart": url => {
      const node = nodes.find(n => n.name === url.searchParams.get("node"));
      const disk = node?.temps?.disks?.find(d => d.name === url.searchParams.get("disk"));
      if (!disk?.smart) return { error: "Demo disk not found" };
      return { ...disk.smart, health_assessment: disk.health };
    },
    "/api/volumes/delete-plan": volumeDeletePlan, "/api/hardware/features": hardware,
    "/api/namespaces": ["default", "lab", "monitoring"],
    "/api/storageclasses": ["harvester-longhorn", "longhorn-r2"],
    "/api/deploy/options": deployOptions,
    "/api/appstore": { total: 1, apps: [demoApp], sort: "popular", spotlight: demoApp },
    "/api/preview": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const joining = body.target_mode === "existing";
      const workloadName = body.workload_name || body.name;
      const containerName = body.container_name || body.name;
      return { deployment: { apiVersion: "apps/v1", kind: "Deployment",
          metadata: { name: joining ? body.target_workload : workloadName, namespace: body.namespace },
          spec: { template: { spec: { containers: [{ name: containerName, image: body.image }] } } } },
        service: null, impact: { mode: joining ? "existing" : "new", workload: joining ? body.target_workload : workloadName,
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
    "/api/images": { distinct: 3, protected: 2, retained: 2,
      node_names: ["harvester-node1", "harvester-node2", "harvester-node3"],
      nodes: [{ node: "harvester-node1", total_gb: 14.2, count: 38 },
        { node: "harvester-node2", total_gb: 11.9, count: 31 },
        { node: "harvester-node3", total_gb: 9.4, count: 27 }],
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
        { name: "docker.io/library/redis:7.2", names: [], digest: "sha256:" + "c".repeat(64),
          size_mb: 41, nodes: ["harvester-node1", "harvester-node2", "harvester-node3"],
          system: false, protected: false, retained_by: [] }] },
    "/api/images/prepull": { ok: true, daemonset: "homestead-pull-redis",
      nodes: ["harvester-node1", "harvester-node3"], skipped: ["harvester-node2"],
      message: "Pulling onto 2 nodes; skipped harvester-node2 (cordoned or not ready)" },
    "/api/images/prepull/stop": { ok: true, message: "Pre-pull homestead-pull-frigate stopped" },
    "/api/move/clusters": [{ name: "shed", url: "http://192.0.2.250:8088",
      user: "admin", added: "2026-09-22 14:05" }, { name: "attic", url: "http://192.0.2.60:8088",
      user: "admin", added: "2026-09-22 16:40" }, { name: "garage", url: "http://192.0.2.71:8088",
      user: "admin", added: "2026-09-22 17:02" }],
    "/api/move/clusters/check": (url, init) => {
      const name = JSON.parse(init?.body || "{}").name;
      if (name === "garage") return { name, version: "2.8.61", protocol: 1, local_version: "2.8.60",
        local_protocol: 1, state: "differs", compatible: true,
        message: "garage runs 2.8.61 and this one 2.8.60. Moves work between them; garage is the newer of the two." };
      return name === "attic"
        ? { name, version: "2.8.55", protocol: 0, local_version: "2.8.60", local_protocol: 1,
            state: "behind", compatible: false,
            message: "attic runs Homestead 2.8.55, too old to move workloads with this one (2.8.60). Update attic first." }
        : { name, version: "2.8.60", protocol: 1, local_version: "2.8.60", local_protocol: 1,
            state: "same", compatible: true, message: "Both run Homestead 2.8.60." };
    },
    "/api/move/clusters/add": [], "/api/move/clusters/remove": [],
    "/api/move/inventory": { namespace: "lab", movable: 2, workloads: [] },
    "/api/move/remote": { cluster: "shed", url: "http://192.0.2.250:8088",
      namespace: "lab", version: "2.8.60", protocol: 1, movable: 2, workloads: [
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
          size_gb: 32, storage_class: "longhorn-haos", access_modes: ["ReadWriteMany"] }] }] },
    "/api/move/plan": { ok: true, blockers: [], cluster: "shed", kind: "container", name: "frigate",
      namespace: "lab", joined: false, will_run: true, addresses: ["frigate on 192.0.2.242"],
      warnings: ["this cluster's Longhorn backup target changes from (none) to s3://homestead-backups@us-east-1/; backups already written to the old one stay there"],
      claims: [{ claim: "frigate-config", size_gb: 10, access_mode: "ReadWriteOnce",
        volume_mode: "Filesystem", backing_image: "" }], total_gb: 10 },
    "/api/move/start": { id: "d1", status: "running" },
    "/api/move/moves/retry": { ok: true }, "/api/move/moves/abandon": { ok: true },
    "/api/move/moves/finish": { ok: true, message: "mosquitto lives here now; removed workload mosquitto on shed" },
    "/api/move/moves": () => [
      { id: "d1", cluster: "shed", kind: "container", name: "frigate", source_namespace: "lab",
        namespace: "lab", status: "running", phase: "restoring", phase_index: 4,
        phases: ["joining", "quiescing", "backing-up", "syncing", "restoring", "creating", "starting", "done"],
        progress: 71, message: "Restoring 1 volume here: 54%", source_removed: false,
        created_at: new Date(Date.now() - 8 * 60e3).toISOString(), claims: [] },
      { id: "d0", cluster: "shed", kind: "container", name: "mosquitto", source_namespace: "lab",
        namespace: "lab", status: "succeeded", phase: "done", phase_index: 7,
        phases: ["joining", "quiescing", "backing-up", "syncing", "restoring", "creating", "starting", "done"],
        progress: 100, message: "mosquitto is running here; still stopped on shed until you remove it there",
        source_removed: false, created_at: new Date(Date.now() - 50 * 60e3).toISOString(), claims: [] }],
    "/api/objectstore": { deployed: true, ready: true, endpoint: "http://192.0.2.244:9000",
      reachable_off_cluster: true, bucket: "homestead-backups", size_gb: 100,
      backup_url: "s3://homestead-backups@us-east-1/",
      image: "quay.io/minio/minio:RELEASE.2024-09-22T00-33-43Z" },
    "/api/objectstore/deploy": { ok: true, endpoint: "http://192.0.2.244:9000",
      bucket: "homestead-backups", access_key: "homestead" },
    "/api/objectstore/longhorn": { url: "s3://homestead-backups@us-east-1/",
      secret: "homestead-backup-credentials", endpoint: "http://192.0.2.244:9000",
      reachable_off_cluster: true, detail: "Longhorn will back up here" },
    "/api/objectstore/remove": { ok: true, detail: "object storage removed" },
    "/api/vmimages": [],
    "/api/vms": [],
    "/api/sources": [{ name: "unraid", host: "192.0.2.10", user: "root", kind: "unraid", base_path: "/mnt/user/appdata", added: "2026-09-20 12:00" }],
    "/api/sources/containers": { containers: [{ name: "media-server", image: "example/media-server:latest", state: "running" }] },
    "/api/sources/browse": { entries: ["media-server", "home-automation"] },
    "/api/sources/inspect": { name: "media-server", image: "example/media-server:latest", remote_path: "/mnt/user/appdata/media-server",
      mount_path: "/config", ports: [{ container: 8096, host: 8096, protocol: "TCP", expose: true }],
      env: { PUID: "1000", PGID: "1000" }, hardware: [], network_mode: "loadbalancer",
      guessed_path: false, shm_mb: 512,
      mounts: [{ source: "/mnt/user/appdata/media-server", path: "/config", type: "bind" },
        { source: "/mnt/user/appdata/media-server/transcode", path: "/transcode", type: "bind" },
        { source: "/mnt/user/media", path: "/media", type: "bind" },
        { source: "", path: "/tmp/cache", type: "tmpfs", size_mb: 1000 }] },
    "/api/import": { ok: true, job: "homestead-import-media-server", pvc: "media-server-appdata",
      deployment: "media-server", note: "Deployment created stopped; start it once the copy job finishes." },
    "/api/imports/delete": { ok: true, message: "Import removed", removed: [] },
    "/api/imports/cleanup-plan": (url, init) => {
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
    "/api/image-updates/progress": { ns: "lab", name: "plex", phase: "progressing", desired: 1,
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
        detail: "tower:/mnt/user/appdata/plex/transcode -> /transcode",
        step_percent: 50, percent: 37.5, rate: "22.10MB/s" },
      { name: "homestead-import-obsidian", app: "obsidian", state: "failed", percent: 12,
        start: "2026-09-21T07:55:00Z", active: 0, succeeded: 0, failed: 1, step: 1, steps: 3,
        folder: "config", step_percent: 36, rate: "", error: "ran out of space on the volume",
        error_detail: 'rsync: [receiver] write failed on "/appdata/home-assistant_v2.db": No space left on device (28)' },
      { name: "homestead-import-krusader", app: "binhex-krusader", state: "done", percent: 100,
        start: "2026-09-21T08:12:00Z", end: "2026-09-21T08:19:00Z", active: 0, succeeded: 1, failed: 0 },
    ],
    "/api/lh/overview": lhOverview,
    "/api/lh/snapshots": [], "/api/lh/backups": lhBackups,
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
      { name: "harvester-longhorn", provisioner: "driver.longhorn.io", replicas: "3", migratable: true,
        expandable: true, reclaim: "Delete", default: true, internal: false, in_use: 2 },
      { name: "longhorn", provisioner: "driver.longhorn.io", replicas: "3", migratable: false,
        encrypted: true, expandable: true, reclaim: "Delete", default: false, internal: false, in_use: 0 },
      { name: "longhorn-r2", provisioner: "driver.longhorn.io", replicas: "2", migratable: true,
        expandable: true, reclaim: "Retain", default: false, internal: false, in_use: 7 },
      { name: "longhorn-static", provisioner: "driver.longhorn.io", replicas: "", migratable: false,
        expandable: false, reclaim: "Delete", default: false, internal: true, in_use: 0 },
    ],
    "/api/network/service/delete": { ok: true, freed: ["192.0.2.246:8989/TCP"],
      message: "Service lab/sonarr-old deleted, releasing 192.0.2.246:8989/TCP" },
    "/api/shares": shares,
    "/api/shares/options": { namespace: "lab", node: "harvester-node2",
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
      { id: "op3", kind: "update", title: "Update home-assistant", status: "running", progress: 62,
        message: "rolling out", started_at: new Date(Date.now() - 6e4).toISOString(),
        href: "/containers", resource: { kind: "Deployment", name: "home-assistant", namespace: "lab" } },
    ]),
    "/api/operations/dismiss": (url, init) => {
      const body = JSON.parse(init?.body || "{}");
      const before = (window.__demoOps || []).length;
      window.__demoOps = (window.__demoOps || []).filter(op => body.all
        ? !["succeeded", "failed", "cancelled"].includes(op.status)
        : op.id !== body.id);
      const gone = before - window.__demoOps.length;
      return { ok: true, dismissed: gone, remaining: window.__demoOps.length,
        detail: gone ? `cleared ${gone} finished job${gone === 1 ? "" : "s"}; 1 still running`
          : "nothing finished to clear" };
    },
    "/api/workloads": workloads, "/api/network": network,
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
    "/api/image-updates/scan-progress": () => {
      const at = (window.__demoScan = (window.__demoScan || 0) + 1);
      const total = 8, done = Math.min(total, at * 2);
      return { running: done < total, done, total, updates: Math.floor(done / 3),
        current: ["frigate", "home-assistant", "paperless", "samba"][at % 4],
        started_at: 0, finished_at: 0, elapsed: at * 0.5 };
    },
    "/api/image-updates": { checked_at: new Date().toISOString(), updates: 3, errors: 0,
      policy: { policy: "approval_required", allows_install: true, reason: "Explicit operator approval is required before rollout." },
      workloads: [{ ns: "lab", name: "frigate", available: true, can_rollback: true,
        images: [{ container: "frigate", deployed: "ghcr.io/blakeblackshear/frigate:stable", candidate: "ghcr.io/blakeblackshear/frigate:stable", candidate_tag: "stable", remote_digest: "sha256:abc", available: true }] },
      { ns: "lab", name: "home-assistant", available: true, can_rollback: false,
        images: [{ container: "home-assistant", deployed: "ghcr.io/home-assistant/home-assistant:2026.8", candidate: "ghcr.io/home-assistant/home-assistant:2026.9", candidate_tag: "2026.9", remote_digest: "sha256:def", available: true }] },
      { ns: "lab", name: "homestead", available: true, can_rollback: true,
        images: [{ container: "homestead", deployed: "ghcr.io/wjcloudy/homestead:2.8.29", candidate: "ghcr.io/wjcloudy/homestead:2.8.60", candidate_tag: "2.8.60", remote_digest: "sha256:ghi", available: true }] }] },
    "/api/flow": {
      nodes: nodes.map((n, i) => ({ id: `n:${n.name}`, name: n.name, copies: i === 0
        ? [{ vid: "v:home", vol: "home-assistant", running: true }, { vid: "v:paperless", vol: "paperless-data", running: true }]
        : i === 1 ? [{ vid: "v:frigate", vol: "frigate-config", running: true }, { vid: "v:home", vol: "home-assistant", running: true }]
        : [{ vid: "v:paperless", vol: "paperless-data", running: true }, { vid: "v:frigate", vol: "frigate-config", running: true }] })),
      volumes: [{ id: "v:frigate", name: "frigate-config", replicas: 2, size_gb: 20, robustness: "healthy", attached: "harvester-node2" },
        { id: "v:home", name: "home-assistant", replicas: 2, size_gb: 10, robustness: "healthy", attached: "harvester-node1" },
        { id: "v:paperless", name: "paperless-data", replicas: 2, size_gb: 100, robustness: "healthy", attached: "harvester-node3" }],
      workloads: [{ id: "w:frigate", name: "frigate", ns: "lab", kind: "container", node: "harvester-node2", hardware: ["igpu", "coral_usb"], uptime: 472221, cpu: .84, mem_mb: 1840, claims: [{ pvc: "frigate-config", vid: "v:frigate" }], ports: [{ name: "web", port: 5000, vip: "192.0.2.214" }] },
        { id: "w:home", name: "home-assistant", ns: "lab", kind: "container", node: "harvester-node1", hardware: [], uptime: 912400, cpu: .31, mem_mb: 738, claims: [{ pvc: "home-assistant", vid: "v:home" }], ports: [{ name: "web", port: 8123, vip: "192.0.2.215" }] },
        { id: "w:paperless", name: "paperless", ns: "lab", kind: "container", node: "harvester-node3", hardware: [], uptime: 220190, cpu: .18, mem_mb: 512, claims: [{ pvc: "paperless-data", vid: "v:paperless" }], ports: [{ name: "web", port: 8000, vip: "192.0.2.216" }] }],
      vips: [{ id: "i:192.0.2.214", ip: "192.0.2.214", ports: [{ app: "frigate", port: 5000 }] },
        { id: "i:192.0.2.215", ip: "192.0.2.215", ports: [{ app: "home-assistant", port: 8123 }] },
        { id: "i:192.0.2.216", ip: "192.0.2.216", ports: [{ app: "paperless", port: 8000 }] }],
    },
  };

  const original = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.origin);
    if (!url.pathname.startsWith("/api/")) return original(input, init);
    const key = url.pathname === "/api/image-updates" ? "/api/image-updates" : url.pathname;
    const configured = responses[key];
    const value = typeof configured === "function" ? configured(url, init) : configured;
    if (value === undefined) return new Response(JSON.stringify({ error: `Demo endpoint not available: ${url.pathname}` }), { status: 404, headers: { "Content-Type": "application/json" } });
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  };
})();

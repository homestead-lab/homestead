/* Deterministic, read-only demo transport used only by release screenshot CI.
   It never contacts a cluster and is inert unless ?demo=1 is present. */
(function () {
  if (new URLSearchParams(location.search).get("demo") !== "1") return;

  const smartDisk = (name, model, serial, temperature, powerHours) => ({
    name, path: `/dev/${name}`, available: true, protocol: name.startsWith("nvme") ? "NVMe" : "ATA",
    model, serial, firmware: "1.0", capacity_gb: name.startsWith("nvme") ? 465.8 : 931.5,
    smart_enabled: true, health: "passed", temperature_c: temperature, power_on_hours: powerHours,
    reallocated: name.startsWith("nvme") ? null : 0, pending: name.startsWith("nvme") ? null : 0,
    uncorrectable: name.startsWith("nvme") ? null : 0, error_count: 0,
    media_errors: name.startsWith("nvme") ? 0 : null, supported_tests: ["short", "long"],
    test: { active: false, status: "No self-test running", remaining_percent: null },
    self_tests: [{ type: "Short offline", status: "Completed without error", lifetime_hours: powerHours - 12,
      signature: `short|ok|${powerHours - 12}` }],
  });

  const nodes = [
    { name: "harvester-node1", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 22.4, cpu_used: 1.79, cpu_cap: 8, mem_pct: 61.7, mem_used_gb: 9.6, mem_cap_gb: 15.6,
      fs_pct: 36.2, fs_used_gb: 168, fs_cap_gb: 464, rx_mbps: 8.4, tx_mbps: 3.1,
      pods: 54, pods_sys: 46, pods_wl: 8, vms: 1, workloads: ["home-assistant", "mosquitto", "samba"],
      hardware: { igpu: true }, temps: { cpu_c: 39, max_c: 51, sensors: 4, smart_helper: { available: true },
        disks: [{ name: "nvme0n1", model: "Samsung SSD 970 EVO Plus", serial: "DEMO-NVME-01", kind: "NVMe", size_gb: 465.8,
          read_mbps: 18.42, write_mbps: 6.17, smart: smartDisk("nvme0n1", "Samsung SSD 970 EVO Plus", "DEMO-NVME-01", 41, 8421) }] } },
    { name: "harvester-node2", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 41.8, cpu_used: 3.34, cpu_cap: 8, mem_pct: 54.1, mem_used_gb: 8.4, mem_cap_gb: 15.6,
      fs_pct: 28.5, fs_used_gb: 132, fs_cap_gb: 464, rx_mbps: 21.9, tx_mbps: 12.6,
      pods: 47, pods_sys: 42, pods_wl: 5, vms: 0, workloads: ["frigate", "homestead"],
      hardware: { igpu: true, coral_usb: true }, temps: { cpu_c: 34, max_c: 47, sensors: 5, smart_helper: { available: true },
        disks: [{ name: "sda", model: "WDC WD100EFAX", serial: "DEMO-SATA-02", kind: "HDD", size_gb: 931.5,
          read_mbps: 3.26, write_mbps: 12.91, smart: smartDisk("sda", "WDC WD100EFAX", "DEMO-SATA-02", 36, 16420) }] } },
    { name: "harvester-node3", status: "Ready", roles: ["worker"], schedulable: true,
      cpu_pct: 16.3, cpu_used: 0.65, cpu_cap: 4, mem_pct: 46.2, mem_used_gb: 7.2, mem_cap_gb: 15.6,
      fs_pct: 31.1, fs_used_gb: 144, fs_cap_gb: 464, rx_mbps: 5.8, tx_mbps: 2.4,
      pods: 31, pods_sys: 28, pods_wl: 3, vms: 0, workloads: ["paperless"],
      hardware: {}, temps: { cpu_c: 36, max_c: 45, sensors: 3, smart_helper: { available: true },
        disks: [{ name: "nvme0n1", model: "Kingston NV2", serial: "DEMO-NVME-03", kind: "NVMe", size_gb: 465.8,
          read_mbps: 0.74, write_mbps: 1.15, smart: smartDisk("nvme0n1", "Kingston NV2", "DEMO-NVME-03", 38, 3912) }] } },
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
    provisioned_gb: 670, actual_gb: 224, volumes: 7, healthy: 7, degraded: 0,
    faulted: 0, unknown: 0, attached: 6, disks: [] };
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
    { name: "pvc-demo-scratch", pvc_name: "scratch-test", namespace: "lab", attached_to: "",
      pod_status: "", state: "detached", robustness: "unknown", node: "",
      size_gb: 5, actual_gb: 0.2, used_pct: 4, replicas: 2,
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
      url: "nfs://backup.example.invalid:/homestead", reason: "", interval: "5m", secret: "" },
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
      ready_endpoints: 1, not_ready_endpoints: 0, health: "healthy", reason: "1 ready endpoint" })),
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
      ] : [],
      active_consumers: attached ? 2 : 0,
      snapshots: { count: attached ? 3 : 1, names: ["daily"] },
      backups: { count: 1, names: ["nightly"] },
      data_present: true, inventory_complete: true, warnings: [],
      blocked: attached,
      blocking_reasons: attached ? [
        "2 active workload reference(s) must be stopped and unmounted first",
        "Longhorn still reports the volume attached to harvester-node2",
      ] : [],
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
    "/api/auth/state": { setup: false, user: "demo", role: "admin" },
    "/api/settings": { thresholds: { cpu: { warning: 70, critical: 88 }, memory: { warning: 70, critical: 88 }, disk: { warning: 75, critical: 90 }, temperature: { warning: 70, critical: 85 } }, smart: { temperature: { warning: 55, critical: 65 }, reallocated_warning: 1, pending_critical: 1, uncorrectable_critical: 1, notify_failures: true }, updates: { policy: "approval_required", notify_available: true, notify_failures: true } },
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
      return disk?.smart || { error: "Demo disk not found" };
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
    "/api/vmimages": [],
    "/api/vms": [],
    "/api/sources": [{ name: "unraid", host: "192.0.2.10", user: "root", kind: "unraid", base_path: "/mnt/user/appdata", added: "2026-09-20 12:00" }],
    "/api/sources/containers": { containers: [{ name: "media-server", image: "example/media-server:latest", state: "running" }] },
    "/api/sources/browse": { entries: ["media-server", "home-automation"] },
    "/api/sources/inspect": { name: "media-server", image: "example/media-server:latest", remote_path: "/mnt/user/appdata/media-server",
      mount_path: "/config", ports: [{ container: 8096, host: 8096, protocol: "TCP", expose: true }],
      env: { PUID: "1000", PGID: "1000" }, hardware: [], network_mode: "loadbalancer", mounts: [] },
    "/api/imports": [],
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
    "/api/operations": [], "/api/workloads": workloads, "/api/network": network,
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
    "/api/image-updates": { checked_at: "2026-09-19T12:00:00Z", updates: 3, errors: 0,
      policy: { policy: "approval_required", allows_install: true, reason: "Explicit operator approval is required before rollout." },
      workloads: [{ ns: "lab", name: "frigate", available: true, can_rollback: true,
        images: [{ container: "frigate", deployed: "ghcr.io/blakeblackshear/frigate:stable", candidate: "ghcr.io/blakeblackshear/frigate:stable", candidate_tag: "stable", remote_digest: "sha256:abc", available: true }] },
      { ns: "lab", name: "home-assistant", available: true, can_rollback: false,
        images: [{ container: "home-assistant", deployed: "ghcr.io/home-assistant/home-assistant:2026.8", candidate: "ghcr.io/home-assistant/home-assistant:2026.9", candidate_tag: "2026.9", remote_digest: "sha256:def", available: true }] },
      { ns: "lab", name: "homestead", available: true, can_rollback: true,
        images: [{ container: "homestead", deployed: "ghcr.io/wjcloudy/homestead:2.8.8", candidate: "ghcr.io/wjcloudy/homestead:2.8.9", candidate_tag: "2.8.9", remote_digest: "sha256:ghi", available: true }] }] },
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

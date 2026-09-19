/* Deterministic, read-only demo transport used only by release screenshot CI.
   It never contacts a cluster and is inert unless ?demo=1 is present. */
(function () {
  if (new URLSearchParams(location.search).get("demo") !== "1") return;

  const nodes = [
    { name: "harvester-node1", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 22.4, cpu_cap: 8, mem_pct: 61.7, mem_used_gb: 9.6, mem_cap_gb: 15.6,
      fs_pct: 36.2, fs_used_gb: 168, fs_cap_gb: 464, rx_mbps: 8.4, tx_mbps: 3.1,
      pods: 54, pods_sys: 46, pods_wl: 8, vms: 1, workloads: ["home-assistant", "mosquitto", "samba"],
      hardware: { igpu: true }, temps: { cpu_c: 39, max_c: 51 } },
    { name: "harvester-node2", status: "Ready", roles: ["control-plane", "etcd"], schedulable: true,
      cpu_pct: 41.8, cpu_cap: 8, mem_pct: 54.1, mem_used_gb: 8.4, mem_cap_gb: 15.6,
      fs_pct: 28.5, fs_used_gb: 132, fs_cap_gb: 464, rx_mbps: 21.9, tx_mbps: 12.6,
      pods: 47, pods_sys: 42, pods_wl: 5, vms: 0, workloads: ["frigate", "homestead"],
      hardware: { igpu: true, coral_usb: true }, temps: { cpu_c: 34, max_c: 47 } },
    { name: "harvester-node3", status: "Ready", roles: ["worker"], schedulable: true,
      cpu_pct: 16.3, cpu_cap: 4, mem_pct: 46.2, mem_used_gb: 7.2, mem_cap_gb: 15.6,
      fs_pct: 31.1, fs_used_gb: 144, fs_cap_gb: 464, rx_mbps: 5.8, tx_mbps: 2.4,
      pods: 31, pods_sys: 28, pods_wl: 3, vms: 0, workloads: ["paperless"],
      hardware: {}, temps: { cpu_c: 36, max_c: 45 } },
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
    { name: "pvc-demo-frigate", pvc_name: "frigate-config", namespace: "lab", attached_to: "frigate",
      pod_status: "Running", state: "attached", robustness: "healthy", node: "harvester-node2",
      size_gb: 20, actual_gb: 3.8, used_pct: 19, replicas: 2,
      access_modes: ["ReadWriteOnce"], storage_class: "longhorn-r2", last_used_secs: 0 },
    { name: "pvc-demo-scratch", pvc_name: "scratch-test", namespace: "lab", attached_to: "",
      pod_status: "", state: "detached", robustness: "unknown", node: "",
      size_gb: 5, actual_gb: 0.2, used_pct: 4, replicas: 2,
      access_modes: ["ReadWriteOnce"], storage_class: "longhorn-r2", last_used_secs: 2400 },
  ];
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
  const responses = {
    "/api/auth/state": { setup: false, user: "demo", role: "admin" },
    "/api/settings": { thresholds: { cpu: { warning: 70, critical: 88 }, memory: { warning: 70, critical: 88 }, disk: { warning: 75, critical: 90 }, temperature: { warning: 70, critical: 85 } }, updates: { policy: "approval_required", notify_available: true, notify_failures: true } },
    "/api/overview": { health: "healthy", health_state: "healthy", health_summary: "All cluster services are healthy", health_issues: [],
      cpu_pct: 27.2, cpu_used: 5.4, cpu_cap: 20, mem_pct: 54.0, mem_used_gb: 25.2, mem_cap_gb: 46.8,
      nodes_ready: 3, nodes_total: 3, workload_pods: 16, system_pods: 116, lb_ip: "192.0.2.242", nodes,
      top_cpu: [{ name: "frigate", ns: "lab", nodes: ["harvester-node2"], cpu: .84 }, { name: "home-assistant", ns: "lab", nodes: ["harvester-node1"], cpu: .31 }, { name: "paperless", ns: "lab", nodes: ["harvester-node3"], cpu: .18 }],
      top_mem: [{ name: "frigate", ns: "lab", nodes: ["harvester-node2"], mem_mb: 1840 }, { name: "home-assistant", ns: "lab", nodes: ["harvester-node1"], mem_mb: 738 }, { name: "paperless", ns: "lab", nodes: ["harvester-node3"], mem_mb: 512 }] },
    "/api/history": history, "/api/storage": storage, "/api/volumes": volumes,
    "/api/volumes/delete-plan": volumeDeletePlan, "/api/hardware/features": hardware,
    "/api/operations": [], "/api/workloads": workloads,
    "/api/image-updates": { checked_at: "2026-09-19T12:00:00Z", updates: 1, errors: 0,
      policy: { policy: "approval_required", allows_install: true, reason: "Explicit operator approval is required before rollout." },
      workloads: [{ ns: "lab", name: "frigate", available: true, can_rollback: true,
        images: [{ container: "frigate", deployed: "ghcr.io/blakeblackshear/frigate:stable", candidate: "ghcr.io/blakeblackshear/frigate:stable", candidate_tag: "stable", remote_digest: "sha256:abc", available: true }] }] },
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

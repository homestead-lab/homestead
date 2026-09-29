#!/usr/bin/env python3
"""Read-only physical console. No web server, credentials or shell shortcuts."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time


def clean(value):
    # Kubernetes names and terminal output must never become terminal controls.
    return "".join(c for c in str(value) if c.isprintable())[:240]


def command(args):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def gib(size):
    return f"{size / (1024 ** 3):.1f} GiB"


def cpu_ticks(text):
    line = next((x for x in text.splitlines() if x.startswith("cpu ")), "")
    values = [int(x) for x in line.split()[1:9]]
    return (sum(values), sum(values[3:5])) if len(values) >= 4 else None


def memory(text):
    values = dict((key, int(value)) for key, value in
                  re.findall(r"^(\w+):\s+(\d+)", text, re.M))
    total = values.get("MemTotal", 0) * 1024
    available = values.get("MemAvailable", values.get("MemFree", 0)) * 1024
    return max(0, total - available), total


def disk_stats():
    # Only local data filesystems. Never walk container mounts or remote shares.
    paths = ["/"]
    for line in read("/proc/mounts").splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] in {"ext4", "ext3", "xfs", "btrfs", "zfs"}:
            path = fields[1].replace("\\040", " ")
            if not any(part in path for part in ("/pods/", "/containerd/", "/docker/", "/kubelet/")):
                paths.append(path)
    rows = []
    seen = set()
    for path in paths:
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            usage = shutil.disk_usage(path)
            seen.add(device)
            rows.append(f"{clean(path):<22} {gib(usage.used)} / {gib(usage.total)}"
                        f"  {100 * usage.used / max(1, usage.total):.0f}%")
        except OSError:
            continue
    return rows


def kube_command():
    for dist, binary in (("k3s", "/usr/local/bin/k3s"),
                         ("rke2", "/var/lib/rancher/rke2/bin/kubectl")):
        config = f"/etc/rancher/{dist}/{dist}.yaml"
        if Path(config).is_file() and Path(binary).is_file():
            prefix = [binary, "kubectl"] if dist == "k3s" else [binary]
            return prefix + ["--kubeconfig", config, "--request-timeout=3s"]
    return None


def node_rows(data):
    rows = []
    ready = 0
    for node in data.get("items", []):
        meta = node.get("metadata", {})
        status = node.get("status", {})
        good = any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in status.get("conditions", []))
        ready += good
        labels = meta.get("labels", {})
        role = "server" if any("node-role.kubernetes.io/" + r in labels
                               for r in ("control-plane", "master")) else "worker"
        address = next((a.get("address", "") for a in status.get("addresses", [])
                        if a.get("type") == "InternalIP"), "-")
        state = "Ready" if good else "NotReady"
        if node.get("spec", {}).get("unschedulable"):
            state += "/cordoned"
        rows.append(clean(f"{meta.get('name', '?'):<20} {state:<18} {role:<7} {address}"
                          f"  {status.get('nodeInfo', {}).get('kubeletVersion', '')}"))
    return f"{ready}/{len(rows)} nodes Ready", rows


def cluster_stats():
    prefix = kube_command()
    if not prefix:
        return ["Cluster: no local server kubeconfig (worker or not yet installed).",
                "View cluster health in Homestead on a server node."]
    raw = command(prefix + ["get", "nodes", "-o", "json"])
    if raw is None:
        return ["Cluster: API unavailable; retrying automatically."]
    try:
        status, rows = node_rows(json.loads(raw))
    except (ValueError, TypeError, AttributeError):
        return ["Cluster: could not read node status; retrying automatically."]
    result = ["Cluster: " + status, *rows]
    # Only request the public Service address, never Secrets or kubeconfig data.
    raw = command(prefix + ["-n", "lab", "get", "service", "homestead", "-o", "json"])
    if raw:
        try:
            svc = json.loads(raw)
            ingress = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            host = next((x.get("ip") or x.get("hostname") for x in ingress
                         if x.get("ip") or x.get("hostname")), None)
            ports = svc.get("spec", {}).get("ports", [])
            if host and ports:
                host = f"[{host}]" if ":" in host else host
                result.insert(1, clean(f"Homestead: http://{host}:{ports[0]['port']}"))
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
    return result


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.local = ["Reading host statistics..."]
        self.cluster = ["Reading cluster status..."]
        self.updated = None
        self.stop = threading.Event()

    def collect_local(self):
        previous = None
        while not self.stop.is_set():
            ticks = cpu_ticks(read("/proc/stat"))
            cpu = "sampling"
            if ticks and previous and ticks[0] > previous[0]:
                busy = 100 * (1 - (ticks[1] - previous[1]) / (ticks[0] - previous[0]))
                cpu = f"{max(0, min(100, busy)):.0f}%"
            previous = ticks
            used, total = memory(read("/proc/meminfo"))
            uptime = read("/proc/uptime").split()
            hours = int(float(uptime[0]) / 3600) if uptime else 0
            addresses = clean(command(["hostname", "-I"]) or "unavailable")
            services = []
            for service in ("k3s", "k3s-agent", "rke2-server", "rke2-agent"):
                state = command(["systemctl", "is-active", service])
                if state:
                    services.append(f"{service}: {clean(state)}")
            rows = [f"Host: {clean(socket.gethostname())}    Uptime: {hours // 24}d {hours % 24}h",
                    f"Addresses: {addresses}",
                    "Kubernetes: " + (", ".join(services) or "no active service"), "",
                    f"CPU: {cpu} of {os.cpu_count() or 1} cores    "
                    f"RAM: {gib(used)} / {gib(total)}  {100 * used / max(1, total):.0f}%",
                    "Disk usage (local filesystems):", *disk_stats()]
            with self.lock:
                self.local = rows
            self.stop.wait(2)

    def collect_cluster(self):
        while not self.stop.is_set():
            rows = cluster_stats()
            with self.lock:
                self.cluster = rows
                self.updated = time.strftime("%H:%M:%S")
            self.stop.wait(15)

    def snapshot(self):
        with self.lock:
            return self.local + ["", "CLUSTER STATUS" +
                                   (f"  checked {self.updated}" if self.updated else ""),
                                   *self.cluster]


DEMO = ["Host: node-1    Uptime: 12d 6h", "Addresses: 192.0.2.10",
        "Kubernetes: k3s: active", "", "CPU: 24% of 8 cores    RAM: 12.4 GiB / 32.0 GiB  39%",
        "Disk usage (local filesystems):", "/                      28.0 GiB / 120.0 GiB  23%",
        "/var/lib/longhorn      360.0 GiB / 960.0 GiB  38%", "", "CLUSTER STATUS  checked 12:30:00",
        "Cluster: 3/3 nodes Ready", "Homestead: http://192.0.2.100:8088",
        "node-1               Ready              server  192.0.2.10  v1.34.1+k3s1",
        "node-2               Ready              server  192.0.2.11  v1.34.1+k3s1",
        "node-3               Ready              worker  192.0.2.12  v1.34.1+k3s1"]


def screen(window, monitor, demo=False):
    import curses
    title_style = curses.A_BOLD
    if curses.has_colors():
        curses.start_color()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
        title_style |= curses.color_pair(1)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    window.timeout(250)
    window.keypad(True)
    offset = 0
    while True:
        height, width = window.getmaxyx()
        rows = DEMO if demo else monitor.snapshot()
        capacity = max(1, height - 5)
        offset = min(offset, max(0, len(rows) - capacity))
        window.erase()

        def line(y, value, attr=0):
            if 0 <= y < height and width > 1:
                try:
                    window.addnstr(y, 0, clean(value), width - 1, attr)
                except curses.error:
                    pass

        line(0, "HOMESTEAD  |  Host & cluster status".ljust(width - 1), title_style)
        line(1, "Read-only console  -  refreshes automatically")
        for y, value in enumerate(rows[offset:offset + capacity], 3):
            line(y, value, curses.A_BOLD if value.startswith("CLUSTER STATUS") else 0)
        line(height - 1, "Enter / Q / Esc: login   Up/Down: scroll   PgUp/PgDn: page", curses.A_REVERSE)
        window.refresh()
        key = window.getch()
        if key in (ord("q"), ord("Q"), 27, 10, 13, curses.KEY_ENTER):
            return
        if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
            offset += capacity if key == curses.KEY_NPAGE else 1
        elif key in (curses.KEY_UP, curses.KEY_PPAGE):
            offset = max(0, offset - (capacity if key == curses.KEY_PPAGE else 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="show fictional example data")
    args = parser.parse_args()
    monitor = Monitor()
    if not args.demo:
        for target in (monitor.collect_local, monitor.collect_cluster):
            threading.Thread(target=target, daemon=True).start()
    try:
        import curses
        curses.wrapper(screen, monitor, args.demo)
    except (ImportError, OSError, KeyboardInterrupt):
        pass  # The getty wrapper always proceeds to the authenticated login.
    finally:
        monitor.stop.set()


if __name__ == "__main__":
    main()

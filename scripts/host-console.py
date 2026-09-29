#!/usr/bin/env python3
"""Read-only physical console. No web server, credentials or shell shortcuts."""
import argparse
from collections import deque
from functools import lru_cache
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


def cpu_samples(text):
    return {line.split()[0]: cpu_ticks("cpu " + " ".join(line.split()[1:]))
            for line in text.splitlines() if re.match(r"^cpu(?:\d+)?\s", line)}


def cpu_percent(current, previous):
    if not current or not previous or current[0] <= previous[0]:
        return None
    return max(0, min(100, 100 * (1 - (current[1] - previous[1]) /
                                  (current[0] - previous[0]))))


def percent(used, total):
    return max(0, min(100, 100 * used / total)) if total else None


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
            rows.append({"path": clean(path), "used": usage.used, "total": usage.total})
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
        rows.append({"name": clean(meta.get("name", "?")), "state": state,
                     "health": "bad" if not good else "warn" if node.get("spec", {}).get("unschedulable") else "ok",
                     "role": role, "address": clean(address),
                     "version": clean(status.get("nodeInfo", {}).get("kubeletVersion", ""))})
    return f"{ready}/{len(rows)} nodes Ready", rows


def cluster_stats():
    prefix = kube_command()
    if not prefix:
        return {"health": "muted", "summary": "Local view: worker or no server kubeconfig",
                "detail": "View cluster health in Homestead on a server node.", "nodes": []}
    raw = command(prefix + ["get", "nodes", "-o", "json"])
    if raw is None:
        return {"health": "bad", "summary": "API unavailable", "detail": "Retrying automatically", "nodes": []}
    try:
        status, rows = node_rows(json.loads(raw))
    except (ValueError, TypeError, AttributeError):
        return {"health": "warn", "summary": "Could not read node status",
                "detail": "Retrying automatically", "nodes": []}
    health = "bad" if any(n["health"] == "bad" for n in rows) else "warn" if not rows or any(n["health"] == "warn" for n in rows) else "ok"
    result = {"health": health, "summary": status, "nodes": rows}
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
                result["url"] = clean(f"http://{host}:{ports[0]['port']}")
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
    return result


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.local = {"hostname": clean(socket.gethostname()), "cpu": None,
                      "cores": [], "history": [], "ram_used": 0, "ram_total": 0,
                      "disks": [], "services": [], "addresses": "Reading host statistics..."}
        self.cluster = {"health": "muted", "summary": "Reading cluster status...", "nodes": []}
        self.updated = None
        self.stop = threading.Event()

    def collect_local(self):
        previous = {}
        history = deque(maxlen=180)
        while not self.stop.is_set():
            ticks = cpu_samples(read("/proc/stat"))
            cpu = cpu_percent(ticks.get("cpu"), previous.get("cpu"))
            cores = [(key[3:], cpu_percent(value, previous.get(key)))
                     for key, value in ticks.items() if key != "cpu"]
            if cpu is not None:
                history.append(cpu)
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
            data = {"hostname": clean(socket.gethostname()), "uptime": f"{hours // 24}d {hours % 24}h",
                    "addresses": addresses, "services": services, "cpu": cpu,
                    "cores": cores, "history": list(history),
                    "load": " ".join(read("/proc/loadavg").split()[:3]),
                    "ram_used": used, "ram_total": total, "disks": disk_stats()}
            with self.lock:
                self.local = data
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
            return {"local": self.local, "cluster": self.cluster, "updated": self.updated}


def demo_data(unhealthy=False):
    unit = 1024 ** 3
    return {"local": {"hostname": "node-1", "uptime": "12d 6h", "addresses": "192.0.2.10",
                      "services": ["k3s: active"], "cpu": 83 if unhealthy else 24,
                      "cores": [(str(i), x) for i, x in enumerate([75, 91, 83, 67, 94, 80, 89, 85] if unhealthy else [22, 31, 12, 40, 18, 27, 20, 22])],
                      "history": [18, 23, 16, 35, 42, 66, 78, 62, 54, 33, 29, 18, 24, 40, 56, 68,
                                  83, 57, 41, 26, 22, 19, 24, 20, 24] * 3 + ([83] if unhealthy else []), "load": "1.39 1.44 1.47",
                      "ram_used": (30 if unhealthy else 12.4) * unit, "ram_total": 32 * unit,
                      "disks": [{"path": "/", "used": 28 * unit, "total": 120 * unit},
                                {"path": "/var/lib/longhorn", "used": (912 if unhealthy else 360) * unit,
                                 "total": 960 * unit}]},
            "cluster": {"health": "bad" if unhealthy else "ok",
                        "summary": "2/3 nodes Ready" if unhealthy else "3/3 nodes Ready",
                        "url": "http://192.0.2.100:8088",
                        "nodes": [{"name": f"node-{i+1}", "state": "NotReady" if unhealthy and i == 2 else "Ready",
                                   "health": "bad" if unhealthy and i == 2 else "ok",
                                   "role": "worker" if i == 2 else "server",
                                   "address": f"192.0.2.{10+i}", "version": "v1.34.1+k3s1"}
                                  for i in range(3)]}, "updated": "12:30:00"}


def usage_style(value):
    return "muted" if value is None else "bad" if value >= 90 else "warn" if value >= 75 else "ok"


# Unicode Braille uses a 2x4 raster per character, with dots 7/8 below 1-6.
# Mapping reference: github.com/agvxov/braille-art-conversions.
DOT_BITS = ((1, 8), (2, 16), (4, 32), (64, 128))


class DotCanvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.dots = [[None] * (width * 2) for _ in range(height * 4)]

    def dot(self, x, y, style):
        if 0 <= x < self.width * 2 and 0 <= y < self.height * 4:
            self.dots[y][x] = style

    def cells(self, braille=True):
        rows = []
        for y in range(self.height):
            row = []
            for x in range(self.width):
                mask, styles = 0, []
                for dy in range(4):
                    for dx in range(2):
                        style = self.dots[y * 4 + dy][x * 2 + dx]
                        if style:
                            mask |= DOT_BITS[dy][dx]
                            styles.append(style)
                # One terminal cell has one foreground colour. White logo
                # layers take priority; graphs use the hottest sample.
                style = next((s for s in ("text", "bad", "warn", "ok", "brand") if s in styles), "muted")
                row.append((chr(0x2800 + mask) if braille and mask else "#" if mask else " ", style))
            rows.append(row)
        return rows


def segment_distance(x, y, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    return ((x - a[0] - t * dx) ** 2 + (y - a[1] - t * dy) ** 2) ** .5


@lru_cache(maxsize=4)
def logo_cells(large=False, braille=True):
    # Rasterise the actual SVG strokes, rather than approximate the roof with
    # slashes. Twice as many columns as rows accounts for tall terminal cells.
    width, height = (16, 7) if large else (12, 5)
    canvas = DotCanvas(width, height)
    house = ((12, 29), (32, 13), (52, 29), (52, 52), (12, 52), (12, 29))
    layers = (((20, 34), (44, 34)), ((20, 43), (44, 43)))
    for y in range(height * 4):
        for x in range(width * 2):
            sx, sy = 8 + (x + .5) * 48 / (width * 2), 8 + (y + .5) * 48 / (height * 4)
            if any(segment_distance(sx, sy, a, b) <= 2.75 for a, b in zip(house, house[1:])):
                canvas.dot(x, y, "brand")
            if any(segment_distance(sx, sy, a, b) <= 2.75 for a, b in layers):
                canvas.dot(x, y, "text")
    return canvas.cells(braille)


class Frame:
    """Clipped cells, with semantic styles independent of terminal colour support."""
    def __init__(self, width, height, braille=True):
        self.width = max(1, width)
        self.rows = [[(" ", "text") for _ in range(self.width)] for _ in range(height)]
        self.header = 0
        self.braille = braille

    def put(self, y, x, text, style="text", limit=None):
        if 0 <= y < len(self.rows):
            for i, char in enumerate(clean(text)[:limit]):
                if 0 <= x + i < self.width:
                    self.rows[y][x + i] = (char, style)

    def box(self, y, x, height, width, title):
        if width < 3 or height < 3:
            return
        for row in (y, y + height - 1):
            self.put(row, x, "+" + "-" * (width - 2) + "+", "border")
        for row in range(y + 1, y + height - 1):
            self.put(row, x, "|", "border")
            self.put(row, x + width - 1, "|", "border")
        self.put(y, x + 2, " " + title + " ", "accent", max(0, width - 4))

    def bar(self, y, x, width, value):
        if width < 2:
            return
        value = None if value is None else max(0, min(100, value))
        self.put(y, x, "[" + " " * (width - 2) + "]", "muted")
        canvas = DotCanvas(width - 2, 1)
        fill = round((width - 2) * 2 * (value or 0) / 100)
        for col in range(fill):
            for row in range(4):
                canvas.dot(col, row, usage_style(value))
        for col, (char, style) in enumerate(canvas.cells(self.braille)[0]):
            self.put(y, x + 1 + col, char, style)

    def graph(self, y, x, width, height, samples):
        canvas = DotCanvas(width, height)
        samples = samples[-width * 2:]
        for col, sample in enumerate(samples):
            if sample is None:
                continue
            fill = round(max(0, min(100, sample)) * height * 4 / 100)
            for row in range(height * 4 - fill, height * 4):
                canvas.dot(width * 2 - len(samples) + col, row, usage_style(sample))
        for row, cells in enumerate(canvas.cells(self.braille)):
            for col, (char, style) in enumerate(cells):
                self.put(y + row, x + col, char, style)

    def text(self):
        return ["".join(char for char, _ in row) for row in self.rows]


def dashboard(data, width, height, braille=True):
    """Panels fit an 80x24 host console; narrow screens stack and scroll."""
    local, cluster = data["local"], data["cluster"]
    disks, nodes = local.get("disks", []), cluster.get("nodes", [])
    wide = width >= 72
    logo = logo_cells(height >= 28 and width >= 60, braille)
    header_h = len(logo)
    cpu_h = max(6, (height - header_h - 1) // 3) if wide else 8
    metrics_h = max(6, 2 + 2 * len(disks))
    cpu_y = header_h
    ram_y = cpu_y + cpu_h
    disk_y = ram_y if wide else ram_y + 6
    cluster_y = ram_y + metrics_h if wide else disk_y + metrics_h
    badge = {"ok": "[OK]", "warn": "[!]", "bad": "[FAIL]", "muted": "[--]"}[cluster.get("health", "muted")]
    summary = badge + " " + cluster["summary"]
    compact_cluster = wide and height < 28 and bool(cluster.get("url")) and len(summary) + len(cluster["url"]) + 3 <= width - 4
    node_start = 2 if compact_cluster else 3
    cluster_h = max(4, len(nodes) * (1 if wide else 2) + node_start + 1, height - 1 - cluster_y)
    frame = Frame(width, cluster_y + cluster_h, braille)
    frame.header = header_h
    header_x = max(len(row) for row in logo) + 4
    for y, row in enumerate(logo):
        for x, (char, style) in enumerate(row):
            frame.put(y, 1 + x, char, style)
    frame.put(0, header_x, "HOMESTEAD / host & cluster" if width >= 60 else "HOMESTEAD", "accent")
    if width >= 64:
        frame.put(0, width - 9, time.strftime("%H:%M:%S"), "muted")
    host_y = 1
    if header_h >= 5:
        frame.put(1, header_x, "Live resources + Kubernetes readiness", "muted")
        host_y = 2
    frame.put(host_y, header_x, f"{local['hostname']}   up {local.get('uptime', '--')}")
    frame.put(host_y + 1, header_x, local.get("addresses", ""), "muted")
    service = ", ".join(local.get("services", []))
    frame.put(host_y + 2, header_x, "[OK] " + service if service else "[!] No active Kubernetes service",
              "ok" if service else "warn")

    frame.box(cpu_y, 0, cpu_h, width, "CPU / history")
    inner = max(1, width - 4)
    graph_w = (inner // 2) if wide else inner
    cpu = local.get("cpu")
    frame.put(cpu_y + 1, 2, "sampling" if cpu is None else f"{cpu:.0f}% busy", usage_style(cpu), graph_w)
    frame.put(cpu_y + 1, 13, "load " + local.get("load", "--"), "muted", max(0, graph_w - 11))
    frame.bar(cpu_y + 2, 2, graph_w, cpu)
    graph_h = cpu_h - 4
    frame.graph(cpu_y + 3, 2, graph_w, graph_h, local.get("history", []))
    if wide:
        cores = local.get("cores", [])
        core_x = graph_w + 4
        core_w = max(1, (width - core_x - 2) // 2)
        frame.put(cpu_y, core_x, f" {len(cores) or os.cpu_count() or 1} CORES ", "accent")
        capacity = (cpu_h - 2) * 2
        for i, (name, value) in enumerate(cores[:capacity]):
            y = cpu_y + 1 + i // 2
            x = core_x + (i % 2) * core_w
            frame.put(y, x, "C" + name, "muted", 4)
            frame.bar(y, x + 4, max(2, core_w - 10), value)
            frame.put(y, x + core_w - 5, " --%" if value is None else f"{value:3.0f}%", usage_style(value))
        if len(cores) > capacity:
            frame.put(cpu_y + cpu_h - 1, core_x, f" +{len(cores) - capacity} cores ", "muted", width - core_x - 2)

    ram_w = width // 2 if wide else width
    disk_x = ram_w if wide else 0
    disk_w = width - disk_x
    frame.box(ram_y, 0, metrics_h if wide else 6, ram_w, "MEMORY")
    used, total = local.get("ram_used", 0), local.get("ram_total", 0)
    ram = percent(used, total)
    frame.put(ram_y + 1, 2, f"{gib(used)} / {gib(total)}", limit=max(0, ram_w - 4))
    frame.bar(ram_y + 2, 2, max(0, ram_w - 11), ram)
    frame.put(ram_y + 2, ram_w - 7, " --%" if ram is None else f"{ram:3.0f}%", usage_style(ram))
    frame.put(ram_y + 3, 2, "Available " + gib(max(0, total - used)), "muted", max(0, ram_w - 4))
    frame.put(ram_y + 4, 2, "High memory usage" if ram is not None and ram >= 90 else "Usage elevated" if ram is not None and ram >= 75 else "Memory available" if ram is not None else "Waiting for metrics",
              usage_style(ram), max(0, ram_w - 4))
    frame.box(disk_y, disk_x, metrics_h, disk_w, "DISKS / used")
    if not disks:
        frame.put(disk_y + 1, disk_x + 2, "Waiting for metrics", "muted", max(0, disk_w - 4))
    for i, disk in enumerate(disks):
        y = disk_y + 1 + i * 2
        usage = percent(disk["used"], disk["total"])
        frame.put(y, disk_x + 2, disk["path"], limit=max(0, disk_w - 4))
        bar_w = max(2, disk_w - 22)
        frame.bar(y + 1, disk_x + 2, bar_w, usage)
        frame.put(y + 1, disk_x + 3 + bar_w,
                  f"{disk['used'] / 1024 ** 3:.0f}/{disk['total'] / 1024 ** 3:.0f}G {usage:.0f}%" if usage is not None else "--%",
                  usage_style(usage), max(0, disk_w - bar_w - 5))

    frame.box(cluster_y, 0, cluster_h, width, "CLUSTER / readiness")
    health = cluster.get("health", "muted")
    frame.put(cluster_y + 1, 2, summary, health,
              max(0, width - (22 if data.get("updated") and wide else 4)))
    if compact_cluster:
        frame.put(cluster_y + 1, len(summary) + 5, cluster["url"], "accent", width - len(summary) - 7)
    elif data.get("updated") and width >= 72:
        frame.put(cluster_y + 1, width - 20, "checked " + data["updated"], "muted")
    if not compact_cluster:
        frame.put(cluster_y + 2, 2, cluster.get("url") or cluster.get("detail", ""), "accent", max(0, width - 4))
    for i, node in enumerate(nodes):
        badge = {"ok": "[OK]", "warn": "[!]", "bad": "[FAIL]"}[node["health"]]
        if wide:
            text = f"{badge:<6} {node['name']:<14.14} {node['state']:<18} {node['role']:<6} {node['address']}"
            if width >= 96:
                text += "  " + node["version"]
            frame.put(cluster_y + node_start + i, 2, text, node["health"], max(0, width - 4))
        else:
            frame.put(cluster_y + node_start + i * 2, 2, f"{badge:<6} {node['name']:<10.10} {node['state']}",
                      node["health"], max(0, width - 4))
            frame.put(cluster_y + node_start + 1 + i * 2, 2, f"{node['role']} {node['address']} {node['version']}",
                      "muted", max(0, width - 4))
    return frame


def screen(window, monitor, demo=False, unhealthy=False, ascii_only=False):
    import curses
    styles = {name: curses.A_BOLD if name in {"ok", "bad", "warn", "accent", "brand"} else
              curses.A_DIM if name == "muted" else 0
              for name in ("ok", "bad", "warn", "accent", "border", "muted", "text", "brand")}
    if curses.has_colors():
        curses.start_color()
        background = curses.COLOR_BLACK
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            pass
        for pair, (name, color) in enumerate((("ok", curses.COLOR_GREEN), ("bad", curses.COLOR_RED),
                                             ("warn", curses.COLOR_YELLOW), ("accent", curses.COLOR_CYAN),
                                             ("border", curses.COLOR_BLUE), ("muted", curses.COLOR_WHITE),
                                             ("brand", curses.COLOR_YELLOW)), 1):
            curses.init_pair(pair, color, background)
            styles[name] |= curses.color_pair(pair)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    window.timeout(250)
    window.keypad(True)
    try:
        "\u28ff".encode(window.encoding)
        unicode_supported = True
    except (UnicodeError, LookupError):
        unicode_supported = False
    braille = unicode_supported and not ascii_only
    offset = 0
    while True:
        height, width = window.getmaxyx()
        # Reserve the last terminal column: curses wraps at the bottom-right cell.
        frame = dashboard(demo_data(unhealthy) if demo else monitor.snapshot(), max(1, width - 1), height, braille)
        capacity = max(1, height - frame.header - 1)
        offset = min(offset, max(0, len(frame.rows) - frame.header - capacity))
        window.erase()
        visible = frame.rows[:frame.header] + frame.rows[frame.header + offset:frame.header + offset + capacity]
        for y, row in enumerate(visible[:max(0, height - 1)]):
            x = 0
            while x < len(row):
                style = row[x][1]
                end = x + 1
                while end < len(row) and row[end][1] == style:
                    end += 1
                try:
                    window.addnstr(y, x, "".join(char for char, _ in row[x:end]),
                                   max(0, width - x - 1), styles[style])
                except curses.error:
                    pass
                x = end
        footer = "Enter/Q/Esc: login  Up/Down: scroll  PgUp/PgDn: page  A: glyphs" if width >= 60 else "Q: login  Up/Dn  A: glyphs"
        if len(frame.rows) - frame.header > capacity:
            footer += f"  {offset + 1}/{len(frame.rows) - frame.header - capacity + 1}"
        try:
            window.addnstr(height - 1, 0, footer, max(0, width - 1), curses.A_REVERSE)
        except curses.error:
            pass
        window.refresh()
        key = window.getch()
        if key in (ord("q"), ord("Q"), 27, 10, 13, curses.KEY_ENTER):
            return
        if key in (ord("a"), ord("A")):
            braille = unicode_supported and not braille
        if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
            offset += capacity if key == curses.KEY_NPAGE else 1
        elif key in (curses.KEY_UP, curses.KEY_PPAGE):
            offset = max(0, offset - (capacity if key == curses.KEY_PPAGE else 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="show fictional example data")
    parser.add_argument("--demo-unhealthy", action="store_true", help="show fictional warnings and a failed node")
    parser.add_argument("--ascii", action="store_true", help="use plain characters for fonts without Braille glyphs")
    args = parser.parse_args()
    monitor = Monitor()
    if not (args.demo or args.demo_unhealthy):
        for target in (monitor.collect_local, monitor.collect_cluster):
            threading.Thread(target=target, daemon=True).start()
    try:
        import curses
        curses.wrapper(screen, monitor, args.demo or args.demo_unhealthy, args.demo_unhealthy, args.ascii)
    except (ImportError, OSError, KeyboardInterrupt):
        pass  # The getty wrapper always proceeds to the authenticated login.
    finally:
        monitor.stop.set()


if __name__ == "__main__":
    main()

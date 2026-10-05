"""What a person would look at when a scenario fails, saved for the run's
artifacts: the cluster, Homestead's jobs and log, Longhorn, and each host's
own log and console."""
import json
from pathlib import Path

from . import log


def collect(ctx, label):
    out = Path(ctx.artifacts) / "diagnostics" / label
    out.mkdir(parents=True, exist_ok=True)
    log.info(f"Collecting diagnostics in {out}")

    def save(name, fn):
        try:
            text = fn()
            (out / name).write_text(text if isinstance(text, str) else json.dumps(text, indent=2))
        except Exception as error:
            (out / name).write_text(f"could not collect: {error}")

    k = ctx.kube
    save("nodes.txt", lambda: k.run("get", "nodes", "-o", "wide", check=False))
    save("pods.txt", lambda: k.run("get", "pods", "-A", "-o", "wide", check=False))
    save("services.txt", lambda: k.run("get", "services", "-A", "-o", "wide", check=False))
    save("events.txt", lambda: k.run("get", "events", "-A", "--sort-by=.lastTimestamp", check=False))
    save("homestead.log", lambda: k.run("logs", "-n", "lab", "-l", "app=homestead", "--all-containers", "--tail=2000", "--prefix", check=False, timeout=60))
    save("longhorn-volumes.yaml", lambda: k.run("get", "volumes.longhorn.io", "-n", "longhorn-system", "-o", "yaml", check=False))
    save("longhorn-replicas.txt", lambda: k.run("get", "replicas.longhorn.io", "-n", "longhorn-system", "-o", "wide", check=False))
    save("jobs.json", lambda: ctx.api.get("/api/operations", wait=30))
    save("longhorn-nodes.yaml", lambda: k.run("get", "nodes.longhorn.io", "-n", "longhorn-system", "-o", "yaml", check=False))
    # Logs of what keeps failing: a pod that restarted, or is not ready, in
    # Longhorn and Homestead's own namespace - its last crash included.
    def failing_logs():
        out_text = []
        for pod in k.items("pods", "-n", "longhorn-system") + k.items("pods", "-n", "lab"):
            meta, status = pod["metadata"], pod.get("status") or {}
            statuses = status.get("containerStatuses") or []
            if status.get("phase") == "Succeeded" or (statuses and all(c.get("ready") for c in statuses)
                                                      and not any(c.get("restartCount") for c in statuses)):
                continue
            for previous in ("--previous", ""):
                text = k.run("logs", "-n", meta["namespace"], meta["name"], "--all-containers", "--tail=80",
                             *([previous] if previous else []), check=False, timeout=30)
                if text.strip():
                    out_text.append(f"===== {meta['namespace']}/{meta['name']} {previous or '(current)'}\n{text}")
            if len(out_text) > 40:
                break
        return "\n".join(out_text) or "no failing pods"
    save("failing-pod-logs.txt", failing_logs)
    # Longhorn's own account of a volume going wrong - a replica faulting, an
    # engine salvaged, a share-manager moved - is in its managers and
    # instance managers, which stay Running through it.
    def longhorn_logs():
        out_text = []
        for pod in k.items("pods", "-n", "longhorn-system"):
            name = pod["metadata"]["name"]
            if not name.startswith(("longhorn-manager-", "instance-manager-", "share-manager-")):
                continue
            text = k.run("logs", "-n", "longhorn-system", name, "--all-containers", "--tail=400",
                         check=False, timeout=30)
            lines = [line for line in text.splitlines()
                     if "level=info" not in line or any(word in line.lower() for word in
                        ("fault", "salvag", "error", "detach", "remount", "replica", "stale", "export"))]
            out_text.append(f"===== {name}\n" + "\n".join(lines[-250:]))
        return "\n".join(out_text) or "no Longhorn pods"
    save("longhorn-logs.txt", longhorn_logs)
    # The first look, in the run's own log: what is not running, and why.
    try:
        pods = k.run("get", "pods", "-A", "-o", "wide", check=False).splitlines()
        log.info("Pods not running:\n  " + "\n  ".join([pods[0]] + [p for p in pods[1:] if "Running" not in p and "Completed" not in p][:30]))
        events = k.run("get", "events", "-A", "--sort-by=.lastTimestamp", "--field-selector=type=Warning", check=False).splitlines()
        log.info("Recent warnings:\n  " + "\n  ".join(events[-25:]))
    except Exception as error:
        log.info(f"(no cluster summary: {error})")
    for node in ctx.lab.nodes:
        # Without the netlog lines: they filled the whole tail before.
        save(f"{node.name}-journal.txt", lambda node=node: node.ssh(
            "sudo journalctl -b --no-pager | grep -v ' netlog: ' | tail -3000", check=False, timeout=90))
        # The services that hold storage and the network up, and the kernel.
        save(f"{node.name}-services-journal.txt", lambda node=node: node.ssh(
            # -k with -u asks for kernel lines from those units - none - so
            # the two are read separately.
            "{ sudo journalctl -b --no-pager -k | tail -400; echo; "
            "sudo journalctl -b --no-pager -u k3s -u k3s-agent -u rke2-server -u rke2-agent -u iscsid "
            "| grep -vE 'netlog|level=info' | tail -1200; }", check=False, timeout=90))
        # What the host's network became: addresses, routes, its resolver and
        # whether it answers, and the packet filter that can stand in the way.
        save(f"{node.name}-network.txt", lambda node=node: node.ssh(
            "ip -br addr; echo; ip route; echo; resolvectl status 2>&1 | head -40; echo; cat /etc/resolv.conf; echo; "
            "for h in github.com registry-1.docker.io; do getent hosts $h || echo \"$h: no answer\"; done; "
            "resolvectl query github.com 2>&1 | head -5; echo; "
            "sudo journalctl -u systemd-resolved -b --no-pager | tail -40; echo; "
            "sudo iptables-save 2>/dev/null | grep -cE '^-A' ; sudo iptables-save -t filter 2>/dev/null | grep -E 'DROP|REJECT' | head -40; "
            "sudo nft list ruleset 2>/dev/null | grep -cE 'drop|reject'; ip -s link show eth0",
            check=False, timeout=90))
        save(f"{node.name}-previous-boot.txt", lambda node=node: node.ssh("sudo journalctl -b -1 --no-pager | tail -300", check=False, timeout=60))
        for console in sorted(node.dir.glob("console-*.log"))[-2:]:
            (out / f"{node.name}-{console.name}").write_text(console.read_text(errors="replace")[-200000:])

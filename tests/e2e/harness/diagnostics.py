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
    save("events.txt", lambda: k.run("get", "events", "-A", "--sort-by=.lastTimestamp", check=False))
    save("homestead.log", lambda: k.run("logs", "-n", "lab", "-l", "app=homestead", "--all-containers", "--tail=2000", "--prefix", check=False, timeout=60))
    save("longhorn-volumes.yaml", lambda: k.run("get", "volumes.longhorn.io", "-n", "longhorn-system", "-o", "yaml", check=False))
    save("longhorn-replicas.txt", lambda: k.run("get", "replicas.longhorn.io", "-n", "longhorn-system", "-o", "wide", check=False))
    save("jobs.json", lambda: ctx.api.get("/api/operations", wait=30))
    for node in ctx.lab.nodes:
        save(f"{node.name}-journal.txt", lambda node=node: node.ssh("sudo journalctl -b --no-pager | tail -500", check=False, timeout=60))
        save(f"{node.name}-previous-boot.txt", lambda node=node: node.ssh("sudo journalctl -b -1 --no-pager | tail -300", check=False, timeout=60))
        for console in sorted(node.dir.glob("console-*.log"))[-2:]:
            (out / f"{node.name}-{console.name}").write_text(console.read_text(errors="replace")[-200000:])

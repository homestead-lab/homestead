"""Volume copies kept whole and balanced: a detached volume short of a copy
is rebuilt offline, and Balance hosts moves copies and containers to a host
that holds too little."""
import json
import time

from harness import log

TWO = "e2e-two-copies"


def _two_copies(ctx):
    """Two copies on three hosts - one host without one, to rebuild onto and
    balance onto. Homestead's own default follows the hosts, to three."""
    ctx.kube.apply(json.dumps({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": TWO},
                               "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True, "reclaimPolicy": "Delete",
                               "volumeBindingMode": "Immediate", "parameters": {"numberOfReplicas": "2", "staleReplicaTimeout": "30"}}))


def offline_rebuild(ctx):
    """Detached, with one of its two copies gone: Longhorn rebuilds it."""
    _two_copies(ctx)
    ctx.app("e2e-detached", storage_class=TWO)
    ctx.kube.run("scale", "-n", "lab", "deploy/e2e-detached", "--replicas=0")
    volume = ctx.kube.wait("e2e-detached detached", lambda: (lambda v: v["status"]["state"] == "detached" and v)(
        ctx.kube.volume("lab", "e2e-detached-data")))
    name = volume["metadata"]["name"]
    # Both copies made before one is taken away.
    replicas = ctx.kube.wait("the volume's two copies", lambda: (lambda rs: len(rs) == 2 and rs)(
        [r for r in ctx.kube.items("replicas.longhorn.io", "-n", "longhorn-system") if r["spec"]["volumeName"] == name]),
        timeout=300, every=10)
    ctx.kube.run("delete", "replicas.longhorn.io", "-n", "longhorn-system", replicas[0]["metadata"]["name"])
    log.info("One copy of a detached volume removed; waiting for it to be rebuilt")
    status = ctx.api.get("/api/longhorn/offline-rebuilding")
    assert status["enabled"], f"offline rebuilding is not on: {status}"

    def whole():
        copies = [r for r in ctx.kube.items("replicas.longhorn.io", "-n", "longhorn-system")
                  if r["spec"]["volumeName"] == name and r["spec"].get("healthyAt") and not r["spec"].get("failedAt")]
        return len({r["spec"]["nodeID"] for r in copies}) == 2
    ctx.kube.wait("the detached volume back to two copies", whole, timeout=1800, every=15)


def balance(ctx):
    """node-3 cordoned while volumes and busy containers are made, then
    uncordoned: Balance hosts moves copies and containers onto it."""
    _two_copies(ctx)
    ctx.api.post("/api/node/cordon", {"node": "node-3", "cordon": True})
    for i in range(3):
        ctx.app(f"e2e-busy-{i}", node="node-1", cpu_burn=True, size="3Gi", storage_class=TWO)
        # Copies under a gigabyte are not worth moving: give each real data.
        ctx.exec(f"e2e-busy-{i}", "dd if=/dev/urandom of=/data/fill bs=1M count=1200 status=none && sync", timeout=900)
    ctx.api.post("/api/node/cordon", {"node": "node-3", "cordon": False})

    volumes = ctx.api.get("/api/longhorn/rebalance/plan")
    assert volumes["moves"], f"no volume copy moves planned: {volumes}"
    assert all(m["to"] == "node-3" for m in volumes["moves"]), volumes["moves"]
    containers = ctx.api.get("/api/workloads/rebalance/plan")
    log.info(f"Planned: {len(volumes['moves'])} copies, {len(containers['moves'])} containers")

    job = ctx.api.post("/api/longhorn/rebalance", {"exclude": [], "review_token": volumes["review_token"]})["operation"]["id"]
    ctx.api.wait_job(job, timeout=3600)
    held = {r["spec"]["nodeID"] for r in ctx.kube.items("replicas.longhorn.io", "-n", "longhorn-system")
            if r["spec"]["volumeName"] in {m["volume"] for m in volumes["moves"]}}
    assert "node-3" in held, "no copy reached node-3"

    if containers["moves"]:
        job = ctx.api.post("/api/workloads/rebalance", {"exclude": [], "review_token": containers["review_token"],
                                                        "moves": containers["moves"], "restart": True})["operation"]["id"]
        ctx.api.wait_job(job, timeout=1800)
        for move in containers["moves"]:
            assert ctx.app_node(move["name"]) == move["to"], f"{move['id']} did not move to {move['to']}"

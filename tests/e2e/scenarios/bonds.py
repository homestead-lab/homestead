"""A host's NICs bonded through Homestead, as a person with a spare port does
it: the review, the change, and Homestead checking the host from outside
before it disarms the rollback the host armed for itself.

Each host has a second NIC on the same bridge (Lab(nics=2)) that netplan does
not name, so it starts switched off - the usual spare. The runner can pull
either NIC's cable (QMP set_link), which the guest sees as its carrier going.

1. node-2's eth0 and its spare become bond0 (active-backup): the host keeps
   its address on bond0 and Kubernetes keeps it Ready.
2. eth0's cable is pulled: the host still answers, through the spare, and
   Homestead says bond0 is running on one member.
3. bond0 is changed to 802.3ad, which the lab's Linux bridge cannot answer
   (no LACP): the job fails saying so, and the host puts its active-backup
   bond back by itself.
4. Back to one NIC: eth0 carries the host's address again.
"""
import time

from harness import log

HOST = "node-2"


def _until(what, check, timeout=300, every=5):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            value = check()
        except Exception as error:      # a host mid-change does not answer
            value, last = None, error
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"{what}: not within {timeout}s" + (f" ({last})" if last else ""))


def _bond(host):
    """bond0 as the host's kernel has it: mode, members, the active one and
    the addresses on it; {} when there is no bond0."""
    out = host.ssh("B=/sys/class/net/bond0/bonding; [ -d $B ] || exit 0; "
                   "echo mode $(cut -d' ' -f1 $B/mode); echo slaves $(cat $B/slaves); echo active $(cat $B/active_slave); "
                   "echo addr $(ip -4 -o addr show dev bond0 | awk '{print $4}')", quiet=True, timeout=30)
    return {line.split(" ", 1)[0]: line.split(" ", 1)[1].split() if " " in line else [] for line in out.splitlines() if line}


def _change(ctx, request, until=("succeeded",)):
    plan = ctx.api.post("/api/node/bond/preview", request)
    assert not plan["refusals"], f"refused: {plan['refusals']}"
    for warning in plan["warnings"]:
        log.info(f"  review: {warning}")
    started = ctx.api.post("/api/node/bond", dict(request, digest=plan["digest"], confirm=request["node"]))
    return ctx.api.wait_job(started["operation"]["id"], timeout=900, until=until)


def run(ctx):
    host = ctx.node(HOST)
    info = ctx.api.post("/api/node/bond/inspect", {"node": HOST})
    assert not info["problem"], info["problem"]
    assert info["shape"]["shape"] == "nic", f"{HOST} is on {info['shape']}, not a plain NIC"
    first = info["shape"]["carrier_nic"]
    spare = next((n for n in info["nics"] if n["mac"] == host.mac2), None)
    assert spare, f"{HOST}'s second NIC ({host.mac2}) is not among {[n['name'] for n in info['nics']]}"
    assert spare["carrier"] is None, f"the spare should start switched off: {spare}"
    log.info(f"{HOST}: {first} carries {info['address']}; spare {spare['name']}")

    # 1 · bond them
    _change(ctx, {"node": HOST, "action": "create", "members": [first, spare["name"]], "mode": "active-backup", "primary": first})
    bond = _until("bond0 carrying the host", lambda: (b := _bond(host)) and b.get("addr") and b, timeout=120)
    assert bond["mode"] == ["active-backup"], bond
    assert sorted(bond["slaves"]) == sorted([first, spare["name"]]), bond
    assert bond["addr"] == [f"{host.ip}/24"], bond
    ctx.kube.nodes_ready(len(ctx.lab.nodes))
    _until("Homestead seeing bond0 as the uplink",
           lambda: ctx.api.get(f"/api/nodes/ports?node={HOST}").get("uplink") == "bond0", timeout=180)
    log.info(f"{HOST}: on bond0 ({first} + {spare['name']})")

    # 2 · pull the cable of the member carrying traffic
    active = bond["active"][0]
    nic = "lan" if active == first else "lan2"
    host.cable(nic, False)
    try:
        _until("the bond failing over", lambda: _bond(host).get("active", [""])[0] not in ("", active), timeout=60)
        host.ssh("true", timeout=15)
        _until("Homestead saying bond0 runs on one member", lambda: any(
            c["kind"] == "members" and c["iface"] == "bond0" for c in ctx.api.get(f"/api/nodes/ports?node={HOST}")["conditions"]),
            timeout=240, every=10)
        log.info(f"{HOST}: answered through the spare with {active}'s cable pulled; Homestead raised it")
    finally:
        host.cable(nic, True)
    _until("both members with link again", lambda: all(
        host.ssh(f"cat /sys/class/net/{m}/bonding_slave/mii_status", quiet=True, timeout=15).strip() == "up"
        for m in (first, spare["name"])), timeout=120)

    # 3 · an 802.3ad bond the switch cannot answer puts the old one back
    job = _change(ctx, {"node": HOST, "action": "change", "members": [first, spare["name"]], "mode": "802.3ad",
                        "lacp_confirmed": True}, until=("failed",))
    assert "LACP" in job.get("message", ""), job.get("message")
    bond = _until("the active-backup bond back", lambda: (b := _bond(host)).get("mode") == ["active-backup"] and b.get("addr") and b,
                  timeout=300)
    assert bond["addr"] == [f"{host.ip}/24"], bond
    ctx.kube.nodes_ready(len(ctx.lab.nodes))
    log.info(f"{HOST}: 802.3ad without a partner rolled back to active-backup")

    # 4 · back to one NIC
    _change(ctx, {"node": HOST, "action": "remove", "keep": first})
    _until(f"{first} carrying the host again", lambda: f"{host.ip}/24" in host.ssh(
        f"ip -4 -o addr show dev {first}", quiet=True, timeout=15) and not _bond(host), timeout=180)
    ctx.kube.nodes_ready(len(ctx.lab.nodes))
    log.info(f"{HOST}: back on {first}")

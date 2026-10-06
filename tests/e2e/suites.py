"""Which scenarios run, on how many hosts, and how they are split into CI
jobs. Every job gets its own runner and cluster, so jobs run side by side and
one failing does not hold up another; scenarios in one job share its cluster
and run in order. A suite's scenarios each get a job of their own unless
"jobs" groups them - short ones together, so no job outlasts about fifteen
minutes and no cluster is built for a minute's work.

"rke2" names the jobs a prod release also runs on RKE2 - where the
distribution changes what happens (installs, drains and reboots, the CNI's
firewall). The rest run on k3s, keeping a release inside the twenty jobs a
repository runs at once. Asked for by hand or by an e2e/ branch, RKE2 runs
everything."""
from scenarios import bonds, existing, longhorn_v2, migration, network, outage, power, rolling, self_data, shutdown, smoke, storage

SUITES = {
    # Every GET route and the node doctor, on three hosts.
    "core": {"nodes": 3, "scenarios": [("api routes", smoke.run), ("node doctor", smoke.doctor)],
             "jobs": [["api routes", "node doctor"]]},
    # A host reboot with apps that move and apps that wait.
    "power": {"nodes": 3, "scenarios": [("reboot a host", power.multi)], "rke2": ["reboot a host"]},
    # One host: reboot through the handoff; a power-off and power-on.
    "single": {"nodes": 1, "scenarios": [("api routes", smoke.run), ("single-host reboot", power.single),
                                         ("single-host power-off", power.single_poweroff)],
               "jobs": [["api routes", "single-host reboot"], ["single-host power-off"]], "rke2": ["single-host reboot"]},
    # The whole cluster down and back.
    "shutdown": {"nodes": 3, "scenarios": [("cluster shutdown", shutdown.run)], "rke2": ["cluster shutdown"]},
    # OS updates with a restart on every host, one at a time.
    "rolling": {"nodes": 3, "scenarios": [("rolling restarts", rolling.run)], "rke2": ["rolling restarts"]},
    # A host failing with no warning; a container crashing.
    "outage": {"nodes": 3, "scenarios": [("container restart", outage.container_restart), ("host outage", outage.host_outage)],
               "rke2": ["host outage"]},
    # Homestead added to clusters someone already runs (HS_ROLE=addons): one
    # with no Longhorn, one whose Longhorn came first. Each host is a
    # single-host cluster of its own, built by k3s's or RKE2's own installer,
    # in one job. On demand: a release already fills its twenty jobs.
    "existing": {"nodes": 2, "bare": True, "on_demand": True, "rke2_memory": 5120,
                 "scenarios": [("plain cluster", existing.plain), ("longhorn first", existing.longhorn_first_on_its_cluster)],
                 "jobs": [["plain cluster", "longhorn first"]]},
    # An app moved between two clusters, as Linked clusters does.
    "migration": {"nodes": 2, "separate": True, "scenarios": [("move between clusters", migration.run)]},
    # VIPs (and their failover), the firewall, Multus LAN networks, the
    # installer, and a host's NICs bonded: three servers and a worker, Multus
    # installed, each host with a spare second NIC. Bonds share the VIP job:
    # a release already fills its twenty.
    "network": {"nodes": 4, "agents": 1, "memory": 3072, "nics": 2, "installer": {"HS_MULTUS": "yes"},
                "scenarios": [("vip", network.vip), ("bonds", bonds.run), ("firewall", network.firewall),
                              ("lan network", network.lan), ("installer", network.installer)],
                "jobs": [["vip", "bonds"], ["firewall", "lan network", "installer"]], "rke2": ["firewall"]},
    # Homestead's own data moved to a new volume twice, across hosts. RKE2's
    # control plane takes more of a host than k3s's: at 4 GiB the copy left
    # under its 1 GiB reserve, a new warning that held the move.
    "self-data": {"nodes": 3, "rke2_memory": 4608, "scenarios": [("move Homestead's data", self_data.run)]},
    # Copies kept whole, and Balance hosts.
    "storage": {"nodes": 3, "scenarios": [("offline rebuild", storage.offline_rebuild), ("balance hosts", storage.balance)],
                "jobs": [["offline rebuild", "balance hosts"]]},
    # Longhorn's V2 data engine, prepared, enabled and given a disk through
    # Homestead; a V2 volume across a host reboot. Two hosts: hugepages and
    # a polling core each leave room for no third on a runner.
    # A server and a worker, 3 CPUs each: V2 polls with one core and its
    # instance manager reserves CPU beside the control plane; the worker is
    # the host that reboots, so etcd keeps its one member.
    "longhorn-v2": {"nodes": 2, "agents": 1, "cpus": 3, "memory": 6144, "data_disk": "20G", "hugepages": 1100,
                    "scenarios": [("longhorn v2", longhorn_v2.run)]},
}


def jobs(suite):
    """The suite's jobs: lists of scenario names, in order."""
    spec = SUITES[suite]
    return spec.get("jobs") or [[name] for name, _ in spec["scenarios"]]


def on_rke2_release(suite, job):
    return any(name in (SUITES[suite].get("rke2") or ()) for name in job)

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
from scenarios import migration, network, outage, power, rolling, self_data, shutdown, smoke, storage

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
    # An app moved between two clusters, as Linked clusters does.
    "migration": {"nodes": 2, "separate": True, "scenarios": [("move between clusters", migration.run)]},
    # VIPs (and their failover), the firewall, Multus LAN networks and the
    # installer: three servers and a worker, Multus installed.
    "network": {"nodes": 4, "agents": 1, "memory": 3072, "installer": {"HS_MULTUS": "yes"},
                "scenarios": [("vip", network.vip), ("firewall", network.firewall), ("lan network", network.lan),
                              ("installer", network.installer)],
                "jobs": [["vip"], ["firewall", "lan network", "installer"]], "rke2": ["firewall"]},
    # Homestead's own data moved to a new volume twice, across hosts.
    "self-data": {"nodes": 3, "scenarios": [("move Homestead's data", self_data.run)]},
    # Copies kept whole, and Balance hosts.
    "storage": {"nodes": 3, "scenarios": [("offline rebuild", storage.offline_rebuild), ("balance hosts", storage.balance)]},
}


def jobs(suite):
    """The suite's jobs: lists of scenario names, in order."""
    spec = SUITES[suite]
    return spec.get("jobs") or [[name] for name, _ in spec["scenarios"]]


def on_rke2_release(suite, job):
    return any(name in (SUITES[suite].get("rke2") or ()) for name in job)

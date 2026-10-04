"""Which scenarios run together, on how many hosts. Each suite gets its own
runner and cluster in CI, so suites run in parallel and one failing does not
hold up another; scenarios in a suite share its cluster and run in order."""
from scenarios import migration, network, outage, power, rolling, self_data, shutdown, smoke, storage

SUITES = {
    # Every GET route and the node doctor, on three hosts.
    "core": {"nodes": 3, "scenarios": [("api routes", smoke.run), ("node doctor", smoke.doctor)]},
    # A host reboot with apps that move and apps that wait.
    "power": {"nodes": 3, "scenarios": [("reboot a host", power.multi)]},
    # One host: reboot through the handoff, then a power-off and power-on.
    "single": {"nodes": 1, "scenarios": [("api routes", smoke.run), ("single-host reboot", power.single),
                                         ("single-host power-off", power.single_poweroff)]},
    # The whole cluster down and back.
    "shutdown": {"nodes": 3, "scenarios": [("cluster shutdown", shutdown.run)]},
    # OS updates with a restart on every host, one at a time.
    "rolling": {"nodes": 3, "scenarios": [("rolling restarts", rolling.run)]},
    # A host failing with no warning, and a container crashing.
    "outage": {"nodes": 3, "scenarios": [("container restart", outage.container_restart), ("host outage", outage.host_outage)]},
    # An app moved between two clusters, as Linked clusters does.
    "migration": {"nodes": 2, "separate": True, "scenarios": [("move between clusters", migration.run)]},
    # VIPs (and their failover), the firewall, Multus LAN networks and the
    # installer: three servers and a worker, Multus installed.
    "network": {"nodes": 4, "agents": 1, "memory": 3072, "installer": {"HS_MULTUS": "yes"},
                "scenarios": [("vip", network.vip), ("firewall", network.firewall), ("lan network", network.lan),
                              ("installer", network.installer)]},
    # Homestead's own data moved to a new volume twice, across hosts.
    "self-data": {"nodes": 3, "scenarios": [("move Homestead's data", self_data.run)]},
    # Copies kept whole, and Balance hosts.
    "storage": {"nodes": 3, "scenarios": [("offline rebuild", storage.offline_rebuild), ("balance hosts", storage.balance)]},
}

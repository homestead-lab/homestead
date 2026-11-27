"""The host rows the list reads send (#1211): /api/nodes, read by the Hosts
page every tick, and the same rows in /api/overview, read by the Dashboard.

A row from homestead_nodes.get_nodes carries the node probe's whole last
reading and every Kubernetes label of the host. Most of that does not change
between two reads and no list shows it: the probe's device list (the /dev
paths and USB devices, by far the largest part), its mount table and its iSCSI
sessions, and the labels (node feature discovery alone adds hundreds). On a
four-host cluster they were 290 of the 329 KiB of each read.

Those parts are left out of the list rows only, and no page reads them from
a list row. The combined list of linked clusters (/api/fleet/all/nodes) cuts
this cluster's rows the same way, as each linked cluster's rows come from its
own /api/nodes (one on an older version still sends them whole). The host's
own read (/api/node) keeps the full row, and so does everything in the server
that reads get_nodes: placement, the capacity reviews, /api/v1 and metrics.
The rows themselves are never changed: they are the kept answer those readers
share.
"""

# The probe's parts only a host's own read needs: the hardware feature
# editor browses "devices" from /api/node.
PROBE_DETAIL = ("devices", "mounts", "iscsi")
# Row fields no list shows. A host's own read keeps them.
ROW_DETAIL = ("labels",)


def list_row(row):
    """row without the detail above: a new dict, row is not changed."""
    if not isinstance(row, dict):
        return row
    out = {key: value for key, value in row.items() if key not in ROW_DETAIL}
    temps = row.get("temps")
    if isinstance(temps, dict):
        out["temps"] = {key: value for key, value in temps.items() if key not in PROBE_DETAIL}
    return out


def list_rows(rows):
    """Each host row as a list read sends it; anything that is not a list
    (an error answer) as it came."""
    return [list_row(row) for row in rows] if isinstance(rows, list) else rows


def overview(answer):
    """/api/overview with its host rows as list rows."""
    if not isinstance(answer, dict) or not isinstance(answer.get("nodes"), list):
        return answer
    return {**answer, "nodes": list_rows(answer["nodes"])}

"""Reading several hosts through the host helper: a few at a time, and a
host that keeps failing is left to the slow cadence (#976).

Each read is a helper pod on that host and can take a minute or more. Read
one after another, one slow host held up every other host and the rest of
the leader's host loop; and a host whose read failed was tried again on
every round, a helper pod every 10 minutes for as long as it stayed broken.

Here at most POOL hosts are read at once, and after TRIES failures in a row a
host is read again only once its last try is `every` old. A host that is
busy with another change (homestead_hostrun.Busy) has not failed: it is
tried again next round, as before.
"""
from concurrent.futures import ThreadPoolExecutor

POOL = 3            # host helper pods at once
TRIES = 3           # failures in a row before a host waits for the slow cadence


def due(failures, node, now, every):
    """Whether node is read this round, given {node: {"count", "at"}}."""
    entry = failures.get(node) or {}
    return int(entry.get("count") or 0) < TRIES or now - float(entry.get("at") or 0) >= every


def failed(failures, node, now):
    entry = failures.setdefault(node, {"count": 0, "at": 0})
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["at"] = now


def worked(failures, node):
    failures.pop(node, None)


def each(nodes, work):
    """work(node) for each node, at most POOL at a time, in order:
    [(node, result, error)], error None when it returned."""
    nodes = list(nodes)
    if not nodes:
        return []

    def one(node):
        try:
            return node, work(node), None
        except Exception as error:
            return node, None, error

    with ThreadPoolExecutor(max_workers=min(POOL, len(nodes))) as pool:
        return list(pool.map(one, nodes))

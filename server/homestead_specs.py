"""Comparing a spec Homestead wants with the one Kubernetes stored.

Kubernetes leaves out fields at their zero value when it stores an object -
a mount's readOnly: false, an empty list - so the stored spec never equals
one written with them, and a reconcile loop that compares the two finds
"drift" every time and rewrites the object. For the SMB and NFS servers,
whose Deployments use the Recreate strategy, every rewrite restarted the
server and cut every open connection, once a minute.

same() compares with those values removed from both sides, so only a real
difference counts. A value set to true, a number, or a non-empty string is
still compared exactly.
"""


def trim(value):
    """The value with false, None, "" and empty lists and maps left out."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = trim(item)
            if item in (False, None, "", [], {}):
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [trim(item) for item in value]
    return value


def same(left, right):
    return trim(left) == trim(right)

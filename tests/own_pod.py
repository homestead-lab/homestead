"""Homestead's own pod, for a test that runs the host power review.

The review reads the pod Homestead runs in, to say which volumes are its own data
(homestead_lhcapacity._own_claims, #1410). It does that through homestead_lhcapacity's
reader, not homestead_power's, and only when Homestead has a pod name. So a test that
binds homestead_power to its cluster answers that read too, under a pod name it sets:

    cluster.add(own_pod.runs_as(self, K.kget))            a tests/fakes/kube.py Kube
    objects[own_pod.path()] = own_pod.runs_as(self, get)  a dict of paths

Without it the read goes to whatever reader an earlier test file left bound (the real
one once server.py is loaded), or fails and the review drops its unclean-restart
warning without saying so.
"""
from unittest import mock

import homestead_power as POWER
from rebind import keep_bound

NAME, CLAIM = "homestead-own-0", "homestead-data"


def path():
    """Where the pod is read: in the namespace Homestead runs in (left as the test has it)."""
    return f"/api/v1/namespaces/{POWER.LHCAP.SELF.NS}/pods/{NAME}"


def claim():
    """The pod's data claim, as the review names it."""
    return f"{POWER.LHCAP.SELF.NS}/{CLAIM}"


def runs_as(test, read):
    """For this test, Homestead runs as the pod NAME and reads it with `read`.
    Returns the pod, for the test to put in its cluster. Only the pod name is set:
    the namespace stays, since jobs and Leases use it too."""
    lhcap = POWER.LHCAP
    keep_bound(test, lhcap)
    lhcap.kget = read
    patch = mock.patch.object(lhcap.SELF, "POD", NAME)
    patch.start()
    test.addCleanup(patch.stop)
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"namespace": lhcap.SELF.NS, "name": NAME},
            "spec": {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": CLAIM}}]}}

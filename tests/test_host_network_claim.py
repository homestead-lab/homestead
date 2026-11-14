"""The Lease a host network change holds while it starts (#1318).

claim_network takes one Lease that every replica shares, so two bond or bridge
changes sent at once to two hosts cannot both pass the network guard. A claim
left by a replica that stopped runs out and is taken over. Before, a replica
judged that by the stamp in the Lease against its own clock: a replica whose
clock ran ten minutes ahead of the one that wrote the claim took over a fresh
claim, and two network changes ran at once. Now a replica counts a claim's age
from when it first saw the claim as it is now, on its own monotonic clock, as
client-go's leader election does. No two clocks are compared.

On the shared Kubernetes fake (tests/fakes/kube.py), with a fake monotonic clock."""
import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_host_bonds as BONDS
import homestead_hostrun as HOSTRUN
import homestead_kube as K
from fakes.kube import Kube
from rebind import keep_bound

OTHER = "other-replica/ab12cd34"


def claim(holder=OTHER, age=0.0):
    """The claim as another replica wrote it, its stamps age seconds behind
    this replica's wall clock."""
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
            "metadata": {"name": BONDS.CLAIM, "namespace": HOSTRUN.NS},
            "spec": {"holderIdentity": holder, "leaseDurationSeconds": BONDS.CLAIM_SECONDS,
                     "acquireTime": stamp, "renewTime": stamp, "leaseTransitions": 3}}


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.kube = self.enterContext(Kube())
        keep_bound(self, BONDS)
        BONDS.bind(None, K.kget, K.ksend)
        self.clock = 50_000.0
        self.enterContext(mock.patch.object(BONDS, "claim_clock", lambda: self.clock, create=True))
        self.path = f"/apis/coordination.k8s.io/v1/namespaces/{HOSTRUN.NS}/leases/{BONDS.CLAIM}"

    def holder(self):
        return self.kube.object(self.path)["spec"]["holderIdentity"]

    def refused(self, why):
        with self.assertRaises(ValueError, msg=why) as caught:
            BONDS.claim_network("node-a", "Bond change on node-a")
        self.assertEqual("node-a: another host's network change is starting: try again in a moment", str(caught.exception))
        self.assertEqual(OTHER, self.holder(), why)

    def test_a_fresh_claim_stamped_by_a_clock_far_behind_is_not_taken(self):
        self.kube.add(claim(age=BONDS.CLAIM_SECONDS + 100))
        self.refused("this replica's clock runs ahead of the writer's: its stamp says nothing about its age")

    def test_a_claim_seen_unchanged_for_its_length_is_taken_over(self):
        self.kube.add(claim())
        self.refused("first seen now")
        self.clock += BONDS.CLAIM_SECONDS
        self.refused("seen for exactly its length")
        self.clock += 1
        holder = BONDS.claim_network("node-a", "Bond change on node-a")
        self.assertEqual(holder, self.holder())
        self.assertEqual(4, self.kube.object(self.path)["spec"]["leaseTransitions"])

    def test_a_claim_that_changes_meanwhile_is_counted_again(self):
        self.kube.add(claim())
        self.refused("first seen now")
        self.clock += BONDS.CLAIM_SECONDS - 100
        self.kube.add(claim(age=-5))
        self.refused("renewed: a new claim as far as this replica can tell")
        self.clock += 200
        self.refused("seen as it is now for only 200 s")
        self.clock += BONDS.CLAIM_SECONDS
        self.assertEqual(BONDS.claim_network("node-a", "Bond change on node-a"), self.holder())

    def test_a_released_claim_is_taken_at_once(self):
        self.kube.add(claim(holder=""))
        holder = BONDS.claim_network("node-a", "Bond change on node-a")
        self.assertEqual(holder, self.holder())

    def test_with_no_claim_one_is_created(self):
        holder = BONDS.claim_network("node-a", "Bond change on node-a")
        self.assertEqual(holder, self.holder())
        self.assertEqual(BONDS.CLAIM_SECONDS, self.kube.object(self.path)["spec"]["leaseDurationSeconds"])


if __name__ == "__main__":
    unittest.main()

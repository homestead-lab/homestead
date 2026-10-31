"""Fleet relays claim their nonces in batches (#985 §2): requests that arrive
while one write of the replay ConfigMap is under way share the next write,
and a nonce one copy of Homestead took is still refused by the other."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_fleet as FLEET
import homestead_kube as K
from fakes.kube import Kube

REPLAY = f"/api/v1/namespaces/{FLEET.NS}/configmaps/{FLEET.NAMES.BRAND}-fleet-replay"


class ReplayBatchTests(unittest.TestCase):
    def setUp(self):
        self.kube = Kube().__enter__()
        self.addCleanup(self.kube.__exit__, None, None, None)
        saved = (FLEET.kget, FLEET.ksend)
        FLEET.kget, FLEET.ksend = K.kget, K.ksend
        self.addCleanup(lambda: setattr(FLEET, "kget", saved[0]) or setattr(FLEET, "ksend", saved[1]))

    def slow_writes(self, seconds=0.05):
        def slow(method, path, body):
            time.sleep(seconds)
            return None             # the fake answers
        for method in ("POST", "PUT"):
            self.kube.react(method, REPLAY, slow)
            self.kube.react(method, REPLAY.rsplit("/", 1)[0], slow)

    def writes(self):
        return [w for w in self.kube.writes if "fleet-replay" in w[1] or "fleet-replay" in str(w[2])]

    def claim_all(self, store, nonces):
        results = []
        def one(nonce):
            try:
                store.claim("member-a", nonce)
                results.append("ok")
            except PermissionError as error:
                results.append(str(error))
        threads = [threading.Thread(target=one, args=(n,)) for n in nonces]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return results

    def test_twenty_concurrent_claims_share_a_couple_of_writes(self):
        self.slow_writes()
        store = FLEET.ReplayStore()
        results = self.claim_all(store, [f"n{i}" for i in range(20)])
        self.assertEqual(["ok"] * 20, results)
        self.assertLessEqual(len(self.writes()), 3, "one write per batch, not one per request")
        self.assertEqual(20, len(FLEET.json.loads(self.kube.object(REPLAY)["data"]["nonces"])))

    def test_the_same_nonce_twice_at_once_is_taken_once(self):
        self.slow_writes()
        results = self.claim_all(FLEET.ReplayStore(), ["same"] * 6)
        self.assertEqual(1, results.count("ok"), results)
        self.assertTrue(all("already used" in r for r in results if r != "ok"))

    def test_a_nonce_one_copy_took_is_refused_by_the_other(self):
        one, two = FLEET.ReplayStore(), FLEET.ReplayStore()
        two.claim("member-a", "warm-up")        # the second copy holds an older ConfigMap now
        one.claim("member-a", "shared")
        with self.assertRaisesRegex(PermissionError, "already used"):
            two.claim("member-a", "shared")
        with self.assertRaisesRegex(PermissionError, "already used"):
            one.claim("member-a", "shared")

    def test_a_quiet_claim_reads_the_configmap_only_when_it_changed_elsewhere(self):
        store = FLEET.ReplayStore()
        store.claim("member-a", "first")
        reads = []
        self.kube.react("GET", REPLAY, lambda method, path, body: reads.append(path))
        store.claim("member-a", "second")
        self.assertEqual([], reads, "its own last write is what the ConfigMap holds")
        FLEET.ReplayStore().claim("member-a", "elsewhere")
        reads.clear()
        store.claim("member-a", "third")
        self.assertEqual(1, len(reads), "a write from another copy is read once, after the conflict")

    def test_the_module_claim_goes_through_the_shared_store(self):
        FLEET._claim_nonce("member-a", "via-module")
        with self.assertRaisesRegex(PermissionError, "already used"):
            FLEET._claim_nonce("member-a", "via-module")


if __name__ == "__main__":
    unittest.main()

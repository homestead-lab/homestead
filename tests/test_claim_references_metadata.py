"""Claim references read ReplicaSets metadata-only, and the full list only when
a ReplicaSet uid or generation not kept yet appears (#980). Any spec change
raises a ReplicaSet's generation, so what claims it names is kept by both."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_kube as K
import homestead_storage as STORAGE
from fakes.kube import Kube

RS = "/apis/apps/v1/replicasets"


def replicaset(name, claim, uid, ns="lab"):
    return {"apiVersion": "apps/v1", "kind": "ReplicaSet",
            "metadata": {"namespace": ns, "name": name, "uid": uid},
            "spec": {"replicas": 0, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": "c", "image": "example/app:1"}],
                                           "volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": claim}}]}}}}


class MetadataListTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, STORAGE, "kget", STORAGE.kget)
        STORAGE.kget = K.kget                      # what server.py binds
        STORAGE._RS.update(key=None, specs={})
        self.addCleanup(STORAGE._RS.update, key=None, specs={})

    def rs_reads(self, kube):
        return [accept for path, accept in kube.reads if path.split("?")[0] == RS]

    def test_a_second_look_reads_replicasets_metadata_only(self):
        with Kube(replicaset("old", "data-a", "u-1")) as kube:
            first = STORAGE.claim_references()
            after_first = len(self.rs_reads(kube))
            second = STORAGE.claim_references()
            reads = self.rs_reads(kube)[after_first:]
        self.assertEqual(["ReplicaSet/old"], first[("lab", "data-a")])
        self.assertEqual(first, second, "the same answer from the kept templates")
        self.assertEqual(1, len(reads), "one read on the second look")
        self.assertIn("PartialObjectMetadataList", reads[0] or "", "and it is metadata only")

    def test_a_new_replicaset_is_read_in_full(self):
        with Kube(replicaset("old", "data-a", "u-1")) as kube:
            STORAGE.claim_references()
            kube.add(replicaset("new", "data-b", "u-2"))
            refs = STORAGE.claim_references()
        self.assertEqual(["ReplicaSet/new"], refs[("lab", "data-b")])
        self.assertEqual(["ReplicaSet/old"], refs[("lab", "data-a")])

    def test_a_replicaset_gone_is_forgotten(self):
        with Kube(replicaset("old", "data-a", "u-1"), replicaset("other", "data-c", "u-3")) as kube:
            STORAGE.claim_references()
            kube.discard("/apis/apps/v1/namespaces/lab/replicasets/old")
            refs = STORAGE.claim_references()
        self.assertNotIn(("lab", "data-a"), refs)
        self.assertNotIn("u-1", {uid for uid, _ in STORAGE._RS["specs"]})

    def test_a_replicaset_template_edited_by_hand_is_read_again(self):
        with Kube(replicaset("bare", "data-a", "u-1")) as kube:
            STORAGE.claim_references()
            edited = kube.object("/apis/apps/v1/namespaces/lab/replicasets/bare")
            edited["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "data-z"
            edited["metadata"]["generation"] = int(edited["metadata"].get("generation") or 1) + 1
            kube.add(edited)
            refs = STORAGE.claim_references()
        self.assertEqual(["ReplicaSet/bare"], refs.get(("lab", "data-z")), "a new generation is read in full")
        self.assertNotIn(("lab", "data-a"), refs)

    def test_the_kept_answer_matches_a_whole_read_for_owner_chains(self):
        # Deployment -> ReplicaSet -> Pod, plus an old ReplicaSet whose template the Deployment no longer names.
        dep = {"apiVersion": "apps/v1", "kind": "Deployment",
               "metadata": {"namespace": "lab", "name": "web", "uid": "d-1"},
               "spec": {"selector": {"matchLabels": {"app": "web"}},
                        "template": {"metadata": {"labels": {"app": "web"}},
                                     "spec": {"containers": [{"name": "c", "image": "example/web:2"}],
                                              "volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "web-new"}}]}}}}
        old = replicaset("web-old", "web-old-data", "r-old")
        old["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "uid": "d-1", "controller": True}]
        new = replicaset("web-new", "web-new", "r-new")
        new["metadata"]["ownerReferences"] = old["metadata"]["ownerReferences"]
        new["spec"]["replicas"] = 1
        pod = {"apiVersion": "v1", "kind": "Pod",
               "metadata": {"namespace": "lab", "name": "web-new-x", "uid": "p-1",
                            "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "web-new", "uid": "r-new", "controller": True}]},
               "spec": {"containers": [{"name": "c", "image": "example/web:2"}],
                        "volumes": [{"name": "d", "persistentVolumeClaim": {"claimName": "web-new"}}]}}
        with Kube(dep, old, new, pod):
            whole = STORAGE.claim_references()                  # the full list, filling what is kept
            kept = STORAGE.claim_references()                   # from the metadata list and what is kept
        self.assertEqual(whole, kept)
        self.assertEqual(["Deployment/web"], kept[("lab", "web-new")])
        self.assertEqual(["ReplicaSet/web-old"], kept[("lab", "web-old-data")], "a claim only an old ReplicaSet names is in use")

    def test_an_unreadable_metadata_list_leaves_replicasets_unread(self):
        with Kube(replicaset("old", "data-a", "u-1")) as kube:
            kube.fail(RS, 500, times=99)
            refs = STORAGE.claim_references()
        self.assertIn("ReplicaSet", refs.get(STORAGE.UNREAD, []))

    def test_a_server_that_ignores_the_metadata_ask_still_works(self):
        # Accept falls back to application/json: a server may answer the whole list.
        with Kube(replicaset("old", "data-a", "u-1")) as kube:
            kube.react("GET", RS, lambda m, p, b: {"kind": "ReplicaSetList", "metadata": {},
                                                   "items": [replicaset("old", "data-a", "u-1")]})
            refs = STORAGE.claim_references()
            refs_again = STORAGE.claim_references()
        self.assertEqual(["ReplicaSet/old"], refs[("lab", "data-a")])
        self.assertEqual(refs, refs_again)

    def test_a_bound_reader_that_is_not_the_kube_client_reads_the_whole_list(self):
        seen = []
        def kget(path):
            seen.append(path)
            return {"metadata": {}, "items": []}
        STORAGE.kget = kget
        STORAGE.claim_references(pods={"metadata": {}, "items": []})
        self.assertIn(RS, seen)


if __name__ == "__main__":
    unittest.main()

"""The Volumes page's extras - its storage classes, the V2 engine and the old
copies a move kept - are asked for on every 15 s refresh. Each was read from
the cluster every time: about ten reads a refresh while the tab is open. A
reading is now kept PAGE_SECONDS, as the main pages' are, so every other
refresh is served the last one; a write Homestead makes to what they show
drops it at once, here and on the other replicas (#978).

The checks made before a write (Longhorn capacity, Set up Longhorn V2) call
v2_engine_status() itself and still read the cluster each time."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))

import homestead_cache as CACHE
import homestead_kube as K
import homestead_reclass as RECLASS
import homestead_routes as ROUTER
import test_page_snapshot_forgets as FORGETS
from test_cycle_readers import Clock
from test_page_snapshot import Cluster, clean_cache, clean_snapshot

LH = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system"
# The routes, and the key each reading is kept under.
EXTRAS = (("/api/storage/classes", "sc:classes"), ("/api/storage/v2", "lhv2"),
          ("/api/volumes/old-copies", "oldcopies"))
KEYS = tuple(key for _path, key in EXTRAS)


def storage_class(name, default=False):
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": name, "annotations": {
                "storageclass.kubernetes.io/is-default-class": "true" if default else "false"}},
            "provisioner": "driver.longhorn.io", "parameters": {"numberOfReplicas": "2"}}


def old_copy(name):
    return {"apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {"name": name, "annotations": {RECLASS.OLD_COPY: "lab/data"}},
            "spec": {"storageClassName": "longhorn-r2", "capacity": {"storage": "1Gi"}},
            "status": {"phase": "Released"}}


class VolumesPage(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        self.clock = Clock(self)
        self.cluster = Cluster(self)
        clean_snapshot(self)
        self.server = self.cluster.server
        v2_off = {"apiVersion": "longhorn.io/v1beta2", "kind": "Setting", "value": "false",
                  "metadata": {"namespace": "longhorn-system", "name": "v2-data-engine"}}
        for obj in (storage_class("longhorn-r2", default=True), storage_class("fast"), old_copy("pv-old"), v2_off):
            self.cluster.kube.add(obj)
        self.table = self.server.MODULE_ROUTES.table()

    def get(self, path):
        return self.table[("GET", path)].handler(None)

    def refresh(self):
        """What the Volumes page asks for besides its main reads."""
        return [self.get(path) for path, _key in EXTRAS]

    def kept(self, *keys):
        return {key for key in keys if CACHE.kept_at(key) is not None}


class ReadsTests(VolumesPage):
    def minute(self):
        """Five refreshes 15 s apart (0 to 60 s): the cluster reads they make,
        and their answers."""
        before, answers = len(self.cluster.gets()), []
        for _ in range(5):
            answers.append(self.refresh())
            self.clock.now += 15
        return len(self.cluster.gets()) - before, answers

    def test_a_minute_of_refreshes_reads_the_cluster_three_times_not_five(self):
        self.refresh()                  # what is read once a process (the API groups)
        clean_cache(self)
        with mock.patch.object(ROUTER, "_cached", [lambda key, seconds, fn: fn()]):
            uncached, was = self.minute()          # as before: read on every refresh
        clean_cache(self)
        kept, now = self.minute()
        per_refresh = uncached // 5
        print(f"\nVolumes page extras, 5 refreshes in 60 s: {uncached} reads uncached, {kept} kept",
              file=sys.stderr)
        self.assertGreaterEqual(per_refresh, 7, "classes, claims, settings, nodes, volumes")
        self.assertEqual(5 * per_refresh, uncached)
        self.assertEqual(3 * per_refresh, kept, "read at 0, 30 and 60 s")
        self.assertEqual(was, now, "the same answers")
        self.assertEqual(["pv-old"], [row["pv"] for row in now[0][2]])

    def test_each_is_kept_as_long_as_the_main_pages_and_no_longer(self):
        self.refresh()
        self.clock.now += ROUTER.PAGE_SECONDS
        self.refresh()
        self.assertEqual({self.clock.now}, {CACHE.kept_at(key) for key in KEYS})

    def test_the_checks_before_a_write_still_read_the_v2_engine_fresh(self):
        self.get("/api/storage/v2")
        for check in (self.server.STORAGE_CLASSES.v2_engine_status, self.server.LHV2_SETUP.status):
            before = len(self.cluster.gets())
            check()
            self.assertGreater(len(self.cluster.gets()), before, check)
        self.assertIs(self.server.STORAGE_CLASSES.v2_engine_status, self.server.LHV2_SETUP.status)

    def test_a_read_under_way_when_a_class_is_written_is_not_kept(self):
        # Real threads: the page's read is part-way when a write lands; what it
        # read before the write must not be served after it.
        import homestead_storage_classes as SC
        reading, written = threading.Event(), threading.Event()
        inner = SC.storage_class_inventory

        def slow():
            rows = inner()
            reading.set()
            written.wait(3)
            return rows
        answers = []
        with mock.patch.object(SC, "storage_class_inventory", slow):
            page = threading.Thread(target=lambda: answers.append(self.get("/api/storage/classes")))
            page.start()
            self.assertTrue(reading.wait(3))
            K.ksend("POST", "/apis/storage.k8s.io/v1/storageclasses", storage_class("new"))
            written.set()
            page.join(3)
        self.assertFalse(page.is_alive())
        self.assertNotIn("new", [row["name"] for row in answers[0]], "read before the write")
        self.assertIn("new", [row["name"] for row in self.get("/api/storage/classes")])


class WritesTests(VolumesPage):
    """Every change Homestead makes to what they show drops the reading."""

    def keep(self):
        self.refresh()
        self.assertEqual(set(KEYS), self.kept(*KEYS))

    def test_a_class_made_the_default_shows_on_the_next_refresh(self):
        self.keep()
        self.table[("POST", "/api/storage/classes/default")].handler(mock.Mock(body={"name": "fast"}))
        self.assertEqual({"lhv2", "oldcopies"}, self.kept(*KEYS))
        rows = {row["name"]: row["default"] for row in self.get("/api/storage/classes")}
        self.assertEqual({"longhorn-r2": False, "fast": True}, rows)

    def test_any_write_to_a_storage_class_drops_the_classes(self):
        # server.py's ISO class and the restore-class repairs write classes too.
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.keep()
            K.fresh_reads(method, "/apis/storage.k8s.io/v1/storageclasses/fast", {}, lambda: None)
            self.assertEqual({"lhv2", "oldcopies"}, self.kept(*KEYS), method)

    def test_a_write_to_a_claim_or_a_backing_volume_drops_the_classes_and_the_old_copies(self):
        # A claim counts towards its class; a move's swap and an old copy's
        # removal write the backing volumes.
        for path in ("/api/v1/namespaces/lab/persistentvolumeclaims/data", "/api/v1/persistentvolumes/pv-old"):
            self.keep()
            K.fresh_reads("PATCH", path, {}, lambda: None)
            self.assertNotIn("sc:classes", self.kept(*KEYS), path)
            self.assertNotIn("oldcopies", self.kept(*KEYS), path)

    def test_removing_an_old_copy_drops_the_old_copies(self):
        self.keep()
        ops, sent = mock.MagicMock(), []
        ops.stored.return_value = []

        def send(method, path, body=None, **options):
            # The forget guard on what the write goes through (the storage
            # guard before it wants the job store's folder).
            return K.fresh_reads(method, path, body, lambda: sent.append((method, path)))
        with mock.patch.object(RECLASS, "kget", K.kget), mock.patch.object(RECLASS, "ksend", send), \
                mock.patch.object(RECLASS, "OPS", ops):
            self.table[("POST", "/api/volumes/old-copies/remove")].handler(mock.Mock(body={"pv": "pv-old"}))
        self.assertEqual([("PATCH", "/api/v1/persistentvolumes/pv-old")], sent)
        self.assertNotIn("oldcopies", self.kept(*KEYS))

    def test_a_write_to_a_host_or_a_longhorn_node_drops_the_v2_engine(self):
        # Hugepages, a cordon, a V2 (block) disk added or removed.
        for path in ("/api/v1/nodes/node-a", LH + "/nodes/node-a"):
            self.keep()
            K.fresh_reads("PATCH", path, {}, lambda: None)
            self.assertNotIn("lhv2", self.kept(*KEYS), path)

    def test_switching_the_v2_engine_on_or_off_drops_it(self):
        for method, path in (("PATCH", LH + "/settings/v2-data-engine"),
                             ("PUT", "/apis/harvesterhci.io/v1beta1/settings/longhorn-v2-data-engine-enabled")):
            self.keep()
            K.fresh_reads(method, path, {}, lambda: None)
            self.assertEqual({"sc:classes", "oldcopies"}, self.kept(*KEYS), path)

    def test_enabling_v2_through_longhorn_capacity_shows_on_the_next_refresh(self):
        self.keep()
        self.assertFalse(self.get("/api/storage/v2")["enabled"])
        self.server.LHCAP.save({"v2": True}, allow_v2_enable=True)
        self.assertTrue(self.get("/api/storage/v2")["enabled"])

    def test_writes_to_other_settings_and_bookkeeping_keep_them(self):
        for path in (LH + "/settings/storage-over-provisioning-percentage",
                     LH + "/settings/v2-data-engine-hugepage-limit",
                     "/api/v1/namespaces/lab/configmaps/homestead-state",
                     "/apis/coordination.k8s.io/v1/namespaces/lab/leases/homestead"):
            self.keep()
            K.fresh_reads("PATCH", path, {}, lambda: None)
            self.assertEqual(set(KEYS), self.kept(*KEYS), path)

    def ended(self, kind, key, answer=("succeeded", 100, "Done")):
        resolver = self.server.OPS.RESOLVERS[kind]
        self.assertIsNotNone(getattr(resolver, "__wrapped__", None), f"{kind}: drops nothing when its job ends")
        with mock.patch.object(resolver, "__wrapped__", lambda item: answer):
            self.keep()
            resolver({"id": "j1", "kind": kind, "ref": {}})
        return key not in self.kept(*KEYS)

    def test_a_job_that_changes_the_v2_engine_drops_it_when_it_ends(self):
        # Host setup (hugepages, kernel modules), the V2 clean-up (hugepages
        # given back), a disk converted to V2, a platform upgrade (Longhorn's
        # version).
        for kind in ("longhorn-v2-prepare", "longhorn-v2-cleanup", "disk-v2-convert", "platform-upgrade"):
            self.assertTrue(self.ended(kind, "lhv2"), kind)
            self.assertFalse(self.ended(kind, "lhv2", ("running", 50, "Working")), kind)

    def test_a_storage_move_that_ends_drops_the_old_copies(self):
        self.assertTrue(self.ended("reclass", "oldcopies"))
        self.assertFalse(self.ended("reclass", "oldcopies", ("running", 82, "Waiting")))

    def test_a_v2_clean_up_rechecked_to_its_end_drops_the_v2_engine(self):
        import homestead_lhv2_cleanup as CLEANUP
        item = {"id": "j1", "kind": CLEANUP.KIND, "ref": {}}
        ops = mock.MagicMock()
        ops.stored.return_value = [item]
        with mock.patch.object(CLEANUP.SETUP, "ops", ops), \
                mock.patch.object(CLEANUP, "public", lambda item: item):
            for answer, dropped in ((("running", 60, "Waiting"), False), (("succeeded", 100, "Done"), True)):
                with mock.patch.object(CLEANUP, "progress", lambda item, answer=answer: answer):
                    self.keep()
                    CLEANUP.recheck("j1")
                    self.assertEqual(dropped, "lhv2" not in self.kept(*KEYS), answer)


class OtherReplicasTests(FORGETS.Forgets):
    """A write on one replica drops the reading on the others too (#928)."""

    def test_their_forgets_are_told_and_heard(self):
        for key in KEYS:
            CACHE.forget_where(lambda k, key=key: k == key)
        self.follow()
        self.assertEqual(set(KEYS), set(self.told()))
        for key in KEYS:
            CACHE.cached(key, ROUTER.PAGE_SECONDS, lambda: "kept")
        self.another_replica_forgot(**{key: time.time() for key in KEYS})
        self.follow()
        self.assertEqual([None] * len(KEYS), [CACHE.kept_at(key) for key in KEYS], "heard here")

    def test_a_class_write_on_a_replica_that_never_read_the_page_is_told(self):
        self.assertFalse(set(KEYS) & set(CACHE._cache))
        K.fresh_reads("POST", "/apis/storage.k8s.io/v1/storageclasses", {}, lambda: None)
        self.follow()
        self.assertIn("sc:classes", self.told())


if __name__ == "__main__":
    unittest.main()

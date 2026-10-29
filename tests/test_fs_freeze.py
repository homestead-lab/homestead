"""Longhorn's filesystem freeze around snapshots (item 5a of spike 5).

Longhorn 1.7 and later can freeze a volume's filesystem for the moment of
each snapshot, its own recurring jobs and backups included, so the copy is
filesystem-consistent instead of crash-consistent. Homestead shows the
cluster setting and each volume's override on Data protection, lets an admin
change them, and marks each write with who made it."""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_changes as CH

AUTHOR = CH.AUTHOR


class Marks(unittest.TestCase):
    """The changedBy mark on objects other than Deployments: a Longhorn
    Setting or Volume, signed over its kind, namespace, name and the value
    written, so a mark left behind by a later change made elsewhere no longer
    checks out."""

    def setUp(self):
        for name, value in (("_author_key", lambda: b"test-key"),
                            ("_authors", lambda: {"users": ["alice", "bob"], "api_keys": ["ci"]})):
            patcher = mock.patch.object(CH, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, CH.REQUEST, "user", None)

    def marked(self, raw):
        return {"metadata": {"annotations": {AUTHOR: raw}}}

    def test_a_mark_reads_back_as_who_made_it(self):
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", "on", now=1000)
        self.assertEqual(json.loads(raw)["at"], 1000)
        self.assertEqual(CH.object_author("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", self.marked(raw), "on"),
                         {"who": "alice", "at": 1000})

    def test_a_mark_for_another_value_does_not_check_out(self):
        """alice turned it on; the chart or kubectl set it back off since."""
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", "on", now=1000)
        self.assertIsNone(CH.object_author("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", self.marked(raw), "off"))

    def test_an_api_key_and_a_job_read_back_too(self):
        CH.REQUEST.user = "api-key:ci"
        raw = CH.object_mark("Volume", "longhorn-system", "pvc-1", "enabled", now=5)
        self.assertEqual(CH.object_author("Volume", "longhorn-system", "pvc-1", self.marked(raw), "enabled")["who"], "api-key:ci")
        CH.REQUEST.user = None
        raw = CH.object_mark("Volume", "longhorn-system", "pvc-1", "enabled", now=5)
        self.assertEqual(CH.object_author("Volume", "longhorn-system", "pvc-1", self.marked(raw), "enabled")["who"], "")

    def test_a_mark_copied_to_another_object_or_kind_does_not_check_out(self):
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Volume", "longhorn-system", "pvc-1", "enabled", now=5)
        self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-2", self.marked(raw), "enabled"))
        self.assertIsNone(CH.object_author("Setting", "longhorn-system", "pvc-1", self.marked(raw), "enabled"))
        self.assertIsNone(CH.object_author("Deployment", "longhorn-system", "pvc-1", self.marked(raw), "enabled"))

    def test_an_altered_or_odd_mark_reads_as_nobody(self):
        CH.REQUEST.user = "alice"
        mark = json.loads(CH.object_mark("Volume", "longhorn-system", "pvc-1", "enabled", now=5))
        for raw in (json.dumps({**mark, "at": 6}), json.dumps({**mark, "by": "0" * 32}), "x" * 300, "not json", None, 7):
            self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1", self.marked(raw), "enabled"), raw)
        self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1", {}, "enabled"))
        self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1", None, "enabled"))

    def test_a_deployment_mark_is_not_an_object_mark(self):
        CH.REQUEST.user = "alice"
        raw = CH.object_mark("Volume", "lab", "app", "enabled", now=5)
        self.assertNotEqual(json.loads(raw)["by"], CH._signed(b"test-key", "lab", "app", 5, "alice"))

    def test_no_signing_key_means_no_mark(self):
        with mock.patch.object(CH, "_author_key", mock.Mock(side_effect=LookupError("no key yet"))):
            self.assertIsNone(CH.object_mark("Volume", "longhorn-system", "pvc-1", "enabled"))
            self.assertIsNone(CH.object_author("Volume", "longhorn-system", "pvc-1",
                                               self.marked('{"at":5,"by":"' + "0" * 32 + '"}'), "enabled"))


class MarkGuard(unittest.TestCase):
    """Every write to a Longhorn Setting or Volume passes the write guard,
    which drops any mark the request brought (the Resources page, an agent)
    and puts one on only for a write made inside CHANGES.marking(value)."""
    SETTING = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/settings/freeze-filesystem-for-snapshot"
    VOLUME = "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/volumes/pvc-1"

    def setUp(self):
        for name, value in (("_author_key", lambda: b"test-key"),
                            ("_authors", lambda: {"users": ["alice", "bob"], "api_keys": []})):
            patcher = mock.patch.object(CH, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, CH.REQUEST, "user", None)

    def test_a_mark_brought_by_the_request_is_dropped(self):
        CH.REQUEST.user = "alice"
        stolen = CH.object_mark("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", "on")
        CH.REQUEST.user = "bob"
        for method, path in (("PATCH", self.SETTING), ("PUT", self.SETTING), ("PATCH", self.VOLUME)):
            body = {"metadata": {"annotations": {AUTHOR: stolen, "keep": "1"}}, "value": "true"}
            CH.note_write(method, path, body)
            self.assertEqual(body["metadata"]["annotations"], {"keep": "1"}, (method, path))
        patch = [{"op": "add", "path": "/metadata/annotations/" + AUTHOR.replace("/", "~1"), "value": stolen}]
        CH.note_write("PATCH", self.VOLUME, patch)
        self.assertEqual(patch, [])

    def test_a_write_inside_marking_is_marked_for_its_value(self):
        CH.REQUEST.user = "alice"
        body = {"value": '{"v1":"true"}'}
        with CH.marking("on"):
            CH.note_write("PATCH", self.SETTING, body)
        found = CH.object_author("Setting", "longhorn-system", "freeze-filesystem-for-snapshot", body, "on")
        self.assertEqual(found["who"], "alice")
        body = {"spec": {"freezeFilesystemForSnapshot": "enabled"}}
        with CH.marking("enabled"):
            CH.note_write("PATCH", self.VOLUME, body)
        self.assertEqual(CH.object_author("Volume", "longhorn-system", "pvc-1", body, "enabled")["who"], "alice")
        self.assertIsNone(getattr(CH.REQUEST, "object_value", None))

    def test_other_longhorn_objects_and_reads_are_left_alone(self):
        body = {"metadata": {"annotations": {AUTHOR: "x"}}}
        CH.note_write("PATCH", "/apis/longhorn.io/v1beta2/namespaces/longhorn-system/nodes/node-1", body)
        CH.note_write("GET", self.SETTING, body)
        self.assertEqual(body["metadata"]["annotations"], {AUTHOR: "x"})


import homestead_fs_freeze as F

LH = F.LH
SETTING_PATH = f"{LH}/settings/{F.SETTING}"


def lh_volume(name, mode="ignored", engine="v1", access="rwo", frontend="blockdev", pv=None, claim="data", state="attached"):
    """A Longhorn volume as the cluster lists it; state=None leaves status.state out."""
    spec = {"freezeFilesystemForSnapshot": mode, "dataEngine": engine, "accessMode": access, "frontend": frontend}
    if mode is None:
        spec.pop("freezeFilesystemForSnapshot")
    status = {"kubernetesStatus": {"namespace": "lab", "pvcName": claim, "pvName": pv or name}}
    if state:
        status["state"] = state
    return {"metadata": {"name": name, "namespace": "longhorn-system"}, "spec": spec, "status": status}


def pv(name, mode="Filesystem"):
    return {"metadata": {"name": name}, "spec": {"volumeMode": mode, "csi": {"driver": "driver.longhorn.io"}}}


def node(name, kernel):
    return {"metadata": {"name": name}, "status": {"nodeInfo": {"kernelVersion": kernel}}}


class Cluster:
    """What Homestead reads, by path; a missing path is a 404, a path set to an
    int is that HTTP error."""

    def __init__(self, value='{"v1":"false"}', annotations=None, version="v1.13.0"):
        self.objects = {
            f"{LH}/volumes": {"items": []},
            f"{LH}/nodes": {"items": [{"metadata": {"name": "node-1"}}, {"metadata": {"name": "node-2"}}]},
            "/api/v1/nodes": {"items": [node("node-1", "6.8.0-45-generic"), node("node-2", "6.8.0-45-generic")]},
            "/api/v1/persistentvolumes": {"items": []},
            f"{LH}/settings/current-longhorn-version": {"value": version},
        }
        if value is not None:
            self.objects[SETTING_PATH] = {"metadata": {"name": F.SETTING, "annotations": annotations or {}}, "value": value}
        self.sent, self.marking = [], []

    def volumes(self, *rows):
        self.objects[f"{LH}/volumes"] = {"items": list(rows)}
        return self

    def pvs(self, *rows):
        self.objects["/api/v1/persistentvolumes"] = {"items": list(rows)}
        return self

    def get(self, path, **options):
        found = self.objects.get(path)
        if found is None or isinstance(found, int):
            raise urllib.error.HTTPError(path, found or 404, "error", {}, None)
        return json.loads(json.dumps(found))

    def send(self, method, path, body=None, ctype=None, **options):
        self.sent.append((method, path, body, ctype))
        self.marking.append(getattr(CH.REQUEST, "object_value", None))
        return {}



class WithCluster(unittest.TestCase):
    def use(self, cluster):
        for name, value in (("kget", cluster.get), ("ksend", cluster.send)):
            patcher = mock.patch.object(F, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return cluster


class Values(unittest.TestCase):
    def test_plain_and_per_engine_values(self):
        self.assertEqual(F.parse_value("true"), (True, "plain"))
        self.assertEqual(F.parse_value("False"), (False, "plain"))
        self.assertEqual(F.parse_value('{"v1":"false"}'), (False, "json"))
        self.assertEqual(F.parse_value('{"v1": "true"}'), (True, "json"))

    def test_odd_values_are_unknown(self):
        for raw in ("", "maybe", None, '{"v2":"true"}', "{broken", '["true"]', '{"v1":true}'):
            self.assertIsNone(F.parse_value(raw)[0], raw)

    def test_a_write_keeps_the_format_it_found(self):
        self.assertEqual(F.encode_value(True, "false"), "true")
        self.assertEqual(F.encode_value(False, "true"), "false")
        self.assertEqual(json.loads(F.encode_value(True, '{"v1":"false"}')), {"v1": "true"})
        self.assertEqual(json.loads(F.encode_value(True, '{"v1":"false","v2":"x"}')), {"v1": "true", "v2": "x"})
        self.assertEqual(json.loads(F.encode_value(True, "{broken")), {"v1": "true"})
        self.assertEqual(F.encode_value(True, ""), "true")

    def test_kernels_at_or_below_5_17_are_old(self):
        for kernel, old in (("5.15.0-91-generic", True), ("5.17.0", True), ("5.18.1", False),
                            ("6.1.0+rpt-rpi-v8", False), ("7.0.0-1020-raspi", False), ("4.19.0", True),
                            ("", None), ("unknown", None), (None, None)):
            self.assertEqual(F.kernel_old(kernel), old, kernel)


class Status(WithCluster):
    def test_longhorn_without_the_setting_says_why(self):
        self.use(Cluster(value=None, version="v1.6.2"))
        d = F.status()
        self.assertFalse(d["supported"])
        self.assertIn("v1.6.2", d["why"])
        self.assertIn("1.7", d["why"])
        self.assertEqual(d["volumes"], [])

    def test_a_setting_that_cannot_be_read_is_an_error_not_unsupported(self):
        cluster = Cluster()
        cluster.objects[SETTING_PATH] = 403
        self.use(cluster)
        with self.assertRaises(urllib.error.HTTPError):
            F.status()

    def test_off_on_this_cluster(self):
        self.use(Cluster().volumes(lh_volume("pvc-a"), lh_volume("pvc-b")).pvs(pv("pvc-a"), pv("pvc-b")))
        d = F.status()
        self.assertTrue(d["supported"])
        self.assertFalse(d["enabled"])
        self.assertEqual(d["version"], "v1.13.0")
        self.assertEqual([v["freezes"] for v in d["volumes"]], ["no", "no"])
        self.assertEqual(d["counts"], {"total": 2, "freezing": 0})

    def test_each_volume_follows_the_cluster_unless_it_says_otherwise(self):
        self.use(Cluster(value="true").volumes(
            lh_volume("pvc-a"), lh_volume("pvc-b", mode="disabled"), lh_volume("pvc-c", mode=None),
            lh_volume("pvc-d", mode="enabled")).pvs(pv("pvc-a"), pv("pvc-b"), pv("pvc-c"), pv("pvc-d")))
        d = F.status()
        self.assertTrue(d["enabled"])
        rows = {v["name"]: v for v in d["volumes"]}
        self.assertEqual(rows["pvc-a"]["freezes"], "yes")
        self.assertEqual(rows["pvc-b"]["freezes"], "no")
        self.assertEqual(rows["pvc-c"]["mode"], "ignored")
        self.assertEqual(rows["pvc-c"]["freezes"], "yes")
        self.assertEqual(rows["pvc-a"]["claim"], "lab/data")
        self.assertEqual(d["counts"], {"total": 4, "freezing": 3})

    def test_an_override_freezes_with_the_cluster_off(self):
        self.use(Cluster().volumes(lh_volume("pvc-a", mode="enabled")).pvs(pv("pvc-a")))
        self.assertEqual(F.status()["volumes"][0]["freezes"], "yes")

    def test_volumes_longhorn_cannot_freeze(self):
        self.use(Cluster(value='{"v1":"true"}').volumes(
            lh_volume("pvc-v2", engine="v2"), lh_volume("pvc-vm"), lh_volume("pvc-rwx", access="rwx"),
            lh_volume("pvc-iscsi", frontend="iscsi")).pvs(pv("pvc-v2"), pv("pvc-vm", "Block"), pv("pvc-rwx"), pv("pvc-iscsi")))
        rows = {v["name"]: v for v in F.status()["volumes"]}
        self.assertEqual(rows["pvc-v2"]["freezes"], "no")
        self.assertIn("V2", rows["pvc-v2"]["why"])
        self.assertEqual(rows["pvc-vm"]["freezes"], "sync")
        self.assertIn("block", rows["pvc-vm"]["why"].lower())
        self.assertEqual(rows["pvc-rwx"]["freezes"], "sync")
        self.assertIn("shared", rows["pvc-rwx"]["why"].lower())
        self.assertEqual(rows["pvc-iscsi"]["freezes"], "sync")

    def test_each_row_says_what_freezing_it_would_do(self):
        """The review dialog counts what a change would do from when_on."""
        self.use(Cluster().volumes(
            lh_volume("pvc-fs"), lh_volume("pvc-v2", engine="v2"), lh_volume("pvc-vm"), lh_volume("pvc-rwx", access="rwx"),
            lh_volume("pvc-off", mode="disabled")).pvs(pv("pvc-fs"), pv("pvc-v2"), pv("pvc-vm", "Block"), pv("pvc-rwx"), pv("pvc-off")))
        rows = {v["name"]: v for v in F.status()["volumes"]}
        self.assertEqual({name: row["when_on"] for name, row in rows.items()},
                         {"pvc-fs": "yes", "pvc-v2": "no", "pvc-vm": "sync", "pvc-rwx": "sync", "pvc-off": "yes"})
        self.assertEqual(rows["pvc-fs"]["freezes"], "no")

    def test_a_detached_volume_freezes_once_attached_not_now(self):
        """While nothing mounts it Longhorn only syncs; the freeze starts when an app uses it (#930)."""
        self.use(Cluster(value="true").volumes(
            lh_volume("pvc-up", state="attached"), lh_volume("pvc-down", state="detached"), lh_volume("pvc-new", state=None),
            lh_volume("pvc-on", mode="enabled", state="detached"), lh_volume("pvc-never", mode="disabled", state="detached"),
            lh_volume("pvc-vm", state="detached"), lh_volume("pvc-v2", engine="v2", state="detached")).pvs(
            pv("pvc-up"), pv("pvc-down"), pv("pvc-new"), pv("pvc-on"), pv("pvc-never"), pv("pvc-vm", "Block"), pv("pvc-v2")))
        d = F.status()
        rows = {v["name"]: v for v in d["volumes"]}
        self.assertEqual({n: r["freezes"] for n, r in rows.items()},
                         {"pvc-up": "yes", "pvc-down": "later", "pvc-new": "later", "pvc-on": "later", "pvc-never": "no",
                          "pvc-vm": "sync", "pvc-v2": "no"})
        self.assertIn("not attached", rows["pvc-down"]["why"].lower())
        self.assertIn("then freezes", rows["pvc-down"]["why"].lower())
        self.assertEqual([r["attached"] for r in (rows["pvc-up"], rows["pvc-down"], rows["pvc-new"])], [True, False, False],
                         "a volume Longhorn has not yet said is attached is not counted as attached")
        self.assertEqual(rows["pvc-down"]["when_on"], "yes")
        self.assertEqual(d["counts"]["freezing"], 1, "a detached volume is not frozen now")

    def test_only_a_volume_longhorn_says_is_attached_freezes_now(self):
        """creating, attaching and detaching are not attached yet (or any more): the
        freeze waits for the volume to be attached, as for a detached one."""
        states = ("attached", "creating", "attaching", "detaching", "detached", "deleting", "unknown")
        self.use(Cluster(value="true").volumes(*(lh_volume(f"pvc-{s}", state=s) for s in states)).pvs(
            *(pv(f"pvc-{s}") for s in states)))
        rows = {v["name"]: v for v in F.status()["volumes"]}
        self.assertEqual({n: (r["attached"], r["freezes"]) for n, r in rows.items()},
                         {"pvc-attached": (True, "yes"), **{f"pvc-{s}": (False, "later") for s in states[1:]}})
        self.assertEqual("Not attached: Longhorn only syncs it until an app uses it, then freezes it",
                         rows["pvc-attaching"]["why"])

    def test_a_detached_volume_set_to_never_still_says_never(self):
        self.use(Cluster(value="true").volumes(lh_volume("pvc-never", mode="disabled", state="detached")).pvs(pv("pvc-never")))
        row = F.status()["volumes"][0]
        self.assertEqual((row["freezes"], row["why"]), ("no", "Set to never freeze"))

    def test_a_volume_without_a_pv_or_kubernetes_status_is_still_listed(self):
        bare = {"metadata": {"name": "pvc-bare"}, "spec": {"freezeFilesystemForSnapshot": "ignored"}}
        self.use(Cluster(value="true").volumes(bare, lh_volume("pvc-x", pv="pv-gone")))
        rows = {v["name"]: v for v in F.status()["volumes"]}
        self.assertEqual(rows["pvc-bare"]["claim"], "")
        self.assertEqual(rows["pvc-bare"]["volume_mode"], "unknown")
        self.assertEqual(rows["pvc-bare"]["freezes"], "later", "no state from Longhorn: not known to be attached")
        self.assertEqual(rows["pvc-x"]["volume_mode"], "unknown")

    def test_an_unknown_value_is_said_and_counts_as_off(self):
        self.use(Cluster(value="maybe").volumes(lh_volume("pvc-a")).pvs(pv("pvc-a")))
        d = F.status()
        self.assertTrue(d["unknown_value"])
        self.assertFalse(d["enabled"])
        self.assertEqual(d["value"], "maybe")

    def test_old_kernels_on_longhorn_hosts_are_named(self):
        cluster = Cluster()
        cluster.objects["/api/v1/nodes"]["items"] = [node("node-1", "5.15.0-91-generic"), node("node-2", "6.8.0"),
                                                      node("not-storage", "5.4.0")]
        self.use(cluster)
        self.assertEqual(F.status()["old_kernels"], [{"host": "node-1", "kernel": "5.15.0-91-generic"}])

    def test_a_value_the_longhorn_chart_sets_is_said(self):
        cluster = Cluster()
        self.use(cluster)
        self.assertFalse(F.status()["from_chart"])
        cluster.objects["/api/v1/namespaces/longhorn-system/configmaps/longhorn-default-setting"] = {
            "data": {"default-setting.yaml": 'default-replica-count: "2"\nfreeze-filesystem-for-snapshot: "true"\n'}}
        self.assertTrue(F.status()["from_chart"])
        cluster.objects["/api/v1/namespaces/longhorn-system/configmaps/longhorn-default-setting"] = {
            "data": {"default-setting.yaml": 'default-replica-count: "2"\n'}}
        self.assertFalse(F.status()["from_chart"])

    def test_who_changed_it_when_the_mark_checks_out_for_the_value_it_holds(self):
        with mock.patch.object(F.CHANGES, "object_author", return_value={"who": "alice", "at": 5}) as author:
            self.use(Cluster(value='{"v1":"true"}'))
            self.assertEqual(F.status()["changed"], {"who": "alice", "at": 5})
        self.assertEqual(author.call_args[0][:3], ("Setting", "longhorn-system", F.SETTING))
        self.assertEqual(author.call_args[0][4], "on")

    def test_on_with_no_mark_that_checks_out_was_changed_outside_homestead(self):
        with mock.patch.object(F.CHANGES, "object_author", return_value=None):
            self.use(Cluster(value="true"))
            self.assertEqual(F.status()["changed"], {"outside": True})
            self.use(Cluster(value="false"))
            self.assertIsNone(F.status()["changed"])

    def test_each_override_says_who_set_it_or_that_it_was_set_elsewhere(self):
        def author(kind, ns, name, obj, value):
            return {"who": "bob", "at": 7} if name == "pvc-a" else None
        with mock.patch.object(F.CHANGES, "object_author", side_effect=author):
            self.use(Cluster().volumes(lh_volume("pvc-a", mode="enabled"), lh_volume("pvc-b", mode="disabled"),
                                       lh_volume("pvc-c")))
            rows = {v["name"]: v for v in F.status()["volumes"]}
        self.assertEqual(rows["pvc-a"]["changed"], {"who": "bob", "at": 7})
        self.assertEqual(rows["pvc-b"]["changed"], {"outside": True})
        self.assertIsNone(rows["pvc-c"]["changed"])


class Writes(WithCluster):
    def test_turning_it_on_keeps_the_per_engine_format_and_is_marked_for_on(self):
        cluster = self.use(Cluster(value='{"v1":"false"}'))
        result = F.save(True)
        self.assertEqual(cluster.sent, [("PATCH", SETTING_PATH, {"value": '{"v1":"true"}'}, F.MERGE)])
        self.assertEqual(cluster.marking, ["on"])
        self.assertIsNone(getattr(CH.REQUEST, "object_value", None))
        self.assertTrue(result["ok"])
        self.assertTrue(result["enabled"])

    def test_turning_it_off_on_a_plain_setting(self):
        cluster = self.use(Cluster(value="true"))
        self.assertFalse(F.save(False)["enabled"])
        self.assertEqual(cluster.sent[0][2]["value"], "false")
        self.assertEqual(cluster.marking, ["off"])

    def test_the_change_is_said_to_last_only_until_the_chart_when_the_chart_sets_it(self):
        cluster = Cluster(value="false")
        cluster.objects[F.CHART_SETTINGS] = {"data": {"default-setting.yaml": "freeze-filesystem-for-snapshot: false\n"}}
        self.use(cluster)
        self.assertIn("chart", F.save(True)["detail"])

    def test_refusals_write_nothing(self):
        cluster = self.use(Cluster(value="false").volumes(lh_volume("pvc-a"), lh_volume("pvc-v2", engine="v2")))
        for bad in ("true", "false", 1, None):
            with self.assertRaises(ValueError):
                F.save(bad)
        for volume, mode in (("pvc-a", "on"), ("pvc-a", ""), ("pvc-a", None), ("pvc-gone", "enabled"),
                             ("pvc-v2", "enabled"), ("", "enabled"), (None, "enabled")):
            with self.assertRaises(ValueError, msg=(volume, mode)):
                F.save_volume(volume, mode)
        self.assertEqual(cluster.sent, [])

    def test_longhorn_without_the_setting_refuses_a_change(self):
        cluster = self.use(Cluster(value=None))
        with self.assertRaises(ValueError) as said:
            F.save(True)
        self.assertIn("1.7", str(said.exception))
        with self.assertRaises(ValueError):
            F.save_volume("pvc-a", "enabled")
        self.assertEqual(cluster.sent, [])

    def test_a_volume_override_is_marked_too(self):
        cluster = self.use(Cluster().volumes(lh_volume("pvc-a", claim="db")))
        result = F.save_volume("pvc-a", "enabled")
        self.assertEqual(cluster.sent, [("PATCH", f"{LH}/volumes/pvc-a", {
            "spec": {"freezeFilesystemForSnapshot": "enabled"}}, F.MERGE)])
        self.assertEqual(cluster.marking, ["enabled"])
        self.assertEqual((result["volume"], result["mode"]), ("pvc-a", "enabled"))
        self.assertIn("lab/db", result["detail"])

    def test_a_v2_volume_may_go_back_to_following_the_cluster(self):
        cluster = self.use(Cluster().volumes(lh_volume("pvc-v2", engine="v2", mode="disabled")))
        F.save_volume("pvc-v2", "ignored")
        self.assertEqual(cluster.sent[0][2]["spec"], {"freezeFilesystemForSnapshot": "ignored"})


class Routes(unittest.TestCase):
    def test_who_may_read_and_change_it(self):
        self.assertEqual({key: role for key, (role, _) in F.ROUTES.items()}, {
            ("GET", "/api/longhorn/fs-freeze"): "viewer",
            ("POST", "/api/longhorn/fs-freeze"): "admin",
            ("POST", "/api/longhorn/fs-freeze/volume"): "admin"})

    def test_the_module_is_read_for_its_routes(self):
        import homestead_routes
        self.assertIn("homestead_fs_freeze", homestead_routes.MODULES)


    def test_a_change_forgets_the_cached_answer_before_and_after_the_write(self):
        request = mock.Mock(body={"enabled": True})
        with mock.patch.object(F, "save", return_value={"ok": True}) as save, \
                mock.patch.object(F.homestead_routes, "forget") as forget:
            F.ROUTES[("POST", "/api/longhorn/fs-freeze")][1](request)
        save.assert_called_once_with(True)
        self.assertEqual(forget.call_args_list, [mock.call("fsfreeze")] * 2)
        request = mock.Mock(body={"volume": "pvc-a", "mode": "enabled"})
        with mock.patch.object(F, "save_volume", return_value={"ok": True}) as save, \
                mock.patch.object(F.homestead_routes, "forget") as forget:
            F.ROUTES[("POST", "/api/longhorn/fs-freeze/volume")][1](request)
        save.assert_called_once_with("pvc-a", "enabled")
        self.assertEqual(forget.call_args_list, [mock.call("fsfreeze")] * 2)



class OnlyHereIsItChanged(unittest.TestCase):
    """Every snapshot Homestead takes, by any path, asks Longhorn, which reads
    the volume's field and then the setting. So nothing else may set them:
    a path that turned the freeze off for its own snapshot would quietly
    make them crash-consistent again."""

    def test_no_other_module_writes_the_setting_or_the_field(self):
        server = Path(__file__).resolve().parents[1] / "server"
        found = [p.name for p in server.glob("*.py") if p.name != "homestead_fs_freeze.py"
                 and ("freezeFilesystemForSnapshot" in p.read_text() or SETTINGS_NAME in p.read_text())]
        self.assertEqual(found, [])


SETTINGS_NAME = "freeze-filesystem-for-snapshot"


if __name__ == "__main__":
    unittest.main()

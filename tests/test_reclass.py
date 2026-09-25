import copy
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_reclass as RC

CLASSES = [{"name": "longhorn-r2", "provisioner": "driver.longhorn.io", "replicas": "2", "migratable": True,
            "internal": False, "reclaim": "Delete"},
           {"name": "longhorn-r3", "provisioner": "driver.longhorn.io", "replicas": "3", "migratable": False,
            "internal": False, "reclaim": "Delete"}]


class Cluster:
    """Just enough Kubernetes: claims bind to volumes, deletes take effect,
    scaling to zero removes the pods, and the copy job finishes as told."""

    def __init__(self, job_log="==> copying\n  1,000  100%  90.00MB/s\n==> verifying\n==> verified\n", job_ok=True):
        self.pvcs = {"frigate-config": {"metadata": {"name": "frigate-config", "labels": {"app": "frigate"},
                                                     "annotations": {"pv.kubernetes.io/bind-completed": "yes"}},
                                        "spec": {"storageClassName": "longhorn-r2", "volumeName": "pv-old",
                                                 "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                                                 "resources": {"requests": {"storage": "20Gi"}}},
                                        "status": {"phase": "Bound", "capacity": {"storage": "20Gi"}}}}
        self.pvs = {"pv-old": {"metadata": {"name": "pv-old", "annotations": {}},
                               "spec": {"persistentVolumeReclaimPolicy": "Delete", "storageClassName": "longhorn-r2",
                                        "capacity": {"storage": "20Gi"},
                                        "claimRef": {"name": "frigate-config", "namespace": "lab"}},
                               "status": {"phase": "Bound"}}}
        self.deps = {"frigate": {"metadata": {"name": "frigate"},
                                 "spec": {"replicas": 1, "template": {"spec": {"volumes": [
                                     {"name": "c", "persistentVolumeClaim": {"claimName": "frigate-config"}}]}}},
                                 "status": {"readyReplicas": 1}}}
        self.jobs, self.job_log, self.job_ok = {}, job_log, job_ok
        self.sent = []
        RC.bind(self.get, self.send, self.text, lambda: CLASSES,
                lambda: {"largest": {"1": 200, "2": 150, "3": 100}}, own_ns="homestead")

    # ---- reads
    def get(self, path):
        ns = "/api/v1/namespaces/lab/"
        if path == ns + "pods":
            return {"items": [{"metadata": {"name": "frigate-abc", "ownerReferences": [{"kind": "ReplicaSet"}]},
                               "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "frigate-config"}}]},
                               "status": {"phase": "Running"}}] if self.deps["frigate"]["spec"]["replicas"] else []}
        if path.startswith(ns + "pods?"):
            return {"items": [{"metadata": {"name": "copy-pod"}, "status": {"phase": "Running"}}]}
        if path.startswith(ns + "persistentvolumeclaims/"):
            return self._or_404(self.pvcs.get(path.rsplit("/", 1)[1]), path)
        if path.startswith("/api/v1/persistentvolumes/"):
            return self._or_404(self.pvs.get(path.rsplit("/", 1)[1]), path)
        if path == "/api/v1/persistentvolumes":
            return {"items": list(self.pvs.values())}
        if path.endswith("/deployments"):
            return {"items": list(self.deps.values())}
        if "/deployments/" in path:
            return self.deps[path.rsplit("/", 1)[1]]
        if "/jobs/" in path:
            return self._or_404(self.jobs.get(path.rsplit("/", 1)[1].split("?")[0]), path)
        if path.endswith("/volumes"):
            return {"items": [{"status": {"actualSize": 6 * 1024 ** 3,
                                          "kubernetesStatus": {"pvcName": "frigate-config", "namespace": "lab"}}}]}
        return {"items": []}

    def _or_404(self, obj, path):
        if obj is None:
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        return copy.deepcopy(obj)

    def text(self, path):
        return self.job_log

    # ---- writes
    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path.split("?")[0], body))
        name = path.split("?")[0].rsplit("/", 1)[1]
        if method == "POST" and path.endswith("/persistentvolumeclaims"):
            claim = copy.deepcopy(body)
            pv = claim["spec"].get("volumeName") or "pv-new"
            claim["spec"]["volumeName"] = pv
            claim["status"] = {"phase": "Bound"}
            self.pvcs[claim["metadata"]["name"]] = claim
            self.pvs.setdefault(pv, {"metadata": {"name": pv, "annotations": {}},
                                     "spec": {"persistentVolumeReclaimPolicy": "Delete"}, "status": {}})
            self.pvs[pv]["spec"]["claimRef"] = {"name": claim["metadata"]["name"]}
            self.pvs[pv]["status"]["phase"] = "Bound"
        elif method == "POST" and path.endswith("/jobs"):
            self.jobs[body["metadata"]["name"]] = {"status": {"succeeded": 1} if self.job_ok else {"failed": 1}}
        elif method == "DELETE" and "/persistentvolumeclaims/" in path:
            gone = self.pvcs.pop(name, None)
            if gone:
                self.pvs[gone["spec"]["volumeName"]]["status"]["phase"] = "Released"
        elif method == "DELETE" and "/jobs/" in path:
            self.jobs.pop(name, None)
        elif method == "PATCH" and "/persistentvolumes/" in path:
            pv = self.pvs[name]
            for key, value in (body.get("spec") or {}).items():
                if value is None:
                    pv["spec"].pop(key, None)
                else:
                    pv["spec"][key] = value
            pv["metadata"]["annotations"].update((body.get("metadata") or {}).get("annotations") or {})
        elif method == "PATCH" and "/deployments/" in path:
            self.deps[name]["spec"].update(body["spec"])
        return body


class OPS:
    def __init__(self):
        self.started = []

    def list_operations(self):
        return []

    def start(self, kind, title, resource, href, ref, message=""):
        item = {"id": "op", "kind": kind, "title": title, "resource": resource, "ref": ref, "status": "running"}
        self.started.append(item)
        return item


def run(item, limit=30):
    """Advance the operation until it finishes, as the poll would."""
    for _ in range(limit):
        status, progress, message = RC.resolve(item)
        item.update(status=status, progress=progress, message=message)
        if status != "running":
            return item
    raise AssertionError("never finished: " + item["message"])


class ReclassTests(unittest.TestCase):
    def test_the_review_names_what_uses_it_and_the_room_it_takes(self):
        Cluster()
        plan = RC.plan("lab", "frigate-config", "longhorn-r3")
        self.assertTrue(plan["ok"], plan["blockers"])
        self.assertEqual([{"kind": "Deployment", "name": "frigate", "replicas": 1, "running": True}], plan["consumers"])
        self.assertEqual((20.0, 6.0, 3, 60.0, 100), (plan["space"]["size_gb"], plan["space"]["used_gb"],
                         plan["space"]["replicas"], plan["space"]["allocated_gb"], plan["space"]["room_gb"]))

    def test_no_room_or_a_shared_folder_on_a_migratable_class_is_refused(self):
        c = Cluster()
        RC.capacity = lambda: {"largest": {"3": 10}}
        self.assertIn("room for a 10 GB", RC.plan("lab", "frigate-config", "longhorn-r3")["blockers"][0])
        c.pvcs["frigate-config"]["spec"].update(storageClassName="longhorn-r3", accessModes=["ReadWriteMany"])
        self.assertIn("migratable", RC.plan("lab", "frigate-config", "longhorn-r2")["blockers"][0])

    def test_a_move_copies_checks_swaps_and_starts_everything_again(self):
        c = Cluster()
        ops = OPS()
        item = RC.start("lab", "frigate-config", "longhorn-r3", ops)
        run(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        claim = c.pvcs["frigate-config"]
        # Same name, new class, bound to the volume the copy went into.
        self.assertEqual(("longhorn-r3", "pv-new"), (claim["spec"]["storageClassName"], claim["spec"]["volumeName"]))
        self.assertEqual({"app": "frigate"}, claim["metadata"]["labels"])
        self.assertNotIn("frigate-config-reclass", c.pvcs)
        # The old volume is kept, released, marked as the old copy; the new one deletes with its claim again.
        self.assertEqual(("Retain", "Released"), (c.pvs["pv-old"]["spec"]["persistentVolumeReclaimPolicy"],
                                                  c.pvs["pv-old"]["status"]["phase"]))
        self.assertEqual("Delete", c.pvs["pv-new"]["spec"]["persistentVolumeReclaimPolicy"])
        self.assertEqual([{"pv": "pv-old", "was": "lab/frigate-config", "storage_class": "longhorn-r2",
                           "size": "20Gi", "since": ""}], RC.old_copies())
        self.assertEqual(1, c.deps["frigate"]["spec"]["replicas"])
        self.assertEqual("pv-old", item["old_pv"])
        self.assertTrue(all(s["state"] == "done" for s in item["steps"]))

    def test_a_copy_that_does_not_match_changes_nothing(self):
        c = Cluster(job_log="==> verifying\n==> differs: config.yml\n", job_ok=False)
        item = run(RC.start("lab", "frigate-config", "longhorn-r3", OPS()))
        self.assertEqual("failed", item["status"])
        self.assertIn("did not match", item["message"])
        self.assertEqual(("longhorn-r2", "pv-old"), (c.pvcs["frigate-config"]["spec"]["storageClassName"],
                                                     c.pvcs["frigate-config"]["spec"]["volumeName"]))
        self.assertNotIn("frigate-config-reclass", c.pvcs)
        self.assertEqual(1, c.deps["frigate"]["spec"]["replicas"], "started again on the original")

    def test_a_succeeded_job_without_a_verified_copy_is_not_trusted(self):
        Cluster(job_log="==> copying\n")
        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        for _ in range(6):
            RC.resolve(item)
        self.assertEqual("copy", item["ref"]["phase"])

    def test_a_copy_that_never_starts_puts_everything_back(self):
        # A job counts a pod stuck waiting for its volume as active; that is
        # not a copy under way.
        c = Cluster(job_log="")
        c.job_ok = None
        original = c.send

        def send(method, path, body=None, **kw):
            result = original(method, path, body, **kw)
            if method == "POST" and path.endswith("/jobs"):
                c.jobs[body["metadata"]["name"]] = {"status": {"active": 1}}
            return result
        c.send = send
        pending = c.get
        c_get = lambda path: ({"items": [{"metadata": {"name": "copy-pod"}, "status": {"phase": "Pending"}}]}
                              if "pods?" in path else pending(path))
        RC.bind(c_get, send, c.text, lambda: CLASSES, None)
        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        for _ in range(5):
            RC.resolve(item)
        self.assertEqual("copy", item["ref"]["phase"])
        item["ref"]["copy_since"] -= RC.START_LIMIT + 1
        status, _, message = RC.resolve(item)
        self.assertEqual("failed", status)
        self.assertIn("never started", message)
        self.assertEqual(1, c.deps["frigate"]["spec"]["replicas"])
        self.assertNotIn("frigate-config-reclass", c.pvcs)

    def test_the_old_copy_is_removed_the_way_its_class_removes_volumes(self):
        c = Cluster()
        run(RC.start("lab", "frigate-config", "longhorn-r3", OPS()))
        RC.remove_old_copy("pv-old")
        self.assertEqual("Delete", c.pvs["pv-old"]["spec"]["persistentVolumeReclaimPolicy"])
        with self.assertRaisesRegex(ValueError, "not an old copy"):
            RC.remove_old_copy("pv-new")

    def test_progress_is_read_from_the_copys_own_output(self):
        self.assertEqual({"percent": 57, "speed": "11.83MB/s", "verifying": False, "verified": False},
                         RC.copy_progress("==> copying\n  1,234,567  57%  11.83MB/s  0:00:04\n", 0))
        dd = "10737418240 bytes (11 GB, 10 GiB) copied, 60 s, 179 MB/s"
        self.assertEqual(50, RC.copy_progress(dd, 20 * 1024 ** 3)["percent"])

    def test_a_vm_on_a_datavolume_is_halted_and_made_to_mount_the_claim_itself(self):
        vm = {"metadata": {"name": "win11", "annotations": {"harvesterhci.io/volumeClaimTemplates":
                                                              '[{"metadata": {"name": "win11-disk"}}, {"metadata": {"name": "other"}}]'}},
              "spec": {"runStrategy": "RerunOnFailure",
                       "dataVolumeTemplates": [{"metadata": {"name": "win11-disk"}}],
                       "template": {"spec": {"volumes": [{"name": "root", "dataVolume": {"name": "win11-disk"}}]}}}}
        sent = []

        def get(path):
            if "/virtualmachines/" in path:
                return copy.deepcopy(vm)
            if "/persistentvolumeclaims/" in path:
                return {"metadata": {"ownerReferences": [{"kind": "DataVolume", "name": "win11-disk"}]}}
            raise AssertionError(path)
        RC.bind(get, lambda m, p, b=None, **k: sent.append((m, p, b)), None, lambda: CLASSES)
        ref = {"claim": "win11-disk", "consumers": [{"kind": "VirtualMachine", "name": "win11",
                                                     "run_strategy": "RerunOnFailure", "via": "dv"}]}
        RC._stop("lab", ref)
        put = next(b for m, p, b in sent if m == "PUT")
        self.assertEqual("Halted", put["spec"]["runStrategy"])
        self.assertEqual({"claimName": "win11-disk"}, put["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"])
        self.assertNotIn("dataVolumeTemplates", put["spec"])
        self.assertNotIn("win11-disk", put["metadata"]["annotations"]["harvesterhci.io/volumeClaimTemplates"])
        released = next(b for m, p, b in sent if m == "PATCH" and "persistentvolumeclaims" in p)
        self.assertEqual({"metadata": {"ownerReferences": None}}, released)
        self.assertEqual("Orphan", next(b for m, p, b in sent if m == "DELETE")["propagationPolicy"])

    def test_a_raw_disk_is_copied_block_for_block(self):
        _, job = RC.job_body("lab", {"claim": "vm-disk", "temp": "vm-disk-reclass", "mode": "Block"})
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("conv=sparse", container["command"][-1])
        self.assertIn("cmp /dev/src /dev/dst", container["command"][-1])
        self.assertEqual(["/dev/src", "/dev/dst"], [d["devicePath"] for d in container["volumeDevices"]])


class NotFoundPartWayTests(unittest.TestCase):
    """The operations poll failed the whole job on any 404, which left a
    move stopped half-way through its swap with nothing to carry it on."""

    def flaky(self, cluster, where, times):
        """Answer 404 to writes on a path containing `where`, `times` times."""
        real = cluster.send
        left = {"n": times}

        def send(method, path, body=None, **kw):
            if where in path and left["n"] > 0:
                left["n"] -= 1
                raise urllib.error.HTTPError("https://k8s" + path, 404, "not found", None, None)
            return real(method, path, body, **kw)
        RC.ksend = send

    def advance_to(self, item, phase):
        for _ in range(30):
            if item["ref"].get("phase") == phase:
                return
            item.update(zip(("status", "progress", "message"), RC.resolve(item)))
        raise AssertionError(f"never reached {phase}")

    def test_a_passing_not_found_during_the_swap_is_tried_again(self):
        c = Cluster()
        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        self.advance_to(item, "swap")
        self.flaky(c, "/persistentvolumes/pv-old", 2)

        status, _, message = RC.resolve(item)
        self.assertEqual("running", status)
        self.assertIn("did not find persistentvolumes pv-old", message)

        run(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertEqual("pv-new", c.pvcs["frigate-config"]["spec"]["volumeName"])

    def test_a_lasting_not_found_after_the_swap_began_stops_and_can_carry_on(self):
        c = Cluster()
        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        self.advance_to(item, "swap")
        self.flaky(c, "/persistentvolumes/pv-old", RC.MISS_LIMIT)

        run(item)
        self.assertEqual("failed", item["status"])
        self.assertIn("Stopped at 'swap'", item["message"])
        self.assertEqual("", RC.resumable(item))

        RC.ksend = c.send            # whatever was missing is back
        item["status"] = "running"
        run(item)
        self.assertEqual("succeeded", item["status"], item["message"])

    def test_a_lasting_not_found_before_the_swap_puts_everything_back(self):
        c = Cluster()
        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        self.advance_to(item, "create")
        self.flaky(c, "/persistentvolumeclaims", RC.MISS_LIMIT)

        run(item)
        self.assertEqual("failed", item["status"])
        self.assertIn("Nothing changed", item["message"])
        self.assertEqual("rolled-back", item["ref"]["phase"])
        self.assertNotEqual("", RC.resumable(item))
        self.assertEqual(1, c.deps["frigate"]["spec"]["replicas"])

    def test_a_workload_started_again_mid_swap_is_stopped_again(self):
        """Kubernetes keeps a deleted claim while a pod mounts it, and the
        swap sat at "Letting go of the original" with no word of why."""
        c = Cluster()
        real_send, real_get = c.send, c.get

        def send(method, path, body=None, **kw):
            if method == "DELETE" and path.endswith("/persistentvolumeclaims/frigate-config")                     and c.deps["frigate"]["spec"]["replicas"]:
                c.pvcs["frigate-config"]["metadata"]["deletionTimestamp"] = "now"
                c.pvcs["frigate-config"]["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
                return body
            return real_send(method, path, body, **kw)

        def get(path):
            claim = c.pvcs.get("frigate-config") or {}
            if (claim.get("metadata") or {}).get("deletionTimestamp") and not c.deps["frigate"]["spec"]["replicas"]:
                real_send("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config")
            return real_get(path)
        RC.ksend, RC.kget = send, get

        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        self.advance_to(item, "swap")
        c.deps["frigate"]["spec"]["replicas"] = 1          # started again by hand
        messages = []
        for _ in range(30):
            item.update(zip(("status", "progress", "message"), RC.resolve(item)))
            messages.append(item["message"])
            if item["status"] != "running":
                break
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertTrue(any("frigate had started again" in m for m in messages), messages)
        self.assertEqual("pv-new", c.pvcs["frigate-config"]["spec"]["volumeName"])
        self.assertEqual(1, c.deps["frigate"]["spec"]["replicas"])

    def test_the_file_browser_holding_the_claim_is_closed(self):
        """Volumes > Files leaves a helper pod mounting the claim for half an
        hour; the swap waited on it, telling you to stop it yourself."""
        c = Cluster()
        helper = {"metadata": {"name": "homestead-files-frigate-config",
                               "labels": {"homestead.io/task": "files", "homestead.io/app": "frigate-config"}},
                  "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "frigate-config"}}]},
                  "status": {"phase": "Running"}}
        state = {"helper": True}
        real_get, real_send = c.get, c.send

        def get(path):
            if path == "/api/v1/namespaces/lab/pods":
                pods = real_get(path)["items"]
                return {"items": pods + ([helper] if state["helper"] else [])}
            claim = c.pvcs.get("frigate-config") or {}
            if (claim.get("metadata") or {}).get("deletionTimestamp") and not state["helper"]:
                real_send("DELETE", "/api/v1/namespaces/lab/persistentvolumeclaims/frigate-config")
            return real_get(path)

        def send(method, path, body=None, **kw):
            if method == "DELETE" and "/pods/homestead-files-" in path:
                state["helper"] = False
                return body
            if method == "DELETE" and path.endswith("/persistentvolumeclaims/frigate-config") and state["helper"]:
                c.pvcs["frigate-config"]["metadata"]["deletionTimestamp"] = "now"
                return body
            return real_send(method, path, body, **kw)
        RC.kget, RC.ksend = get, send

        review = RC.plan("lab", "frigate-config", "longhorn-r3")
        self.assertTrue(review["ok"], review["blockers"])
        self.assertTrue(any("file browser" in w for w in review["warnings"]))
        item = RC.start("lab", "frigate-config", "longhorn-r3", OPS())
        self.advance_to(item, "swap")
        state["helper"] = True                     # opened again mid-move
        run(item)
        self.assertEqual("succeeded", item["status"], item["message"])
        self.assertFalse(state["helper"])

    def test_each_attempt_has_a_copy_job_of_its_own(self):
        Cluster()
        first = RC.start("lab", "frigate-config", "longhorn-r3", OPS())["ref"]["job_name"]
        second = RC.start("lab", "frigate-config", "longhorn-r3", OPS())["ref"]["job_name"]
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("frigate-config-reclass-"))
        self.assertLessEqual(len(first), 63)


if __name__ == "__main__":
    unittest.main()

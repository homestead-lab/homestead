"""A VM copied while it runs, and a VM whose firmware or TPM state cannot travel."""
import unittest
from unittest import mock

import test_cluster_copy as base
from test_move_flow import source, client
import homestead_move_state as STATE
import homestead_move_plan as PLAN
import homestead_move_phases as PHASE

FREEZE = "/apis/subresources.kubevirt.io/v1/namespaces/lab/virtualmachineinstances/desktop/freeze"
UNFREEZE = FREEZE.replace("/freeze", "/unfreeze")


class LiveCopyTests(unittest.TestCase):
    setUp = base.ClusterCopyTests.setUp
    LIVE = True
    seed_vm = base.ClusterCopyTests.seed_vm
    vm_path = staticmethod(base.ClusterCopyTests.vm_path)
    vmi_path = staticmethod(base.ClusterCopyTests.vmi_path)

    def run_copy(self, move_id):
        """Tick without stopping anything by hand: a live copy needs nothing stopped."""
        strategies = set()
        for _ in range(40):
            current = STATE._find(move_id)
            if current["status"] != "running":
                return STATE._public(current), strategies
            PHASE.tick_all()
            strategies.add(self.cluster.get(self.vm_path())["spec"]["runStrategy"])
            self.assertIn(self.vmi_path(), self.cluster.objects, "the source VM was stopped")
        self.fail("Copy did not settle")

    def agent(self, connected=True):
        vmi = self.cluster.objects[self.vmi_path()]
        vmi["status"]["conditions"] = [{"type": "AgentConnected", "status": "True" if connected else "False"}]

    def snapshots_appear(self):
        """Longhorn takes each snapshot as soon as it is asked for."""
        made = self.lh.ensure_move_backup
        def ensure(volume, snapshot, backup, **kwargs):
            self.cluster.put(f"{source.LH_API}/namespaces/{source.LHNS}/snapshots/{snapshot}",
                             {"metadata": {"name": snapshot}, "spec": {"volume": volume},
                              "status": {"creationTime": "2026-11-06T00:00:00Z", "readyToUse": True}})
            return made(volume, snapshot, backup, **kwargs)
        patch = mock.patch.object(self.lh, "ensure_move_backup", side_effect=ensure)
        patch.start(); self.addCleanup(patch.stop)

    def test_running_vm_is_copied_without_stopping_and_says_the_disks_are_crash_consistent(self):
        self.seed_vm()
        review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertTrue(review["ok"], review["blockers"])
        self.assertTrue(review["live"])
        self.assertTrue(any("power cut" in w for w in review["warnings"]), review["warnings"])
        move = PLAN.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        done, strategies = self.run_copy(move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertEqual({"Always"}, strategies)
        self.assertEqual("Halted", self.cluster.get(self.vm_path("copied"))["spec"]["runStrategy"])
        self.assertEqual(2, len(self.lh.made))
        self.assertFalse([c for c in self.cluster.calls if c[1] in (FREEZE, UNFREEZE)])
        annotations = self.cluster.get(self.vm_path())["metadata"].get("annotations") or {}
        self.assertFalse([k for k in annotations if "move-" in k], annotations)

    def test_guest_agent_freezes_once_around_every_disk_snapshot(self):
        self.seed_vm()
        self.agent()
        self.snapshots_appear()
        review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertTrue(any("freezes its filesystems" in w for w in review["warnings"]), review["warnings"])
        move = PLAN.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        recorded = []
        backup = source.backup
        def watch(*args, **kwargs):
            answer = backup(*args, **kwargs)
            recorded[:] = answer["backups"]
            return answer
        with mock.patch.object(source, "backup", side_effect=watch):
            done, strategies = self.run_copy(move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertEqual({"Always"}, strategies)
        calls = [c[1] for c in self.cluster.calls]
        self.assertEqual(1, calls.count(FREEZE))
        self.assertEqual(1, calls.count(UNFREEZE))
        self.assertLess(calls.index(FREEZE), calls.index(UNFREEZE))
        self.assertEqual({"30s"}, {c[2]["unfreezeGracePeriod"] for c in self.cluster.calls if c[1] == FREEZE})
        self.assertEqual([True, True], [row.get("frozen") for row in recorded])

    def test_failed_freeze_still_copies_with_crash_consistent_disks(self):
        self.seed_vm()
        self.agent()
        send = source.ksend
        def refuse(method, path, body=None, **kwargs):
            if path == FREEZE:
                raise RuntimeError("guest agent does not support fsfreeze")
            return send(method, path, body, **kwargs)
        with mock.patch.object(source, "ksend", side_effect=refuse):
            move = PLAN.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
            done, _ = self.run_copy(move["id"])
        self.assertEqual("succeeded", done["status"], done["message"])
        self.assertNotIn(UNFREEZE, [c[1] for c in self.cluster.calls])

    def test_older_source_pauses_the_vm_and_the_review_says_so(self):
        hello = client.hello
        older = lambda: dict(hello(), capabilities=["copy-source-lease", "copy-source-fence", "copy-destination"])
        self.seed_vm()
        with mock.patch.object(client, "hello", side_effect=older):
            review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertFalse(review["live"])
        self.assertTrue(any("pauses desktop" in w for w in review["warnings"]), review["warnings"])

    def test_source_that_stops_instead_of_holding_live_fails_the_copy(self):
        self.seed_vm()
        quiesce = source.quiesce
        def ignore_live(*args, live=False, **kwargs):
            return quiesce(*args, **kwargs)
        move = PLAN.start("shed", "vm", "desktop", "copied", transfer_mode="copy")
        with mock.patch.object(source, "quiesce", side_effect=ignore_live):
            PHASE.tick_all(); PHASE.tick_all(); PHASE.tick_all()
        current = STATE._find(move["id"])
        self.assertEqual("failed", current["status"])
        self.assertIn("did not hold desktop for a live copy", current["message"])

    def test_choosing_to_stop_the_vm_copies_it_stopped_and_starts_it_again(self):
        self.seed_vm()
        review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy", stop_source=True)
        self.assertFalse(review["live"])
        self.assertTrue(review["can_live"])
        self.assertTrue(any("stops while its disks are backed up" in w for w in review["warnings"]), review["warnings"])
        move = PLAN.start("shed", "vm", "desktop", "copied", transfer_mode="copy", stop_source=True)
        self.assertFalse(STATE._find(move["id"]).get("live"))
        halted = False
        for _ in range(40):
            current = STATE._find(move["id"])
            if current["status"] != "running":
                break
            if current["phase"] == "quiescing" and current.get("flags", {}).get("quiesced"):
                halted = halted or self.cluster.get(self.vm_path())["spec"]["runStrategy"] == "Halted"
                self.cluster.objects.pop(self.vmi_path(), None)
            PHASE.tick_all()
        self.assertEqual("succeeded", current["status"], current["message"])
        self.assertTrue(halted, "the source VM was not stopped for the copy")
        self.assertEqual("Always", self.cluster.get(self.vm_path())["spec"]["runStrategy"])

    def test_stop_choice_reaches_the_plan_from_the_route(self):
        import homestead_move_engine as engine
        request = mock.Mock(body={"cluster": "shed", "kind": "vm", "name": "desktop", "namespace": "copied",
                                  "transfer_mode": "copy", "stop_source": True})
        with mock.patch.object(PLAN, "plan", return_value={"ok": True}) as planned:
            engine._plan_or_start(request, False)
        self.assertTrue(planned.call_args.kwargs["stop_source"])

    def test_stopped_vm_copy_has_no_running_warning(self):
        self.seed_vm(running=False)
        review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertFalse([w for w in review["warnings"] if "keeps running" in w or "pauses" in w])


class PersistentStateTests(unittest.TestCase):
    setUp = base.ClusterCopyTests.setUp
    LIVE = True
    seed_vm = base.ClusterCopyTests.seed_vm
    vm_path = staticmethod(base.ClusterCopyTests.vm_path)
    vmi_path = staticmethod(base.ClusterCopyTests.vmi_path)

    def persistent(self, tpm=True, efi=True):
        vm = self.seed_vm()
        domain = vm["spec"]["template"]["spec"]["domain"]
        if tpm:
            domain["devices"]["tpm"] = {"persistent": True}
        if efi:
            domain["firmware"]["bootloader"]["efi"]["persistent"] = True
        self.cluster.put(self.vm_path(), vm)

    def default_class(self, annotation="storageclass.kubernetes.io/is-default-class"):
        path = "/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"
        current = self.cluster.objects.get(path) or {"metadata": {"name": "longhorn-r2"}, "provisioner": "driver.longhorn.io"}
        current.setdefault("metadata", {}).setdefault("annotations", {})[annotation] = "true"
        self.cluster.put(path, current)

    def test_copy_and_move_warn_instead_of_refusing(self):
        self.persistent()
        self.default_class()
        for mode in ("copy", "move"):
            with self.subTest(mode=mode):
                review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode=mode)
                self.assertTrue(review["ok"], review["blockers"])
                notes = [w for w in review["warnings"] if "outside its disks" in w]
                self.assertEqual(1, len(notes), review["warnings"])
                self.assertIn("UEFI settings and TPM", notes[0])
                self.assertIn("blank UEFI settings and a new, empty TPM", notes[0])

    def test_tpm_only_names_only_the_tpm(self):
        self.persistent(efi=False)
        self.default_class()
        review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        note = next(w for w in review["warnings"] if "outside its disks" in w)
        self.assertIn("keeps its TPM in", note)
        self.assertNotIn("UEFI", note)

    def test_no_default_class_blocks_a_move_that_starts_and_warns_a_copy(self):
        self.persistent()
        copy_review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="copy")
        self.assertTrue(copy_review["ok"], copy_review["blockers"])
        self.assertTrue(any("no single default storage class" in w for w in copy_review["warnings"]))
        move_review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="move")
        self.assertTrue(any("no single default storage class" in b for b in move_review["blockers"]), move_review)

    def test_kubevirt_state_class_or_virt_default_is_enough(self):
        self.persistent()
        self.default_class("storageclass.kubevirt.io/is-default-virt-class")
        self.assertTrue(PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="move")["ok"])
        self.cluster.objects["/apis/storage.k8s.io/v1/storageclasses/longhorn-r2"]["metadata"]["annotations"] = {}
        self.cluster.put("/apis/kubevirt.io/v1/kubevirts/kubevirt", {
            "metadata": {"name": "kubevirt"}, "spec": {"configuration": {"vmStateStorageClass": "missing"}}})
        review = PLAN.plan("shed", "vm", "desktop", "copied", transfer_mode="move")
        self.assertTrue(any("storage class missing" in b for b in review["blockers"]), review)


if __name__ == "__main__":
    unittest.main()

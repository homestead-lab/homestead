"""VM previews must never provision or modify a dependency."""
import copy
import unittest
import urllib.error
from unittest import mock

import test_vm_edit as edit_fixtures
import test_vm_create as create_fixtures
import homestead_vms as vms
import homestead_imports as imports


class EditPreparationTests(unittest.TestCase):
    def setUp(self):
        self.cluster = edit_fixtures.Cluster({"harvester": True, "cdi": True})

    def bound_disk(self):
        self.pvc = {"metadata": {"name": "web-disk", "namespace": "lab", "uid": "disk-uid", "resourceVersion": "50"},
                    "spec": {"storageClassName": "example-storage", "resources": {"requests": {"storage": "20Gi"}}},
                    "status": {"phase": "Bound", "capacity": {"storage": "20Gi"}}}
        self.storage_class = {"allowVolumeExpansion": True}
        original = self.cluster.get
        def read(path):
            if path.endswith("/persistentvolumeclaims"): return {"items": [copy.deepcopy(self.pvc)]}
            if path.endswith("/persistentvolumeclaims/web-disk"): return copy.deepcopy(self.pvc)
            if path == "/apis/storage.k8s.io/v1/storageclasses/example-storage":
                if self.storage_class is None: raise urllib.error.HTTPError(path, 404, "missing", {}, None)
                return copy.deepcopy(self.storage_class)
            return original(path)
        patch = mock.patch.object(vms, "kget", side_effect=read)
        patch.start()
        self.addCleanup(patch.stop)

    def test_unsupported_disk_growth_is_refused_before_vm_and_secret_changes(self):
        self.bound_disk()
        for sc in ({"allowVolumeExpansion": False}, None):
            self.storage_class = sc
            with self.assertRaisesRegex(ValueError, "StorageClass"):
                vms.edit("lab", "web", {"disks": [{"name": "root", "size": "30Gi"}], "cloud_init": {"user_data": "new"}})
            self.assertEqual([], self.cluster.sent)

    def test_class_is_rechecked_at_commit_before_any_writes(self):
        self.bound_disk()
        prepared = vms.prepare_edit("lab", "web", {"disks": [{"name": "root", "size": "30Gi"}], "cloud_init": {"user_data": "new"}})
        self.storage_class["allowVolumeExpansion"] = False
        with self.assertRaisesRegex(ValueError, "does not allow"):
            vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)

    def test_pending_disk_growth_cannot_be_shrunk_to_current_capacity(self):
        self.bound_disk()
        self.pvc["status"]["capacity"]["storage"] = "10Gi"
        with self.assertRaisesRegex(ValueError, "grow but not shrink"):
            vms.prepare_edit("lab", "web", {"disks": [{"name": "root", "size": "15Gi"}]})
        self.assertEqual([], self.cluster.sent)

    def test_unchanged_size_during_growth_does_not_submit_another_resize(self):
        self.bound_disk()
        self.pvc["status"]["capacity"]["storage"] = "10Gi"
        self.storage_class = None
        prepared = vms.prepare_edit("lab", "web", {"disks": [{"name": "root", "size": "20Gi"}]})
        self.assertEqual([], prepared["resize"])
        self.assertEqual([], self.cluster.sent)

    def test_cloud_init_with_invalid_memory_never_patches_secret(self):
        for memory in ("invalid", "0Gi", "-1Gi"):
            with self.assertRaises(ValueError):
                vms.edit("lab", "web", {"cloud_init": {"user_data": "new secret"}, "memory": memory})
        self.assertEqual([], self.cluster.sent)

    def test_disk_source_and_invalid_cpu_never_delete_datavolume(self):
        with self.assertRaises(ValueError):
            vms.edit("lab", "web", {"disks": [{"name": "root", "source": {"image": "default/image-ubuntu"}}], "cores": 0})
        self.assertEqual([], self.cluster.sent)

    def test_image_download_is_not_started_during_validation(self):
        with mock.patch.object(vms.HVIMAGE, "download") as download:
            with self.assertRaises(ValueError):
                vms.edit("lab", "web", {"add_disks": [{"kind": "disk", "url": "https://example.test/vm.img"}], "run_strategy": "invalid"})
        download.assert_not_called()
        self.assertEqual([], self.cluster.sent)

    def test_preparation_is_pure_and_does_not_mutate_current_object(self):
        current = copy.deepcopy(self.cluster.vm)
        prepared = vms.prepare_edit("lab", "web", {"cloud_init": {"user_data": "new secret"},
                "add_disks": [{"kind": "disk", "url": "https://example.test/vm.img"}]}, current=current)
        self.assertEqual(self.cluster.vm, current)
        self.assertEqual([], self.cluster.sent)
        self.assertEqual({"image-download", "secret"}, {row["kind"] for row in prepared["effects"]})
        self.assertEqual("10", prepared["vm"]["metadata"]["resourceVersion"])

    def test_cloud_init_unavailable_does_not_become_empty_config(self):
        get = self.cluster.get
        def read(path):
            if "/secrets/" in path:
                raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
            return get(path)
        with mock.patch.object(vms, "kget", side_effect=read):
            with self.assertRaisesRegex(ValueError, "could not be read"):
                vms.prepare_edit("lab", "web", {"cloud_init": {"user_data": "replacement"}})
        self.assertEqual([], self.cluster.sent)

    def test_incomplete_claim_inventory_never_allows_source_replacement(self):
        get = self.cluster.get
        for result in ({}, {"items": [], "metadata": {"continue": "next"}}):
            with mock.patch.object(vms, "kget", side_effect=lambda path: result if path.endswith("/persistentvolumeclaims") else get(path)):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    vms.prepare_edit("lab", "web", {"disks": [{"name": "root", "source": {"image": "default/image-ubuntu"}}]})
        self.assertEqual([], self.cluster.sent)

    def test_changed_vm_rejects_commit_before_any_dependency_write(self):
        prepared = vms.prepare_edit("lab", "web", {"cloud_init": {"user_data": "new"}})
        self.cluster.vm["metadata"]["resourceVersion"] = "11"
        with self.assertRaisesRegex(ValueError, "VM changed"):
            vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)

    def test_changed_secret_rejects_commit(self):
        prepared = vms.prepare_edit("lab", "web", {"cloud_init": {"user_data": "new"}})
        get = self.cluster.get
        def read(path):
            obj = get(path)
            if "/secrets/" in path:
                obj["metadata"]["uid"] = "replacement"
            return obj
        with mock.patch.object(vms, "kget", side_effect=read):
            with self.assertRaisesRegex(ValueError, "dependency changed"):
                vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)

    def test_pvc_appearing_after_review_prevents_deleting_its_datavolume(self):
        prepared = vms.prepare_edit("lab", "web", {"disks": [{"name": "root", "source": {"image": "default/image-ubuntu"}}]})
        get = self.cluster.get
        def read(path):
            if path.endswith("/persistentvolumeclaims/web-disk"):
                return {"metadata": {"uid": "new-pvc", "resourceVersion": "1"}}
            return get(path)
        with mock.patch.object(vms, "kget", side_effect=read):
            with self.assertRaisesRegex(ValueError, "now has a PVC"):
                vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)

    def test_after_download_recheck_runs_before_secret_writes(self):
        prepared = vms.prepare_edit("lab", "web", {"add_disks": [{"url": "https://example.test/vm.img"}],
                                                  "cloud_init": {"user_data": "new"}})
        def download(*args):
            self.cluster.vm["metadata"]["resourceVersion"] = "changed"
            return {"namespace": "lab", "name": "image", "storage_class": "image-sc"}
        with mock.patch.object(vms.HVIMAGE, "download", side_effect=download):
            with self.assertRaisesRegex(ValueError, "VM changed"):
                vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)

    def test_resolved_download_is_passed_to_fresh_admission_before_writes(self):
        prepared = vms.prepare_edit("lab", "web", {"add_disks": [{"url": "https://example.test/vm.img"}]})
        seen = []
        def admission(result):
            seen.append(result)
            raise ValueError("capacity changed")
        with mock.patch.object(vms.HVIMAGE, "download", return_value={"namespace": "lab", "name": "image", "storage_class": "image-sc"}):
            with self.assertRaisesRegex(ValueError, "capacity changed"):
                vms.commit_edit(prepared, before_save=admission)
        self.assertEqual("image-sc", vms._claim_templates(seen[0]["vm"])[0]["spec"]["storageClassName"])
        self.assertEqual([], self.cluster.sent)

    def test_secret_patch_keeps_optimistic_preconditions(self):
        vms.edit("lab", "web", {"cloud_init": {"user_data": "new"}})
        patch = next(body for method, path, body in self.cluster.sent if method == "PATCH")
        self.assertEqual({"uid": "secret-uid", "resourceVersion": "20"}, patch["metadata"])

    def test_new_disk_name_collision_after_preparation_cannot_adopt_a_pvc_or_dv(self):
        prepared = vms.prepare_edit("lab", "web", {"add_disks": [{"kind": "disk", "size": "2Gi"}]})
        get = self.cluster.get
        for resource in ("persistentvolumeclaims", "datavolumes"):
            def read(path):
                if path.endswith(f"/{resource}/web-disk-0"):
                    return {"metadata": {"name": "web-disk-0", "uid": "other", "resourceVersion": "1"}}
                return get(path)
            with mock.patch.object(vms, "kget", side_effect=read):
                with self.assertRaisesRegex(ValueError, "cannot be adopted"):
                    vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)

    def test_existing_datavolume_is_never_deleted_before_or_after_a_vm_edit(self):
        prepared = vms.prepare_edit("lab", "web", {"disks": [{"name": "root", "source": {"image": "default/image-ubuntu"}}]})
        with self.assertRaisesRegex(ValueError, "existing DataVolume"):
            vms.commit_edit(prepared)
        self.assertEqual([], self.cluster.sent)


class CreatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = create_fixtures.VmCreateTests()
        self.fixture.setUp()
        self.body = {"name": "new-vm", "password": "test-only-password", "memory": "2Gi", "network": "pod"}

    def test_invalid_network_after_disk_definition_does_not_create_disk(self):
        for platform in (create_fixtures.K3S_BARE, create_fixtures.HARVESTER):
            with self.assertRaises(ValueError):
                imports.create_vm({**self.body, "network": "not a network"}, platform, "storage")
        self.assertEqual([], self.fixture.sent)

    def test_invalid_static_ip_does_not_create_claim_or_secret(self):
        with self.assertRaises(ValueError):
            imports.create_vm({**self.body, "static_ip": {"address": "192.0.2.2"}}, create_fixtures.K3S_BARE, "storage")
        self.assertEqual([], self.fixture.sent)

    def test_invalid_cpu_memory_and_disk_are_zero_write(self):
        for fields in ({"cores": 0}, {"cores": 129}, {"memory": "0Gi"}, {"memory": "nonsense"}, {"disk_gb": -1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                imports.create_vm({**self.body, **fields}, create_fixtures.K3S_BARE, "storage")
        self.assertEqual([], self.fixture.sent)

    def test_pure_preparation_includes_both_planned_claim_and_secret(self):
        prepared = imports.prepare_vm(self.body, create_fixtures.K3S_BARE, "storage")
        self.assertEqual(1, len(prepared["claims"]))
        self.assertEqual(1, len(prepared["secrets"]))
        self.assertEqual([], self.fixture.sent)

    def test_prepare_harvester_url_never_starts_download(self):
        with mock.patch.object(imports.HVIMAGE, "download") as download:
            prepared = imports.prepare_vm({**self.body, "image_url": "https://example.test/vm.img"}, create_fixtures.HARVESTER, "storage")
        self.assertEqual(1, len(prepared["downloads"]))
        download.assert_not_called()
        self.assertEqual([], self.fixture.sent)

    def test_new_admission_refusal_after_download_creates_no_claim_secret_or_vm(self):
        prepared = imports.prepare_vm({**self.body, "image_url": "https://example.test/vm.img"}, create_fixtures.HARVESTER, "storage")
        with mock.patch.object(imports.HVIMAGE, "download", return_value={"namespace": "lab", "name": "image", "storage_class": "image-sc"}):
            with self.assertRaisesRegex(ValueError, "refused"):
                imports.commit_vm(prepared, before_save=mock.Mock(side_effect=ValueError("refused")))
        self.assertEqual([], self.fixture.sent)

    def test_failed_vm_creation_keeps_dependencies_instead_of_deleting_them(self):
        prepared = imports.prepare_vm(self.body, create_fixtures.K3S_BARE, "storage")
        send = imports.ksend
        def fail(method, path, body=None, **kwargs):
            if path.endswith("/virtualmachines"):
                raise urllib.error.HTTPError(path, 500, "uncertain", {}, None)
            return send(method, path, body, **kwargs)
        with mock.patch.object(imports, "ksend", side_effect=fail):
            with self.assertRaises(urllib.error.HTTPError):
                imports.commit_vm(prepared)
        self.assertEqual(["POST", "POST"], [method for method, _, _ in self.fixture.sent])


if __name__ == "__main__":
    unittest.main()

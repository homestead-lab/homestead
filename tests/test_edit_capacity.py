import copy
import unittest
from unittest import mock

import test_rollout_capacity as fixtures
import server
import homestead_lifecycle as lifecycle
import homestead_capacity_review as review


class EditCapacityTests(unittest.TestCase):
    get = fixtures.RolloutCapacityTests.get

    def setUp(self):
        fixtures.RolloutCapacityTests.setUp(self)
        self.config = {"ns": "lab", "name": "shared", "containers": [
            {"original_name": "main", "name": "main", "memory": "6Gi", "memory_limit": "6Gi"}]}
        for patch in (mock.patch.object(lifecycle, "kget", side_effect=self.get),
                      mock.patch.object(lifecycle, "hardware_features", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, path, config):
        handler = object.__new__(server.H)
        handler.path, handler.headers = path, {}
        handler._guard = lambda path: False
        handler._body = lambda: copy.deepcopy(config)
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server, "guard_managed_smb"), mock.patch.object(server, "guard_self"), \
                mock.patch.object(server, "persist_icon_config") as icons, \
                mock.patch.object(lifecycle, "ksend") as send, \
                mock.patch.object(lifecycle, "create_pvc") as create:
            handler.do_POST()
        return handler._send.call_args.args, send, create, icons

    def reviewed(self):
        result, send, create, icons = self.call("/api/edit/preview", self.config)
        self.assertEqual(200, result[0], result)
        send.assert_not_called()
        create.assert_not_called()
        icons.assert_not_called()
        return {**copy.deepcopy(self.config), "capacity_token": result[1]["capacity_token"], "confirm_capacity": True}

    def test_preview_is_read_only_and_counts_all_containers(self):
        self.current["spec"]["template"]["spec"]["containers"].append({"name": "sidecar", "resources": {
            "requests": {"memory": "1Gi"}, "limits": {"memory": "1Gi"}}})
        before = copy.deepcopy(self.current)
        _, _, plan = server.edit_capacity_plan(self.config)
        self.assertEqual(7, plan["pod_request_gb"])
        self.assertEqual(before, self.current)

    def test_oversized_edit_never_writes_even_with_confirmation(self):
        self.config["containers"][0].update(memory="20Gi", memory_limit="20Gi")
        config = self.reviewed()
        result, send, create, icons = self.call("/api/edit", config)
        self.assertEqual(409, result[0])
        send.assert_not_called()
        create.assert_not_called()
        icons.assert_not_called()

    def test_unreviewed_edit_never_writes(self):
        result, send, create, icons = self.call("/api/edit", self.config)
        self.assertEqual(409, result[0])
        for target in (send, create, icons):
            target.assert_not_called()

    def test_valid_review_keeps_fresh_resource_version(self):
        result, send, _, _ = self.call("/api/edit", self.reviewed())
        self.assertEqual(200, result[0], result)
        self.assertEqual("10", send.call_args.args[2]["metadata"]["resourceVersion"])
        self.assertEqual("6Gi", send.call_args.args[2]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"])

    def test_changed_controller_invalidates_review_before_writes(self):
        config = self.reviewed()
        self.current["metadata"]["resourceVersion"] = "11"
        result, send, create, icons = self.call("/api/edit", config)
        self.assertEqual(409, result[0])
        for target in (send, create, icons):
            target.assert_not_called()

    def test_selected_host_is_part_of_same_edit_and_plan(self):
        self.config["node"] = "missing"
        prepared, _, plan = server.edit_capacity_plan(self.config)
        self.assertFalse(plan["blocked"])
        spec = prepared["deployment"]["spec"]["template"]["spec"]
        self.assertNotIn("kubernetes.io/hostname", spec.get("nodeSelector", {}))
        preference = spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0]
        self.assertEqual(["missing"], preference["preference"]["matchExpressions"][0]["values"])
        self.assertEqual("Recreate", prepared["deployment"]["spec"]["strategy"]["type"])

    def test_new_volume_is_planned_without_creating(self):
        self.config["containers"][0]["volumes"] = [{"kind": "new-rwx", "source": "new-data", "path": "/data", "size_gb": 5}]
        with mock.patch.object(lifecycle, "create_pvc") as create:
            prepared = lifecycle.prepare_edit(self.config, self.current)
        create.assert_not_called()
        self.assertEqual("ReadWriteMany", prepared["claims"][0]["access_mode"])

    def test_seed_preview_does_not_write_and_seed_version_binds_review(self):
        spec = self.current["spec"]["template"]["spec"]
        spec["initContainers"] = [{"name": "seed", "volumeMounts": [{"name": "seed", "mountPath": "/src"}]}]
        spec["volumes"] = [{"name": "seed", "configMap": {"name": "config"}}]
        self.objects["/api/v1/namespaces/lab/configmaps/config"] = {
            "metadata": {"resourceVersion": "1"}, "data": {"config": "old"}}
        self.config["seed_configs"] = [{"init_container": "seed", "config_map": "config", "key": "config", "value": "new"}]
        config = self.reviewed()
        self.objects["/api/v1/namespaces/lab/configmaps/config"]["metadata"]["resourceVersion"] = "2"
        result, send, _, _ = self.call("/api/edit", config)
        self.assertEqual(409, result[0])
        send.assert_not_called()

    def test_paused_scale_up_is_not_treated_as_template_only(self):
        self.current["spec"]["paused"] = True
        self.config["replicas"] = 20
        with self.assertRaisesRegex(ValueError, "paused"):
            server.edit_capacity_plan(self.config)


if __name__ == "__main__":
    unittest.main()

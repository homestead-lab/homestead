import base64
import copy
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_vms as VMS

# A VM whose DataVolume CDI refused: its URL was a file name.
VM = {"metadata": {"name": "web", "namespace": "lab", "uid": "u1"},
      "spec": {"runStrategy": "RerunOnFailure",
               "dataVolumeTemplates": [{"metadata": {"name": "web-disk"},
                                        "spec": {"source": {"http": {"url": "ubuntu.img"}},
                                                 "pvc": {"resources": {"requests": {"storage": "20Gi"}}, "storageClassName": "longhorn-r2"}}}],
               "template": {"spec": {
                   "domain": {"cpu": {"cores": 2}, "memory": {"guest": "2Gi"},
                              "devices": {"disks": [{"name": "root", "disk": {"bus": "virtio"}, "bootOrder": 1},
                                                    {"name": "cloudinit", "disk": {"bus": "virtio"}}],
                                          "interfaces": [{"name": "default", "masquerade": {}, "model": "virtio"}]}},
                   "networks": [{"name": "default", "pod": {}}],
                   "volumes": [{"name": "root", "dataVolume": {"name": "web-disk"}},
                               {"name": "cloudinit", "cloudInitNoCloud": {"secretRef": {"name": "web-ci"}}}]}}},
      "status": {"printableStatus": "Stopped",
                 "conditions": [{"type": "Ready", "status": "False", "message": "VMI does not exist"},
                                {"type": "Failure", "status": "True",
                                 "message": "Error encountered while creating DataVolumes: Invalid source URL"}]}}
MADE = {"metadata": {"name": "web-old"}, "spec": {"resources": {"requests": {"storage": "10Gi"}}},
        "status": {"capacity": {"storage": "10Gi"}}}


class Cluster:
    def __init__(self, platform):
        self.vm = copy.deepcopy(VM)
        self.sent = []
        self.secret = {"userdata": base64.b64encode(b"#cloud-config\n").decode()}
        VMS.bind(self.get, self.send, lambda *a, **k: [])
        VMS.platform = lambda: platform
        VMS.images = lambda: [{"name": "image-ubuntu", "namespace": "default", "display": "ubuntu", "size_gb": 3.5,
                               "storage_class": "longhorn-image-ubuntu"}]

    def get(self, path):
        if path.endswith("/virtualmachines/web"):
            return copy.deepcopy(self.vm)
        if path.endswith("/persistentvolumeclaims"):
            return {"items": [MADE]}
        if path.endswith("/secrets/web-ci"):
            return {"data": dict(self.secret)}
        if path.endswith("/namespaces/lab/datavolumes"):
            return {"items": [{"metadata": {"name": "web-disk", "namespace": "lab"},
                               "status": {"phase": "ImportInProgress", "progress": "47.5%"}}]}
        raise urllib.error.HTTPError(path, 404, "missing", None, None)

    def send(self, method, path, body=None, **kw):
        self.sent.append((method, path, body))
        if method == "PUT":
            self.vm = body
        return body


class VmEditTests(unittest.TestCase):
    def test_a_refused_disk_says_so_and_a_stopped_vm_is_not_a_problem(self):
        Cluster({"harvester": True, "cdi": True})
        row = VMS._row(copy.deepcopy(VM), {})
        self.assertIn("Invalid source URL", row["problem"])
        self.assertIn("never made", row["problem"])
        vm = copy.deepcopy(VM)
        vm["status"]["conditions"] = vm["status"]["conditions"][:1]
        self.assertEqual("", VMS._row(vm, {})["problem"])

    def test_an_unmade_disk_takes_a_new_source(self):
        c = Cluster({"harvester": False, "cdi": True})
        with self.assertRaisesRegex(ValueError, "not a download address"):
            VMS.edit("lab", "web", {"disks": [{"name": "root", "source": {"url": "ubuntu.img"}}]})
        VMS.edit("lab", "web", {"disks": [{"name": "root", "source": {"url": "https://example.test/u.img"}}]})
        template = c.vm["spec"]["dataVolumeTemplates"][0]
        self.assertEqual("https://example.test/u.img", template["spec"]["source"]["http"]["url"])
        self.assertEqual({"resources": {"requests": {"storage": "20Gi"}}, "storageClassName": "longhorn-r2"},
                         template["spec"]["storage"])
        self.assertIn(("DELETE", "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/web-disk", None), c.sent)

    def test_on_harvester_an_unmade_disk_can_become_an_image(self):
        c = Cluster({"harvester": True, "cdi": True})
        VMS.edit("lab", "web", {"disks": [{"name": "root", "source": {"image": "default/image-ubuntu"}}]})
        self.assertNotIn("dataVolumeTemplates", c.vm["spec"])
        claim = json.loads(c.vm["metadata"]["annotations"][VMS.CLAIM_TEMPLATES])[0]
        self.assertEqual(("web-disk", "longhorn-image-ubuntu"),
                         (claim["metadata"]["name"], claim["spec"]["storageClassName"]))
        self.assertEqual({"name": "root", "persistentVolumeClaim": {"claimName": "web-disk"}},
                         c.vm["spec"]["template"]["spec"]["volumes"][0])

    def test_adding_disks_nics_and_changing_boot_and_host(self):
        c = Cluster({"harvester": False, "cdi": False})
        VMS.edit("lab", "web", {"node": "node-1",
                                "disks": [{"name": "root", "boot": "2", "bus": "sata"}],
                                "add_disks": [{"kind": "disk", "size": "30", "storage_class": "local-path", "boot": 1}],
                                "nics": [{"name": "default", "model": "e1000", "mac": "52:54:00:AA:BB:CC"}],
                                "add_nics": [{"network": "default/vlan1"}]})
        tspec = c.vm["spec"]["template"]["spec"]
        disks = tspec["domain"]["devices"]["disks"]
        self.assertEqual((2, "sata"), (disks[0]["bootOrder"], disks[0]["disk"]["bus"]))
        self.assertEqual({"name": "disk-0", "disk": {"bus": "virtio"}, "bootOrder": 1}, disks[-1])
        post = next(b for m, p, b in c.sent if m == "POST")
        self.assertEqual(("web-disk-0", "30Gi", "local-path"),
                         (post["metadata"]["name"], post["spec"]["resources"]["requests"]["storage"],
                          post["spec"]["storageClassName"]))
        self.assertEqual({"kubernetes.io/hostname": "node-1"}, tspec["nodeSelector"])
        ifaces = tspec["domain"]["devices"]["interfaces"]
        self.assertEqual(("e1000", "52:54:00:aa:bb:cc"), (ifaces[0]["model"], ifaces[0]["macAddress"]))
        self.assertEqual({"name": "nic-0", "model": "virtio", "bridge": {}}, ifaces[1])
        self.assertEqual({"name": "nic-0", "multus": {"networkName": "default/vlan1"}}, tspec["networks"][1])
        with self.assertRaisesRegex(ValueError, "CD-ROM needs"):
            VMS.edit("lab", "web", {"add_disks": [{"kind": "cd-rom"}]})
        with self.assertRaisesRegex(ValueError, "pod network once"):
            VMS.edit("lab", "web", {"add_nics": [{"network": "pod"}]})

    def test_detaching_keeps_the_volume_and_drops_its_template(self):
        c = Cluster({"harvester": False, "cdi": True})
        result = VMS.edit("lab", "web", {"disks": [{"name": "root", "remove": True}]})
        self.assertNotIn("dataVolumeTemplates", c.vm["spec"])
        self.assertEqual(["cloudinit"], [d["name"] for d in c.vm["spec"]["template"]["spec"]["domain"]["devices"]["disks"]])
        self.assertIn("detached and kept", result["detail"])
        self.assertFalse([m for m, p, b in c.sent if m == "DELETE" and "persistentvolumeclaims" in p])

    def test_deleting_mid_download_stops_it_instead_of_keeping_half_a_disk(self):
        c = Cluster({"harvester": False, "cdi": True})
        self.assertEqual([{"claim": "web-disk", "phase": "ImportInProgress", "progress": 47.5}],
                         VMS._filling(copy.deepcopy(VM), VMS._datavolumes("lab")))
        result = VMS.delete("lab", "web")
        self.assertFalse([p for m, p, b in c.sent if m == "PATCH"])       # never released
        vm_delete = next(b for m, p, b in c.sent if m == "DELETE" and p.endswith("/virtualmachines/web"))
        self.assertEqual("Foreground", vm_delete["propagationPolicy"])
        self.assertIn(("DELETE", "/apis/cdi.kubevirt.io/v1beta1/namespaces/lab/datavolumes/web-disk", None), c.sent)
        self.assertIn("unfinished download", result["detail"])
        deleting = copy.deepcopy(VM)
        deleting["metadata"]["deletionTimestamp"] = "2026-09-24T12:00:00Z"
        self.assertEqual(("Deleting", []), (VMS._row(deleting, {})["status"], VMS._row(deleting, {})["actions"]))

    def test_cloud_init_in_harvesters_secret_is_edited_there(self):
        c = Cluster({"harvester": True, "cdi": True})
        self.assertEqual({"user_data": "#cloud-config\n", "network_data": "", "source": "secret"},
                         VMS._read_cloud_init(copy.deepcopy(VM), "lab"))
        VMS.edit("lab", "web", {"cloud_init": {"user_data": "#cloud-config\nhostname: web\n", "network_data": ""}})
        patch = next(b for m, p, b in c.sent if m == "PATCH" and p.endswith("/secrets/web-ci"))
        self.assertEqual("#cloud-config\nhostname: web\n", base64.b64decode(patch["data"]["userdata"]).decode())


if __name__ == "__main__":
    unittest.main()

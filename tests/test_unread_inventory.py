"""A list that could not be read is not an empty list.

A host's impact preview read no VMs' devices or service addresses as "none",
Network > Your VIPs read an unreadable registry as no VIPs, Containers and
Edit read unread Services as no exposure, and the VM disk and import lists
read a failed read as nothing there. Each now says what could not be read.
Everything here runs on the shared fake."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import homestead_container_import as CONTAINER_IMPORT
import homestead_impact as IMPACT
import homestead_imports as IMP
import homestead_kube as K
import homestead_network_inventory as INV
import homestead_uptime as UP
import homestead_vip_registry as REG
import homestead_vm_create as VM_CREATE
from fakes.kube import Kube
from test_page_snapshot import Cluster, clean_cache, clean_snapshot, setUpModule  # noqa: F401 (restart record off)

RAW_VMS = "/apis/kubevirt.io/v1/virtualmachines"


class PreviewTests(unittest.TestCase):
    NODES = [{"name": "node-a", "status": "Ready"}, {"name": "node-b", "status": "Ready"}]
    VM = {"name": "vm1", "ns": "lab", "node": "node-a", "running": True, "disks": []}
    ADDRESS = {"ip": "192.0.2.50", "kind": "vip", "node": "node-a", "services": ["lab/web"]}

    def preview(self, raw_vms, addresses):
        return IMPACT.preview("node-a", [], [], [self.VM], raw_vms, [], addresses, self.NODES, sets=[])

    def test_a_vm_whose_definition_was_read_without_devices_still_moves(self):
        p = self.preview([{"metadata": {"namespace": "lab", "name": "vm1"}, "spec": {}}], [])
        self.assertEqual("moves", p["apps"][0]["outcome"])
        self.assertEqual([], p["unread"])

    def test_unread_vm_definitions_leave_a_running_vm_unknown_never_moving(self):
        p = self.preview(None, [])
        self.assertEqual("unknown", p["apps"][0]["outcome"])
        self.assertIn("passed through", p["apps"][0]["why"])
        self.assertIn(IMPACT.VMS_UNREAD, p["unread"])

    def test_read_addresses_on_this_host_are_counted(self):
        p = self.preview([], [self.ADDRESS])
        self.assertEqual(1, p["counts"]["addresses"])

    def test_unread_addresses_make_the_count_unknown_never_zero(self):
        p = self.preview([], None)
        self.assertIsNone(p["counts"]["addresses"])
        self.assertEqual([], p["addresses"])
        self.assertIn(IMPACT.ADDRESSES_UNREAD, p["unread"])


class NodeImpactTests(unittest.TestCase):
    def setUp(self):
        clean_cache(self)
        clean_snapshot(self)
        self.cluster = Cluster(self)
        self.NODES = self.cluster.server.NODES

    def test_a_host_preview_with_vm_definitions_unread_says_so(self):
        self.cluster.kube.fail(RAW_VMS, 500, times=1)
        self.assertIsNone(self.NODES._impact_sources()["raw_vms"])

    def test_a_host_preview_with_the_network_inventory_unread_says_addresses_are_unknown(self):
        def broken():
            raise RuntimeError("network inventory unavailable")
        self.enterContext(mock.patch.object(self.NODES.INV, "inventory", broken))
        p = self.NODES.node_impact("node-a")
        self.assertIsNone(p["counts"]["addresses"])
        self.assertIn(IMPACT.ADDRESSES_UNREAD, p["unread"])



class YourVipsTests(unittest.TestCase):
    """Network > Your VIPs, the default workload VIP and what may be offered as
    free all come from the VIP registry ConfigMap."""
    def setUp(self):
        clean_cache(self)
        clean_snapshot(self)
        self.cluster = Cluster(self)
        self.NETWORK = self.cluster.server.NETWORK
        # Other test files bind the networking modules to fakes of their own
        # and leave them so: read through this cluster.
        import homestead_network_state as STATE
        for name, value in (("kget", K.kget), ("ksend", K.ksend)):
            self.enterContext(mock.patch.object(STATE, name, value))

    def break_registry(self, how="timeout"):
        """A registry read that times out (it used to read as no VIPs) or
        answers 500 (it used to fail the whole Network page and Dashboard)."""
        if how == "500":
            self.cluster.kube.fail(REG._vip_map_path(), 500, times=999)
            return

        def timeout(method, path, body):
            raise TimeoutError("timed out")
        self.cluster.kube.react("GET", REG._vip_map_path(), timeout)

    def test_no_registry_at_all_is_no_vips(self):
        self.assertEqual([], REG.registered())
        self.assertEqual([], self.NETWORK.inventory()["registered_vips"])

    def test_a_registry_that_cannot_be_read_raises_never_empty(self):
        self.break_registry()
        with self.assertRaisesRegex(RuntimeError, "Your VIPs could not be read"):
            REG.registered()

    def test_the_default_vip_is_not_the_shared_fallback_when_the_registry_is_unread(self):
        self.break_registry()
        with self.assertRaisesRegex(RuntimeError, "Your VIPs could not be read"):
            REG.shared_vip()

    def test_the_inventory_names_the_registry_unread_and_offers_no_default(self):
        for how in ("timeout", "500"):
            with self.subTest(how=how):
                clean_cache(self)
                self.cluster.kube._failures.clear()
                self.cluster.kube._reactions.clear()
                self.break_registry(how)
                inv = self.NETWORK.inventory()
                self.assertIsNone(inv["registered_vips"])
                self.assertIsNone(inv["shared_vip"]["ip"])
                self.assertIn(REG.UNREAD, inv["unread"])
                self.assertIn("Your VIPs could not be read", inv["vips_unread"])

    def test_an_address_is_not_offered_or_accepted_while_the_registry_is_unread(self):
        self.break_registry()
        inv = self.NETWORK.inventory()
        self.assertIn("could not read", INV.address_problem("192.0.2.200", inv))

    def test_the_overview_still_answers_without_a_vip_when_the_registry_is_unread(self):
        self.break_registry()
        self.assertIsNone(self.cluster.route("ov")["lb_ip"])



SERVICES = "/api/v1/services"


class ServicesTests(unittest.TestCase):
    """Containers' listeners and Edit's exposed ports come from the Services."""
    def setUp(self):
        clean_cache(self)
        clean_snapshot(self)
        self.cluster = Cluster(self)
        self.WL = self.cluster.server.WORKLOADS
        self.cluster.kube.add({"apiVersion": "v1", "kind": "Service",
                               "metadata": {"namespace": "lab", "name": "web"},
                               "spec": {"type": "LoadBalancer", "selector": {"app": "web"},
                                        "ports": [{"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}]},
                               "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.50"}]}}})
        deployment = self.cluster.kube.object("/apis/apps/v1/namespaces/lab/deployments/web")
        deployment["spec"]["template"]["spec"]["containers"][0]["ports"] = [{"containerPort": 8080, "protocol": "TCP"}]
        self.cluster.kube.add(deployment)

    def web(self):
        return next(w for w in self.WL.get_workloads() if w["name"] == "web")

    def test_read_services_give_the_app_its_listener(self):
        self.assertEqual([("192.0.2.50", 80)], [(p["ip"], p["port"]) for p in self.web()["ports"]])

    def test_unread_services_leave_the_apps_listeners_unknown_never_none(self):
        self.cluster.kube.fail(SERVICES, 500, times=999)
        self.assertIsNone(self.web()["ports"])

    def test_edit_shows_the_exposed_port_when_services_are_read(self):
        form = self.WL.edit_form(type("R", (), {"query": {"ns": ["lab"], "name": ["web"]}, "role": "admin"})())
        port = form["containers"][0]["ports"][0]
        self.assertEqual((True, 80), (port["expose"], port["host"]))

    def test_edit_refuses_to_open_when_services_are_unread(self):
        # Opened with every port unexposed, its save would delete the Service
        # (sync_workload_ports removes one with no exposed port left).
        self.cluster.kube.fail("/api/v1/namespaces/lab/services", 500, times=999)
        with self.assertRaisesRegex(ValueError, "Services could not be read"):
            self.WL.edit_form(type("R", (), {"query": {"ns": ["lab"], "name": ["web"]}, "role": "admin"})())


class UptimeWithServicesUnreadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        UP.bind(directory.name)

    def test_an_app_whose_listeners_are_unknown_is_not_asked_and_keeps_its_state(self):
        app = {"ns": "lab", "name": "web", "desired": 1,
               "ports": [{"ip": "192.0.2.50", "port": 80, "protocol": "TCP"}]}
        miss = lambda t: {"ok": False, "ms": None, "code": None, "error": "connection refused"}
        for minute in range(UP.DOWN_AFTER):
            UP.observe([app], now=1_000_000 + 60 * minute, ask=miss)
        self.assertEqual("down", UP.report()["lab/web"]["state"])
        t, why = UP.target({**app, "ports": None})
        self.assertIsNone(t)
        self.assertIn("couldn't be read", why)
        UP.observe([{**app, "ports": None}], now=1_000_000 + 60 * UP.DOWN_AFTER, ask=miss)
        self.assertEqual("down", UP.report()["lab/web"]["state"])
        self.assertEqual(1, len(UP.alert_facts(UP.report())))



DATAVOLUMES = "/apis/cdi.kubevirt.io/v1beta1/datavolumes"
JOBS = "/apis/batch/v1/jobs"


def datavolume(name="disk1"):
    return {"apiVersion": "cdi.kubevirt.io/v1beta1", "kind": "DataVolume",
            "metadata": {"namespace": "lab", "name": name, "labels": {IMP.VM_DISK_LABEL: "true"}},
            "spec": {"storage": {"resources": {"requests": {"storage": "10Gi"}}}},
            "status": {"phase": "Succeeded", "progress": "100.0%"}}


def vm_using(disk):
    return {"apiVersion": "kubevirt.io/v1", "kind": "VirtualMachine",
            "metadata": {"namespace": "lab", "name": "vm1"},
            "spec": {"template": {"spec": {"volumes": [{"name": "d", "dataVolume": {"name": disk}}]}}}}


class VmDisksAndImportsTests(unittest.TestCase):
    """The VM disks list (/api/vm-disks) and recent imports (/api/imports):
    the pages already say "could not read" for a call that fails."""
    def setUp(self):
        self.kube = self.enterContext(Kube(datavolume(), vm_using("disk1")))
        self.enterContext(mock.patch.object(IMP, "kget", K.kget))
        self.enterContext(mock.patch.object(IMP, "NS", "lab"))

    def test_read_disks_are_listed_with_the_vm_using_them(self):
        rows = VM_CREATE.list_vm_disks()
        self.assertEqual([("disk1", True, ["vm1"])], [(r["name"], r["in_use"], r["used_by"]) for r in rows])

    def test_no_cdi_installed_is_no_disks(self):
        with Kube(groups=("apps",), absent_groups={"cdi.kubevirt.io", "kubevirt.io"}):
            self.assertEqual([], VM_CREATE.list_vm_disks())

    def test_unread_disks_raise_never_an_empty_list(self):
        self.kube.fail(DATAVOLUMES, 500, times=999)
        with self.assertRaisesRegex(Exception, "500"):
            VM_CREATE.list_vm_disks()

    def test_unread_vms_raise_never_a_disk_shown_as_free(self):
        self.kube.fail(RAW_VMS, 500, times=999)
        with self.assertRaisesRegex(Exception, "500"):
            VM_CREATE.list_vm_disks()
        # A new VM may not take a disk another VM might hold.
        with self.assertRaisesRegex(Exception, "500"):
            VM_CREATE._disk_consumers()

    def test_no_imports_is_an_empty_list(self):
        self.assertEqual([], CONTAINER_IMPORT.import_status())

    def test_unread_imports_raise_never_an_empty_list(self):
        self.kube.fail(JOBS, 500, times=999)
        with self.assertRaisesRegex(Exception, "500"):
            CONTAINER_IMPORT.import_status()


if __name__ == "__main__":
    unittest.main()

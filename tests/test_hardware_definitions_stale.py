"""Two admins (or two tabs) saving hardware definitions no longer undo each
other: a save names the list its form read, and is refused in words when the
stored list changed since; the write itself names the resourceVersion it
read, so a save landing in between is not written over either."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import server
import homestead_kube as K
from fakes.kube import Kube

HW = server.HW
SAVE = HW.ROUTES[("POST", "/api/hardware/features")][1]
LIST = HW.ROUTES[("GET", "/api/hardware/features")][1]

CORAL = {"id": "coral_usb", "name": "Google Coral USB", "label": "hardware/coral-usb",
         "host_path": "/dev/bus/usb", "container_path": "/dev/bus/usb", "path_type": "Directory",
         "usb_ids": ["1a6e:089a"]}


def feature(fid, name):
    return {"id": fid, "name": name, "host_path": f"/dev/{fid}", "container_path": f"/dev/{fid}",
            "path_type": "CharDevice", "usb_ids": []}


class Request:
    def __init__(self, body=None):
        self.body = body or {}


class StaleSaveTests(unittest.TestCase):
    def setUp(self):
        self.path = f"/api/v1/namespaces/{HW.NS}/configmaps/{HW._hardware_map()}"
        self.stored_map = {"apiVersion": "v1", "kind": "ConfigMap",
                           "metadata": {"name": HW._hardware_map(), "namespace": HW.NS},
                           "data": {"features.json": json.dumps([CORAL])}}
        self.addCleanup(HW._bust)

    def form(self):
        """What the Hardware features list shows, and so what a form built on it read."""
        HW._bust()
        return [f for f in LIST(Request()) if not f["builtin"]]

    def stored(self, kube):
        return [f["id"] for f in json.loads(kube.object(self.path)["data"]["features.json"])]

    def test_the_second_of_two_saves_from_the_same_read_is_refused_and_nothing_is_lost(self):
        with Kube(self.stored_map) as kube:
            first, second = self.form(), self.form()        # two admins open the list
            SAVE(Request({"features": first + [feature("hailo", "Hailo-8")], "read": first}))
            with self.assertRaisesRegex(ValueError, r"changed .* since .*nothing was saved") as refused:
                SAVE(Request({"features": second + [feature("zigbee", "Zigbee stick")], "read": second}))
            self.assertNotIn("409", str(refused.exception))
            self.assertEqual(["coral_usb", "hailo"], self.stored(kube), "the first save was undone")

    def test_a_delete_from_a_stale_list_does_not_take_a_new_definition_with_it(self):
        with Kube(self.stored_map) as kube:
            stale = self.form()
            SAVE(Request({"features": stale + [feature("hailo", "Hailo-8")], "read": stale}))
            with self.assertRaises(ValueError):
                SAVE(Request({"features": [], "read": stale}))   # deletes coral_usb as that tab saw the list
            self.assertEqual(["coral_usb", "hailo"], self.stored(kube))

    def test_a_save_landing_between_the_servers_read_and_its_write_is_kept(self):
        with Kube(self.stored_map) as kube:
            read = self.form()
            real, raced = HW.ksend, []

            def ksend(method, path, body=None, **options):
                if path.startswith(self.path) and not raced:
                    raced.append(1)
                    other = kube.object(self.path)
                    other["data"]["features.json"] = json.dumps([CORAL, feature("hailo", "Hailo-8")])
                    K.send("PUT", self.path, other)
                return real(method, path, body, **options)
            HW.ksend = ksend
            self.addCleanup(setattr, HW, "ksend", real)
            with self.assertRaises(ValueError):
                SAVE(Request({"features": read + [feature("zigbee", "Zigbee stick")], "read": read}))
            self.assertEqual(["coral_usb", "hailo"], self.stored(kube), "the save in between was written over")

    def test_a_save_from_the_current_list_goes_through(self):
        with Kube(self.stored_map) as kube:
            read = self.form()
            SAVE(Request({"features": read + [feature("hailo", "Hailo-8")], "read": read}))
            again = self.form()
            SAVE(Request({"features": [f for f in again if f["id"] != "coral_usb"], "read": again}))
            self.assertEqual(["hailo"], self.stored(kube))

    def assert_refused_with_a_fix(self, stored_data, why):
        """A stored list Homestead cannot read is refused, naming the ConfigMap,
        what is wrong and how to reset it; it is not written over."""
        broken = dict(self.stored_map, data=stored_data)
        with Kube(broken) as kube:
            read = self.form()
            with self.assertRaises(ValueError) as caught:
                SAVE(Request({"features": read + [feature("hailo", "Hailo-8")], "read": read}))
            message = str(caught.exception)
            self.assertIn(f"{HW.NS}/{HW._hardware_map()}", message)
            self.assertRegex(message, why)
            self.assertIn("nothing was saved", message.lower())
            self.assertRegex(message, rf"delete .*{HW._hardware_map()}.* built-in defaults|built-in defaults.*delete .*{HW._hardware_map()}")
            self.assertNotIn("another admin", message, "not a concurrent save")
            self.assertEqual(stored_data, kube.object(self.path).get("data", {}), "the stored list was written over")

    def test_a_stored_entry_that_is_not_valid_is_refused_with_a_fix_named(self):
        bad = dict(CORAL, id="Not An ID!")
        self.assert_refused_with_a_fix({"features.json": json.dumps([bad])}, r"not valid")

    def test_stored_text_that_is_not_json_is_refused_with_a_fix_named(self):
        self.assert_refused_with_a_fix({"features.json": "[{not json"}, r"not valid JSON")

    def test_stored_json_that_is_not_a_list_is_refused_with_a_fix_named(self):
        self.assert_refused_with_a_fix({"features.json": '{"coral_usb": {}}'}, r"not a list")

    def test_a_configmap_with_no_features_key_is_refused_with_a_fix_named(self):
        self.assert_refused_with_a_fix({"other": "x"}, r"no features\.json")

    def test_read_is_capped_like_the_features(self):
        with Kube(self.stored_map) as kube:
            many = [feature(f"dev{i}", f"Device {i}") for i in range(33)]
            with self.assertRaisesRegex(ValueError, "at most 32"):
                SAVE(Request({"features": [CORAL], "read": many}))
            self.assertEqual(["coral_usb"], self.stored(kube))

    def test_a_read_that_is_not_a_list_of_features_is_a_bad_request_not_a_concurrent_save(self):
        with Kube(self.stored_map) as kube:
            with self.assertRaises(ValueError) as caught:
                SAVE(Request({"features": [CORAL], "read": [{"id": "Not An ID!", "name": "x"}]}))
            message = str(caught.exception)
            self.assertIn("read", message)
            self.assertNotIn("another admin", message, "nobody saved")
            self.assertEqual(["coral_usb"], self.stored(kube))

    def test_a_save_from_an_older_page_without_read_still_saves_on_what_is_stored(self):
        with Kube(self.stored_map) as kube:
            SAVE(Request({"features": [CORAL, feature("hailo", "Hailo-8")]}))
            self.assertEqual(["coral_usb", "hailo"], self.stored(kube))
            stored = json.loads(kube.object(self.path)["data"]["features.json"])
            self.assertEqual("hardware/coral-usb", stored[0]["label"], "the stored label is kept")

    def test_the_first_save_on_a_new_cluster_starts_from_the_defaults_it_showed(self):
        with Kube() as kube:
            read = self.form()
            self.assertEqual(["coral_pcie", "coral_usb"], [f["id"] for f in read])
            SAVE(Request({"features": read + [feature("hailo", "Hailo-8")], "read": read}))
            self.assertEqual(["coral_pcie", "coral_usb", "hailo"], self.stored(kube))


if __name__ == "__main__":
    unittest.main()

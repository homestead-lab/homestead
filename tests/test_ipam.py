import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_ipam as IPAM

FACTS = {"node_ips": ["192.0.2.21", "192.0.2.22", "10.52.0.1"],
         "vips": [{"ip": "192.0.2.242", "listeners": [{"namespace": "lab", "service": "homestead"}]},
                  {"ip": "192.0.2.120", "listeners": [{"namespace": "lab", "service": "plex"}]}],
         "pools": [{"name": "lan", "ranges": [{"start": "192.0.2.240", "end": "192.0.2.250"}]}]}


class Store:
    def __init__(self):
        self.cm, self.version, self.secret = None, 0, None

    def get(self, path):
        if "/secrets/" in path:
            if self.secret is None:
                raise urllib.error.HTTPError(path, 404, "missing", None, None)
            return self.secret
        if self.cm is None:
            raise urllib.error.HTTPError(path, 404, "missing", None, None)
        return json.loads(json.dumps(self.cm))

    def send(self, method, path, body=None, **kw):
        if "/secrets" in path:
            self.secret = body
            return body
        if method == "PUT" and body["metadata"].get("resourceVersion") != str(self.version):
            raise urllib.error.HTTPError(path, 409, "conflict", None, None)
        self.version += 1
        self.cm = body
        self.cm["metadata"]["resourceVersion"] = str(self.version)
        return body


class IpamTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        IPAM.bind(self.store.get, self.store.send, "lab", lambda: FACTS)
        IPAM._scans.clear()
        IPAM.save_subnets([{"cidr": "192.0.2.0/24", "name": "LAN", "gateway": "192.0.2.1",
                            "dhcp_start": "192.0.2.100", "dhcp_end": "192.0.2.199"}])

    def subnet(self):
        return IPAM.view()["subnets"][0]

    def row(self, ip):
        return next(r for r in self.subnet()["rows"] if r["ip"] == ip)

    def test_subnets_are_checked(self):
        for rows in ([{"cidr": "nonsense"}], [{"cidr": "10.0.0.0/16"}],
                     [{"cidr": "192.0.2.0/24", "gateway": "10.0.0.1"}],
                     [{"cidr": "192.0.2.0/24", "dhcp_start": "192.0.2.100"}],
                     [{"cidr": "192.0.2.0/24"}, {"cidr": "192.0.2.128/25"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                IPAM.save_subnets(rows)

    def test_the_cluster_is_merged_in_and_clashes_are_flagged(self):
        s = self.subnet()
        self.assertEqual("node", self.row("192.0.2.21")["cluster"])
        self.assertEqual(["lab/plex"], self.row("192.0.2.120")["services"])
        # a VIP inside the DHCP range is flagged
        self.assertTrue(any("DHCP" in f["text"] for f in self.row("192.0.2.120")["flags"]))
        self.assertEqual("lan", self.row("192.0.2.242")["pool"])
        self.assertNotIn("10.52.0.1", [r["ip"] for r in s["rows"]])
        # the next free static address skips the gateway, the DHCP range and the pool
        self.assertEqual("192.0.2.2", s["next_free"][0])
        self.assertNotIn("192.0.2.150", s["next_free"])
        self.assertEqual(100, s["dhcp_size"])
        # a node outside every documented subnet suggests its /24
        self.assertEqual(["10.52.0.0/24"], IPAM.view()["suggested"])

    def test_documenting_and_bulk_changes(self):
        IPAM.save_record({"ip": "192.0.2.10", "name": "  Tower  ", "mac": "AA-BB-CC-DD-EE-FF", "kind": "static",
                          "tags": ["NAS", "storage"]})
        row = self.row("192.0.2.10")
        self.assertEqual(("Tower", "aa:bb:cc:dd:ee:ff", "static", ["nas", "storage"]),
                         (row["name"], row["mac"], row["kind"], row["tags"]))
        IPAM.save_record({"ip": "192.0.2.150", "kind": "static", "name": "printer"})
        self.assertTrue(self.row("192.0.2.150")["flags"])
        result = IPAM.bulk(["192.0.2.10", "192.0.2.150"], {"tags_add": ["office"], "tags_remove": ["nas"], "owner": "me"})
        self.assertEqual(2, result["count"])
        self.assertEqual(["office", "storage"], self.row("192.0.2.10")["tags"])
        IPAM.bulk(["192.0.2.150"], {"forget": True})
        self.assertNotIn("192.0.2.150", [r["ip"] for r in self.subnet()["rows"]])
        for bad in ({"ip": "300.1.1.1"}, {"ip": "192.0.2.9", "mac": "zz"}, {"ip": "192.0.2.9", "kind": "odd"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                IPAM.save_record(bad)

    def test_a_scan_finds_hosts_and_flags_undocumented_ones(self):
        answering = {"192.0.2.10": {"up": True, "ports": [445]}, "192.0.2.60": {"up": True, "ports": [80]}}
        IPAM.save_record({"ip": "192.0.2.10", "name": "Tower"})
        IPAM.scan("192.0.2.0/24", probe=lambda ip: answering.get(ip, {"up": False, "ports": []}), workers=8)
        IPAM._threads["192.0.2.0/24"].join(30)
        s = self.subnet()
        self.assertEqual("done", s["scan"]["state"])
        self.assertTrue(self.row("192.0.2.60")["scan"]["up"])
        self.assertTrue(any("not documented" in f["text"] for f in self.row("192.0.2.60")["flags"]))
        self.assertFalse(self.row("192.0.2.10")["flags"])

    def test_a_refused_connection_still_means_someone_is_home(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        refused = closed.getsockname()[1]
        closed.close()
        try:
            host = IPAM._probe("127.0.0.1", ports=(refused, port), timeout=0.5)
        finally:
            server.close()
        self.assertTrue(host["up"])
        self.assertEqual([port], host["ports"])

    def test_unifi_sync_adds_what_the_controller_knows_without_overwriting_people(self):
        IPAM.save_unifi({"url": "https://192.0.2.1", "api_key": "k", "site": "default"})
        self.assertIn("api_key", self.store.secret["data"])
        IPAM.save_record({"ip": "192.0.2.30", "name": "Office printer", "kind": "static"})
        api = "https://192.0.2.1/proxy/network/integration/v1"
        pages = {
            f"{api}/sites": {"data": [{"id": "s1", "internalReference": "default", "name": "Default"}]},
            f"{api}/sites/s1/clients?offset=0&limit=200": {"totalCount": 3, "data": [
                {"name": "HP-Printer", "macAddress": "11:11:11:11:11:11", "ipAddress": "192.0.2.30", "type": "WIRED"},
                {"name": "phone", "macAddress": "22:22:22:22:22:22", "ipAddress": "192.0.2.130", "type": "WIRELESS"},
                {"name": "tv", "macAddress": "33:33:33:33:33:33", "ipAddress": "192.0.2.40", "type": "WIRED"}]},
            f"{api}/sites/s1/devices?offset=0&limit=200": {"totalCount": 1, "data": [
                {"name": "UDM", "macAddress": "44:44:44:44:44:44", "ipAddress": "192.0.2.1", "model": "UDM-Pro"}]},
            "https://192.0.2.1/proxy/network/api/s/default/rest/user": {"data": [
                {"mac": "33:33:33:33:33:33", "use_fixedip": True, "fixed_ip": "192.0.2.40"},
                {"mac": "55:55:55:55:55:55", "use_fixedip": True, "fixed_ip": "192.0.2.41", "name": "camera"}]},
        }
        result = IPAM.sync_unifi(get=lambda url: pages[url])
        self.assertEqual(3, result["clients"])
        self.assertEqual(("Office printer", "static"), (self.row("192.0.2.30")["name"], self.row("192.0.2.30")["kind"]))
        # UniFi's own name for it is kept beside the person's, not over it
        self.assertEqual("HP-Printer", self.row("192.0.2.30")["unifi"]["name"])
        self.assertEqual("", self.row("192.0.2.130")["name"])
        self.assertEqual("phone", self.row("192.0.2.130")["unifi"]["name"])
        self.assertEqual("11:11:11:11:11:11", self.row("192.0.2.30")["mac"])
        self.assertEqual("reservation", self.row("192.0.2.40")["kind"])
        self.assertEqual("dhcp", self.row("192.0.2.130")["kind"])
        self.assertEqual(("camera", "reservation"), (self.row("192.0.2.41")["unifi"]["name"], self.row("192.0.2.41")["kind"]))
        self.assertEqual("infrastructure", self.row("192.0.2.1")["kind"])
        self.assertIsNotNone(IPAM.view()["unifi"]["last_sync"])

    def test_unifi_networks_fill_in_dhcp_ranges_and_devices_get_a_category(self):
        IPAM.save_unifi({"url": "https://192.0.2.1", "api_key": "k"})
        IPAM.save_subnets([{"cidr": "192.0.2.0/24"}])
        api = "https://192.0.2.1/proxy/network/integration/v1"
        classic = "https://192.0.2.1/proxy/network/api/s/default"
        pages = {
            f"{api}/sites": {"data": [{"id": "s1", "internalReference": "default", "name": "Default"}]},
            f"{api}/sites/s1/clients?offset=0&limit=200": {"totalCount": 0, "data": []},
            f"{api}/sites/s1/devices?offset=0&limit=200": {"totalCount": 3, "data": [
                {"name": "Core", "macAddress": "44:44:44:44:44:01", "ipAddress": "192.0.2.2", "model": "USW-Pro-24"},
                {"name": "Hall", "macAddress": "44:44:44:44:44:02", "ipAddress": "192.0.2.3", "model": "U6-Lite"},
                {"name": "Door", "macAddress": "44:44:44:44:44:03", "ipAddress": "192.0.2.4", "model": "UVC-G4-Doorbell"}]},
            f"{classic}/rest/user": {"data": [{"mac": "66:66:66:66:66:66", "use_fixedip": True, "fixed_ip": "192.0.2.50",
                                                "name": "plug"},
                                               {"mac": "44:44:44:44:44:02", "name": "Hallway AP", "hostname": "u6-lite-hall"}]},
            f"{classic}/rest/networkconf": {"data": [
                {"name": "LAN", "ip_subnet": "192.0.2.1/24", "dhcpd_enabled": True,
                 "dhcpd_start": "192.0.2.100", "dhcpd_stop": "192.0.2.199"},
                {"name": "IoT", "ip_subnet": "192.168.20.1/24", "vlan_enabled": True, "vlan": "20", "dhcpd_enabled": True,
                 "dhcpd_start": "192.168.20.10", "dhcpd_stop": "192.168.20.250"},
                {"name": "WAN", "purpose": "wan"}]},
        }
        result = IPAM.sync_unifi(get=lambda url: pages[url])
        self.assertIn("1 reserved", result["detail"])
        s = self.subnet()
        self.assertEqual(("LAN", "192.0.2.1", "192.0.2.100", "192.0.2.199"),
                         (s["name"], s["gateway"], s["dhcp_start"], s["dhcp_end"]))
        self.assertEqual(["switch", "access-point", "cctv"],
                         [self.row(ip)["category"] for ip in ("192.0.2.2", "192.0.2.3", "192.0.2.4")])
        self.assertTrue(self.row("192.0.2.50")["unifi"]["reserved"])
        hall = self.row("192.0.2.3")
        self.assertEqual(("", "Hallway AP", "u6-lite-hall"), (hall["name"], hall["unifi"]["name"], hall["unifi"]["hostname"]))
        offered = IPAM.view()["unifi_networks"]
        self.assertEqual([("192.168.20.0/24", 20)], [(n["cidr"], n["vlan"]) for n in offered])
        # a category someone set is kept on the next sync
        IPAM.bulk(["192.0.2.3"], {"category": "iot"})
        IPAM.sync_unifi(get=lambda url: pages[url])
        self.assertEqual("iot", self.row("192.0.2.3")["category"])
        with self.assertRaises(ValueError):
            IPAM.save_record({"ip": "192.0.2.9", "category": "toaster"})

    def test_unifi_is_optional_and_can_be_disconnected(self):
        self.assertFalse(IPAM.view()["unifi"]["configured"])
        IPAM.save_unifi({"url": "https://192.0.2.1"})
        self.assertFalse(IPAM.view()["unifi"]["configured"], "an address without a key is not a connection")
        IPAM.save_unifi({"url": "https://192.0.2.1", "api_key": "k"})
        self.assertTrue(IPAM.view()["unifi"]["configured"])
        deleted = []
        send = self.store.send
        IPAM.bind(self.store.get, lambda m, path, body=None, **k: deleted.append(path) if m == "DELETE" else send(m, path, body),
                  "lab", lambda: FACTS)
        IPAM.save_unifi({"forget": True})
        self.assertTrue(any(path.endswith("/secrets/homestead-unifi") for path in deleted))
        view = IPAM.view()
        self.assertFalse(view["unifi"]["configured"])
        self.assertEqual([], view["unifi_networks"])

    def test_a_refused_key_says_so(self):
        IPAM.save_unifi({"url": "https://192.0.2.1", "api_key": "bad"})

        def refuse(url):
            raise urllib.error.HTTPError(url, 401, "no", None, None)
        with self.assertRaisesRegex(ValueError, "refused the API key"):
            IPAM.sync_unifi(get=refuse)
        self.assertIn("refused", IPAM.view()["unifi"]["last_error"])
        with self.assertRaises(ValueError):
            IPAM.save_unifi({"url": "http://192.0.2.1"})


if __name__ == "__main__":
    unittest.main()

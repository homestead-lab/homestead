"""Every combined view across linked clusters has a declared policy.

Architecture in the All clusters view failed with "this route has no
declared authorization policy": the server offered /api/fleet/all/flow, but
the route policy listed the other combined lists only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server
import homestead_route_policy as ROUTE_POLICY


class FleetAllRouteTests(unittest.TestCase):
    def test_every_combined_list_is_readable_by_a_viewer(self):
        for name in server.FLEET_LISTS:
            path = f"/api/fleet/all/{name}"
            self.assertEqual("viewer", server.needed_role(path, "GET"), path)

    def test_only_the_combined_lists_are_declared(self):
        self.assertIsNone(ROUTE_POLICY.role("/api/fleet/all/secrets", "GET"))
        self.assertIsNone(ROUTE_POLICY.role("/api/fleet/all/flow", "POST"))


if __name__ == "__main__":
    unittest.main()

"""Architecture aggregation retains graph boundaries and caller permissions."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server"))
import server

class FleetArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.graph={"workloads":[{"id":"w:app"}],"nodes":[{"id":"n:host"}],"volumes":[{"id":"v:data"}],"vips":[{"id":"i:192.0.2.1"}]}
        self.members=[{"id":"home","name":"Home","handle":"home","self":True,"reachable":True},{"id":"away","name":"Away","handle":"away","self":False,"reachable":True},{"id":"offline","name":"Offline","handle":"offline","self":False,"reachable":False}]
        for patch in [mock.patch.object(server.FLEET,"summary",return_value={"self":"home","members":self.members}),mock.patch.dict(server.FLEET_LISTS,{"flow":lambda:copy.deepcopy(self.graph)})]:
            patch.start();self.addCleanup(patch.stop)

    def test_matching_resource_names_stay_in_distinct_cluster_graphs_and_relay_uses_caller_role(self):
        with mock.patch.object(server.FLEET,"call",return_value=copy.deepcopy(self.graph)) as relay:
            result,missing=server.FLEET.gather("flow",server.FLEET_LISTS["flow"],"alice@entry","viewer")
        self.assertEqual(["home","away"],[c["site"]["id"] for c in result["clusters"]])
        self.assertEqual(["w:app","w:app"],[c["workloads"][0]["id"] for c in result["clusters"]])
        self.assertEqual("offline",missing[0]["id"])
        self.assertEqual(missing,result["missing"])
        relay.assert_called_once_with(self.members[1],"GET","/api/flow",timeout=12,user="alice",role="viewer")
        self.assertNotIn("site",self.graph)

    def test_unavailable_or_old_member_is_named_without_discarding_other_graphs(self):
        for reply in ({"error":"not found"},[],None):
            with self.subTest(reply=reply),mock.patch.object(server.FLEET,"call",return_value=reply):
                result,missing=server.FLEET.gather("flow",server.FLEET_LISTS["flow"],"alice","operator")
            self.assertEqual(1,len(result["clusters"]))
            self.assertEqual({"offline","away"},{m["id"] for m in missing})

    def test_local_failure_keeps_remote_graph_visible(self):
        def fail(): raise RuntimeError("API unavailable")
        with mock.patch.dict(server.FLEET_LISTS,{"flow":fail}),mock.patch.object(server.FLEET,"call",return_value=copy.deepcopy(self.graph)):
            result,missing=server.FLEET.gather("flow",server.FLEET_LISTS["flow"],"alice","viewer")
        self.assertEqual(["away"],[c["site"]["id"] for c in result["clusters"]])
        self.assertIn("home",[m["id"] for m in missing])

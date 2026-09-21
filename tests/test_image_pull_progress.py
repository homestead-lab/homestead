import copy
import sys
import time
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import harvui_operations as operations
import harvui_updates as updates


def _stamp(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


DEPLOYMENT = {
    "metadata": {"name": "plex", "namespace": "lab", "generation": 3, "annotations": {}},
    "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "plex"}},
             "template": {"metadata": {"labels": {"app": "plex"}},
                          "spec": {"containers": [{"name": "plex", "image": "plex:2"}]}}},
    "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1,
               "readyReplicas": 0, "availableReplicas": 0, "unavailableReplicas": 1},
}
POD = {
    "metadata": {"name": "plex-abc", "namespace": "lab", "labels": {"app": "plex"}},
    "spec": {"nodeName": "harvester-node2"},
    "status": {"phase": "Pending", "containerStatuses": [
        {"name": "plex", "ready": False, "state": {"waiting": {"reason": "ContainerCreating"}}}]},
}


class ImagePullProgressTests(unittest.TestCase):
    """A rollout stuck at 0/1 should say it is fetching an image, not just wait."""

    def setUp(self):
        self.events = []
        self.dep = copy.deepcopy(DEPLOYMENT)

        def get(path):
            if "/events" in path:
                return {"items": self.events}
            if path == "/api/v1/pods":
                return {"items": [copy.deepcopy(POD)]}
            if path.endswith("/deployments/plex"):
                return self.dep
            if "/serviceaccounts/" in path:
                return {"imagePullSecrets": []}
            raise AssertionError(path)

        updates.bind(get, lambda *a, **k: {}, "lab", str(Path(__file__).parent))

    def test_a_pull_in_flight_is_reported_with_its_image_node_and_age(self):
        self.events = [{"reason": "Pulling", "lastTimestamp": _stamp(75),
                        "message": 'Pulling image "ghcr.io/example/plex:2"'}]

        pull = updates.progress("lab", "plex")["pull"]

        self.assertEqual("pulling", pull["state"])
        self.assertEqual("ghcr.io/example/plex:2", pull["image"])
        self.assertEqual("harvester-node2", pull["node"])
        self.assertGreaterEqual(pull["seconds"], 70)

    def test_a_finished_pull_carries_how_long_it_took(self):
        self.events = [
            {"reason": "Pulling", "lastTimestamp": _stamp(90), "message": 'Pulling image "plex:2"'},
            {"reason": "Pulled", "lastTimestamp": _stamp(5),
             "message": 'Successfully pulled image "plex:2" in 1m24.5s (1m24.5s including waiting)'},
        ]

        pull = updates.progress("lab", "plex")["pull"]

        self.assertEqual("pulled", pull["state"])
        self.assertEqual("1m24.5s", pull["took"])

    def test_a_failed_pull_is_reported_rather_than_a_bare_phase(self):
        self.events = [{"reason": "Failed", "lastTimestamp": _stamp(3),
                        "message": 'Failed to pull image "plex:2": not found'}]

        self.assertEqual("failed", updates.progress("lab", "plex")["pull"]["state"])

    def test_a_running_pod_is_not_asked_about_pulls(self):
        ready = copy.deepcopy(POD)
        ready["status"] = {"phase": "Running", "containerStatuses": [{"name": "plex", "ready": True}]}

        def get(path):
            if "/events" in path:
                raise AssertionError("a ready pod needs no pull lookup")
            if path == "/api/v1/pods":
                return {"items": [ready]}
            if path.endswith("/deployments/plex"):
                return self.dep
            raise AssertionError(path)

        updates.bind(get, lambda *a, **k: {}, "lab", str(Path(__file__).parent))
        self.assertEqual({}, updates.progress("lab", "plex")["pull"])


class ActivityTrayMessageTests(unittest.TestCase):
    def setUp(self):
        self.state = {"desired": 1, "ready": 0, "phase": "progressing", "pull": {}}
        operations.bind(lambda path: {}, str(Path(__file__).parent),
                        lambda ns, name: self.state)

    def _message(self):
        return operations._deployment({"ref": {"namespace": "lab", "name": "plex"}})[2]

    def test_the_tray_says_what_the_wait_is_for(self):
        self.state["pull"] = {"state": "pulling", "node": "harvester-node2", "seconds": 135}

        self.assertEqual("Pulling image on harvester-node2 · 2m 15s so far", self._message())

    def test_a_pulled_image_moves_on_to_starting(self):
        self.state["pull"] = {"state": "pulled", "took": "1m24.5s"}

        self.assertEqual("Image pulled in 1m24.5s; starting container", self._message())

    def test_without_a_pull_the_replica_count_still_speaks(self):
        self.assertEqual("0/1 replicas ready", self._message())


if __name__ == "__main__":
    unittest.main()

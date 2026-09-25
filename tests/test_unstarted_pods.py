import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server

WAITING = {"state": {"waiting": {"reason": "ImagePullBackOff"}}}
RUNNING = {"state": {"running": {"startedAt": "2026-09-25T10:00:00Z"}}}


def pod(name, statuses, deleting=""):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = deleting
    return {"metadata": meta, "status": {"phase": "Pending", "containerStatuses": statuses}}


class UnstartedPodTests(unittest.TestCase):
    """Each Stop and Start of doublecommander left one more pod behind: pods
    that never started seldom finish stopping."""

    def setUp(self):
        self.saved = server.kget, server.ksend
        self.pods, self.deleted = [], []

        def kget(path):
            if "/deployments/" in path:
                return {"spec": {"selector": {"matchLabels": {"app": "doublecommander"}}}}
            return {"items": self.pods}
        server.kget = kget
        server.ksend = lambda method, path, body=None, **kw: self.deleted.append(path.split("/pods/")[1].split("?")[0])

    def tearDown(self):
        server.kget, server.ksend = self.saved

    def test_stopping_clears_pods_that_never_started(self):
        self.pods = [pod("stuck-pull", [WAITING]), pod("creating", []), pod("was-running", [RUNNING])]
        server.clear_unstarted_pods("lab", "doublecommander", stopping=True)
        self.assertEqual(["stuck-pull", "creating"], self.deleted)

    def test_starting_clears_only_those_still_stuck_stopping(self):
        self.pods = [pod("left-over", [WAITING], deleting="2026-01-01T00:00:00Z"), pod("fresh", [WAITING])]
        server.clear_unstarted_pods("lab", "doublecommander", stopping=False)
        self.assertEqual(["left-over"], self.deleted)


if __name__ == "__main__":
    unittest.main()

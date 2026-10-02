import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_api_errors as errors
import server

GIB = 1024 ** 3


def rejection(text, code=422, status=True):
    body = json.dumps({"kind": "Status", "status": "Failure", "message": text}) if status else text
    return urllib.error.HTTPError("https://example.invalid/api", code, "Rejected", {}, io.BytesIO(body.encode()))


def capacity_error(growth=600, scheduled=4000):
    return ('admission webhook "validator.longhorn.io" denied the request: '
            'error while CheckReplicasSizeExpansion for volume example-volume: '
            f'cannot schedule {growth * GIB} more bytes to disk example-disk with '
            f'&{{StorageAvailable:{1000 * GIB} StorageMaximum:{3000 * GIB} StorageReserved:0 '
            f'StorageScheduled:{scheduled * GIB} OverProvisioningPercentage:150 MinimalAvailablePercentage:20}}; '
            'Scheduling space condition failed: ScheduledTotal = allocation details')


class ApiErrorTests(unittest.TestCase):
    def test_expansion_explains_allocation_limit_without_internal_identifiers(self):
        text = errors.message(rejection(capacity_error()))
        self.assertIn("would exceed its storage allocation limit", text)
        self.assertIn("extra 600 GiB", text)
        self.assertIn("move replicas", text)
        self.assertIn("Cluster-wide free space", text)
        self.assertNotIn("example-disk", text)
        self.assertNotIn("kind", text)
        self.assertLess(len(text), 500)

    def test_free_space_rejection_does_not_claim_allocation_limit_was_exceeded(self):
        text = errors.message(rejection(capacity_error(growth=900, scheduled=1000)))
        self.assertIn("does not have enough capacity", text)
        self.assertNotIn("would exceed", text)

    def test_unknown_capacity_details_still_give_actionable_explanation(self):
        text = errors.message(rejection(capacity_error().split(" with ")[0] + "; Scheduling space condition failed"))
        self.assertIn("extra 600 GiB", text)
        self.assertIn("add storage", text)

    def test_unrelated_longhorn_refusal_keeps_its_actual_message(self):
        text = 'admission webhook "validator.longhorn.io" denied the request: volume is restoring'
        self.assertEqual(text, errors.message(rejection(text)))

    def test_kubernetes_message_is_extracted_before_truncating(self):
        error = rejection("access denied")
        self.assertEqual("access denied", errors.message(error, 20))
        self.assertEqual("a" * 500, errors.message(rejection("a" * 1000), 500))

    def test_non_status_malformed_plain_and_empty_bodies_remain_bounded(self):
        for text in ('{"message": "other service"}', '{"kind":"Status",', "proxy unavailable", ""):
            with self.subTest(text=text):
                error = rejection(text, status=False)
                self.assertEqual((text or str(error))[:20], errors.message(error, 20))

    def test_rejected_volume_edit_preserves_status_and_stops_before_replica_changes(self):
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/volumes/edit", {}
        handler._guard = lambda path: False
        handler._body = lambda: {"namespace": "lab", "name": "example-data", "size_gb": 605, "replicas": 3}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        pvc = {"spec": {"storageClassName": "example-storage", "volumeName": "example-volume",
                        "resources": {"requests": {"storage": "5Gi"}}}, "status": {"phase": "Bound"}}
        with mock.patch.object(server, "kget", side_effect=[pvc, {"allowVolumeExpansion": True}]), \
                mock.patch.object(server, "ksend", side_effect=rejection(capacity_error())) as send:
            handler.do_POST()
        code, body = handler._send.call_args.args
        self.assertEqual(422, code)
        self.assertIn("storage allocation limit", body["error"])
        self.assertNotIn("ok", body)
        send.assert_called_once()
        self.assertIn("persistentvolumeclaims/example-data", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

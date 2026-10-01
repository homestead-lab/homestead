import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import server


class ChannelAPITests(unittest.TestCase):
    def call(self, channel):
        settings = server.validate_app_settings({"site_name": "Shed", "catalog_url": "https://example.com/feed",
            "updates": {"policy": "notify_only", "notify_failures": False}})
        current = {"metadata": {"resourceVersion": "7"}, "data": {"settings.json": json.dumps(settings)}}
        handler = object.__new__(server.H)
        handler.path, handler.headers = "/api/image-updates/channel", {}
        handler._guard = lambda path: False
        handler._body = lambda: {"channel": channel}
        handler._client_ip = lambda: "127.0.0.1"
        handler._send = mock.Mock()
        with mock.patch.object(server, "kget", return_value=copy.deepcopy(current)), \
             mock.patch.object(server, "ksend") as send, \
             mock.patch.object(server.UPDATES, "invalidate") as invalidate, \
             mock.patch.object(server, "_fleet_rename"):
            handler.do_POST()
        return handler._send.call_args.args, send, invalidate

    def test_selecting_dev_preserves_other_settings_and_writes_only_the_settings_map(self):
        result, send, invalidate = self.call("dev")
        self.assertEqual(200, result[0])
        self.assertEqual("dev", result[1]["updates"]["channel"])
        self.assertEqual("Shed", result[1]["site_name"])
        self.assertEqual("https://example.com/feed", result[1]["catalog_url"])
        self.assertEqual("notify_only", result[1]["updates"]["policy"])
        self.assertFalse(result[1]["updates"]["notify_failures"])
        send.assert_called_once()
        method, path, body = send.call_args.args
        self.assertEqual("PUT", method)
        self.assertIn("/configmaps/", path)
        self.assertEqual("7", body["metadata"]["resourceVersion"])
        invalidate.assert_called_once()

    def test_invalid_channel_does_not_change_any_cluster_resource(self):
        result, send, invalidate = self.call("edge")
        self.assertEqual(400, result[0])
        send.assert_not_called()
        invalidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

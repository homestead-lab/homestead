"""update_json: JSON kept in a ConfigMap or Secret key, changed by read, apply,
write with the version read, and again on a 409 (#621). Two writers at once
must both keep their change. The fake API below checks resourceVersion as
Kubernetes does; tests/fakes/kube.py (#646) is not on this branch's base."""
import base64
import copy
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_kube as K
import homestead_names as NAMES

MAPS = "/api/v1/namespaces/lab/configmaps"
SECRETS = "/api/v1/namespaces/lab/secrets"
STORE = MAPS + "/store"


def http_error(code, path="/x"):
    return urllib.error.HTTPError(path, code, "error", {}, io.BytesIO(b""))


class FakeApi:
    """Objects by path, each PUT checked against its resourceVersion and each
    POST refused if the name is taken, as the API server does."""
    def __init__(self, objects=None):
        self.objects, self.version, self.writes = {}, 100, []
        for path, obj in (objects or {}).items():
            self._keep(path, obj)

    def _keep(self, path, obj):
        self.version += 1
        obj = copy.deepcopy(obj)
        obj.setdefault("metadata", {})["resourceVersion"] = str(self.version)
        self.objects[path] = obj

    def kget(self, path, **_):
        if path not in self.objects:
            raise http_error(404, path)
        return copy.deepcopy(self.objects[path])

    def ksend(self, method, path, body=None, *args, **_):
        self.writes.append((method, path))
        if method == "PUT":
            if path not in self.objects:
                raise http_error(404, path)
            if (body.get("metadata") or {}).get("resourceVersion") != self.objects[path]["metadata"]["resourceVersion"]:
                raise http_error(409, path)
            self._keep(path, body)
        elif method == "POST":
            target = path + "/" + body["metadata"]["name"]
            if target in self.objects:
                raise http_error(409, path)
            self._keep(target, body)
        else:
            raise AssertionError(f"unexpected {method}")
        return copy.deepcopy(self.objects[path if method == "PUT" else target])

    def patched(self):
        stack = mock.patch.multiple(K, kget=self.kget, ksend=self.ksend)
        return stack

    def stored(self, path=STORE, key="state.json"):
        return json.loads(self.objects[path]["data"][key])


def config_map(value, **extra_data):
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "store", "namespace": "lab", "labels": {"keep": "me"}},
            "data": {"state.json": json.dumps(value), **extra_data}}


def add(name):
    def change(current):
        rows = dict(current or {})
        rows[name] = True
        return rows
    return change


class UpdateJsonTests(unittest.TestCase):
    def test_two_writers_interleaved_both_keep_their_change(self):
        api = FakeApi({STORE: config_map({"a": True})})
        calls = []

        def first(current):
            calls.append(copy.deepcopy(current))
            if len(calls) == 1:
                # Another replica saves between this read and this write.
                K.update_json(STORE, "state.json", add("second"))
            return add("first")(current)

        with api.patched():
            K.update_json(STORE, "state.json", first)
        self.assertEqual(api.stored(), {"a": True, "first": True, "second": True})

    def test_a_409_is_retried_with_the_fresh_value(self):
        api = FakeApi({STORE: config_map({"count": 1})})
        seen = []

        def bump(current):
            seen.append(current["count"])
            if len(seen) == 1:
                api._keep(STORE, config_map({"count": 5}))    # written meanwhile: the PUT gets 409
            return {"count": current["count"] + 1}

        with api.patched():
            result = K.update_json(STORE, "state.json", bump)
        self.assertEqual(seen, [1, 5])
        self.assertEqual(result, {"count": 6})
        self.assertEqual(api.stored(), {"count": 6})
        self.assertEqual([m for m, _ in api.writes], ["PUT", "PUT"])

    def test_fn_raising_writes_nothing(self):
        api = FakeApi({STORE: config_map({"a": True})})

        def refuse(current):
            raise ValueError("that name is taken")

        with api.patched(), self.assertRaisesRegex(ValueError, "that name is taken"):
            K.update_json(STORE, "state.json", refuse)
        self.assertEqual(api.writes, [])
        self.assertEqual(api.stored(), {"a": True})

    def test_retries_exhausted_give_a_clear_error_and_write_nothing_stale(self):
        api = FakeApi({STORE: config_map({"n": 0})})

        def always_beaten(current):
            api._keep(STORE, config_map({"n": current["n"] + 100}))
            return {"n": -1}

        with api.patched(), self.assertRaises(RuntimeError) as caught:
            K.update_json(STORE, "state.json", always_beaten, attempts=3)
        self.assertIn("store kept changing", str(caught.exception))
        self.assertIn("Try again", str(caught.exception))
        self.assertEqual([m for m, _ in api.writes], ["PUT"] * 3)
        self.assertNotEqual(api.stored(), {"n": -1})

    def test_other_keys_and_metadata_are_kept(self):
        api = FakeApi({STORE: config_map({"a": True}, other="untouched")})
        with api.patched():
            K.update_json(STORE, "state.json", add("b"))
        stored = api.objects[STORE]
        self.assertEqual(stored["data"]["other"], "untouched")
        self.assertEqual(stored["metadata"]["labels"], {"keep": "me"})
        self.assertEqual(api.stored(), {"a": True, "b": True})

    def test_missing_key_is_none_to_fn(self):
        api = FakeApi({STORE: {"apiVersion": "v1", "kind": "ConfigMap",
                               "metadata": {"name": "store", "namespace": "lab"}}})
        seen = []
        with api.patched():
            K.update_json(STORE, "state.json", lambda current: seen.append(current) or {"x": 1})
        self.assertEqual(seen, [None])
        self.assertEqual(api.stored(), {"x": 1})

    def test_not_json_is_an_error_not_an_empty_store(self):
        api = FakeApi({STORE: config_map({}, **{"state.json": "{not json"})})
        with api.patched(), self.assertRaisesRegex(ValueError, "not valid JSON"):
            K.update_json(STORE, "state.json", add("b"))
        self.assertEqual(api.writes, [])

    def test_replace_writes_over_a_stored_value_that_is_not_json(self):
        # A save whose new value does not depend on the old must not be
        # blocked by a hand-edited value it would replace.
        for bad in ("{not json", "", "07"):
            with self.subTest(stored=bad):
                api = FakeApi({STORE: config_map({}, **{"state.json": bad})})
                seen = []
                with api.patched():
                    K.update_json(STORE, "state.json", lambda current: seen.append(current) or {"x": 1}, replace=True)
                self.assertEqual(seen, [None])
                self.assertEqual(api.stored(), {"x": 1})

    def test_replace_still_keeps_a_change_saved_meanwhile_in_another_key(self):
        api = FakeApi({STORE: config_map({}, other="old")})
        calls = []

        def first(current):
            calls.append(current)
            if len(calls) == 1:
                api._keep(STORE, config_map({}, other="new"))
            return {"x": 1}

        with api.patched():
            K.update_json(STORE, "state.json", first, replace=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(api.objects[STORE]["data"]["other"], "new")

    def test_deleted_between_read_and_write_is_created(self):
        api = FakeApi({STORE: config_map({"a": True})})

        def gone_meanwhile(current):
            if current == {"a": True}:
                api.objects.pop(STORE)       # someone deletes it before this write
            return add("b")(current)

        with api.patched():
            K.update_json(STORE, "state.json", gone_meanwhile)
        self.assertEqual([m for m, _ in api.writes], ["PUT", "POST"])
        self.assertEqual(api.stored(), {"b": True})

    def test_missing_object_is_created_with_the_managed_label(self):
        api = FakeApi()
        with api.patched():
            K.update_json(STORE, "state.json", add("a"))
        self.assertEqual(api.writes, [("POST", MAPS)])
        made = api.objects[STORE]
        self.assertEqual((made["kind"], made["metadata"]["namespace"]), ("ConfigMap", "lab"))
        self.assertEqual(made["metadata"]["labels"], {NAMES.key("managed"): "true"})
        self.assertEqual(api.stored(), {"a": True})

    def test_create_race_is_retried_as_an_update(self):
        api = FakeApi()
        calls = []

        def first(current):
            calls.append(current)
            if len(calls) == 1:
                K.update_json(STORE, "state.json", add("second"))    # creates it first
            return add("first")(current)

        with api.patched():
            K.update_json(STORE, "state.json", first)
        self.assertEqual(calls[0], None)
        self.assertEqual(api.stored(), {"first": True, "second": True})

    def test_secret_key_is_base64(self):
        path = SECRETS + "/store"
        api = FakeApi({path: {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                              "metadata": {"name": "store", "namespace": "lab"},
                              "data": {"state.json": base64.b64encode(b'{"a": true}').decode()}}})
        with api.patched():
            K.update_json(path, "state.json", add("b"))
        raw = api.objects[path]["data"]["state.json"]
        self.assertEqual(json.loads(base64.b64decode(raw)), {"a": True, "b": True})

    def test_new_secret_is_created_as_opaque(self):
        api = FakeApi()
        with api.patched():
            K.update_json(SECRETS + "/store", "state.json", add("a"))
        made = api.objects[SECRETS + "/store"]
        self.assertEqual((made["kind"], made["type"]), ("Secret", "Opaque"))
        self.assertEqual(json.loads(base64.b64decode(made["data"]["state.json"])), {"a": True})

    def test_only_a_config_map_or_secret_path(self):
        with self.assertRaises(ValueError):
            K.update_json("/api/v1/namespaces/lab/pods/store", "state.json", add("a"))

    def test_a_module_bound_read_and_write_are_used(self):
        # A module bound with its own kget and ksend (most still are) passes
        # them; the client's own are not touched.
        api = FakeApi({STORE: config_map({"a": True})})
        refuse = mock.Mock(side_effect=AssertionError("the client's own transport was used"))
        with mock.patch.multiple(K, kget=refuse, ksend=refuse):
            K.update_json(STORE, "state.json", add("b"), read=api.kget, write=api.ksend)
        self.assertEqual(api.stored(), {"a": True, "b": True})
        with mock.patch.multiple(K, kget=refuse, ksend=refuse):
            K.update_json(MAPS + "/fresh", "state.json", add("c"), read=api.kget, write=api.ksend)
        self.assertEqual(api.stored(MAPS + "/fresh"), {"c": True})

    def test_other_failures_are_not_retried(self):
        api = FakeApi({STORE: config_map({})})

        def forbidden(*args, **kwargs):
            raise http_error(403)

        with mock.patch.multiple(K, kget=api.kget, ksend=forbidden), self.assertRaises(urllib.error.HTTPError) as caught:
            K.update_json(STORE, "state.json", add("a"))
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()

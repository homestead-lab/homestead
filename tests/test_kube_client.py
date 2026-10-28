"""homestead_kube's client: the reads and writes every module will share
(#619). Reads go through kget and writes through ksend, which is where tests
patch them, so these tests patch the same names."""
import contextlib
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get("HOMESTEAD_TEST_SERVER_DIR", str(Path(__file__).resolve().parents[1] / "server")))
import homestead_kube as K


def http_error(code, path="/x"):
    return urllib.error.HTTPError(path, code, "error", {}, io.BytesIO(b""))


class Reads:
    """kget answering from a dict of path -> object or exception."""
    def __init__(self, answers):
        self.answers, self.paths = answers, []

    def __call__(self, path, **_):
        self.paths.append(path)
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


PODS = "/api/v1/namespaces/lab/pods"
SNAPSHOTS = "/apis/snapshot.storage.k8s.io/v1/volumesnapshots"


class GetTests(unittest.TestCase):
    def test_get_reads_through_kget(self):
        with mock.patch.object(K, "kget", Reads({PODS + "/a": {"kind": "Pod"}})):
            self.assertEqual(K.get(PODS + "/a"), {"kind": "Pod"})

    def test_get_optional_is_none_when_not_found(self):
        with mock.patch.object(K, "kget", Reads({PODS + "/a": http_error(404)})):
            self.assertIsNone(K.get_optional(PODS + "/a"))

    def test_get_optional_returns_the_object(self):
        with mock.patch.object(K, "kget", Reads({PODS + "/a": {"kind": "Pod"}})):
            self.assertEqual(K.get_optional(PODS + "/a"), {"kind": "Pod"})

    def test_get_optional_raises_any_other_failure(self):
        for code in (401, 403, 429, 500, 503):
            with self.subTest(code=code), mock.patch.object(K, "kget", Reads({PODS + "/a": http_error(code)})):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    K.get_optional(PODS + "/a")
                self.assertEqual(caught.exception.code, code)
        with mock.patch.object(K, "kget", Reads({PODS + "/a": OSError("timed out")})):
            with self.assertRaises(OSError):
                K.get_optional(PODS + "/a")

    def test_get_optional_reads_through_a_module_reader_when_given(self):
        # A module bound to its own reader (bind(kget, ...)) keeps reading
        # through it: 404 is None, anything else raises, the client is not used.
        read = Reads({PODS + "/a": {"kind": "Pod"}, PODS + "/b": http_error(404), PODS + "/c": http_error(500)})
        with mock.patch.object(K, "kget", Reads({})):
            self.assertEqual(K.get_optional(PODS + "/a", read=read), {"kind": "Pod"})
            self.assertIsNone(K.get_optional(PODS + "/b", read=read))
            with self.assertRaises(urllib.error.HTTPError):
                K.get_optional(PODS + "/c", read=read)
        self.assertEqual(read.paths, [PODS + "/a", PODS + "/b", PODS + "/c"])

    def test_get_optional_answers_what_it_is_given_for_missing(self):
        # A module that must tell a 404 from a null answer passes its own
        # marker: only a 404 comes back as it, a null answer stays None.
        gone = object()
        read = Reads({PODS + "/a": http_error(404), PODS + "/b": None, PODS + "/c": {"kind": "Pod"}})
        self.assertIs(K.get_optional(PODS + "/a", read=read, missing=gone), gone)
        self.assertIsNone(K.get_optional(PODS + "/b", read=read, missing=gone))
        self.assertEqual(K.get_optional(PODS + "/c", read=read, missing=gone), {"kind": "Pod"})
        with self.assertRaises(urllib.error.HTTPError):
            K.get_optional(PODS + "/a", read=Reads({PODS + "/a": http_error(500)}), missing=gone)

    def test_an_unbound_reader_fails_rather_than_reading_the_cluster(self):
        # A module whose bind() never ran passes read=None: that is a mistake
        # to report, not a cue to read through the real client.
        client = Reads({PODS + "/a": {"kind": "Pod"}})
        with mock.patch.object(K, "kget", client):
            with self.assertRaises(TypeError):
                K.get_optional(PODS + "/a", read=None)
            self.assertEqual(client.paths, [])
            self.assertEqual(K.get_optional(PODS + "/a"), {"kind": "Pod"})


class ItemsTests(unittest.TestCase):
    def test_a_whole_list_is_returned(self):
        with mock.patch.object(K, "kget", Reads({PODS: {"items": [{"metadata": {"name": "a"}}], "metadata": {}}})):
            self.assertEqual(K.items(PODS), [{"metadata": {"name": "a"}}])

    def test_an_empty_list_is_empty(self):
        with mock.patch.object(K, "kget", Reads({PODS: {"items": []}})):
            self.assertEqual(K.items(PODS), [])

    def test_a_list_cut_short_by_continue_raises(self):
        with mock.patch.object(K, "kget", Reads({PODS: {"items": [{}], "metadata": {"continue": "token"}}})):
            with self.assertRaises(ValueError):
                K.items(PODS)

    def test_an_answer_without_a_list_raises(self):
        for answer in ({}, {"items": None}, {"items": {}}, [], None):
            with self.subTest(answer=answer), mock.patch.object(K, "kget", Reads({PODS: answer})):
                with self.assertRaises(ValueError):
                    K.items(PODS)

    def test_a_row_that_is_not_an_object_raises(self):
        with mock.patch.object(K, "kget", Reads({PODS: {"items": [{}, "x"]}})):
            with self.assertRaises(ValueError):
                K.items(PODS)

    def test_a_failed_read_raises(self):
        for error in (http_error(403), http_error(500), OSError("timed out")):
            with self.subTest(error=error), mock.patch.object(K, "kget", Reads({PODS: error})):
                with self.assertRaises(type(error)):
                    K.items(PODS)

    def test_not_found_raises_without_an_optional_group(self):
        reads = Reads({PODS: http_error(404)})
        with mock.patch.object(K, "kget", reads):
            with self.assertRaises(urllib.error.HTTPError):
                K.items(PODS)
        self.assertEqual(reads.paths, [PODS])

    def test_a_throttled_read_is_left_to_kget(self):
        # kget already retries a 429; items does not add a second round.
        reads = Reads({PODS: http_error(429)})
        with mock.patch.object(K, "kget", reads):
            with self.assertRaises(urllib.error.HTTPError):
                K.items(PODS)
        self.assertEqual(reads.paths, [PODS])

    def test_an_optional_group_that_is_not_installed_is_empty(self):
        reads = Reads({SNAPSHOTS: http_error(404), "/apis": {"groups": [{"name": "apps"}]}})
        with mock.patch.object(K, "kget", reads):
            self.assertEqual(K.items(SNAPSHOTS, optional_group="snapshot.storage.k8s.io"), [])
        self.assertEqual(reads.paths, [SNAPSHOTS, "/apis"])

    def test_not_found_in_an_installed_optional_group_raises(self):
        reads = Reads({SNAPSHOTS: http_error(404), "/apis": {"groups": [{"name": "snapshot.storage.k8s.io"}]}})
        with mock.patch.object(K, "kget", reads):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                K.items(SNAPSHOTS, optional_group="snapshot.storage.k8s.io")
        self.assertEqual(caught.exception.code, 404)

    def test_unreadable_discovery_raises(self):
        # An answer that names no group at all says nothing about this one: a
        # real /apis always serves apps and batch.
        for discovery in ({}, {"groups": None}, {"groups": []}, {"groups": [{}]}, {"groups": ["apps"]},
                          http_error(503)):
            reads = Reads({SNAPSHOTS: http_error(404), "/apis": discovery})
            with self.subTest(discovery=discovery), mock.patch.object(K, "kget", reads):
                with self.assertRaises((ValueError, urllib.error.HTTPError)) as caught:
                    K.items(SNAPSHOTS, optional_group="snapshot.storage.k8s.io")
                if isinstance(caught.exception, urllib.error.HTTPError):
                    self.assertEqual(caught.exception.code, 503)

    def test_an_installed_optional_group_lists_normally(self):
        with mock.patch.object(K, "kget", Reads({SNAPSHOTS: {"items": [{"metadata": {"name": "s"}}]}})):
            self.assertEqual(K.items(SNAPSHOTS, optional_group="snapshot.storage.k8s.io"), [{"metadata": {"name": "s"}}])

    def test_items_reads_through_a_module_reader_when_given(self):
        # A module bound to its own reader keeps listing through it, API
        # discovery included; the client is not used.
        read = Reads({PODS: {"items": [{"metadata": {"name": "a"}}]}, SNAPSHOTS: http_error(404),
                      "/apis": {"groups": [{"name": "apps"}]}})
        with mock.patch.object(K, "kget", Reads({})):
            self.assertEqual(K.items(PODS, read=read), [{"metadata": {"name": "a"}}])
            self.assertEqual(K.items(SNAPSHOTS, optional_group="snapshot.storage.k8s.io", read=read), [])
        self.assertEqual(read.paths, [PODS, SNAPSHOTS, "/apis"])

    def test_items_through_a_module_reader_is_as_strict(self):
        read = Reads({PODS: {"items": [{}], "metadata": {"continue": "token"}}})
        with self.assertRaises(ValueError):
            K.items(PODS, read=read)

    def test_items_with_an_unbound_reader_fails_rather_than_reading_the_cluster(self):
        client = Reads({PODS: {"items": []}})
        with mock.patch.object(K, "kget", client):
            with self.assertRaises(TypeError):
                K.items(PODS, read=None)
            self.assertEqual(client.paths, [])

    def test_an_incomplete_list_is_its_own_kind_of_value_error(self):
        # A module can put an incomplete list in its own words without
        # rewording other ValueErrors (an invalid name, an answer not JSON).
        answers = {PODS: {"items": None}, PODS + "?x": {"items": ["x"]}, SNAPSHOTS: http_error(404), "/apis": {"groups": None}}
        for path, options in ((PODS, {}), (PODS + "?x", {}), (SNAPSHOTS, {"optional_group": "snapshot.storage.k8s.io"})):
            with self.subTest(path=path):
                with self.assertRaises(K.Incomplete) as caught:
                    K.items(path, read=Reads(answers), **options)
                self.assertIsInstance(caught.exception, ValueError)
        with self.assertRaises(ValueError) as caught:
            K.items(PODS, read=Reads({PODS: ValueError("not a valid Kubernetes name")}))
        self.assertNotIsInstance(caught.exception, K.Incomplete)


class SendTests(unittest.TestCase):
    def test_send_writes_through_ksend(self):
        calls = []
        with mock.patch.object(K, "ksend", lambda *a, **k: calls.append((a, k)) or {"ok": 1}):
            self.assertEqual(K.send("PATCH", PODS + "/a", {"x": 1}, ctype="application/merge-patch+json"), {"ok": 1})
        self.assertEqual(calls, [(("PATCH", PODS + "/a", {"x": 1}), {"ctype": "application/merge-patch+json"})])

    def test_delete_if_present_deletes(self):
        calls = []
        with mock.patch.object(K, "ksend", lambda *a, **k: calls.append(a) or {"kind": "Status"}):
            self.assertEqual(K.delete_if_present(PODS + "/a"), {"kind": "Status"})
        self.assertEqual(calls, [("DELETE", PODS + "/a", None)])

    def test_delete_if_present_is_none_when_already_gone(self):
        def gone(*_a, **_k):
            raise http_error(404)
        with mock.patch.object(K, "ksend", gone):
            self.assertIsNone(K.delete_if_present(PODS + "/a"))

    def test_delete_if_present_sends_through_a_modules_own_sender(self):
        calls = []
        with mock.patch.object(K, "ksend", lambda *a, **k: self.fail("sent through the shared client")):
            self.assertEqual(K.delete_if_present(PODS + "/a", send=lambda *a, **k: calls.append(a) or {}), {})
            self.assertIsNone(K.delete_if_present(PODS + "/a", send=lambda *a, **k: (_ for _ in ()).throw(http_error(404))))
            with self.assertRaises(urllib.error.HTTPError):
                K.delete_if_present(PODS + "/a", send=lambda *a, **k: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(calls, [("DELETE", PODS + "/a", None)])

    def test_delete_if_present_raises_any_other_failure(self):
        for code in (403, 409, 422, 500):
            def refused(*_a, code=code, **_k):
                raise http_error(code)
            with self.subTest(code=code), mock.patch.object(K, "ksend", refused):
                with self.assertRaises(urllib.error.HTTPError):
                    K.delete_if_present(PODS + "/a")


class WriteGuardTests(unittest.TestCase):
    def guarded(self, log, **refuse):
        """Patch what each registered guard calls, recording the order."""
        def step(name):
            def run(*_a, **_k):
                log.append(name)
                if name in refuse:
                    raise refuse[name]
            return run

        @contextlib.contextmanager
        def activity():
            log.append("activity")
            try:
                yield
            finally:
                log.append("activity done")

        def storage(method, path, body, dispatch, ops, read, own_controller=None):
            log.append("storage")
            if "storage" in refuse:
                raise refuse["storage"]
            return dispatch()

        def transport(*_a, **_k):
            log.append("transport")
            return {"written": True}

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(K, "self_data_activity", activity, create=True))
        stack.enter_context(mock.patch.object(K, "require_self_data_write", step("self-data fence"), create=True))
        stack.enter_context(mock.patch.object(K, "authorize_workload_write", step("workload access")))
        stack.enter_context(mock.patch.object(K.CHANGES, "note_write", step("change note")))
        stack.enter_context(mock.patch.object(K.STORAGE_GUARD, "send", storage))
        stack.enter_context(mock.patch.object(K, "_ksend", transport))
        return stack

    def test_the_guards_are_registered_in_todays_order(self):
        self.assertEqual([guard.__name__ for guard in K.WRITE_GUARDS],
                         ["self_data_fence", "workload_access", "change_note", "storage_guard", "fresh_reads"])

    def test_a_write_goes_through_every_guard_in_order(self):
        log = []
        with self.guarded(log):
            self.assertEqual(K.ksend("PUT", PODS + "/a", {"x": 1}), {"written": True})
        self.assertEqual(log, ["activity", "self-data fence", "workload access", "change note", "storage",
                               "transport", "activity done"])

    def test_a_refusing_guard_stops_the_write(self):
        for refusing, ran in (("self-data fence", ["activity", "self-data fence", "activity done"]),
                              ("workload access", ["activity", "self-data fence", "workload access", "activity done"]),
                              ("storage", ["activity", "self-data fence", "workload access", "change note", "storage",
                                           "activity done"])):
            log = []
            with self.subTest(refusing=refusing), self.guarded(log, **{refusing: ValueError("refused")}):
                with self.assertRaisesRegex(ValueError, "refused"):
                    K.ksend("DELETE", PODS + "/a")
                self.assertEqual(log, ran)

    def test_send_goes_round_the_guards(self):
        log = []
        with self.guarded(log):
            K.send("POST", PODS, {"metadata": {"name": "a"}})
        self.assertIn("transport", log)
        self.assertEqual(log[:2], ["activity", "self-data fence"])

    def test_the_storage_guard_is_handed_the_write_and_homesteads_own_controller(self):
        seen = {}

        def storage(method, path, body, dispatch, ops, read, own_controller=None):
            seen.update(method=method, path=path, body=body, ops=ops, read=read, own=own_controller)
            return dispatch()

        log = []
        with self.guarded(log), mock.patch.object(K.STORAGE_GUARD, "send", storage):
            K.ksend("PATCH", PODS + "/a", {"x": 1})
        self.assertEqual((seen["method"], seen["path"], seen["body"]), ("PATCH", PODS + "/a", {"x": 1}))
        self.assertIs(seen["ops"], K.OPS)
        self.assertIs(seen["read"], K.kget)
        self.assertEqual(seen["own"], (K.SELF.NS, K.NAMES.BRAND))

    def test_an_added_guard_runs_where_it_is_placed(self):
        log = []

        def audit(method, path, body, proceed):
            log.append("audit")
            return proceed()

        with self.guarded(log), mock.patch.object(K, "WRITE_GUARDS", list(K.WRITE_GUARDS)):
            K.add_write_guard(audit, before="change_note")
            self.assertEqual([guard.__name__ for guard in K.WRITE_GUARDS],
                             ["self_data_fence", "workload_access", "audit", "change_note", "storage_guard", "fresh_reads"])
            K.ksend("PUT", PODS + "/a", {})
        self.assertEqual(log[:5], ["activity", "self-data fence", "workload access", "audit", "change note"])

    def test_adding_a_guard_without_a_place_appends_it(self):
        def last(method, path, body, proceed):
            return proceed()

        with mock.patch.object(K, "WRITE_GUARDS", list(K.WRITE_GUARDS)):
            K.add_write_guard(last)
            self.assertIs(K.WRITE_GUARDS[-1], last)
            with self.assertRaises(ValueError):
                K.add_write_guard(last, before="no_such_guard")


if __name__ == "__main__":
    unittest.main()

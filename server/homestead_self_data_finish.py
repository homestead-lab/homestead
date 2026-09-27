"""Retire a verified handoff, never either data volume.

The completed receipt is written only on the verified destination. It lets that
destination survive cleanup/restart without keeping the coordinator forever.
An old source has no matching receipt and remains fenced even after its anchor
is deleted. Each helper deletion is UID-bound and journalled before dispatch.
"""
import copy
import json
import os
import stat
import urllib.error

import homestead_shared as SHARED
import homestead_self_data_anchor as A
import homestead_self_data_bootstrap as B
from homestead_storage_journal import Held, identity

FILE = ".self-data-completed-v1.json"


def read(directory, namespace, deployment):
    path = os.path.join(directory, FILE)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise Held("The completed data-move receipt cannot be read") from None
    try:
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode): raise Held("The completed move receipt is not a regular file")
            raw = stream.read(A.MAX_BYTES * 2 + 1)
            after = os.fstat(stream.fileno())
        published = os.stat(path, follow_symlinks=False)
        if (len(raw) > A.MAX_BYTES * 2 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
                (published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns)):
            raise Held("The completed move receipt changed while being read")
        value = json.loads(raw)
        A._keys(value, ("protocol", "anchor", "cleanup"), ("source_binding",))
        if type(value["protocol"]) is not int or value["protocol"] != 1: raise ValueError()
        obj = value["anchor"]
        operation = obj["metadata"]["labels"][A.LABEL]
        anchor = A.Anchor(None, None, namespace, deployment)
        state = anchor._decode(obj, operation, identity(obj)["uid"])
        anchor.obj, anchor.state = copy.deepcopy(obj), state
        from homestead_self_data_worker import progress
        if state.get("setup_aborted"):
            binding = value["source_binding"]
            A._keys(binding, ("data_volume", "destination_pvc", "destination_pv"))
            if binding["data_volume"] != "data" or binding["destination_pvc"] != {"name": state["source"]["name"], "uid": state["source"]["uid"]}: raise ValueError()
            A._keys(binding["destination_pv"], ("name", "uid"))
            A._name(binding["destination_pv"]["name"])
            if not isinstance(binding["destination_pv"]["uid"], str) or not binding["destination_pv"]["uid"]: raise ValueError()
        elif progress(anchor, 0)["status"] != "done": raise Held("The saved data move does not prove completion")
        expected = {r["target"]["path"]: r for r in state.get("setup", {}).get("resources", [])}
        expected[anchor.path] = None
        if not isinstance(value["cleanup"], dict) or set(value["cleanup"]) - set(expected): raise ValueError()
        receipts = {r["target"]["path"]: receipt["after"]["uid"] for r, receipt in zip(state.get("setup", {}).get("resources", []), state.get("setup", {}).get("receipts", [])) if receipt["state"] == "accepted"}
        receipts[anchor.path] = anchor.handle()["uid"]
        for path, row in value["cleanup"].items():
            A._keys(row, ("uid", "state"))
            if row["uid"] != receipts.get(path) or row["state"] not in ("intent", "deleted"): raise ValueError()
        return value, anchor
    except Held:
        raise
    except Exception:
        raise Held("The completed data-move receipt is invalid; it cannot authorize startup") from None


def _get(read_api, path):
    try: return read_api(path)
    except urllib.error.HTTPError as error:
        if error.code == 404: return None
        raise


def _save(directory, value):
    SHARED.write_json(os.path.join(directory, FILE), value, durable=True, mode=0o600)


def finish(fence, read_api, send):
    """One cleanup pass; an unresolved deletion is observed, never replayed.

    Callable after verified app startup, and again after a process restart.
    Failure does not stop the verified destination. No PVC/PV delete path exists.
    """
    from homestead_self_data_fence import read_marker, MARKER
    directory = fence.directory
    with SHARED.SharedLock("self-data-finish", strict=True, directory=lambda: directory):
        marker = read_marker(directory)
        saved = read(directory, fence.namespace, fence.deployment)
        if not marker:
            current = fence.inspect()
            if current.get("mode") == "recovery" and current["writable"] and (not saved or saved[1].state["operation"] != current["operation"]):
                anchor = A.Anchor(read_api, None, fence.namespace, fence.deployment).load(operation=current["operation"], uid=current["anchor_uid"])
                _save(directory, {"protocol": 1, "anchor": anchor.obj, "cleanup": {}, "source_binding": current["source_binding"]})
                saved = read(directory, fence.namespace, fence.deployment)
            if not saved: return {"done": True}
        if marker and (not saved or saved[1].state["operation"] != marker["operation"]):
            state = fence.inspect()
            if state.get("mode") not in ("done", "recovery") or not state["writable"]: raise Held("The data move has not completed")
            anchor = A.Anchor(read_api, None, fence.namespace, fence.deployment).load(operation=marker["operation"], uid=marker["anchor_uid"])
            from homestead_self_data_worker import progress
            if progress(anchor, 0)["status"] not in ("done", "cancelled"): raise Held("Completion evidence is not ready for cleanup")
            value = {"protocol": 1, "anchor": anchor.obj, "cleanup": {}}
            if anchor.state.get("setup_aborted"): value["source_binding"] = state["source_binding"]
            _save(directory, value)
            saved = read(directory, fence.namespace, fence.deployment)
        value, anchor = saved
        # This verifies the actual destination/controller/claim binding, not
        # just the presence of a file which an earlier copy may have carried.
        fence.inspect()
        expected_marker = A.pointer(fence.namespace, anchor.state, anchor.handle()["uid"]) if "plan" in anchor.state else None
        if marker is not None and marker != expected_marker: raise Held("Another data move is active; cleanup is not authorized")
        setup = anchor.state.get("setup", {"resources": [], "receipts": []})
        aborted = anchor.state.get("setup_aborted") is True
        if not aborted and not B.complete(setup): raise Held("Helper cleanup needs complete creation receipts")
        if any(r["state"] != "accepted" for r in setup["receipts"]):
            raise Held("Homestead is back on its original volume. A helper creation has an uncertain outcome: retain its control record and inspect the named resources before another move")
        # Stop/release the only helper Pod before its access or Service.
        order = [7, 8, 6, 5, 4, 3, 2, 1, 0]
        targets = [(setup["resources"][i]["target"]["path"], setup["receipts"][i]["after"]["uid"], setup["receipts"][i]["fingerprint"]) for i in order if i < len(setup["receipts"])]
        targets.append((anchor.path, anchor.handle()["uid"], None))
        for path, uid, fingerprint in targets:
            obj = _get(read_api, path)
            row = value["cleanup"].get(path)
            if obj is None:
                if row is None or row["state"] != "deleted":
                    value["cleanup"][path] = {"uid": uid, "state": "deleted"}; _save(directory, value)
                continue
            if identity(obj)["uid"] != uid: raise Held("A helper name was reused; its replacement will not be deleted")
            if row is not None:
                if row["state"] == "intent" and obj.get("metadata", {}).get("deletionTimestamp"):
                    return {"done": False, "message": "Waiting for temporary helper removal"}
                raise Held("Helper deletion is still pending or its reply was lost. Nothing was retried; inspect the retained cleanup receipt")
            if fingerprint is not None and B.fingerprint(obj) != fingerprint: raise Held("A helper changed before cleanup; inspect it first")
            if fingerprint is None:
                state = anchor._decode(obj, anchor.state["operation"], uid)
                if (state["phase"] != "done" and not state.get("setup_aborted")) or state.get("plan") != anchor.state.get("plan"):
                    raise Held("The completed control record changed before cleanup")
            value["cleanup"][path] = {"uid": uid, "state": "intent"}; _save(directory, value)
            send("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground", "preconditions": identity(obj)})
            if _get(read_api, path) is not None: return {"done": False, "message": "Waiting for temporary helper removal"}
            value["cleanup"][path]["state"] = "deleted"; _save(directory, value)
        # The destination receipt is already durable; a crash before/after this
        # unlink cannot authorize the original source or strand this destination.
        if marker is not None:
            if read_marker(directory) != expected_marker: raise Held("The active move changed during cleanup")
            os.unlink(os.path.join(directory, MARKER))
            if os.name == "posix":
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
        return {"done": True, "message": "Temporary helpers removed. Both data volumes are retained."}

"""Permanent compact approval fingerprints, independent of display history.

Callers hold the shared operations lock. Wall-clock expiry is not a safe reason
to forget a consumed approval: another replica's clock can move backwards.
No tokens, VM specs or credentials are stored here. Never prune this ledger.
"""
import json
import os
import re

import homestead_shared as SHARED

STORE = "vm-power-approvals.json"
# Keep historical filenames: replacing the ledger would resurrect approvals.
MARKER = ".vm-power-approvals-initialized.json"
KINDS = {"vm-power", "vm-create", "vm-edit", "workload-rename", "workload-copy", "import-create"}
_MISSING = object()


def protected(kind, ref):
    return (kind in KINDS or (kind == "k3s-cluster" and ref.get("dispatch_protocol") == 2)
            or (kind == "reclass" and ref.get("storage_approval_protocol") == 1))


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return _MISSING
    except (OSError, ValueError) as error:
        raise ValueError("VM approval history cannot be read; recover the ledger before changing VMs") from error


def _read(directory):
    marker = _load(os.path.join(directory, MARKER))
    if marker is not _MISSING and marker != {"version": 1}:
        raise ValueError("VM approval history marker is invalid; recover the ledger before changing VMs")
    value = _load(os.path.join(directory, STORE))
    if value is _MISSING:
        if marker is not _MISSING:
            raise ValueError("VM approval history is missing; recover the ledger before changing VMs")
        return {}, False
    if (not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("receipts"), dict)
            or any(not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(ident, str)
                   or not re.fullmatch(r"[0-9a-f]{24}", ident)
                   for digest, ident in value["receipts"].items())):
        raise ValueError("VM approval history is invalid; recover the ledger before changing VMs")
    return value["receipts"], marker is not _MISSING


def find(directory, digest):
    receipts, _ = _read(directory)
    return receipts.get(digest)


def remember(directory, items):
    pending = [item for item in items if protected(item.get("kind"), item.get("ref") or {})]
    if not pending:
        return
    receipts, initialized = _read(directory)
    changed = False
    for item in pending:
        digest, ident = item.get("ref", {}).get("review_digest"), item["id"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(r"[0-9a-f]{24}", ident):
            raise ValueError("VM job has an invalid approval receipt; recover history before changing jobs")
        if digest in receipts and receipts[digest] != ident:
            raise ValueError("VM approval belongs to another job; recover history before changing jobs")
        if digest not in receipts:
            receipts[digest] = ident
            changed = True
    if changed or not initialized:
        SHARED.write_json(os.path.join(directory, STORE), {"version": 1, "receipts": receipts}, durable=True, separators=(",", ":"))
    if not initialized:
        SHARED.write_json(os.path.join(directory, MARKER), {"version": 1}, durable=True)

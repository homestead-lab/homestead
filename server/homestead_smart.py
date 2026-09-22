"""SMART inventory and self-test orchestration for the optional node probe.

The ordinary node-probe container remains unprivileged.  A separate, opt-in
sidecar owns raw-device access and accepts signed test requests only from
Homestead.  This module keeps device validation and Kubernetes pod discovery in
one place so the browser never receives a helper credential.
"""
import hashlib
import homestead_names as NAMES
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request


kget = None
signing_key = None
DEFAULT_NAMESPACE = "lab"


def bind(_kget, namespace, _signing_key):
    global kget, signing_key, DEFAULT_NAMESPACE
    kget, signing_key, DEFAULT_NAMESPACE = _kget, _signing_key, namespace
    NAMES.bind(_kget)


def _name(value, label):
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError(f"{label} is invalid")
    return value


def _probe(node):
    node = _name(node, "node")
    pods = NAMES.nodeprobe_pods(DEFAULT_NAMESPACE)
    pod = next((item for item in pods
                if item.get("spec", {}).get("nodeName") == node
                and item.get("status", {}).get("phase") == "Running"
                and item.get("status", {}).get("podIP")), None)
    if not pod:
        raise ValueError(f"the SMART helper is not running on {node}")
    statuses = {row.get("name"): row for row in
                pod.get("status", {}).get("containerStatuses", []) or []}
    smart = statuses.get("smart") or {}
    if not smart.get("ready"):
        waiting = ((smart.get("state") or {}).get("waiting") or {})
        reason = waiting.get("message") or waiting.get("reason") or "not ready"
        raise ValueError(f"the SMART helper on {node} is {reason}")
    return pod["status"]["podIP"]


def _request(node, disk=None, body=None):
    ip = _probe(node)
    query = "?" + urllib.parse.urlencode({"disk": disk}) if disk else ""
    url = f"http://{ip}:9100/{query}"
    raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if raw is not None:
        stamp = str(int(time.time()))
        key = signing_key()
        if isinstance(key, str):
            key = key.encode()
        headers.update({
            "Content-Type": "application/json",
            "X-Homestead-Time": stamp,
            "X-Homestead-Signature": hmac.new(
                key, stamp.encode() + b"." + raw, hashlib.sha256).hexdigest(),
        })
    request = urllib.request.Request(url, data=raw,
                                     method="POST" if raw is not None else "GET",
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45 if raw is not None else 20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode()).get("error")
        except Exception:
            detail = None
        raise ValueError(detail or f"SMART helper returned HTTP {error.code}")


def inventory(node):
    result = _request(node)
    if not isinstance(result, dict) or not isinstance(result.get("disks"), list):
        raise ValueError("the SMART helper returned an invalid inventory")
    return result


def disk(node, name):
    name = _name(name, "disk")
    result = _request(node, name)
    row = result.get("disk") if isinstance(result, dict) else None
    if not isinstance(row, dict) or row.get("name") != name:
        raise ValueError(f"disk {name} was not found on {node}")
    return row


def start_test(node, name, test_type):
    node, name = _name(node, "node"), _name(name, "disk")
    test_type = str(test_type or "").lower()
    if test_type not in ("short", "long"):
        raise ValueError("SMART test type must be short or long")
    current = disk(node, name)
    if not current.get("available"):
        raise ValueError(current.get("unavailable_reason") or "SMART is unavailable for this disk")
    if test_type not in (current.get("supported_tests") or []):
        raise ValueError(f"this disk does not advertise a {test_type} SMART self-test")
    if (current.get("test") or {}).get("active"):
        raise ValueError("a SMART self-test is already running on this disk")
    result = _request(node, body={"node": node, "disk": name, "test": test_type})
    expected = max(30, int(result.get("expected_seconds", 0) or 0))
    return {
        "ok": True, "node": node, "disk": name, "test": test_type,
        "expected_seconds": expected,
        "baseline": str(result.get("baseline") or ""),
        "started_epoch": int(result.get("started_epoch") or time.time()),
        "message": str(result.get("message") or f"{test_type.title()} SMART test started"),
    }


def progress(ref):
    report = disk(ref["node"], ref["disk"])
    test = report.get("test") or {}
    elapsed = max(0, int(time.time()) - int(ref.get("started_epoch", time.time())))
    expected = max(30, int(ref.get("expected_seconds", 30) or 30))
    if test.get("active"):
        remaining = test.get("remaining_percent")
        pct = (max(5, min(95, 100 - int(remaining))) if remaining is not None
               else max(5, min(90, round(elapsed / expected * 90))))
        return "running", pct, test.get("status") or f"SMART {ref['test']} test running"

    latest = (report.get("self_tests") or [{}])[0]
    signature = str(latest.get("signature") or "")
    changed = bool(signature and signature != str(ref.get("baseline") or ""))
    status = str(latest.get("status") or "").lower()
    if changed:
        passed = any(phrase in status for phrase in
                     ("completed without error", "completed successfully", "passed"))
        failed = not passed and any(word in status for word in
                                    ("fail", "error", "aborted", "interrupted", "unknown"))
        return ("failed" if failed else "succeeded"), 100, (
            latest.get("status") or f"SMART {ref['test']} test complete")
    if elapsed <= min(20, max(5, expected // 10)):
        return "running", max(2, min(10, round(elapsed / expected * 90))), \
            "Waiting for the drive to report the self-test"
    if elapsed > expected + 600:
        return "failed", 100, "The drive did not report a SMART self-test result"
    if elapsed >= expected:
        return "succeeded", 100, \
            "SMART self-test command completed; this device exposes no new result log entry"
    return "running", max(10, min(95, round(elapsed / expected * 90))), \
        f"SMART {ref['test']} test running"

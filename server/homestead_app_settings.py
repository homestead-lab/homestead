"""Homestead's cluster-wide settings: alert thresholds, the update policy,
SMART limits, the site's name and the App Store's catalogue. What they are
and what each may be; server.py keeps reading and saving the ConfigMap they
live in, because every part of it reads them.
"""
import json
import re
import urllib.parse

# server.py's reader and writer, bound at start (bind below).
_bound = {"payload": None, "save": None}


def bind(payload, save):
    """The settings as the Settings page shows them, and how they are saved."""
    _bound.update(payload=payload, save=save)


DEFAULTS = {
    "thresholds": {
        "cpu": {"warning": 70, "critical": 88},
        "memory": {"warning": 70, "critical": 88},
        "disk": {"warning": 75, "critical": 90},
        "temperature": {"warning": 70, "critical": 85},
    },
    "updates": {
        "channel": "prod",
        "policy": "approval_required",
        "notify_available": True,
        "notify_failures": True,
        "maintenance": {"days": [0, 1, 2, 3, 4, 5, 6],
                        "start": "02:00", "duration_minutes": 120},
    },
    "smart": {
        "temperature": {"warning": 55, "critical": 65},
        "reallocated_warning": 1,
        "pending_critical": 1,
        "uncorrectable_critical": 1,
        "notify_failures": True,
    },
    # What to call this installation, shown under the Homestead wordmark. Blank
    # means nothing is shown: better than a word that describes nobody's setup.
    "site_name": "",
    # Where the App Store reads its catalogue: any feed in the Community
    # Applications format. Blank means the public Community Applications feed.
    "catalog_url": "",
    # Rebuild missing copies of detached volumes. Longhorn's own setting where
    # it has one; this drives Homestead's stand-in where it has not.
    "longhorn": {"offline_rebuilding": True},
    # Monthly restore tests of every app with backups (homestead_restore_test).
    "restore_tests": {"enabled": False},
}


def validate(value):
    """Validate and normalize cluster-wide UI and workload-update policy."""
    incoming = (value or {}).get("thresholds") or {}
    out = json.loads(json.dumps(DEFAULTS))
    for metric, defaults in out["thresholds"].items():
        supplied = incoming.get(metric) or {}
        warning = int(supplied.get("warning", defaults["warning"]))
        critical = int(supplied.get("critical", defaults["critical"]))
        upper = 120 if metric == "temperature" else 100
        if warning < 1 or critical > upper or warning >= critical:
            unit = "°C" if metric == "temperature" else "%"
            raise ValueError(f"{metric} thresholds must be ordered between 1 and {upper}{unit}")
        out["thresholds"][metric] = {"warning": warning, "critical": critical}
    smart_in = (value or {}).get("smart") or {}
    smart_temp = smart_in.get("temperature") or out["smart"]["temperature"]
    temp_warning = int(smart_temp.get("warning", out["smart"]["temperature"]["warning"]))
    temp_critical = int(smart_temp.get("critical", out["smart"]["temperature"]["critical"]))
    if temp_warning < 1 or temp_critical > 120 or temp_warning >= temp_critical:
        raise ValueError("drive temperature thresholds must be ordered between 1 and 120°C")
    out["smart"]["temperature"] = {"warning": temp_warning, "critical": temp_critical}
    for key in ("reallocated_warning", "pending_critical", "uncorrectable_critical"):
        count = int(smart_in.get(key, out["smart"][key]))
        if count < 1 or count > 1_000_000:
            raise ValueError(f"{key} must be between 1 and 1000000")
        out["smart"][key] = count
    notify = smart_in.get("notify_failures", out["smart"]["notify_failures"])
    if not isinstance(notify, bool):
        raise ValueError("SMART notify_failures must be true or false")
    out["smart"]["notify_failures"] = notify
    update_in = (value or {}).get("updates") or {}
    channel = update_in.get("channel", out["updates"]["channel"])
    if channel not in ("prod", "dev"):
        raise ValueError("update channel must be prod or dev")
    out["updates"]["channel"] = channel
    policy = str(update_in.get("policy", out["updates"]["policy"]))
    if policy not in ("notify_only", "approval_required", "maintenance_window"):
        raise ValueError("update policy must be notify_only, approval_required, or maintenance_window")
    out["updates"]["policy"] = policy
    for key in ("notify_available", "notify_failures"):
        supplied = update_in.get(key, out["updates"][key])
        if not isinstance(supplied, bool):
            raise ValueError(f"{key} must be true or false")
        out["updates"][key] = supplied
    maintenance = update_in.get("maintenance") or {}
    start = str(maintenance.get("start", out["updates"]["maintenance"]["start"]))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start):
        raise ValueError("maintenance start must use 24-hour HH:MM UTC")
    try:
        duration = int(maintenance.get("duration_minutes",
                                       out["updates"]["maintenance"]["duration_minutes"]))
        days = sorted(set(int(day) for day in maintenance.get(
            "days", out["updates"]["maintenance"]["days"])))
    except (TypeError, ValueError):
        raise ValueError("maintenance days and duration are invalid")
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("maintenance days must contain values from 0 (Monday) to 6 (Sunday)")
    if duration < 15 or duration > 1440:
        raise ValueError("maintenance duration must be between 15 and 1440 minutes")
    out["updates"]["maintenance"] = {
        "days": days, "start": start, "duration_minutes": duration}
    rebuild = ((value or {}).get("longhorn") or {}).get("offline_rebuilding", True)
    if not isinstance(rebuild, bool):
        raise ValueError("offline rebuilding must be true or false")
    out["longhorn"] = {"offline_rebuilding": rebuild}
    tests = ((value or {}).get("restore_tests") or {}).get("enabled", False)
    if not isinstance(tests, bool):
        raise ValueError("restore tests must be on or off")
    out["restore_tests"] = {"enabled": tests}
    site = str((value or {}).get("site_name", out["site_name"]) or "").strip()
    if len(site) > 40:
        raise ValueError("site name must be 40 characters or fewer")
    out["site_name"] = site
    url = str((value or {}).get("catalog_url", out["catalog_url"]) or "").strip()
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or len(url) > 500:
            raise ValueError("the catalogue address must be a plain http:// or https:// URL")
    out["catalog_url"] = url
    return out


ROUTES = {
    ("GET", "/api/settings"): ("viewer", lambda request: _bound["payload"]()),
    ("POST", "/api/settings"): ("admin", lambda request: {"ok": True, **_bound["save"](request.body)}),
}

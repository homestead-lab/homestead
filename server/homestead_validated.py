"""Which versions of risky updates Homestead has validated (issue #1348,
docs/plans/resilience-and-validated-updates.md, section 5).

A risky update is a kernel and the host packages that change kernel or
confinement behaviour, or a platform part: k3s, RKE2, Longhorn, KubeVirt, CDI.
Homestead's CI runs each new version through its own tests and records the
verdict in a list, validated-versions.json:

- validated: it passed the tests;
- bad: it breaks something, with the reason, what it affects (`affects`) and,
  when there is one, a workaround;
- untested: seen, not yet run. A version the list does not name is untested,
  never validated, as in homestead_upgrade_known.

Host entries are kept per distribution, release and architecture
("ubuntu-26.04-amd64"), platform entries per part. The first validated
distribution is Ubuntu 26.04; on any other, host versions read as untested.

The list is published, signed, as an asset of a standing GitHub release
(validated-versions), apart from Homestead's own releases, so a newly
validated kernel needs no Homestead release. Homestead fetches it twice a day,
checks its Ed25519 signature (homestead_ed25519) against the public key
below, and keeps the newest good copy on its data volume. Offline, or before
the first fetch, it uses the copy built into its image
(homestead_validated_bundled), which needs no signature: it came with the
image. A list older than STALE_DAYS says so.

The signature covers the document without its "signature" field, as compact
JSON with sorted keys (canonical()). scripts/validated_versions.py signs.
"""
import base64
import calendar
import json
import os
import re
import time

import homestead_ed25519 as ED25519
import homestead_fetch as FETCH
import homestead_shared as SHARED
import homestead_validated_bundled as BUNDLED

URL = "https://github.com/homestead-lab/homestead/releases/download/validated-versions/validated-versions.json"
# The list's public key, hex. Empty until the key is made: Homestead then
# uses the built-in list only and fetches nothing (a list it cannot check
# is no better than none).
PUBLIC_KEY = ""
EVERY = 12 * 3600
STALE_DAYS = 30
MAX_BYTES = 1 << 20
DEADLINE = 60
STATUSES = ("validated", "bad", "untested")
PLATFORM_PARTS = ("k3s", "rke2", "longhorn", "kubevirt", "cdi")
HOST_KEY = re.compile(r"[a-z]+-[0-9][0-9.]*-(amd64|arm64)")
DATA_DIR = None     # None: homestead_shared's
fetch = None        # a test's fake for FETCH.get_text


def _dir():
    return DATA_DIR or SHARED.DIR


def _cache_path():
    return os.path.join(_dir(), "validated-versions.json")


def _state_path():
    return os.path.join(_dir(), "validated-versions-state.json")


def canonical(document):
    """The bytes the signature covers."""
    body = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def check(document):
    """The list's shape, raising ValueError for what is wrong. Returns it."""
    if not isinstance(document, dict):
        raise ValueError("The list is not a JSON object.")
    _when(document.get("generated"))
    for section in ("host", "platform", "images"):
        if not isinstance(document.get(section, {}), dict):
            raise ValueError(f"The list's {section} section is not an object.")
    for key, entries in (document.get("host") or {}).items():
        if not HOST_KEY.fullmatch(key):
            raise ValueError(f"{key} is not a distribution, release and architecture, like ubuntu-26.04-amd64.")
        _entries(entries, key, ("package", "version"))
    for part, entries in (document.get("platform") or {}).items():
        if part not in PLATFORM_PARTS:
            raise ValueError(f"{part} is not a platform part Homestead validates.")
        _entries(entries, part, ("version",))
    for image, entries in (document.get("images") or {}).items():
        _entries(entries, image, ("version",))
    return document


def _entries(entries, where, required):
    if not isinstance(entries, list):
        raise ValueError(f"The list's entries for {where} are not a list.")
    for entry in entries:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(field), str) or not entry.get(field)
                                              for field in required):
            raise ValueError(f"An entry for {where} has no {' or '.join(required)}.")
        if entry.get("status") not in STATUSES:
            raise ValueError(f"An entry for {where} has status {entry.get('status')!r}; it is one of {', '.join(STATUSES)}.")
        if not isinstance(entry.get("affects", []), list):
            raise ValueError(f"An entry for {where} lists what it affects as something other than a list.")


def _when(stamp):
    try:
        return calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        raise ValueError("The list has no generated time like 2026-11-18T00:00:00Z.") from None


def verify(document, public_key=None):
    """Whether the list's signature is the key's (hex)."""
    key = PUBLIC_KEY if public_key is None else public_key
    if not key:
        return False
    try:
        signature = base64.b64decode(str(document.get("signature") or ""), validate=True)
        return ED25519.verify(bytes.fromhex(key), canonical(document), signature)
    except (ValueError, TypeError):
        return False


def _load_state():
    try:
        return SHARED.read_json(_state_path(), {})
    except SHARED.Unreadable:
        return {}


def _cached():
    """The downloaded copy, if it is there, well formed and still verifies."""
    try:
        document = SHARED.read_json(_cache_path(), {})
    except SHARED.Unreadable:
        return None
    if not document:
        return None
    try:
        check(document)
    except ValueError:
        return None
    return document if verify(document) else None


def current():
    """(the list in use, where it came from: "downloaded" or "built-in").
    The newer of the two by its generated time."""
    built_in = check(BUNDLED.LIST)
    downloaded = _cached()
    if downloaded and _when(downloaded["generated"]) > _when(built_in["generated"]):
        return downloaded, "downloaded"
    return built_in, "built-in"


def tick(now=None):
    """On the leader, from the OS updates loop: fetch the list when it is
    EVERY seconds since the last try. Returns a line for the log when
    something changed, else ""."""
    now = time.time() if now is None else now
    if not PUBLIC_KEY:
        return ""
    state = _load_state()
    if now - float(state.get("tried") or 0) < EVERY:
        return ""
    state["tried"] = now
    try:
        text = (fetch or FETCH.get_text)(URL, limit=MAX_BYTES, seconds=DEADLINE, timeout=20,
                                         headers={"User-Agent": "Homestead"}, what="the validated-versions list")
        document = check(json.loads(text))
        if not verify(document):
            raise ValueError("Its signature does not match Homestead's key, so it was not used.")
        state.update(fetched=now, error="")
        have, _ = current()
        newer = _when(document["generated"]) > _when(have["generated"])
        if newer:
            SHARED.write_json(_cache_path(), document, indent=1, sort_keys=True)
        SHARED.write_json(_state_path(), state, indent=1, sort_keys=True)
        return f"validated versions: list of {document['generated']} downloaded" if newer else ""
    except Exception as error:
        state["error"] = str(error)[:300] or type(error).__name__
        SHARED.write_json(_state_path(), state, indent=1, sort_keys=True)
        return f"validated versions: the list could not be downloaded: {state['error']}"


def summary(now=None):
    """What Settings shows about the list."""
    now = time.time() if now is None else now
    document, source = current()
    state = _load_state()
    age = now - _when(document["generated"])
    return {"generated": document["generated"], "source": source, "stale": age > STALE_DAYS * 86400,
            "age_days": int(age // 86400), "signed": bool(PUBLIC_KEY), "fetched": state.get("fetched"),
            "error": state.get("error", ""), "first_tier": BUNDLED.FIRST_TIER}


def status(kind, key, version, package=None):
    """A version's status and its entry: kind "host" (key: ubuntu-26.04-amd64,
    with package) or "platform" (key: k3s, longhorn...), or "images". Untested
    when the list does not name it, and for a host on a distribution outside
    the validated tier."""
    document, _ = current()
    entries = (document.get(kind) or {}).get(key) or []
    for entry in entries:
        if entry.get("version") == version and (package is None or entry.get("package") == package):
            return {"status": entry["status"], **entry}
    return {"status": "untested", "version": version, **({"package": package} if package else {})}


def validated_tier(key):
    """Whether host versions are validated on this distribution at all."""
    return key in BUNDLED.FIRST_TIER


ROUTES = {
    ("GET", "/api/updates/validated"): ("viewer", lambda request: summary()),
}

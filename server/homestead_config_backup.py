"""Homestead's own configuration, backed up to a file and restored from one.

A backup is the ConfigMaps and Secrets that hold what people set up in
Homestead - its settings, users, VIPs, IP addresses, shares and the rest -
grouped into parts, so a restore can bring back some parts and leave others.
It is not the workloads or their data: those have volume backups.

The file carries password hashes and keys, so it is always encrypted with a
passphrase the person chooses and Homestead does not keep. The standard
library has no AES, so the file is sealed from standard primitives: scrypt
turns the passphrase into two keys, SHAKE-256 (an extendable-output function)
gives the keystream the contents are XORed with, under a fresh random nonce,
and HMAC-SHA256 over the header, nonce and ciphertext is checked before
anything is decrypted - so a wrong passphrase and a changed file are the same
refusal. Linked clusters are left out: restoring an old shared key would cut
the links.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import zlib

import homestead_api_errors as API_ERRORS
import homestead_kube as KUBE

kget = ksend = None
VERSION = ""
FORMAT = "homestead-config-backup"
FILE_VERSION = 1
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 8
MAX_BACKUP_BYTES = 8 * 1024 * 1024
_site = lambda: ""
_after_restore = lambda parts: None

# Each part: what it is, and the objects that hold it. Filled in by bind(),
# which knows each module's names and namespaces.
PARTS = []


def bind(_kget, _ksend, version, parts, site=None, after_restore=None):
    """parts: [{"id", "label", "detail", "objects": [(kind, namespace, name)],
    "caution": text shown when chosen for a restore, "default": bool}]."""
    global kget, ksend, VERSION, PARTS, _site, _after_restore
    kget, ksend, VERSION, PARTS = _kget, _ksend, version, list(parts)
    _site = site or (lambda: "")
    _after_restore = after_restore or (lambda parts: None)


def _part(part_id):
    part = next((p for p in PARTS if p["id"] == part_id), None)
    if not part:
        raise ValueError(f"no configuration part called {part_id}")
    return part


def _read(kind, namespace, name):
    found = KUBE.get_optional(f"/api/v1/namespaces/{namespace}/{kind}/{name}", read=kget)
    if found is None:
        return None
    return {"data": found.get("data") or {}, "binaryData": found.get("binaryData") or {}}


def parts():
    """The parts, and whether this Homestead has anything in each."""
    out = []
    for p in PARTS:
        present = 0
        for kind, namespace, name in p["objects"]:
            try:
                present += 1 if _read(kind, namespace, name) else 0
            except Exception:
                pass
        out.append({"id": p["id"], "label": p["label"], "detail": p.get("detail", ""),
                    "caution": p.get("caution", ""), "default": p.get("default", True), "present": present > 0})
    return out


# ------------------------------------------------------------------ sealing
def _keys(passphrase, salt, params):
    material = hashlib.scrypt(passphrase.encode(), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                              dklen=64, maxmem=128 * params["r"] * params["n"] * 2)
    return material[:32], material[32:]


def _stream(key, nonce, length):
    return hashlib.shake_256(b"homestead-config-backup/1" + key + nonce).digest(length)


def _header(doc):
    """What the tag covers besides the ciphertext: everything a reader is shown."""
    fields = {k: doc[k] for k in ("format", "version", "homestead", "site", "created", "parts", "kdf", "cipher")}
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def seal(payload, passphrase, meta):
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise ValueError(f"the passphrase needs at least {MIN_PASSPHRASE} characters")
    salt, nonce = os.urandom(16), os.urandom(16)
    enc_key, mac_key = _keys(passphrase, salt, SCRYPT)
    plain = zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 9)
    cipher = bytes(a ^ b for a, b in zip(plain, _stream(enc_key, nonce, len(plain))))
    doc = {"format": FORMAT, "version": FILE_VERSION, **meta,
           "kdf": {"name": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode()},
           "cipher": "shake256-xor+hmac-sha256",
           "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(cipher).decode()}
    doc["tag"] = base64.b64encode(hmac.new(mac_key, _header(doc) + nonce + cipher, hashlib.sha256).digest()).decode()
    return doc


def unseal(doc, passphrase):
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("that is not a Homestead configuration backup")
    if int(doc.get("version") or 0) > FILE_VERSION:
        raise ValueError("That backup was made by a newer Homestead. Update this one first.")
    try:
        kdf = doc["kdf"]
        params = {"n": int(kdf["n"]), "r": int(kdf["r"]), "p": int(kdf["p"])}
        if (kdf.get("name") != "scrypt" or params != SCRYPT or
                len(doc["data"]) > (MAX_BACKUP_BYTES * 4 // 3 + 8)):
            raise ValueError
        salt, nonce = base64.b64decode(kdf["salt"]), base64.b64decode(doc["nonce"])
        cipher, tag = base64.b64decode(doc["data"]), base64.b64decode(doc["tag"])
        if len(salt) != 16 or len(nonce) != 16 or len(tag) != 32 or len(cipher) > MAX_BACKUP_BYTES:
            raise ValueError
        enc_key, mac_key = _keys(passphrase or "", salt, params)
        expected = hmac.new(mac_key, _header(doc) + nonce + cipher, hashlib.sha256).digest()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("that backup file is damaged") from error
    if not hmac.compare_digest(expected, tag):
        raise PermissionError("wrong passphrase, or the file was changed since it was made")
    plain = bytes(a ^ b for a, b in zip(cipher, _stream(enc_key, nonce, len(cipher))))
    decoder = zlib.decompressobj()
    expanded = decoder.decompress(plain, MAX_BACKUP_BYTES + 1)
    if len(expanded) > MAX_BACKUP_BYTES or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
        raise ValueError("backup contents exceed the size limit or are damaged")
    return json.loads(expanded.decode())


# ------------------------------------------------------------------ backup
def backup(part_ids, passphrase):
    """The chosen parts as a sealed file (a dict, to be saved as JSON)."""
    chosen = [_part(i) for i in (part_ids or [p["id"] for p in PARTS])]
    payload = {}
    for p in chosen:
        payload[p["id"]] = [{"kind": kind, "name": name, "namespace": namespace,
                             "object": _read(kind, namespace, name)} for kind, namespace, name in p["objects"]]
    meta = {"homestead": VERSION, "site": str(_site() or ""), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parts": [{"id": p["id"], "label": p["label"]} for p in chosen]}
    return seal(payload, passphrase, meta)


# ------------------------------------------------------------------ restore
def inspect(doc, passphrase):
    """What a backup holds, part by part, against what is here now."""
    payload = unseal(doc, passphrase)
    rows = []
    for entry in doc.get("parts", []):
        part = next((p for p in PARTS if p["id"] == entry["id"]), None)
        saved = payload.get(entry["id"], [])
        if not part:
            rows.append({"id": entry["id"], "label": entry.get("label", entry["id"]), "state": "unknown",
                         "detail": "this Homestead does not know this part", "restorable": False})
            continue
        held = [o for o in saved if o.get("object")]
        current = [_read(kind, ns, name) for kind, ns, name in part["objects"]]
        now = {name: obj for (kind, ns, name), obj in zip(part["objects"], current)}
        same = all((now.get(o["name"]) or {}).get("data") == o["object"].get("data") for o in held)
        state = "empty" if not held else "same" if same else "differs"
        rows.append({"id": part["id"], "label": part["label"], "detail": part.get("detail", ""),
                     "caution": part.get("caution", ""), "default": part.get("default", True),
                     "state": state, "restorable": bool(held)})
    return {"homestead": doc.get("homestead", ""), "site": doc.get("site", ""), "created": doc.get("created", ""),
            "parts": rows}


def _write(kind, namespace, name, saved):
    path = f"/api/v1/namespaces/{namespace}/{kind}/{name}"
    body = {"apiVersion": "v1", "kind": "ConfigMap" if kind == "configmaps" else "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "data": saved.get("data") or {}}
    if saved.get("binaryData"):
        body["binaryData"] = saved["binaryData"]
    if kind == "secrets":
        body["type"] = "Opaque"
    try:
        current = kget(path)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return ksend("POST", f"/api/v1/namespaces/{namespace}/{kind}", body)
    meta = current.get("metadata") or {}
    body["metadata"].update({k: meta[k] for k in ("resourceVersion", "labels", "annotations") if meta.get(k)})
    if kind == "secrets" and current.get("type"):
        body["type"] = current["type"]
    return ksend("PUT", path, body)


def _error_text(error):
    """The full error, as the cluster gave it."""
    if isinstance(error, urllib.error.HTTPError):
        return API_ERRORS.message(error)
    return str(error) or type(error).__name__


def restore(doc, passphrase, part_ids):
    """Put the chosen parts back as the backup had them. Nothing else changes:
    a part not chosen, or an object the backup did not hold, is left as it is.

    A write that fails stops the restore there: the parts are written one
    after another and some depend on others (users with roles, IP addresses
    with virtual IPs). The answer lists every chosen part as restored,
    skipped, failed or not started, with ok false, so the page can show what
    changed. The caches are cleared for every part written, the failed one
    too, since it may be partly written."""
    if not part_ids:
        raise ValueError("choose at least one part to restore")
    payload = unseal(doc, passphrase)
    chosen = [_part(i) for i in part_ids]
    done, skipped, rows, failed = [], [], [], None
    for part in chosen:
        row = {"id": part["id"], "label": part["label"]}
        rows.append(row)
        if failed:
            row.update(state="not_started", detail="Not started, because an earlier part failed.")
            continue
        saved = {o["name"]: o for o in payload.get(part["id"], []) if o.get("object")}
        if not saved:
            skipped.append(part["label"])
            row.update(state="skipped", detail="The backup holds nothing for this part.")
            continue
        current = ""
        try:
            for kind, namespace, name in part["objects"]:
                if name in saved:
                    current = name
                    _write(kind, namespace, name, saved[name]["object"])
        except Exception as error:
            failed = part
            row.update(state="failed", detail=(
                "Writing this part failed, so it may be partly restored. "
                f"Restore it again from the same backup. Error from {current}: {_error_text(error)}"))
            continue
        done.append(part["id"])
        row.update(state="restored", detail="")
    after_error = ""
    try:
        _after_restore(done + ([failed["id"]] if failed else []))
    except Exception as error:
        after_error = (" Homestead could not reload the restored parts, so some pages may show the old values."
                       f" Restart Homestead to read them afresh. Error: {_error_text(error)}")
    labels = lambda ids: ", ".join(_part(i)["label"] for i in ids)
    if failed:
        detail = f"The restore stopped at {failed['label']}, which may be partly restored."
        if done:
            detail += f" Restored before it: {labels(done)}."
        waiting = [r["label"] for r in rows if r["state"] == "not_started"]
        if waiting:
            detail += f" Not started: {', '.join(waiting)}."
    else:
        detail = ((f"restored {labels(done)}" if done else "nothing to restore")
                  + (f"; the backup held nothing for {', '.join(skipped)}" if skipped else ""))
    if after_error and not detail.endswith("."):
        detail += "."
    return {"ok": not failed and not after_error, "restored": done, "skipped": skipped, "parts": rows,
            "detail": detail + after_error}


# Its routes and who may use them (homestead_routes.py).
ROUTES = {
    ("GET", "/api/config/parts"): ("admin", lambda request: parts()),
    ("POST", "/api/config/inspect"): ("admin", lambda request: inspect(request.body.get("file"), str(request.body.get("passphrase") or ""))),
}

"""A fake Kubernetes API server for the tests (#619).

    with Kube(pod, claim, absent_groups=("snapshot.storage.k8s.io",)) as kube:
        MODULE.bind(K.kget, K.ksend)   # or anything that already calls them
        ...
        kube.object(path), kube.writes, kube.discard(path)

It holds objects by API path and answers the way the API server does: 404,
409 AlreadyExists and Conflict, 422 for a JSON patch that does not apply,
merge, JSON and strategic-merge patches, delete preconditions, finalizers,
owner garbage collection, label and field selectors, limit/continue pages,
a Secret's stringData kept as data, and /apis discovery. It replaces
homestead_kube.transport, so a read goes
through kget (and its 429 retry) and a write goes round WRITE_GUARDS first,
as in the app. While it is in use the helpers server.py binds into
homestead_kube (the self-data fence and the cluster shutdown state) are
stand-ins that let every write through; the rest of the guards are real.

A test that patches kget or ksend itself goes round the fake, as before.

Not modelled (add it when a test needs it): watches, server-side apply,
admission and validation, namespace existence and termination, the
$setElementOrder / $retainKeys strategic-merge directives, and subresources
other than status and pod eviction (answer those with react())."""
import base64
import contextlib
import copy
import io
import itertools
import json
import re
import threading
import time
import urllib.error
import urllib.parse
from unittest import mock

import homestead_kube as K

# Strategic merge: lists merged by a key instead of replaced. "ports" lists
# merge by containerPort in a pod and by port in a Service.
MERGE_KEYS = {"containers": "name", "initContainers": "name", "ephemeralContainers": "name",
              "volumes": "name", "env": "name", "imagePullSecrets": "name", "volumeMounts": "mountPath",
              "volumeDevices": "devicePath", "ownerReferences": "uid", "conditions": "type",
              "ports": ("containerPort", "port")}
MERGED_PRIMITIVE_LISTS = {"finalizers"}
LIST_KINDS = {"endpoints": "Endpoints"}
# Resources whose name in the path is not the kind made plural.
PLURALS = {"endpoints": "endpoints", "networkattachmentdefinition": "network-attachment-definitions"}


def plural(kind):
    kind = kind.lower()
    if kind in PLURALS:
        return PLURALS[kind]
    if kind.endswith(("s", "x", "ch", "sh")):
        return kind + "es"
    if kind.endswith("y") and kind[-2:-1] not in "aeiou":
        return kind[:-1] + "ies"
    return kind + "s"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Kube:
    def __init__(self, *objects, groups=(), absent_groups=()):
        self._objects = {}  # (group, namespace, plural, name) -> object
        self._lock = threading.RLock()
        self._serial = itertools.count(1)
        self._rv = 100
        self.groups = set(groups)
        self.absent_groups = set(absent_groups)
        self.writes = []  # (method, path, body, ctype) of every write that reached the fake
        self._failures = []  # [path, method, code, times left]
        self._reactions = []
        self._stack = None
        for obj in objects:
            self.add(obj)

    # Using it ---------------------------------------------------------------

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(mock.patch.object(K, "transport", self))
        self._stack.enter_context(mock.patch.object(K, "self_data_activity", contextlib.nullcontext, create=True))
        self._stack.enter_context(mock.patch.object(K, "require_self_data_write", lambda: None, create=True))
        self._stack.enter_context(mock.patch.object(
            K, "cluster_shutdown", lambda: type("NoShutdown", (), {"state": staticmethod(lambda: None)}), create=True))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    def add(self, obj):
        """Put an object in as it is, filling in what the API server would."""
        with self._lock:
            obj = copy.deepcopy(obj)
            group, _ = self._group_version(obj)
            meta = obj.setdefault("metadata", {})
            self._stamp(meta, keep=True)
            if group:
                self.groups.add(group)
            self._objects[(group, meta.get("namespace") or "", plural(obj["kind"]), meta["name"])] = obj
            return self.path_of(obj)

    def path_of(self, obj):
        group, version = self._group_version(obj)
        meta = obj.get("metadata") or {}
        base = f"/apis/{group}/{version}" if group else f"/api/{version}"
        ns = f"/namespaces/{meta['namespace']}" if meta.get("namespace") else ""
        return f"{base}{ns}/{plural(obj['kind'])}/{meta['name']}"

    def object(self, path):
        """A copy of the object at path, or None."""
        with self._lock:
            found = self._objects.get(self._key(self._parse(path)))
            return copy.deepcopy(found)

    def discard(self, path):
        """Take the object at path away as something outside the app would: no
        write is logged, no finalizer or guard is asked, what it owned is
        collected. Not there already is no error."""
        with self._lock:
            if self._objects.pop(self._key(self._parse(path)), None) is not None:
                self._rv += 1
                self._collect()

    def fail(self, path, code, times=1, method=None):
        """Answer the next times requests for path (any method, or just
        method) with code, before anything else happens."""
        self._failures.append([path, method, code, times])

    def react(self, method, path, answer):
        """answer(method, path, body) answers requests for path ("*" for any
        method) in place of the fake; returning None lets the fake answer."""
        self._reactions.append((method, path, answer))

    # The transport ----------------------------------------------------------

    def __call__(self, method, path, body=None, ctype=None, timeout=15):
        path_only, _, query = path.partition("?")
        body = copy.deepcopy(body)
        with self._lock:
            if method != "GET":
                self.writes.append((method, path, copy.deepcopy(body), ctype))
            for failure in self._failures:
                if failure[0] == path_only and failure[1] in (None, method) and failure[3] > 0:
                    failure[3] -= 1
                    raise self._error(failure[2], "Failure", f"injected failure for {path_only}", path)
            for want, where, answer in self._reactions:
                if where == path_only and want in ("*", method):
                    result = answer(method, path, body)
                    if result is not None:
                        return copy.deepcopy(result)
            if path_only == "/apis":
                return self._discovery()
            if path_only == "/api":
                return {"kind": "APIVersions", "versions": ["v1"]}
            req = self._parse(path_only)
            if req is None or req["group"] in self.absent_groups:
                raise self._error(404, "NotFound", "the server could not find the requested resource", path)
            req["query"] = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            handler = {"GET": self._get, "POST": self._post, "PUT": self._put,
                       "PATCH": self._patch, "DELETE": self._delete}[method]
            return copy.deepcopy(handler(req, body, ctype, path))

    # Requests ---------------------------------------------------------------

    def _get(self, req, body, ctype, path):
        if req["name"] is None:
            return self._list(req)
        if req["sub"] not in (None, "status"):
            raise self._error(404, "NotFound", "the server could not find the requested resource", path)
        return self._existing(req, path)

    def _list(self, req):
        rows = [obj for key, obj in sorted(self._objects.items())
                if key[0] == req["group"] and key[2] == req["plural"] and (not req["ns"] or key[1] == req["ns"])]
        rows = [obj for obj in rows if _labels_match(obj, req["query"].get("labelSelector", ""))
                and _fields_match(obj, req["query"].get("fieldSelector", ""))]
        meta = {"resourceVersion": str(self._rv)}
        start = int(req["query"].get("continue") or 0)
        limit = int(req["query"].get("limit") or 0)
        if limit:
            if start + limit < len(rows):
                meta["continue"] = str(start + limit)
            rows = rows[start:start + limit]
        kind = rows[0]["kind"] if rows else LIST_KINDS.get(req["plural"], "")
        version = f"{req['group']}/{req['version']}" if req["group"] else req["version"]
        return {"kind": (kind + "List") if kind else "List", "apiVersion": version, "metadata": meta, "items": rows}

    def _post(self, req, body, ctype, path):
        if req["name"] is not None:
            if req["plural"] == "pods" and req["sub"] == "eviction":
                return self._delete(dict(req, sub=None), None, ctype, path)
            raise self._error(404, "NotFound", "the server could not find the requested resource", path)
        meta = body.setdefault("metadata", {})
        if not meta.get("name") and meta.get("generateName"):
            meta["name"] = meta["generateName"] + f"{next(self._serial):05d}"
        if req["ns"]:
            meta["namespace"] = req["ns"]
        req = dict(req, name=meta.get("name"))
        if self._key(req) in self._objects:
            raise self._error(409, "AlreadyExists", f'{self._what(req)} "{req["name"]}" already exists', path)
        for field in ("uid", "resourceVersion", "creationTimestamp", "deletionTimestamp"):
            meta.pop(field, None)
        self._stamp(meta)
        meta["generation"] = 1
        if req["group"]:
            self.groups.add(req["group"])
        _fold_string_data(req, body)
        self._objects[self._key(req)] = body
        return body

    def _put(self, req, body, ctype, path):
        current = self._existing(req, path)
        sent = (body.get("metadata") or {}).get("resourceVersion")
        if sent and sent != current["metadata"]["resourceVersion"]:
            raise self._conflict(req, path)
        if req["sub"] == "status":
            new = dict(current, status=body.get("status"))
        else:
            new = body
            new.setdefault("metadata", {})
            for field in ("uid", "creationTimestamp", "deletionTimestamp", "generation"):
                if field in current["metadata"]:
                    new["metadata"][field] = current["metadata"][field]
            new["metadata"]["namespace"], new["metadata"]["name"] = current["metadata"].get("namespace"), req["name"]
            new["metadata"] = {k: v for k, v in new["metadata"].items() if v is not None}
        return self._store(req, new)

    def _patch(self, req, body, ctype, path):
        current = self._existing(req, path)
        if ctype == "application/json-patch+json":
            new = _json_patch(current, body, lambda message: self._error(422, "Invalid", message, path))
        else:
            sent = ((body or {}).get("metadata") or {}).get("resourceVersion")
            if sent and sent != current["metadata"]["resourceVersion"]:
                raise self._conflict(req, path)
            if ctype == "application/merge-patch+json":
                new = _merge_patch(current, body)
            elif ctype == "application/strategic-merge-patch+json":
                new = _strategic_merge(current, body)
            else:
                raise self._error(415, "UnsupportedMediaType", f"the body of the request was in an unknown format - accepted media types include: application/json-patch+json, application/merge-patch+json, application/strategic-merge-patch+json; sent {ctype}", path)
        for field in ("uid", "name", "namespace", "creationTimestamp"):
            if field in current["metadata"]:
                new.setdefault("metadata", {})[field] = current["metadata"][field]
        return self._store(req, new)

    def _delete(self, req, body, ctype, path):
        if req["name"] is None:
            listing = self._list(req)
            for row in listing["items"]:
                self._remove(dict(req, name=row["metadata"]["name"]), body or {})
            return {"kind": "Status", "apiVersion": "v1", "status": "Success", "metadata": {}}
        current = self._existing(req, path)
        options = body or {}
        wanted = options.get("preconditions") or {}
        for field in ("uid", "resourceVersion"):
            if wanted.get(field) and wanted[field] != current["metadata"].get(field):
                raise self._error(409, "Conflict", f'Operation cannot be fulfilled on {self._what(req)} "{req["name"]}": '
                                  f'the {"UID" if field == "uid" else "ResourceVersion"} in the precondition '
                                  f'({wanted[field]}) does not match the {"UID" if field == "uid" else "ResourceVersion"} '
                                  f'in record ({current["metadata"].get(field)}). The object might have been modified', path)
        return self._remove(req, options)

    # Bookkeeping ------------------------------------------------------------

    def _store(self, req, new):
        _fold_string_data(req, new)
        meta = new["metadata"]
        if meta.get("deletionTimestamp") and not meta.get("finalizers"):
            self._objects.pop(self._key(req), None)
            self._collect()
            return new
        current = self._objects[self._key(req)]
        if new.get("spec") != current.get("spec"):
            meta["generation"] = int(current["metadata"].get("generation") or 1) + 1
        self._rv += 1
        meta["resourceVersion"] = str(self._rv)
        self._objects[self._key(req)] = new
        return new

    def _remove(self, req, options):
        obj = self._objects[self._key(req)]
        if obj["metadata"].get("finalizers"):
            if not obj["metadata"].get("deletionTimestamp"):
                obj["metadata"]["deletionTimestamp"] = _now()
                self._rv += 1
                obj["metadata"]["resourceVersion"] = str(self._rv)
            return obj
        del self._objects[self._key(req)]
        self._rv += 1
        if options.get("propagationPolicy") != "Orphan" and options.get("orphanDependents") is not True:
            self._collect()
        return obj

    def _collect(self):
        """Garbage collection: what is owned only by objects that are gone goes too."""
        while True:
            uids = {obj["metadata"].get("uid") for obj in self._objects.values()}
            orphans = [key for key, obj in self._objects.items()
                       if (obj["metadata"].get("ownerReferences") or [])
                       and not any(ref.get("uid") in uids for ref in obj["metadata"]["ownerReferences"])
                       and not obj["metadata"].get("deletionTimestamp")]
            if not orphans:
                return
            for key in orphans:
                obj = self._objects[key]
                if obj["metadata"].get("finalizers"):
                    obj["metadata"]["deletionTimestamp"] = _now()
                else:
                    del self._objects[key]

    def _stamp(self, meta, keep=False):
        if not (keep and meta.get("uid")):
            meta["uid"] = f"uid-{next(self._serial)}"
        rv = meta.get("resourceVersion") if keep else None
        if rv and str(rv).isdigit():
            self._rv = max(self._rv, int(rv))
        if not rv:
            self._rv += 1
            meta["resourceVersion"] = str(self._rv)
        if not (keep and meta.get("creationTimestamp")):
            meta["creationTimestamp"] = _now()

    def _existing(self, req, path):
        found = self._objects.get(self._key(req))
        if found is None:
            raise self._error(404, "NotFound", f'{self._what(req)} "{req["name"]}" not found', path)
        return copy.deepcopy(found)

    def _discovery(self):
        groups = sorted(self.groups - self.absent_groups)
        return {"kind": "APIGroupList", "apiVersion": "v1",
                "groups": [{"name": g, "versions": [], "preferredVersion": {}} for g in groups]}

    @staticmethod
    def _group_version(obj):
        group, _, version = obj["apiVersion"].rpartition("/")
        return group, version

    @staticmethod
    def _parse(path):
        parts = path.split("?", 1)[0].strip("/").split("/")
        if parts[:1] == ["api"] and len(parts) >= 3:
            group, version, rest = "", parts[1], parts[2:]
        elif parts[:1] == ["apis"] and len(parts) >= 4:
            group, version, rest = parts[1], parts[2], parts[3:]
        else:
            return None
        ns = ""
        if rest[0] == "namespaces" and len(rest) >= 3 and rest[2] not in ("status", "finalize"):
            ns, rest = rest[1], rest[2:]
        return {"group": group, "version": version, "ns": ns, "plural": rest[0],
                "name": urllib.parse.unquote(rest[1]) if len(rest) > 1 else None,
                "sub": rest[2] if len(rest) > 2 else None}

    @staticmethod
    def _key(req):
        return (req["group"], req["ns"], req["plural"], req["name"]) if req else None

    @staticmethod
    def _what(req):
        return f"{req['plural']}.{req['group']}" if req["group"] else req["plural"]

    def _conflict(self, req, path):
        return self._error(409, "Conflict", f'Operation cannot be fulfilled on {self._what(req)} "{req["name"]}": '
                           "the object has been modified; please apply your changes to the latest version and try again", path)

    @staticmethod
    def _error(code, reason, message, path):
        status = {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
                  "message": message, "reason": reason, "code": code}
        headers = {"Retry-After": "0"} if code == 429 else {}
        return urllib.error.HTTPError(path, code, reason, headers, io.BytesIO(json.dumps(status).encode()))


# Patches ----------------------------------------------------------------------

def _fold_string_data(req, obj):
    """A Secret's stringData is kept as base64 data, as the API server does."""
    if req["plural"] == "secrets" and not req["group"] and obj.get("stringData"):
        data = obj.setdefault("data", {})
        for key, value in obj.pop("stringData").items():
            data[key] = base64.b64encode(str(value).encode()).decode()
    elif req["plural"] == "secrets" and not req["group"]:
        obj.pop("stringData", None)


def _merge_patch(target, patch):
    """RFC 7386: objects merge, null removes, anything else replaces."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _strategic_merge(target, patch, field=None):
    """Kubernetes' strategic merge: a merge patch whose keyed lists (MERGE_KEYS)
    merge item by item, with $patch: delete and $patch: replace."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    patch = dict(patch)
    if patch.pop("$patch", None) == "replace":
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, list) and key in MERGED_PRIMITIVE_LISTS:
            result[key] = list(result.get(key) or []) + [v for v in value if v not in (result.get(key) or [])]
        elif isinstance(value, list) and key in MERGE_KEYS and isinstance(result.get(key), list):
            result[key] = _merge_list(result[key], value, MERGE_KEYS[key])
        else:
            result[key] = _strategic_merge(result.get(key), value)
    return result


def _merge_list(current, patch, keys):
    keys = (keys,) if isinstance(keys, str) else keys
    merged = [copy.deepcopy(item) for item in current]
    for item in patch:
        key = next((k for k in keys if isinstance(item, dict) and k in item), None)
        at = next((i for i, row in enumerate(merged) if key and isinstance(row, dict) and row.get(key) == item[key]), None)
        if isinstance(item, dict) and item.get("$patch") == "delete":
            if at is not None:
                merged.pop(at)
        elif at is None:
            merged.append(copy.deepcopy(item))
        else:
            merged[at] = _strategic_merge(merged[at], item)
    return merged


def _json_patch(target, ops, invalid):
    """RFC 6902 add, remove, replace, move, copy and test; all or nothing."""
    doc = copy.deepcopy(target)

    def walk(pointer):
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
        parent = doc
        for part in parts[:-1]:
            try:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            except (KeyError, IndexError, ValueError, TypeError):
                raise invalid(f"the server rejected our request: path {pointer} does not exist") from None
        return parent, parts[-1]

    def read(pointer):
        parent, last = walk(pointer)
        try:
            return parent[int(last)] if isinstance(parent, list) else parent[last]
        except (KeyError, IndexError, ValueError, TypeError):
            raise invalid(f"the server rejected our request: path {pointer} does not exist") from None

    def remove(pointer):
        value = read(pointer)
        parent, last = walk(pointer)
        parent.pop(int(last) if isinstance(parent, list) else last)
        return value

    def add(pointer, value):
        parent, last = walk(pointer)
        if isinstance(parent, list):
            parent.insert(len(parent) if last == "-" else int(last), value)
        elif isinstance(parent, dict):
            parent[last] = value
        else:
            raise invalid(f"the server rejected our request: path {pointer} does not exist")

    for op in ops or []:
        kind, path = op.get("op"), op.get("path", "")
        if kind == "test":
            if read(path) != op.get("value"):
                raise invalid(f"the server rejected our request: test failed at {path}")
        elif kind == "remove":
            remove(path)
        elif kind == "add":
            add(path, copy.deepcopy(op.get("value")))
        elif kind == "replace":
            read(path)
            parent, last = walk(path)
            parent[int(last) if isinstance(parent, list) else last] = copy.deepcopy(op.get("value"))
        elif kind == "move":
            add(path, remove(op["from"]))
        elif kind == "copy":
            add(path, copy.deepcopy(read(op["from"])))
        else:
            raise invalid(f"the server rejected our request: unknown op {kind}")
    return doc


# Selectors --------------------------------------------------------------------

_SET_TERM = re.compile(r"^\s*([^\s!=]+)\s+(in|notin)\s+\(([^)]*)\)\s*$")


def _terms(selector):
    return [t for t in re.split(r",(?![^()]*\))", selector or "") if t.strip()]


def _labels_match(obj, selector):
    labels = (obj.get("metadata") or {}).get("labels") or {}
    for term in _terms(selector):
        found = _SET_TERM.match(term)
        if found:
            key, op, values = found[1], found[2], {v.strip() for v in found[3].split(",")}
            if (key in labels and labels[key] in values) != (op == "in"):
                return False
            continue
        term = term.strip()
        if term.startswith("!"):
            if term[1:] in labels:
                return False
        elif "!=" in term:
            key, value = term.split("!=", 1)
            if labels.get(key.strip()) == value.strip():
                return False
        elif "=" in term:
            key, value = re.split(r"==?", term, maxsplit=1)
            if labels.get(key.strip()) != value.strip():
                return False
        elif term not in labels:
            return False
    return True


def _fields_match(obj, selector):
    for term in _terms(selector):
        negate = "!=" in term
        key, value = term.split("!=", 1) if negate else re.split(r"==?", term, maxsplit=1)
        found = obj
        for part in key.strip().split("."):
            found = found.get(part) if isinstance(found, dict) else None
        if (str(found if found is not None else "") == value.strip()) == negate:
            return False
    return True

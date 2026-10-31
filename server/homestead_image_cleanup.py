"""Unused container images, cleaned from every host at once.

An update keeps one previous image per app for Roll back; the one before
becomes unused, and nothing removed it but a person on the Image cache page,
one image at a time, or the kubelet once a host's image disk is 85% full. So
old versions piled up on every host an app had run on.

Two ways out, both through the per-image clean-up the Image cache page
already uses (homestead_image_cache.cleanup_image):

* Clean all unused - a reviewed action: every image nothing keeps (not
  running, not a stopped app's or a scheduled job's, not a Roll back copy,
  not Harvester's or Kubernetes' own) from every host that holds it. The
  review returns a token over what it lists; the run plans again and refuses
  if anything changed, so it removes exactly what was reviewed.
* Automatic clean-up - off until an admin turns it on (settings
  image_cleanup.auto). The leader, once an hour, removes images that have
  been unused for image_cleanup.after_days days. "Unused since" is when
  Homestead first saw an image unused, kept on its data volume, since a host
  does not say when an image was last used.

Whether an image is still needed comes from strict reads (#620): a list that
fails stops the plan, so nothing is removed on a guess.
"""
import hashlib
import os
import time

import homestead_leader_state as LEADER_STATE
import homestead_shared as SHARED

STORE = "image-unused-since.json"
AUTO_EVERY = 3600          # seconds between the leader's automatic rounds
_lock = SHARED.SharedLock("image-unused-since")
_auto = {"at": 0.0}


def _imports():
    import homestead_imports as I
    return I


def _images():
    import homestead_image_cache as IMAGES
    return IMAGES


def _path():
    I = _imports()
    return os.path.join(I.SCAN_DIR, STORE) if I.SCAN_DIR else ""


def _load_since():
    path = _path()
    if not path:
        return {}
    return SHARED.read_json(path, {})


def unused(report):
    """The images in an image cache report that clean-up may remove: nothing
    keeps them, they are not the platform's, and they have a digest to
    remove them by."""
    return [image for image in report.get("images") or []
            if image.get("digest") and not image.get("protected") and not image.get("system")
            and image.get("nodes")]


def _held_only_unread(image):
    """Whether only records Homestead could not read keep this image. They
    keep every image, so they say nothing about whether this one is used."""
    rows = image.get("retained_by") or []
    return bool(image.get("protected") and rows and all(row.get("holds_all") for row in rows))


def track(report, now=None):
    """Record when each unused image was first seen unused; forget images
    that are used again or gone. Returns digest -> first-seen time.

    An image kept only because a record cannot be read keeps the date it
    already has (wrap-up row 80): otherwise every clock restarted after the
    record was fixed. Nothing new starts while it is unread, and an image
    something readable keeps is forgotten as before."""
    now = int(time.time() if now is None else now)
    current = {image["digest"] for image in unused(report)}
    unread = {image["digest"] for image in report.get("images") or []
              if image.get("digest") and _held_only_unread(image)}
    path = _path()
    if not path:
        return {digest: now for digest in current}
    with _lock:
        since = _load_since()
        kept = {digest: int(since[digest]) if isinstance(since.get(digest), (int, float)) else now for digest in current}
        kept.update({digest: int(since[digest]) for digest in unread
                     if isinstance(since.get(digest), (int, float))})
        if kept != since:
            SHARED.write_json(path, kept, separators=(",", ":"))
    return {digest: kept[digest] for digest in current}


def token(images):
    """What a review lists, as one value: the same images on the same hosts."""
    parts = sorted(f"{image['digest']}|{','.join(sorted(image['nodes']))}" for image in images)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def plan(older_than_days=None, now=None):
    """Every unused image, with its hosts, size and how long it has been
    unused; older_than_days keeps only those unused at least that long."""
    return _plan(older_than_days, now)[0]


def _plan(older_than_days=None, now=None):
    now = int(time.time() if now is None else now)
    report = _images().image_cache()
    since = track(report, now)
    images = []
    for image in unused(report):
        first = since.get(image["digest"], now)
        if older_than_days is not None and now - first < older_than_days * 86400:
            continue
        images.append({"digest": image["digest"], "name": image["name"], "nodes": image["nodes"],
                       "size_mb": image.get("size_mb") or 0, "unused_since": first})
    images.sort(key=lambda image: -image["size_mb"])
    # Each host holds its own copy: what removing them frees is per host.
    freed = round(sum(image["size_mb"] * len(image["nodes"]) for image in images), 1)
    return {"images": images, "count": len(images), "freed_mb": freed, "token": token(images),
            "complete": bool(report.get("complete")),
            "hosts": sorted({node for image in images for node in image["nodes"]})}, report


def _remove(images, report):
    """Start each image's removal on every host that holds it; an image that
    became needed meanwhile is refused by cleanup_image and skipped."""
    IMAGES = _images()
    removed, skipped = [], []
    for image in images:
        try:
            IMAGES.cleanup_image(image["digest"], image["nodes"], report)
            removed.append(image)
        except (ValueError, PermissionError) as error:
            skipped.append({"name": image["name"], "reason": str(error)[:200]})
    return removed, skipped


def clean_unused(body):
    """Clean all unused, as reviewed: the run plans again, and refuses unless
    it lists the same images on the same hosts."""
    reviewed = str((body or {}).get("token") or "")
    if not reviewed:
        raise ValueError("review the unused images first")
    fresh, report = _plan()
    if fresh["token"] != reviewed:
        raise ValueError("the unused images changed since the review; review again")
    if not fresh["images"]:
        return {"ok": True, "removed": 0, "skipped": [], "freed_mb": 0,
                "detail": "No unused images to clean up"}
    removed, skipped = _remove(fresh["images"], report)
    freed = round(sum(image["size_mb"] * len(image["nodes"]) for image in removed), 1)
    return {"ok": True, "removed": len(removed), "skipped": skipped, "freed_mb": freed,
            "detail": f"Cleaning {len(removed)} unused image{'' if len(removed) == 1 else 's'} "
                      f"from {len({node for image in removed for node in image['nodes']})} hosts"}


def _settings():
    import homestead_settings_helpers as SETTINGS
    return (SETTINGS.get_app_settings() or {}).get("image_cleanup") or {}


def auto_tick(now=None):
    """The leader's hourly round: track what is unused, and when automatic
    clean-up is on, remove images unused for its number of days. Its result
    is shared, so the page shows it whichever copy answers."""
    now = time.time() if now is None else now
    if now - _auto["at"] < AUTO_EVERY:
        return None
    _auto["at"] = now
    settings = _settings()
    if not settings.get("auto"):
        track(_images().image_cache(), now)
        return None
    days = int(settings.get("after_days") or 14)
    due, report = _plan(older_than_days=days, now=now)
    removed, skipped = _remove(due["images"], report) if due["images"] else ([], [])
    result = {"at": int(now), "after_days": days, "removed": len(removed),
              "freed_mb": round(sum(image["size_mb"] * len(image["nodes"]) for image in removed), 1),
              "names": [image["name"] for image in removed][:20], "skipped": skipped[:20]}
    LEADER_STATE.put("image-cleanup", result)
    if removed or skipped:
        print(f"image clean-up: removed {len(removed)} unused for {days}+ days, skipped {len(skipped)}", flush=True)
    return result


def status():
    """The automatic clean-up's setting and its last round, for the page."""
    settings = _settings()
    last, _ = LEADER_STATE.get("image-cleanup")
    return {"auto": bool(settings.get("auto")), "after_days": int(settings.get("after_days") or 14),
            "last": last if isinstance(last, dict) else None, "every_seconds": AUTO_EVERY}


ROUTES = {
    ("POST", "/api/images/cleanup-unused/preview"): ("admin", lambda request: plan()),
    ("POST", "/api/images/cleanup-unused"): ("admin", lambda request: clean_unused(request.body)),
    ("GET", "/api/images/cleanup-auto"): ("viewer", lambda request: status()),
}

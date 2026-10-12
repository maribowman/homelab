"""Ansible filters for the TrueNAS image prune.

``truenas_config_images(config)``: image references found in an app's config (``app.config``),
used for apps that are not running and so have no ``active_workloads``. Collects every string
under an ``image`` key (compose services of custom apps) and ``repository[:tag]`` of every dict
with a string ``repository`` (catalog ``images`` values). Returns them sorted and unique; ``[]``
when none are found.

``truenas_prune_plan``: decide which TrueNAS Docker images may be deleted.

``images`` is a list of normalized image dicts built by the role from ``app.image.query``::

  {id, repos: [str], tags: [str], created: str ISO, size: int}

``repos`` holds repository strings (plain ``ghcr.io/x/y`` or digest refs ``ghcr.io/x/y@sha256:…``),
``tags`` full tag refs (``ghcr.io/x/y:v1``). ``in_use`` is a list of image references as in
``app.query`` → ``active_workloads.images``. ``keep_patterns`` are regexes (``re.search``) on the
normalized repository. ``stopped`` is a list of image references of apps that are not running
(``truenas_config_images``); only their repository counts: a stopped app has no containers that
would make TrueNAS refuse the delete, and its image may not carry the configured tag.

References are normalized on both sides: digest (``@sha256:…``) and tag are stripped to get the
repository (a ``:`` before the last ``/`` is a registry port, not a tag), then a leading
``docker.io/`` or ``index.docker.io/`` and after it ``library/`` are removed, so
``docker.io/library/nginx:1.2`` and ``nginx:1.2`` are the same reference. A ref without tag or
digest means ``:latest``. ``<none>`` tags/repos are ignored.

An image is in use when an ``in_use`` ref matches one of its tags (repository + tag) or one of
its digest refs (repository + digest). Images are grouped by repository; an image with tags in
several repositories takes part in each. Per repository:

  1. in use                                          keep, why ``in_use``
  2. the ``rollback_count`` (default 2) newest
     not-in-use images whose ``created`` is strictly
     older than the newest in-use image               keep, why ``rollback``
  3. no image of the repository in use (stopped
     app, upgrade that left the app down)             keep the ``rollback_count`` newest, why
                                                     ``newest``; the repository is listed in
                                                     ``unused_repos``
  4. repository matches a keep pattern               keep all, why ``pinned`` (not in unused_repos)
  5. repository referenced by a ``stopped`` app       keep all, why ``stopped_app`` (not in
                                                     unused_repos), even when a running app
                                                     uses the same repository
  6. everything else                                delete, with reason
       ``older``         older than the rollback images (repository in use)
       ``newer_unused``  not in use, created at or after the newest in-use image (failed upgrade)
       ``superseded``    older than the newest images of a repository that is not in use
       ``dangling``      no repository at all

Keep wins across repositories: an image kept in any of its repositories is never deleted. Each
image appears once, in ``keep`` or in ``delete``; its ``repo`` is the repository that decided
(for a keep the strongest why: in_use > pinned > stopped_app > rollback > newest; for a delete the first
repository by name; ``""`` for dangling images without one).

Returns a dict:
  delete        [{id, repo, tags, size, reason}]
  keep          [{id, repo, tags, why}]
  unused_repos  sorted unique repositories with no image in use (pinned ones excluded)
  freed_bytes   sum of ``size`` over ``delete``

Lists are sorted by repository, then ``created``, then ``id``. ``created`` accepts ISO 8601 with
any number of fractional digits, ``Z``, offsets or no zone (treated as UTC). Bad input raises
``TypeError``.
"""

import re
from datetime import datetime, timezone

_REQUIRED = ("id", "repos", "tags", "created", "size")
_KEEP_RANK = {"in_use": 0, "pinned": 1, "stopped_app": 2, "rollback": 3, "newest": 4}
_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:[.,](\d+))?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)


def _split_ref(ref):
    """(repository, tag, digest) of an image reference; tag/digest are "" when absent."""
    ref = str(ref).strip()
    name, _, digest = ref.partition("@")
    tag = ""
    colon = name.rfind(":")
    if colon > name.rfind("/"):
        name, tag = name[:colon], name[colon + 1 :]
    for prefix in ("docker.io/", "index.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    if name.startswith("library/"):
        name = name[len("library/") :]
    if name == "<none>":
        name = ""
    if tag == "<none>":
        tag = ""
    return name, tag, digest


def _ref_keys(ref, default_latest):
    """Normalized match keys of a reference: "repo:tag" and/or "repo@digest"."""
    repo, tag, digest = _split_ref(ref)
    if not repo:
        return repo, set()
    keys = set()
    if digest:
        keys.add(f"{repo}@{digest}")
    if tag or (default_latest and not digest):
        keys.add(f"{repo}:{tag or 'latest'}")
    return repo, keys


def _parse_created(value, image_id):
    """``created`` as an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        match = _ISO.match(str(value).strip()) if isinstance(value, str) else None
        if not match:
            raise TypeError(f"truenas_prune_plan: image {image_id!r} has unparsable created {value!r}")
        date, clock, frac, zone = match.groups()
        if clock.count(":") == 1:
            clock += ":00"
        frac = (frac or "0")[:6].ljust(6, "0")
        if not zone or zone.upper() == "Z":
            zone = "+00:00"
        elif len(zone) == 3:
            zone += ":00"
        elif ":" not in zone:
            zone = f"{zone[:3]}:{zone[3:]}"
        try:
            dt = datetime.fromisoformat(f"{date}T{clock}.{frac}{zone}")
        except ValueError as exc:
            raise TypeError(f"truenas_prune_plan: image {image_id!r} has invalid created {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _str_list(value, field, image_id):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError(f"truenas_prune_plan: image {image_id!r} field {field!r} must be a list of strings")
    return value


def _normalize_image(image):
    """Validated internal record of one input image."""
    if not isinstance(image, dict):
        raise TypeError(f"truenas_prune_plan: each image must be a dict, got {type(image).__name__}")
    missing = [key for key in _REQUIRED if key not in image]
    if missing:
        raise TypeError(f"truenas_prune_plan: image is missing keys {missing}")
    image_id = image["id"]
    if not isinstance(image_id, str) or not image_id:
        raise TypeError(f"truenas_prune_plan: image id must be a non-empty string, got {image_id!r}")
    size = image["size"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise TypeError(f"truenas_prune_plan: image {image_id!r} size must be an int, got {size!r}")

    repos, keys = set(), set()
    for ref in _str_list(image["tags"], "tags", image_id):
        repo, ref_keys = _ref_keys(ref, default_latest=True)
        if repo:
            repos.add(repo)
            keys |= ref_keys
    for ref in _str_list(image["repos"], "repos", image_id):
        # A plain repository string is not a tag reference, so no implicit :latest here.
        repo, ref_keys = _ref_keys(ref, default_latest=False)
        if repo:
            repos.add(repo)
            keys |= ref_keys
    return {
        "id": image_id,
        "tags": list(image["tags"]),
        "size": size,
        "created": _parse_created(image["created"], image_id),
        "repos": sorted(repos),
        "keys": keys,
    }


def _decide_repo(members, in_use_ids, rollback_count):
    """{id: ("keep", why) | ("delete", reason)} for the images of one repository."""
    by_age = sorted(members, key=lambda m: (m["created"], m["id"]))
    used = [m for m in by_age if m["id"] in in_use_ids]
    decisions = {}
    if not used:
        newest = {m["id"] for m in by_age[-rollback_count:]} if rollback_count else set()
        for m in by_age:
            decisions[m["id"]] = ("keep", "newest") if m["id"] in newest else ("delete", "superseded")
        return decisions, True

    newest_used = used[-1]["created"]
    older = [m for m in by_age if m["id"] not in in_use_ids and m["created"] < newest_used]
    rollback = {m["id"] for m in older[-rollback_count:]} if rollback_count else set()
    for m in by_age:
        if m["id"] in in_use_ids:
            decisions[m["id"]] = ("keep", "in_use")
        elif m["id"] in rollback:
            decisions[m["id"]] = ("keep", "rollback")
        elif m["created"] < newest_used:
            decisions[m["id"]] = ("delete", "older")
        else:
            decisions[m["id"]] = ("delete", "newer_unused")
    return decisions, False


def truenas_config_images(config):
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            repository = node.get("repository")
            if isinstance(repository, str) and repository.strip():
                tag = str(node.get("tag") or "").strip() if isinstance(node.get("tag"), (str, int)) else ""
                refs.add(f"{repository.strip()}:{tag}" if tag else repository.strip())
            for key, value in node.items():
                if key == "image" and isinstance(value, str):
                    if value.strip():
                        refs.add(value.strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(config)
    return sorted(refs)


def _ref_list(value, name):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise TypeError(f"truenas_prune_plan: {name} must be a list of strings")
    return value


def truenas_prune_plan(images, in_use, keep_patterns=None, rollback_count=2, stopped=None):
    if isinstance(rollback_count, bool) or not isinstance(rollback_count, int) or rollback_count < 0:
        raise TypeError("truenas_prune_plan: rollback_count must be an int >= 0")
    if not isinstance(images, list):
        raise TypeError(f"truenas_prune_plan: images must be a list, got {type(images).__name__}")
    in_use = _ref_list(in_use, "in_use")
    stopped_repos = {_split_ref(ref)[0] for ref in _ref_list(stopped, "stopped")} - {""}
    if keep_patterns is None:
        keep_patterns = []
    if not isinstance(keep_patterns, list) or not all(isinstance(p, str) for p in keep_patterns):
        raise TypeError("truenas_prune_plan: keep_patterns must be a list of strings")
    try:
        patterns = [re.compile(p) for p in keep_patterns]
    except re.error as exc:
        raise TypeError(f"truenas_prune_plan: invalid keep pattern: {exc}") from exc

    records = [_normalize_image(i) for i in images]
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)):
        raise TypeError("truenas_prune_plan: duplicate image ids")

    in_use_keys = set()
    for ref in in_use:
        in_use_keys |= _ref_keys(ref, default_latest=True)[1]
    in_use_ids = {r["id"] for r in records if r["keys"] & in_use_keys}

    groups = {}
    for r in records:
        for repo in r["repos"]:
            groups.setdefault(repo, []).append(r)

    decisions = {r["id"]: [] for r in records}  # id -> [(repo, kind, label)]
    unused_repos = set()
    for repo, members in groups.items():
        if any(p.search(repo) for p in patterns):
            for m in members:
                decisions[m["id"]].append((repo, "keep", "pinned"))
            continue
        if repo in stopped_repos:
            for m in members:
                decisions[m["id"]].append((repo, "keep", "in_use" if m["id"] in in_use_ids else "stopped_app"))
            continue
        repo_decisions, unused = _decide_repo(members, in_use_ids, rollback_count)
        if unused:
            unused_repos.add(repo)
        for image_id, (kind, label) in repo_decisions.items():
            decisions[image_id].append((repo, kind, label))

    keep, delete = [], []
    for r in records:
        entries = decisions[r["id"]]
        keeps = [(_KEEP_RANK[label], repo, label) for repo, kind, label in entries if kind == "keep"]
        if keeps:
            _, repo, why = min(keeps)
            keep.append({"id": r["id"], "repo": repo, "tags": r["tags"], "why": why, "_created": r["created"]})
        elif entries:
            repo, _, reason = min(entries)
            delete.append(
                {"id": r["id"], "repo": repo, "tags": r["tags"], "size": r["size"], "reason": reason, "_created": r["created"]}
            )
        else:
            delete.append(
                {"id": r["id"], "repo": "", "tags": r["tags"], "size": r["size"], "reason": "dangling", "_created": r["created"]}
            )

    def ordered(items):
        items = sorted(items, key=lambda e: (e["repo"], e["_created"], e["id"]))
        for e in items:
            del e["_created"]
        return items

    delete = ordered(delete)
    return {
        "delete": delete,
        "keep": ordered(keep),
        "unused_repos": sorted(unused_repos),
        "freed_bytes": sum(e["size"] for e in delete),
    }


class FilterModule:
    """TrueNAS image prune filters."""

    def filters(self):
        return {"truenas_prune_plan": truenas_prune_plan, "truenas_config_images": truenas_config_images}

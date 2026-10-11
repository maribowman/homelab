"""Ansible filter ``truenas_upgrade_plan``: decide how far a TrueNAS app may be upgraded unattended.

``app`` is one ``app.query`` item, ``summary`` the ``app.upgrade_summary`` result. A TrueNAS
``human_version`` is ``"<upstream>_<catalog>"`` (e.g. ``"v1.132.3_1.2.3"``). An upgrade is
non-major when neither the upstream major nor the catalog major changes; for major ``0`` the
"major" is ``0.minor``. Upstream versions that have no usable major (dates, ``latest``,
calendar versions such as ``2024.10.1-ls12``) are decided on the catalog major only.

Returns a dict:
  action                  upgrade | held_back | none
  target_version          catalog version to pass to app.upgrade ("" unless action is upgrade)
  target_human_version    human version of the target ("" unless action is upgrade)
  current_human_version   installed human version ("" when unknown)
  latest_human_version    newest offered human version ("" when unknown)
  reason                  one-line explanation for the run report

``upgrade`` with ``latest_human_version != target_human_version`` is a partial upgrade: the rest
is held back and should be reported as a todo.
"""

import re

_NUMERIC = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")
# Leading components with this many digits are calendar versions/dates, not semver majors.
_CALVER_DIGITS = 4


def _numbers(text):
    """Leading dotted integers of a version string as a tuple, or None."""
    match = _NUMERIC.match(str(text).strip()) if text is not None else None
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _catalog(text):
    """Catalog version as a comparable 3-tuple, or None."""
    nums = _numbers(text)
    return (nums + (0, 0, 0))[:3] if nums else None


def _major_key(nums):
    """Major for comparison: (major,) or (0, minor) for 0.x versions."""
    return (0, nums[1] if len(nums) > 1 else 0) if nums[0] == 0 else (nums[0],)


def _upstream_major(human):
    """Upstream major key from "<upstream>_<catalog>", or None when it cannot be parsed."""
    if not human or "_" not in str(human):
        return None
    nums = _numbers(str(human).rsplit("_", 1)[0])
    if not nums or len(str(nums[0])) >= _CALVER_DIGITS:
        return None
    return _major_key(nums)


def _fmt(key):
    return ".".join(str(n) for n in key)


def _entry(version, human):
    """Normalise one offered version; catalog falls back to the human_version suffix."""
    human = "" if human is None else str(human)
    if version is None and "_" in human:
        version = human.rsplit("_", 1)[1]
    return {"version": "" if version is None else str(version), "human": human, "catalog": _catalog(version)}


def truenas_upgrade_plan(app, summary):
    if not isinstance(app, dict):
        raise TypeError(f"truenas_upgrade_plan: app must be a dict, got {type(app).__name__}")
    if summary is None:
        summary = {}
    if not isinstance(summary, dict):
        raise TypeError(f"truenas_upgrade_plan: summary must be a dict, got {type(summary).__name__}")

    current = _entry(app.get("version"), app.get("human_version"))
    latest = _entry(summary.get("latest_version"), summary.get("latest_human_version"))

    offered = summary.get("available_versions_for_upgrade")
    if isinstance(offered, list):
        candidates = [_entry(o.get("version"), o.get("human_version")) for o in offered if isinstance(o, dict)]
    else:
        candidates = [latest] if latest["version"] or latest["human"] else []

    if not latest["human"] and candidates:
        newest = max((c for c in candidates if c["catalog"]), key=lambda c: c["catalog"], default=None)
        latest = newest or latest

    plan = {
        "action": "none",
        "target_version": "",
        "target_human_version": "",
        "current_human_version": current["human"],
        "latest_human_version": latest["human"],
        "reason": "",
    }

    if app.get("upgrade_available") is False:
        plan["reason"] = "no upgrade available"
        return plan
    if current["catalog"] is None:
        if latest["version"] and latest["version"] != current["version"]:
            plan.update(action="held_back", reason=f"cannot parse current catalog version {current['version']!r}")
        else:
            plan["reason"] = "no upgrade available (current version unknown)"
        return plan

    newer = [c for c in candidates if c["catalog"] and c["catalog"] > current["catalog"]]
    if latest["catalog"] and latest["catalog"] > current["catalog"] and latest not in newer:
        newer.append(latest)
    if not newer:
        plan["reason"] = "no upgrade available"
        return plan

    cur_cat_major = _major_key(current["catalog"])
    cur_up_major = _upstream_major(current["human"])

    def blocker(cand):
        """Why cand is a major upgrade ("" when it qualifies), plus a note when upstream was unparsable."""
        cat_major = _major_key(cand["catalog"])
        if cat_major != cur_cat_major:
            return f"catalog major {_fmt(cur_cat_major)}→{_fmt(cat_major)}", ""
        up_major = _upstream_major(cand["human"])
        if cur_up_major is None or up_major is None:
            return "", "upstream version not parseable, decided on catalog major only"
        if up_major != cur_up_major:
            return f"upstream major {_fmt(cur_up_major)}→{_fmt(up_major)}", ""
        return "", ""

    # Only offered versions are upgrade targets; latest_* alone (with an explicit empty list) can only be held back.
    qualifying = [c for c in newer if c in candidates and not blocker(c)[0]]
    newest = max(newer, key=lambda c: c["catalog"])
    newest_label = newest["human"] or newest["version"]
    current_label = current["human"] or current["version"]

    if not qualifying:
        why = blocker(newest)[0] or "not offered in available_versions_for_upgrade"
        plan.update(action="held_back", reason=f"{current_label} → {newest_label} held back ({why})")
        return plan

    target = max(qualifying, key=lambda c: c["catalog"])
    target_label = target["human"] or target["version"]
    parts = [f"{current_label} → {target_label} (non-major)"]
    if newest["catalog"] > target["catalog"]:
        parts.append(f"{newest_label} held back ({blocker(newest)[0] or 'not offered'})")
    note = blocker(target)[1]
    if note:
        parts.append(note)
    plan.update(
        action="upgrade",
        target_version=target["version"],
        target_human_version=target["human"],
        reason="; ".join(parts),
    )
    return plan


class FilterModule:
    """TrueNAS app version filters."""

    def filters(self):
        return {"truenas_upgrade_plan": truenas_upgrade_plan}

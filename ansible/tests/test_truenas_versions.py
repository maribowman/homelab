"""Tests for the truenas_upgrade_plan filter (no Ansible needed)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "roles/update_truenas_apps/filter_plugins/truenas_versions.py"
_spec = importlib.util.spec_from_file_location("truenas_versions", _PATH)
tv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tv)
plan = tv.truenas_upgrade_plan


def app(human, upgrade_available=True):
    return {"name": "x", "version": human.rsplit("_", 1)[1], "human_version": human, "upgrade_available": upgrade_available}


def summary(*humans, latest=None):
    offered = [{"version": h.rsplit("_", 1)[1], "human_version": h} for h in humans]
    latest = latest or (humans[-1] if humans else None)
    result = {"available_versions_for_upgrade": offered, "changelog": None}
    if latest:
        result.update(latest_version=latest.rsplit("_", 1)[1], latest_human_version=latest)
    return result


@pytest.mark.parametrize(
    ("current", "offered", "target"),
    [
        ("v1.132.3_1.2.3", ["v1.132.4_1.2.4"], "v1.132.4_1.2.4"),  # patch
        ("v1.132.3_1.2.3", ["v1.135.0_1.3.0"], "v1.135.0_1.3.0"),  # minor
        ("0.21.0_0.4.1", ["0.21.3_0.4.7"], "0.21.3_0.4.7"),  # 0.x patch
        ("1.132.3_1.2.3", ["v1.133.0_1.2.4"], "v1.133.0_1.2.4"),  # leading v on one side only
    ],
)
def test_non_major_upgrades(current, offered, target):
    result = plan(app(current), summary(*offered))
    assert result["action"] == "upgrade"
    assert result["target_human_version"] == target
    assert result["target_version"] == target.rsplit("_", 1)[1]
    assert result["current_human_version"] == current
    assert result["latest_human_version"] == target
    assert "non-major" in result["reason"]


@pytest.mark.parametrize(
    ("current", "offered", "why"),
    [
        ("v1.132.3_1.2.3", "v2.0.0_1.3.0", "upstream major 1→2"),
        ("v1.132.3_1.2.3", "v1.133.0_2.0.0", "catalog major 1→2"),
        ("0.21.0_1.0.5", "0.22.0_1.0.6", "upstream major 0.21→0.22"),  # 0.x minor bump upstream
        ("v1.2.0_0.4.1", "v1.2.1_0.5.0", "catalog major 0.4→0.5"),  # 0.x minor bump catalog
    ],
)
def test_majors_are_held_back(current, offered, why):
    result = plan(app(current), summary(offered))
    assert result["action"] == "held_back"
    assert result["target_version"] == ""
    assert result["latest_human_version"] == offered
    assert why in result["reason"]


def test_multiple_candidates_pick_highest_qualifying():
    offered = ["v1.10.0_1.2.10", "v1.9.0_1.2.9", "v2.0.0_1.3.0", "v1.11.0_2.0.0"]  # unsorted on purpose
    result = plan(app("v1.8.0_1.2.8"), summary(*offered, latest="v1.11.0_2.0.0"))
    assert result["action"] == "upgrade"
    assert result["target_version"] == "1.2.10"  # semantic: 1.2.10 > 1.2.9
    assert result["latest_human_version"] == "v1.11.0_2.0.0"  # partial upgrade, rest held back
    assert "v1.11.0_2.0.0 held back (catalog major 1→2)" in result["reason"]


def test_nothing_qualifies():
    result = plan(app("v1.0.0_1.0.0"), summary("v2.0.0_1.1.0", "v1.1.0_2.0.0", latest="v2.1.0_2.1.0"))
    assert result["action"] == "held_back"
    assert result["latest_human_version"] == "v2.1.0_2.1.0"


@pytest.mark.parametrize(
    ("current", "offered", "action"),
    [
        ("2024-10-01_1.0.3", "2024-11-15_1.0.4", "upgrade"),
        ("latest_1.0.3", "latest_1.1.0", "upgrade"),
        ("2024.10.1-ls12_1.0.3", "2025.1.0-ls3_1.0.4", "upgrade"),
        ("2024.10.1-ls12_1.0.3", "2025.1.0-ls3_2.0.0", "held_back"),  # catalog major still counts
        ("v1.2.3_1.0.3", "RELEASE.2025-01-01_1.0.4", "upgrade"),  # unparsable on one side only
    ],
)
def test_unparsable_upstream_uses_catalog_major(current, offered, action):
    result = plan(app(current), summary(offered))
    assert result["action"] == action
    if action == "upgrade":
        assert "catalog major only" in result["reason"]


def test_no_upgrade_available():
    result = plan(app("v1.0.0_1.0.0", upgrade_available=False), summary("v1.0.1_1.0.1"))
    assert result["action"] == "none"
    assert result["target_version"] == ""


def test_offered_versions_not_newer():
    assert plan(app("v1.0.0_1.0.5"), summary("v1.0.0_1.0.5", "v0.9.0_1.0.4"))["action"] == "none"


def test_missing_list_uses_latest():
    result = plan(app("v1.0.0_1.0.0"), {"latest_version": "1.0.1", "latest_human_version": "v1.0.1_1.0.1"})
    assert result["action"] == "upgrade"
    assert result["target_version"] == "1.0.1"


def test_empty_list_with_newer_latest_is_held_back():
    result = plan(app("v1.0.0_1.0.0"), summary(latest="v1.0.1_1.0.1"))
    assert result["action"] == "held_back"
    assert "not offered" in result["reason"]


@pytest.mark.parametrize("summary_value", [None, {}, {"available_versions_for_upgrade": []}])
def test_missing_summary_fields(summary_value):
    result = plan(app("v1.0.0_1.0.0"), summary_value)
    assert result == {
        "action": "none",
        "target_version": "",
        "target_human_version": "",
        "current_human_version": "v1.0.0_1.0.0",
        "latest_human_version": "",
        "reason": "no upgrade available",
    }


def test_missing_app_fields():
    # No version: catalog comes from human_version.
    result = plan({"human_version": "v1.0.0_1.0.0"}, summary("v1.0.1_1.0.1"))
    assert result["action"] == "upgrade"
    # Nothing known about the installed version: never upgrade blindly.
    result = plan({}, summary("v1.0.1_1.0.1"))
    assert result["action"] == "held_back"
    assert result["current_human_version"] == ""
    assert plan({}, {})["action"] == "none"


def test_odd_candidate_entries_are_ignored():
    odd = {"available_versions_for_upgrade": ["1.0.1", None, {}, {"version": "1.0.2"}]}
    result = plan(app("v1.0.0_1.0.0"), odd)
    assert result["action"] == "upgrade"
    assert result["target_version"] == "1.0.2"
    assert "catalog major only" in result["reason"]


@pytest.mark.parametrize(("app_value", "summary_value"), [("x", {}), ({}, ["x"])])
def test_invalid_types_raise(app_value, summary_value):
    with pytest.raises(TypeError):
        plan(app_value, summary_value)


def test_filter_module_registers_filter():
    assert tv.FilterModule().filters()["truenas_upgrade_plan"] is plan

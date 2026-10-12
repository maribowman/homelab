"""Tests for the truenas_prune_plan filter (no Ansible needed)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "roles/prune_truenas_images/filter_plugins/truenas_prune.py"
_spec = importlib.util.spec_from_file_location("truenas_prune", _PATH)
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)
plan = tp.truenas_prune_plan

REPO = "ghcr.io/immich-app/immich-server"


def img(image_id, created, tags=(), repos=None, size=100, dangling=False):
    if repos is None:
        repos = sorted({t.rsplit(":", 1)[0] for t in tags})
    return {
        "id": image_id,
        "repos": list(repos),
        "tags": list(tags),
        "created": created,
        "size": size,
        "dangling": dangling,
    }


def ids(entries):
    return [e["id"] for e in entries]


def whys(result):
    return {e["id"]: e["why"] for e in result["keep"]}


def reasons(result):
    return {e["id"]: e["reason"] for e in result["delete"]}


def test_in_use_plus_one_older_both_kept():
    images = [
        img("new", "2025-03-01T00:00:00Z", [f"{REPO}:v2"]),
        img("old", "2025-01-01T00:00:00Z", [f"{REPO}:v1"]),
    ]
    result = plan(images, [f"{REPO}:v2"])
    assert result["delete"] == []
    assert whys(result) == {"new": "in_use", "old": "rollback"}
    assert ids(result["keep"]) == ["old", "new"]  # sorted by created
    assert result["unused_repos"] == []
    assert result["freed_bytes"] == 0


def test_three_versions_oldest_deleted_with_one_rollback():
    images = [
        img("v3", "2025-03-01T00:00:00Z", [f"{REPO}:v3"]),
        img("v2", "2025-02-01T00:00:00Z", [f"{REPO}:v2"]),
        img("v1", "2025-01-01T00:00:00Z", [f"{REPO}:v1"]),
    ]
    result = plan(images, [f"{REPO}:v3"], rollback_count=1)
    assert whys(result) == {"v3": "in_use", "v2": "rollback"}
    assert reasons(result) == {"v1": "older"}
    assert result["delete"][0]["repo"] == REPO


def test_newer_unused_deleted():
    images = [
        img("cur", "2025-02-01T00:00:00Z", [f"{REPO}:v2"]),
        img("prev", "2025-01-01T00:00:00Z", [f"{REPO}:v1"]),
        img("failed", "2025-03-01T00:00:00Z", [f"{REPO}:v3"]),
    ]
    result = plan(images, [f"{REPO}:v2"])
    assert whys(result) == {"cur": "in_use", "prev": "rollback"}
    assert reasons(result) == {"failed": "newer_unused"}


def test_dangling_with_digest_repo_is_rollback():
    repo = "ghcr.io/maribowman/roastbeef-swag"
    images = [
        img("cur", "2025-02-01T00:00:00.123456789Z", [f"{repo}:latest"], repos=[f"{repo}@sha256:bbb"]),
        img("prev", "2025-01-01T00:00:00Z", [], repos=[f"{repo}@sha256:aaa"], dangling=True),
        img("older", "2024-12-01T00:00:00Z", [], repos=[f"{repo}@sha256:999"], dangling=True),
    ]
    result = plan(images, [f"{repo}:latest"], rollback_count=1)
    assert whys(result) == {"cur": "in_use", "prev": "rollback"}
    assert reasons(result) == {"older": "older"}


def test_in_use_by_digest_ref():
    repo = "ghcr.io/x/y"
    images = [
        img("cur", "2025-02-01T00:00:00Z", [], repos=[f"{repo}@sha256:bbb"], dangling=True),
        img("prev", "2025-01-01T00:00:00Z", [], repos=[f"{repo}@sha256:aaa"], dangling=True),
    ]
    result = plan(images, [f"{repo}@sha256:bbb"])
    assert whys(result) == {"cur": "in_use", "prev": "rollback"}


def test_truenas_25_10_digest_pinned_refs():
    # Shape seen on TrueNAS 25.10: apps run repo:tag@sha256:... and most images have no repo_tags,
    # only repo_digests (passed whole as repos); one image can carry two digests of the same repo.
    pg = "ixsystems/postgres-upgrade"
    images = [
        img("server-cur", "2026-10-07T18:40:14.15638162Z", [], repos=[f"{REPO}@sha256:be56"]),
        img("server-prev", "2026-09-28T16:55:59.233195878Z", [], repos=[f"{REPO}@sha256:d317"]),
        img("pg-cur", "2026-10-03T17:10:51Z", [], repos=[f"{pg}@sha256:7215", f"{pg}@sha256:81e6"]),
        img("pg-prev", "2026-09-30T17:20:20Z", [], repos=[f"{pg}@sha256:336c", f"{pg}@sha256:e28b"]),
        img("pg-old", "2026-09-15T19:54:43Z", [], repos=[f"{pg}@sha256:8a90"]),
        img("rustfs-cur", "2026-10-03T05:01:08Z", [], repos=["rustfs/rustfs@sha256:1803"]),
        img("rustfs-prev", "2026-09-16T10:05:51Z", ["rustfs/rustfs:1.0.0"], repos=["rustfs/rustfs@sha256:8cc9"]),
        img("pg18", "2026-09-19T00:36:15Z", [], repos=["postgres@sha256:5a5a"]),
    ]
    in_use = [
        f"{REPO}:v3.3.0@sha256:be56",
        f"{pg}:1.2.21@sha256:81e6",
        f"{pg}:1.2.21@sha256:7215",
        "rustfs/rustfs:1.0.1@sha256:1803",
        "postgres:18.6-trixie@sha256:5a5a",
    ]
    result = plan(images, in_use)
    assert whys(result) == {
        "server-cur": "in_use", "server-prev": "rollback",
        "pg-cur": "in_use", "pg-prev": "rollback", "pg-old": "rollback",
        "rustfs-cur": "in_use", "rustfs-prev": "rollback",
        "pg18": "in_use",
    }
    assert result["delete"] == []
    assert result["unused_repos"] == []
    assert reasons(plan(images, in_use, rollback_count=1)) == {"pg-old": "older"}


def test_default_keeps_two_rollback_images():
    images = [
        img("v4", "2025-04-01T00:00:00Z", [f"{REPO}:v4"]),
        img("v3", "2025-03-01T00:00:00Z", [f"{REPO}:v3"]),
        img("v2", "2025-02-01T00:00:00Z", [f"{REPO}:v2"]),
        img("v1", "2025-01-01T00:00:00Z", [f"{REPO}:v1"]),
    ]
    result = plan(images, [f"{REPO}:v4"])
    assert whys(result) == {"v4": "in_use", "v3": "rollback", "v2": "rollback"}
    assert reasons(result) == {"v1": "older"}


def test_upgrade_left_app_down_keeps_new_and_previous():
    # the upgraded app is not running, so nothing is in use: the previous (working) image must survive
    images = [
        img("new", "2025-03-01T00:00:00Z", [], repos=[f"{REPO}@sha256:ccc"]),
        img("prev", "2025-02-01T00:00:00Z", [], repos=[f"{REPO}@sha256:bbb"]),
        img("old", "2025-01-01T00:00:00Z", [], repos=[f"{REPO}@sha256:aaa"]),
    ]
    result = plan(images, [])
    assert whys(result) == {"new": "newest", "prev": "newest"}
    assert reasons(result) == {"old": "superseded"}
    assert result["unused_repos"] == [REPO]


def test_rollback_count_zero_keeps_only_in_use():
    images = [
        img("cur", "2025-02-01T00:00:00Z", [f"{REPO}:v2"]),
        img("prev", "2025-01-01T00:00:00Z", [f"{REPO}:v1"]),
        img("stopped", "2025-01-01T00:00:00Z", ["other/app:1"]),
    ]
    result = plan(images, [f"{REPO}:v2"], rollback_count=0)
    assert whys(result) == {"cur": "in_use"}
    assert reasons(result) == {"prev": "older", "stopped": "superseded"}


@pytest.mark.parametrize("count", [-1, 1.5, "2", True])
def test_bad_rollback_count_raises_type_error(count):
    with pytest.raises(TypeError):
        plan([], [], rollback_count=count)


def test_dangling_without_repo_deleted():
    images = [
        img("orphan", "2025-01-01T00:00:00Z", [], repos=[], size=42, dangling=True),
        img("none", "2025-01-01T00:00:00Z", ["<none>:<none>"], repos=["<none>@<none>"], size=8, dangling=True),
    ]
    result = plan(images, [])
    assert reasons(result) == {"orphan": "dangling", "none": "dangling"}
    assert all(e["repo"] == "" for e in result["delete"])
    assert result["freed_bytes"] == 50


def test_repo_not_in_use_keeps_newest_and_is_listed_with_one_rollback():
    images = [
        img("a2", "2025-02-01T00:00:00Z", ["redis:7.2"]),
        img("a1", "2025-01-01T00:00:00Z", ["redis:7.0"]),
        img("cur", "2025-01-01T00:00:00Z", [f"{REPO}:v1"]),
    ]
    result = plan(images, [f"{REPO}:v1"], rollback_count=1)
    assert whys(result) == {"a2": "newest", "cur": "in_use"}
    assert reasons(result) == {"a1": "superseded"}
    assert result["unused_repos"] == ["redis"]


def test_keep_pattern_pins_repo():
    images = [
        img("p3", "2025-03-01T00:00:00Z", ["ghcr.io/home-assistant/home-assistant:2025.3"]),
        img("p2", "2025-02-01T00:00:00Z", ["ghcr.io/home-assistant/home-assistant:2025.2"]),
        img("p1", "2025-01-01T00:00:00Z", ["ghcr.io/home-assistant/home-assistant:2025.1"]),
    ]
    result = plan(images, [], ["home-assistant$"])
    assert result["delete"] == []
    assert set(whys(result).values()) == {"pinned"}
    assert result["unused_repos"] == []


def test_two_in_use_tags_rollback_older_than_newest_in_use():
    images = [
        img("u2", "2025-03-01T00:00:00Z", ["postgres:16"]),
        img("mid", "2025-02-01T00:00:00Z", ["postgres:15.2"]),
        img("u1", "2025-01-15T00:00:00Z", ["postgres:15"]),
        img("old", "2025-01-01T00:00:00Z", ["postgres:14"]),
    ]
    result = plan(images, ["postgres:16", "postgres:15"], rollback_count=1)
    # The rollback is the newest unused image older than the newest in-use one (u2), i.e. "mid".
    assert whys(result) == {"u2": "in_use", "u1": "in_use", "mid": "rollback"}
    assert reasons(result) == {"old": "older"}


def test_docker_io_library_normalization():
    images = [
        img("cur", "2025-02-01T00:00:00+02:00", ["docker.io/library/nginx:1.27"]),
        img("prev", "2025-01-01T00:00:00", ["nginx:1.26"]),
        img("old", "2024-12-01 00:00:00.5", ["index.docker.io/library/nginx:1.25"]),
    ]
    result = plan(images, ["nginx:1.27"], rollback_count=1)
    assert whys(result) == {"cur": "in_use", "prev": "rollback"}
    assert reasons(result) == {"old": "older"}
    assert {e["repo"] for e in result["keep"] + result["delete"]} == {"nginx"}

    result = plan([img("x", "2025-01-01T00:00:00Z", ["redis:7"])], ["docker.io/library/redis:7"])
    assert whys(result) == {"x": "in_use"}


def test_registry_port_and_implicit_latest():
    images = [
        img("cur", "2025-02-01T00:00:00Z", ["registry.local:5000/team/app"], repos=["registry.local:5000/team/app"]),
        img("prev", "2025-01-01T00:00:00Z", ["registry.local:5000/team/app:1.0"], repos=[]),
    ]
    result = plan(images, ["registry.local:5000/team/app:latest"])
    assert whys(result) == {"cur": "in_use", "prev": "rollback"}
    assert {e["repo"] for e in result["keep"]} == {"registry.local:5000/team/app"}


def test_image_with_tags_in_two_repos_keep_wins():
    # "shared" is too old for repo a (deleted there) but in use via repo b: it must be kept, once.
    images = [
        img("a3", "2025-03-01T00:00:00Z", ["a:3"]),
        img("a2", "2025-02-01T00:00:00Z", ["a:2"]),
        img("shared", "2025-01-01T00:00:00Z", ["a:1", "b:1"], size=500),
        img("a0", "2024-12-01T00:00:00Z", ["a:0"], size=7),
    ]
    result = plan(images, ["a:3", "b:1"], rollback_count=1)
    assert whys(result) == {"a3": "in_use", "a2": "rollback", "shared": "in_use"}
    assert reasons(result) == {"a0": "older"}
    assert ids(result["keep"]).count("shared") == 1
    assert result["freed_bytes"] == 7


def test_image_in_two_repos_deleted_once():
    images = [
        img("new", "2025-03-01T00:00:00Z", ["a:2", "b:2"]),
        img("mid", "2025-02-01T00:00:00Z", ["a:1"]),
        img("dup", "2025-01-01T00:00:00Z", ["a:0", "b:0"], size=300),
        img("b1", "2025-02-15T00:00:00Z", ["b:1"]),
    ]
    result = plan(images, ["a:2"], rollback_count=1)
    assert reasons(result) == {"dup": "older"}
    assert result["delete"][0]["repo"] == "a"
    assert ids(result["delete"]) == ["dup"]
    assert result["freed_bytes"] == 300


def test_empty_input():
    assert plan([], []) == {"delete": [], "keep": [], "unused_repos": [], "freed_bytes": 0}
    assert plan([], None, None)["freed_bytes"] == 0


def test_freed_bytes_sum_and_sorting():
    images = [
        img("b_old", "2024-01-01T00:00:00Z", ["b:0"], size=1000),
        img("a_cur", "2025-03-01T00:00:00Z", ["a:3"], size=1),
        img("a_old2", "2025-01-01T00:00:00Z", ["a:1"], size=200),
        img("a_old1", "2024-01-01T00:00:00Z", ["a:0"], size=300),
        img("a_rb", "2025-02-01T00:00:00Z", ["a:2"], size=1),
        img("b_new", "2025-01-01T00:00:00Z", ["b:1"], size=1),
        img("dang", "2023-01-01T00:00:00Z", [], repos=[], size=4, dangling=True),
    ]
    result = plan(images, ["a:3"], rollback_count=1)
    assert ids(result["delete"]) == ["dang", "a_old1", "a_old2", "b_old"]
    assert result["freed_bytes"] == 1000 + 200 + 300 + 4
    assert ids(result["keep"]) == ["a_rb", "a_cur", "b_new"]
    assert result["unused_repos"] == ["b"]


def test_deterministic_regardless_of_input_order():
    images = [
        img("v3", "2025-03-01T00:00:00Z", ["x:3"]),
        img("v1", "2025-01-01T00:00:00Z", ["x:1"]),
        img("v2", "2025-02-01T00:00:00Z", ["x:2"]),
    ]
    assert plan(images, ["x:3"]) == plan(list(reversed(images)), ["x:3"])


@pytest.mark.parametrize(
    ("images", "in_use", "keep"),
    [
        ("nope", [], []),
        (["not-a-dict"], [], []),
        ([{"id": "x"}], [], []),
        ([img("x", "yesterday", ["a:1"])], [], []),
        ([img("x", "2025-01-01T00:00:00Z", ["a:1"], size="10")], [], []),
        ([img("x", "2025-01-01T00:00:00Z", ["a:1"]), img("x", "2025-01-01T00:00:00Z", ["a:2"])], [], []),
        ([], "a:1", []),
        ([], [], "a"),
        ([], [], ["("]),
    ],
)
def test_bad_input_raises_type_error(images, in_use, keep):
    with pytest.raises(TypeError):
        plan(images, in_use, keep)


def test_filter_module_exports():
    assert tp.FilterModule().filters()["truenas_prune_plan"] is plan

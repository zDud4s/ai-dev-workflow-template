"""Meta-tests for the test catalog (.ai/tests/) and its selector.

This module is the catalog's own guardrail. It belongs to the ``catalog`` group
which is marked ``always`` in the overlay, so it runs at every gate — meaning a
stale or broken catalog can never slip past validation.

The path literals below double as the scanner's coupling signal: referencing
``.ai/scripts/select_tests.py`` / the two catalog YAMLs here is what makes the
``catalog`` group ``cover`` them, so editing the selector re-runs these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import select_tests  # resolved via .ai/scripts on sys.path (see conftest.py)

REPO_ROOT = Path(__file__).resolve().parents[1]
SELECT_TESTS = REPO_ROOT / ".ai" / "scripts" / "select_tests.py"
GENERATED = REPO_ROOT / ".ai" / "tests" / "catalog.generated.yaml"
OVERLAY = REPO_ROOT / ".ai" / "tests" / "catalog.overlay.yaml"


def test_catalog_files_exist() -> None:
    assert SELECT_TESTS.is_file()
    assert GENERATED.is_file()
    assert OVERLAY.is_file()


def test_generated_catalog_is_in_sync() -> None:
    """Lockfile invariant: the committed catalog equals a fresh in-memory build.

    If this fails you added/renamed a test (or changed what a test references)
    without regenerating. Fix with::

        python .ai/scripts/select_tests.py --sync
    """
    assert select_tests.is_in_sync(REPO_ROOT), (
        "catalog.generated.yaml is stale — run "
        "`python .ai/scripts/select_tests.py --sync` and commit the result"
    )


def test_every_test_file_belongs_to_exactly_one_group() -> None:
    catalog = select_tests.load_generated(REPO_ROOT)
    members: dict[str, list[str]] = {}
    for gid, g in catalog["groups"].items():
        for t in g["tests"]:
            members.setdefault(t, []).append(gid)

    on_disk = {f"tests/{p.name}" for p in select_tests.discover_test_files(REPO_ROOT)}
    mapped = set(members)

    assert on_disk == mapped, (
        f"orphan tests (on disk, not in catalog): {sorted(on_disk - mapped)}; "
        f"phantom tests (in catalog, not on disk): {sorted(mapped - on_disk)}"
    )
    dupes = {t: gids for t, gids in members.items() if len(gids) > 1}
    assert not dupes, f"test files mapped to multiple groups: {dupes}"


def test_group_tests_and_covers_resolve_to_real_files() -> None:
    catalog = select_tests.load_generated(REPO_ROOT)
    missing: list[str] = []
    for gid, g in catalog["groups"].items():
        for rel in g["tests"] + g["covers"]:
            if not (REPO_ROOT / rel).is_file():
                missing.append(f"{gid}:{rel}")
    assert not missing, f"catalog references non-existent files: {missing}"


def test_overlay_always_and_aliases_are_consistent() -> None:
    overlay = select_tests.load_overlay(REPO_ROOT)
    catalog = select_tests.load_generated(REPO_ROOT)
    group_ids = set(catalog["groups"])

    unknown_always = [g for g in overlay["always"] if g not in group_ids]
    assert not unknown_always, f"`always` lists non-existent groups: {unknown_always}"

    # Every alias target must be a real group id (the canonical destination).
    bad_targets = [
        (src, dst) for src, dst in overlay["aliases"].items() if dst not in group_ids
    ]
    assert not bad_targets, f"aliases point to non-existent groups: {bad_targets}"


def test_catalog_group_is_marked_always() -> None:
    """The meta-test's own group must always run, else staleness could hide."""
    overlay = select_tests.load_overlay(REPO_ROOT)
    assert "catalog" in overlay["always"]


def test_select_maps_changed_source_to_its_group() -> None:
    """A changed covered source file selects exactly the groups that cover it."""
    catalog = select_tests.load_generated(REPO_ROOT)
    overlay = select_tests.load_overlay(REPO_ROOT)

    # Pick any group that has at least one cover, and use its first cover.
    sample = next(
        (gid, g["covers"][0])
        for gid, g in catalog["groups"].items()
        if g["covers"]
    )
    gid, cover = sample
    sel = select_tests.select(catalog, overlay, [cover])
    assert gid in sel["touched"]
    assert not sel["unmapped"]


def test_select_unmapped_source_triggers_fallback() -> None:
    catalog = select_tests.load_generated(REPO_ROOT)
    overlay = select_tests.load_overlay(REPO_ROOT)
    sel = select_tests.select(catalog, overlay, [".ai/dashboard/server/__nonexistent__.py"])
    assert sel["fallback"] is True


def test_select_inert_change_does_not_trigger_fallback() -> None:
    catalog = select_tests.load_generated(REPO_ROOT)
    overlay = select_tests.load_overlay(REPO_ROOT)
    sel = select_tests.select(catalog, overlay, [".ai/local/ledgers/metrics.jsonl"])
    assert sel["fallback"] is False
    assert not sel["unmapped"]


def test_gate_always_includes_always_groups() -> None:
    catalog = select_tests.load_generated(REPO_ROOT)
    overlay = select_tests.load_overlay(REPO_ROOT)
    # An empty diff still pulls every always group into the gate.
    sel = select_tests.select(catalog, overlay, [])
    for g in overlay["always"]:
        assert g in sel["gate"]


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("test_jobs_a11y", "jobs"),
        ("test_pty_lifecycle", "pty"),
        ("test_auto_select_table", "auto"),
        ("test_structure", "structure"),
    ],
)
def test_area_of(stem: str, expected: str) -> None:
    assert select_tests.area_of(stem) == expected

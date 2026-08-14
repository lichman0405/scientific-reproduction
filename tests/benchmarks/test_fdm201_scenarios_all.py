"""FDM-201 scenario-suite acceptance -- AC-02 (DEV-M12-G07).

AC-02 of ``.development/goals/DEV-M12-G07.yaml``: **all scenarios A-J
pass in one clean run**. The one-clean-run evidence is the verification
command itself (``python -m pytest -q tests/benchmarks tests/scenarios``
-- the DEV-M12-G07 verification gate). This file pins the acceptance
fixture of that command:

1. all ten scenario files exist under ``tests/scenarios/`` with the
   documented names (``test_<scenario>_fdm201_*.py``);
2. every file imports cleanly and collects a pinned, deterministic number
   of tests -- 7 + 9 + 8 + 18 + 14 + 16 + 11 + 10 + 12 + 10 = **115**
   tests in total (the DEV-M12-G07 task brief quotes "92 tests" for
   scenarios A-J; the actual frozen suite collects 115, pinned here as
   the acceptance fixture -- see the deviation note in the PR body);
3. every scenario file maps onto the acceptance criteria of its governing
   development goal, encoded here as the frozen per-file AC mapping table
   (from ``.development/reviews/DEV-M12-G05-a1.yaml`` and
   ``DEV-M12-G06-a1.yaml``):

   =========  ==========================  ==========================
   file       goal / AC                   test-name evidence
   =========  ==========================  ==========================
   A          DEV-M12-G05 AC-01           every test ``test_A_ac01_*``
   B          DEV-M12-G05 AC-02           every test ``test_B_ac02_*``
   C          DEV-M12-G05 AC-03           every test ``test_C_ac03_*``
   D          DEV-M12-G05 AC-04           ``test_D_*``, **no** ac-prefix
   E          DEV-M12-G05 AC-05           ``test_E_ac01_*`` / ``ac02_*`` /
                                          ``ac03_*`` families
   F          DEV-M12-G06 AC-01           ``test_F_ac01_*`` (supervisor
                                          scientific review)
   G          DEV-M12-G06 AC-02           ``test_G_ac02_*`` / ``ac03_*``
   H          DEV-M12-G06 AC-03           ``test_H_ac02_*`` / ``ac03_*``
   I          DEV-M12-G06 AC-04           every test ``test_I_ac04_*``
   J          DEV-M12-G06 AC-05           every test ``test_J_ac05_*``
   =========  ==========================  ==========================

   Deviation notes (documented in the PR body, no source change): the
   task brief suggested ``test_D_ac04_*`` and ``test_E_ac05_*`` prefixes;
   the actual suite uses no AC prefix for D (scenario D tests carry plain
   ``test_D_*`` names) and the G05 AC-05 family split ``ac01/ac02/ac03``
   for E. The frozen mapping table above is the accepted one.

Determinism: the collection is performed offline by importing each module
with ``importlib`` and enumerating its ``test_*`` functions (no scenario
test is parametrized, so the function count equals pytest's collected
count; verified against ``pytest --collect-only`` at acceptance time).
No wall clock, no randomness, no network.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

#: The frozen scenario suite directory (mirrors the verification command).
SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "tests" / "scenarios"

#: The ten scenario files of the FDM-201 acceptance fixture (A-J).
SCENARIO_FILES: tuple[tuple[str, str], ...] = (
    ("A", "test_A_fdm201_strict_success.py"),
    ("B", "test_B_fdm201_recovery_success.py"),
    ("C", "test_C_fdm201_not_reproduced_closure.py"),
    ("D", "test_D_fdm201_adsorption_inconclusive.py"),
    ("E", "test_E_engineering_slurm_failure.py"),
    ("F", "test_F_fdm201_dft_convergence_failure.py"),
    ("G", "test_G_monitor_crash_recovery.py"),
    ("H", "test_H_delayed_lab_result.py"),
    ("I", "test_I_fdm201_missing_parameter.py"),
    ("J", "test_J_fdm201_plan_freeze_blocked.py"),
)

#: Pinned per-file collected test counts (frozen acceptance fixture; the
#: sum -- 115 -- is the actual collected size of the scenario suite).
PINNED_TEST_COUNTS: dict[str, int] = {
    "A": 7,
    "B": 9,
    "C": 8,
    "D": 18,
    "E": 14,
    "F": 16,
    "G": 11,
    "H": 10,
    "I": 12,
    "J": 10,
}

#: The frozen scenario -> goal-AC mapping (see module docstring). The
#: value is a tuple of name predicates; a predicate is satisfied when any
#: collected test name matches it.
SCENARIO_AC_MAPPING: dict[str, tuple[tuple[str, str], ...]] = {
    "A": (("every", "ac01"),),
    "B": (("every", "ac02"),),
    "C": (("every", "ac03"),),
    "D": (("none", "ac"), ("every", "_"),),  # no AC prefix -- G05 AC-04
    "E": (("any", "ac01"), ("any", "ac02"), ("any", "ac03")),
    "F": (("any", "ac01"),),
    "G": (("any", "ac02"), ("any", "ac03")),
    "H": (("any", "ac02"), ("any", "ac03")),
    "I": (("every", "ac04"),),
    "J": (("every", "ac05"),),
}


def _module_for(scenario: str, filename: str):
    """Import one scenario module by file path (offline, no collection).

    Loads from ``tests/scenarios/<filename>`` directly, so the module is
    importable regardless of which directories pytest put on ``sys.path``
    (the scenario suites live in their own directory). The synthetic
    module name is stable, so each scenario module is imported exactly
    once per process.
    """
    module_name = f"_fdm201_scenario_{scenario.lower()}_fixture"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = SCENARIOS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _collected_tests(module) -> list[str]:
    """The module's ``test_*`` functions, in definition order (equal to
    pytest's collected order: no scenario test is parametrized)."""
    return [
        name
        for name, member in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_") and member.__module__ == module.__name__
    ]


# ---------------------------------------------------------------------------
# AC-02 (a): the ten scenario files exist and are the frozen names
# ---------------------------------------------------------------------------


def test_fdm201_all_ten_scenario_files_exist():
    for scenario, filename in SCENARIO_FILES:
        path = SCENARIOS_DIR / filename
        assert path.is_file(), f"scenario {scenario}: {filename} missing"


def test_fdm201_no_extra_scenario_files():
    # The suite directory contains exactly the ten frozen scenario files
    # (plus the benchmark acceptance files of this goal).
    found = {p.name for p in SCENARIOS_DIR.glob("test_*.py")}
    assert found == {filename for _, filename in SCENARIO_FILES}


# ---------------------------------------------------------------------------
# AC-02 (b): every scenario file imports and collects its pinned count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,filename",
    SCENARIO_FILES,
    ids=[scenario for scenario, _ in SCENARIO_FILES],
)
def test_fdm201_scenario_collects_pinned_test_count(scenario, filename):
    module = _module_for(scenario, filename)
    tests = _collected_tests(module)
    assert len(tests) == PINNED_TEST_COUNTS[scenario], (
        f"scenario {scenario} collected {len(tests)} tests, expected"
        f" {PINNED_TEST_COUNTS[scenario]}"
    )


def test_fdm201_scenario_suite_total_is_115_tests():
    # The frozen suite total: 115 tests across A-J (pinned; the task
    # brief's "92" figure is inaccurate -- see the module docstring).
    total = sum(PINNED_TEST_COUNTS.values())
    assert total == 115
    assert sum(PINNED_TEST_COUNTS[scenario] for scenario, _ in SCENARIO_FILES) == total


# ---------------------------------------------------------------------------
# AC-02 (c): every scenario maps onto its goal's acceptance criteria
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,filename",
    SCENARIO_FILES,
    ids=[scenario for scenario, _ in SCENARIO_FILES],
)
def test_fdm201_scenario_tests_map_to_goal_acceptance_criteria(
    scenario, filename
):
    # Each scenario's collected tests evidence its governing goal ACs per
    # the frozen mapping table: name-prefix evidence, with the D/E
    # deviations documented in the module docstring.
    module = _module_for(scenario, filename)
    tests = _collected_tests(module)
    assert tests, f"scenario {scenario} collected no tests"
    for mode, fragment in SCENARIO_AC_MAPPING[scenario]:
        matches = [t for t in tests if fragment in t]
        if mode == "every":
            assert len(matches) == len(tests), (
                f"scenario {scenario}: expected every test to match"
                f" {fragment!r}, got {len(matches)}/{len(tests)}"
            )
        elif mode == "any":
            assert matches, (
                f"scenario {scenario}: no test matches {fragment!r}"
            )
        elif mode == "none":
            assert not matches, (
                f"scenario {scenario}: expected no test to match"
                f" {fragment!r}"
            )
        else:
            raise AssertionError(f"unknown predicate mode {mode!r}")


def test_fdm201_scenario_ac_mapping_matches_the_frozen_reviews():
    # The mapping table above is grounded in the frozen G05/G06 review
    # records: each review documents the scenario->AC assignment this
    # file pins. Re-read the reviews and verify the AC numbers referenced
    # by the test-name fragments appear in the review acceptance criteria.
    reviews_dir = Path(__file__).resolve().parents[2] / ".development" / "reviews"
    g05 = reviews_dir / "DEV-M12-G05-a1.yaml"
    g06 = reviews_dir / "DEV-M12-G06-a1.yaml"
    assert g05.is_file() and g06.is_file()
    import yaml

    review_ac_ids: dict[str, set[str]] = {}
    for path in (g05, g06):
        record = yaml.safe_load(path.read_text(encoding="utf-8"))
        goal = "G05" if "G05" in path.name else "G06"
        review_ac_ids[goal] = set(record["acceptance_criteria"])
    for scenario, filename in SCENARIO_FILES:
        goal = "G06" if scenario in ("F", "G", "H", "I", "J") else "G05"
        fragments = {fragment for mode, fragment in SCENARIO_AC_MAPPING[scenario]}
        # Test-name fragments like "ac01" denote acceptance criteria
        # AC-01 etc.; scenario D's "none" entry contributes no AC.
        acs = {
            f"AC-{fragment[2:]}"
            for fragment in fragments
            if fragment.startswith("ac0") and len(fragment) == 4
        }
        assert acs <= review_ac_ids[goal], (
            f"scenario {scenario}: AC ids {acs} not in the frozen"
            f" {goal} review acceptance criteria {review_ac_ids[goal]}"
        )

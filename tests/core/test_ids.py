"""Tests for the deterministic ID API (DEV-M1-G01, acceptance AC-02).

Covered behaviors:
  * determinism: same kind + same parts -> identical ID, always;
  * format: ``sr_<kind>_<32 lowercase hex chars>``;
  * collision behavior: identical canonical inputs collide (by design) and
    non-canonical state does not change an ID; distinct inputs differ with
    overwhelming probability;
  * kind prefixing and validation of the documented input contract.
"""

from __future__ import annotations

import re

import pytest

from scientific_reproduction.core.ids import (
    ID_HEX_DIGITS,
    InvalidIdError,
    InvalidKindError,
    generate_id,
    is_valid_id,
    parse_id,
)

ID_RE = re.compile(r"^sr_[a-z0-9_-]+_[0-9a-f]{32}$")


def test_same_inputs_produce_identical_ids() -> None:
    first = generate_id("goal", "GOAL-001", "v1", "STRICT_REPRODUCTION")
    second = generate_id("goal", "GOAL-001", "v1", "STRICT_REPRODUCTION")
    assert first == second


def test_deterministic_across_repeated_calls() -> None:
    reference = generate_id("project", "RP-FDM201-001")
    for _ in range(100):
        assert generate_id("project", "RP-FDM201-001") == reference


def test_id_matches_documented_format() -> None:
    value = generate_id("goal", "GOAL-001")
    assert ID_RE.fullmatch(value) is not None
    assert value.startswith("sr_goal_")
    kind, digest = value[3:].split("_")
    assert kind == "goal"
    assert len(digest) == ID_HEX_DIGITS
    assert len(value) == len("sr_goal_") + ID_HEX_DIGITS


def test_kind_is_prefixed_into_id() -> None:
    assert generate_id("project", "RP-1").startswith("sr_project_")
    assert generate_id("run", "RUN-1").startswith("sr_run_")
    assert generate_id("evidence", "E-1").startswith("sr_evidence_")


def test_kind_with_hyphen_and_underscore_is_supported() -> None:
    value = generate_id("closure-contract", "CLC-1")
    assert value.startswith("sr_closure-contract_")
    assert generate_id("inventory_item", "INV-1").startswith("sr_inventory_item_")


def test_different_parts_produce_different_ids() -> None:
    ids = {
        generate_id("goal", "GOAL-001"),
        generate_id("goal", "GOAL-002"),
        generate_id("goal", "GOAL-001", "v1"),
        generate_id("goal", "GOAL-001", "v2"),
    }
    assert len(ids) == 4


def test_part_order_matters() -> None:
    assert generate_id("goal", "A", "B") != generate_id("goal", "B", "A")


def test_different_kinds_produce_different_ids() -> None:
    assert generate_id("goal", "X-1") != generate_id("run", "X-1")


def test_collision_behavior_identical_kind_and_parts() -> None:
    # Identical canonical inputs are the same object by definition and must
    # produce the identical ID -- this is the documented collision contract.
    a = generate_id("project", "RP-FDM201-001", "PLANNING")
    b = generate_id("project", "RP-FDM201-001", "PLANNING")
    assert a == b


def test_renamed_noncanonical_field_does_not_change_id() -> None:
    # The ID depends only on the documented canonical parts. A rename or
    # corruption of a non-canonical field (e.g. the title) cannot silently
    # change the ID of an already-committed object, because callers pass
    # only canonical fields to generate_id.
    committed = generate_id("project", "RP-FDM201-001")
    assert generate_id("project", "RP-FDM201-001") == committed
    # Changing a canonical part, by contrast, yields a different ID.
    renamed = generate_id("project", "RP-FDM201-001", "renamed")
    assert renamed != committed


def test_empty_parts_are_deterministic() -> None:
    assert generate_id("project") == generate_id("project")


def test_empty_string_part_is_allowed() -> None:
    assert generate_id("goal", "") == generate_id("goal", "")


@pytest.mark.parametrize(
    "kind",
    [
        "",  # empty
        "Project",  # uppercase
        "project plan",  # whitespace
        "1project",  # must start with a letter
        "projectä",  # non-ascii
        "p" * 33,  # too long
        "-project",  # leading hyphen
    ],
)
def test_invalid_kind_is_rejected(kind: str) -> None:
    with pytest.raises(InvalidKindError):
        generate_id(kind, "X")


def test_non_string_part_is_rejected() -> None:
    with pytest.raises(TypeError):
        generate_id("project", 123)  # type: ignore[arg-type]


def test_is_valid_id() -> None:
    good = generate_id("goal", "GOAL-001")
    assert is_valid_id(good)
    assert is_valid_id(good, kind="goal")
    assert not is_valid_id(good, kind="run")
    for bad in ["", "goal_abc123", "sr_goal_xyz", "sr_goal_12345678", "sr_goal"]:
        assert not is_valid_id(bad)


def test_is_valid_id_rejects_uppercase_hex() -> None:
    # Documented format is lowercase hex; a hand-crafted uppercase variant
    # is not a generated ID.
    assert not is_valid_id("sr_goal_" + "A" * 32)


def test_parse_id_round_trip() -> None:
    value = generate_id("evidence", "EVID-1", "CLAIM-1")
    kind, digest = parse_id(value)
    assert kind == "evidence"
    assert value == f"sr_{kind}_{digest}"


def test_parse_id_rejects_invalid_values() -> None:
    for bad in ["", "not-an-id", "sr_goal_123"]:
        with pytest.raises(InvalidIdError):
            parse_id(bad)


def test_ids_are_json_serializable_strings() -> None:
    import json

    value = generate_id("run", "RUN-001")
    assert json.loads(json.dumps(value)) == value

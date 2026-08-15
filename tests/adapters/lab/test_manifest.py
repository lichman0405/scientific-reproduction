"""Pure decision-layer tests: required-return coverage (result-manifest v1.1).

The pure evaluator (``adapters/lab/manifest.py``) decides whether a
returned Result Package can be collected. ``required_return`` entries
of the dispatched execution package are raw-data-export tokens, not
file names; this suite proves the v1.1 coverage rules:

* an explicit ``required_return_files`` mapping entry in the returned
  result manifest is the operator's declaration of coverage (AC-03 --
  never guessed from file stems or extensions): the mapped file must be
  declared in ``files`` and present in the incoming handoff;
* a required return without an explicit mapping entry stays on the v1.0
  exact-name rule (the token itself must be a declared file name);
* a mapping that is present but malformed is corrupt operator data,
  recorded as a missing manifest field -- never silently dropped;
* the assessment records the exact mapping the decision was computed
  from, and the typed :class:`LabResultManifest` record round-trips it.
"""

from __future__ import annotations

import pytest

from scientific_reproduction.adapters.lab.manifest import (
    RESULT_MANIFEST_RETURN_MAPPING_KEY,
    RESULT_MANIFEST_VERSION,
    LabResultManifest,
    ResultManifestError,
    evaluate_result_manifest,
)
from tests.adapters.lab.lab_helpers import (
    PACKAGE_ID,
    RUN_ID,
    make_result_manifest,
)

KWARGS = dict(
    expected_run_id=RUN_ID,
    expected_package_id=PACKAGE_ID,
    required_returns=("yield_pct", "product_photo"),
    present_files=("yield_pct.txt", "2026-08-15_product_photo.jpg"),
)


def _assess(manifest, **overrides):
    return evaluate_result_manifest(manifest, **{**KWARGS, **overrides})


# ---------------------------------------------------------------------------
# v1.1 -- explicit required_return_files mapping (never guessed)
# ---------------------------------------------------------------------------


def test_manifest_natural_filenames_cover_required_returns_via_mapping():
    # The issue's real-lab case: natural file names (extensioned,
    # timestamped) cover semantic required_return tokens through the
    # operator's explicit mapping -- no filename engineering needed.
    manifest = make_result_manifest(
        files=("yield_pct.txt", "2026-08-15_product_photo.jpg"),
        required_return_files={
            "yield_pct": "yield_pct.txt",
            "product_photo": "2026-08-15_product_photo.jpg",
        },
    )
    assessment = _assess(manifest)
    assert assessment.valid is True
    assert assessment.unmet_required_returns == ()
    assert assessment.required_return_files == (
        ("product_photo", "2026-08-15_product_photo.jpg"),
        ("yield_pct", "yield_pct.txt"),
    )


def test_manifest_mapping_covers_only_part_of_required_returns():
    # One required return mapped, the other not: only the unmapped one
    # is recorded as unmet (nothing is silently matched).
    manifest = make_result_manifest(
        files=("yield_pct.txt",),
        required_return_files={"yield_pct": "yield_pct.txt"},
    )
    assessment = _assess(manifest)
    assert assessment.valid is False
    assert assessment.unmet_required_returns == ("product_photo",)


def test_manifest_mapped_file_declared_but_absent_is_uncovered():
    # The mapped file is declared in files but absent from the handoff:
    # the absence is recorded as a missing declared file AND the required
    # return as uncovered -- a full decision record, never a silent default.
    manifest = make_result_manifest(
        files=("yield_pct.txt", "2026-08-15_product_photo.jpg"),
        required_return_files={
            "yield_pct": "yield_pct.txt",
            "product_photo": "2026-08-15_product_photo.jpg",
        },
    )
    assessment = _assess(
        manifest, present_files=("yield_pct.txt",)
    )
    assert assessment.valid is False
    assert assessment.missing_files == ("2026-08-15_product_photo.jpg",)
    assert assessment.unmet_required_returns == ("product_photo",)


def test_manifest_mapped_file_not_declared_is_uncovered():
    # The mapping may not smuggle in files: a mapped file that the
    # manifest never declares in files covers nothing (files stays the
    # canonical declaration of the returned data files).
    manifest = make_result_manifest(
        files=("yield_pct.txt",),
        required_return_files={
            "yield_pct": "yield_pct.txt",
            "product_photo": "product_photo.jpg",
        },
    )
    assessment = _assess(
        manifest,
        present_files=("yield_pct.txt", "product_photo.jpg"),
    )
    assert assessment.valid is False
    assert assessment.unmet_required_returns == ("product_photo",)


def test_manifest_mapping_overrides_the_exact_name_fallback():
    # An explicit mapping entry wins over the v1.0 exact-name rule: the
    # operator's declaration is honored, not the token-as-filename guess.
    manifest = make_result_manifest(
        files=("raw-data.xlsx",),
        required_return_files={"raw-data.csv": "raw-data.xlsx"},
    )
    assessment = _assess(
        manifest,
        required_returns=("raw-data.csv",),
        present_files=("raw-data.xlsx",),
    )
    assert assessment.valid is True
    assert assessment.unmet_required_returns == ()


def test_manifest_extra_mapping_entries_are_harmless():
    # Mapping entries for tokens the dispatch never required are surplus
    # declarations (like extra files in the handoff), not errors.
    manifest = make_result_manifest(
        files=("yield_pct.txt", "batch-log.txt"),
        required_return_files={
            "yield_pct": "yield_pct.txt",
            "batch_log": "batch-log.txt",
        },
    )
    assessment = _assess(
        manifest,
        required_returns=("yield_pct",),
        present_files=("yield_pct.txt", "batch-log.txt"),
    )
    assert assessment.valid is True
    assert assessment.unmet_required_returns == ()
    assert assessment.required_return_files == (
        ("batch_log", "batch-log.txt"),
        ("yield_pct", "yield_pct.txt"),
    )


# ---------------------------------------------------------------------------
# v1.0 fallback -- the exact-name rule still holds without a mapping
# ---------------------------------------------------------------------------


def test_manifest_unmapped_required_return_keeps_exact_name_rule():
    # Without a mapping, the v1.0 rule is unchanged: a required return
    # is covered only by a declared file whose name is exactly the token.
    manifest = make_result_manifest(files=("raw-data.csv",))
    assessment = _assess(
        manifest,
        required_returns=("raw-data.csv",),
        present_files=("raw-data.csv",),
    )
    assert assessment.valid is True
    assert assessment.unmet_required_returns == ()
    assert assessment.required_return_files == ()


def test_manifest_mapping_is_optional():
    # A v1.0 manifest without the optional mapping entry evaluates
    # exactly as before; the entry is not a required field.
    manifest = make_result_manifest(files=("raw-data.csv",))
    assert RESULT_MANIFEST_RETURN_MAPPING_KEY not in manifest
    assessment = _assess(
        manifest,
        required_returns=("raw-data.csv",),
        present_files=("raw-data.csv",),
    )
    assert assessment.valid is True
    assert assessment.missing_fields == ()


# ---------------------------------------------------------------------------
# Malformed mapping -- corrupt operator data, never silently dropped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed",
    [
        ["yield_pct.txt"],
        {"yield_pct": 7},
        {"yield_pct": ""},
        {"": "yield_pct.txt"},
        None,
    ],
)
def test_manifest_malformed_mapping_recorded_as_missing_field(malformed):
    manifest = make_result_manifest(files=("yield_pct.txt",))
    manifest[RESULT_MANIFEST_RETURN_MAPPING_KEY] = malformed
    assessment = _assess(manifest)
    assert assessment.valid is False
    assert RESULT_MANIFEST_RETURN_MAPPING_KEY in assessment.missing_fields


# ---------------------------------------------------------------------------
# The typed record -- LabResultManifest carries and validates the mapping
# ---------------------------------------------------------------------------


def test_manifest_record_roundtrips_the_mapping():
    manifest = make_result_manifest(
        files=("yield_pct.txt", "2026-08-15_product_photo.jpg"),
        required_return_files={
            "yield_pct": "yield_pct.txt",
            "product_photo": "2026-08-15_product_photo.jpg",
        },
    )
    record = LabResultManifest.from_dict(manifest)
    assert record.required_return_files == (
        ("product_photo", "2026-08-15_product_photo.jpg"),
        ("yield_pct", "yield_pct.txt"),
    )
    assert record.to_dict()[RESULT_MANIFEST_RETURN_MAPPING_KEY] == {
        "yield_pct": "yield_pct.txt",
        "product_photo": "2026-08-15_product_photo.jpg",
    }
    # The record round-trips through from_dict/to_dict unchanged.
    assert LabResultManifest.from_dict(record.to_dict()) == record


def test_manifest_record_without_mapping_stays_v1_0_shape():
    manifest = make_result_manifest(files=("raw-data.csv",))
    record = LabResultManifest.from_dict(manifest)
    assert record.required_return_files == ()
    assert record.to_dict()[RESULT_MANIFEST_RETURN_MAPPING_KEY] == {}
    assert LabResultManifest.from_dict(record.to_dict()) == record


def test_manifest_record_rejects_malformed_mapping():
    manifest = make_result_manifest(files=("yield_pct.txt",))
    manifest[RESULT_MANIFEST_RETURN_MAPPING_KEY] = ["yield_pct.txt"]
    with pytest.raises(TypeError):
        LabResultManifest.from_dict(manifest)
    manifest[RESULT_MANIFEST_RETURN_MAPPING_KEY] = {"yield_pct": ""}
    with pytest.raises(ResultManifestError):
        LabResultManifest.from_dict(manifest)


def test_manifest_record_constructor_validates_the_mapping_field():
    with pytest.raises(TypeError, match="required_return_files must be a tuple"):
        LabResultManifest(
            manifest_version=RESULT_MANIFEST_VERSION,
            package_id=PACKAGE_ID,
            project_id="sr_project_reference_flow_0001",
            goal_id="sr_goal_reference_flow_0001",
            run_id=RUN_ID,
            files=("raw-data.csv",),
            required_return_files=["raw-data.csv"],  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="entries must be"):
        LabResultManifest(
            manifest_version=RESULT_MANIFEST_VERSION,
            package_id=PACKAGE_ID,
            project_id="sr_project_reference_flow_0001",
            goal_id="sr_goal_reference_flow_0001",
            run_id=RUN_ID,
            files=("raw-data.csv",),
            required_return_files=(("raw-data.csv",),),  # type: ignore[arg-type]
        )
    with pytest.raises(ResultManifestError, match="non-empty strings"):
        LabResultManifest(
            manifest_version=RESULT_MANIFEST_VERSION,
            package_id=PACKAGE_ID,
            project_id="sr_project_reference_flow_0001",
            goal_id="sr_goal_reference_flow_0001",
            run_id=RUN_ID,
            files=("raw-data.csv",),
            required_return_files=(("raw-data.csv", ""),),
        )

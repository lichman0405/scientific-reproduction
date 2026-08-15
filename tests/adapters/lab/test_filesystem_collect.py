"""AC-02/AC-03 tests: returned Result Package collection (DEV-M7-G01).

Every collect test runs against an injected ``tmp_path`` handoff root
and proves that collection

* AC-02 -- associates the returned package with the **correct Run**: the
  result manifest's ``run_id`` must exactly match the Run of the
  dispatch; a package whose run reference is absent or belongs to a
  different Run is refused (never guessed, never silently matched);
* AC-03 -- **detects** missing required result-manifest entries, missing
  declared data files and uncovered required returns: the collection is
  rejected with a stable, specific error carrying a decision record of
  WHAT is missing (never a silent default), and the pure decision layer
  returns the same assessment without raising.
"""

from __future__ import annotations

import pytest

from scientific_reproduction.adapters.lab.base import (
    CollectionRejectedError,
    DispatchNotFoundError,
    LabAdapterDataError,
    ResultNotAvailableError,
)
from scientific_reproduction.adapters.lab.manifest import (
    RESULT_MANIFEST_REQUIRED_FIELDS,
    evaluate_result_manifest,
)
from tests.adapters.lab.lab_helpers import (
    OTHER_PACKAGE_ID,
    OTHER_RUN_ID,
    RUN_ID,
    make_package,
    make_result_manifest,
    write_result_package,
)


def _dispatch_default(adapter, **package_kwargs):
    """Dispatch the reference package (accepting overrides)."""
    return adapter.dispatch(make_package(**package_kwargs))


# ---------------------------------------------------------------------------
# AC-02 -- the returned package is associated with the correct Run
# ---------------------------------------------------------------------------


def test_collect_ac02_returned_package_associated_with_correct_run(handoff):
    # AC-02: collecting a returned package whose manifest carries exactly
    # the dispatch's run_id associates it with that Run.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(files=("raw-data.csv",)),
        files={"raw-data.csv": "1.0,2.0,3.0\n"},
    )
    result = adapter.collect(dispatch_id)
    assert result.dispatch_id == dispatch_id
    assert result.run_id == RUN_ID
    assert result.manifest.run_id == RUN_ID
    assert result.manifest.package_id == "sr_package_reference_flow_0001"
    assert result.collected_files == ("raw-data.csv",)
    assert result.manifest.files == ("raw-data.csv",)
    assert result.result_path == str(base / "incoming" / RUN_ID)


def test_collect_ac02_manifest_without_run_reference_refused(handoff):
    # AC-02: a returned package whose manifest carries NO run reference
    # is refused -- the Run is never guessed from the directory or any
    # other signal.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    manifest = make_result_manifest()
    del manifest["run_id"]
    write_result_package(base, RUN_ID, manifest, files={"raw-data.csv": "data\n"})
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assert "run reference" in str(exc.value)
    assert "never matched" in str(exc.value)
    assert exc.value.assessment.run_matched is False
    assert exc.value.assessment.run_id is None
    assert exc.value.assessment.expected_run_id == RUN_ID
    assert exc.value.assessment.valid is False


def test_collect_ac02_manifest_with_different_run_refused_never_matched(handoff):
    # AC-02: a returned package that belongs to a DIFFERENT Run is
    # refused; the actual and expected run ids are both recorded -- the
    # package is never silently matched to this Run.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(run_id=OTHER_RUN_ID),
        files={"raw-data.csv": "data\n"},
    )
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assert OTHER_RUN_ID in str(exc.value)
    assert RUN_ID in str(exc.value)
    assert exc.value.assessment.run_matched is False
    assert exc.value.assessment.run_id == OTHER_RUN_ID
    assert exc.value.assessment.expected_run_id == RUN_ID


def test_collect_ac02_manifest_with_different_package_refused(handoff):
    # AC-02: a returned package answering a DIFFERENT execution package
    # (same run, foreign package_id) is refused too.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(package_id=OTHER_PACKAGE_ID),
        files={"raw-data.csv": "data\n"},
    )
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assert OTHER_PACKAGE_ID in str(exc.value)
    assert exc.value.assessment.package_matched is False


# ---------------------------------------------------------------------------
# AC-03 -- missing required result manifest/data is detected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", RESULT_MANIFEST_REQUIRED_FIELDS)
def test_collect_ac03_missing_required_manifest_field_detected(handoff, field):
    # AC-03: every required result-manifest entry is DETECTED -- the
    # collection is rejected with the exact missing field recorded.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    manifest = make_result_manifest()
    del manifest[field]
    write_result_package(base, RUN_ID, manifest, files={"raw-data.csv": "data\n"})
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assert field in exc.value.assessment.missing_fields
    assert field in str(exc.value)
    assert exc.value.assessment.valid is False


def test_collect_ac03_missing_declared_data_file_detected(handoff):
    # AC-03: a data file the result manifest DECLARES but the incoming
    # handoff does not contain is detected by name.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(files=("raw-data.csv", "log.csv")),
        files={"raw-data.csv": "data\n"},
    )
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assert exc.value.assessment.missing_files == ("log.csv",)
    assert "log.csv" in str(exc.value)
    assert exc.value.assessment.valid is False


def test_collect_ac03_unmet_required_return_detected(handoff):
    # AC-03: a required return declared by the dispatched execution
    # package (required_return) that the returned files do not cover is
    # detected by name.
    base, adapter = handoff
    dispatch_id = _dispatch_default(
        adapter, required_return=("raw-data.csv", "summary.csv")
    ).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(files=("raw-data.csv",)),
        files={"raw-data.csv": "data\n"},
    )
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assert exc.value.assessment.unmet_required_returns == ("summary.csv",)
    assert "summary.csv" in str(exc.value)
    assert exc.value.assessment.valid is False


def test_collect_ac03_detection_is_a_decision_record_not_a_silent_default(handoff):
    # AC-03: the detection is a decision record -- the PURE evaluator
    # returns the exact same assessment without raising, and the refused
    # collection carries it; nothing is defaulted and nothing is silent.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    manifest = make_result_manifest(files=("raw-data.csv", "log.csv"))
    write_result_package(
        base,
        RUN_ID,
        manifest,
        files={"raw-data.csv": "data\n"},
    )
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    refused = exc.value
    assert refused.assessment.valid is False
    assert refused.assessment.missing_files == ("log.csv",)
    pure = evaluate_result_manifest(
        manifest,
        expected_run_id=RUN_ID,
        expected_package_id="sr_package_reference_flow_0001",
        required_returns=("raw-data.csv",),
        present_files=("raw-data.csv",),
    )
    assert pure == refused.assessment
    assert pure.missing_files == ("log.csv",)


def test_collect_ac03_multiple_missing_items_all_recorded(handoff):
    # AC-03: every deficiency is recorded at once -- missing field(s),
    # missing file(s) and uncovered required return(s) in one decision.
    base, adapter = handoff
    dispatch_id = _dispatch_default(
        adapter, required_return=("raw-data.csv", "summary.csv")
    ).dispatch_id
    manifest = make_result_manifest(files=("raw-data.csv", "log.csv"))
    del manifest["project_id"]
    write_result_package(base, RUN_ID, manifest, files={"raw-data.csv": "data\n"})
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assessment = exc.value.assessment
    assert assessment.missing_fields == ("project_id",)
    assert assessment.missing_files == ("log.csv",)
    assert assessment.unmet_required_returns == ("summary.csv",)
    assert assessment.valid is False


def test_collect_ac03_natural_filenames_cover_required_returns_via_mapping(handoff):
    # AC-03: a required return is a raw-data-export TOKEN, not a file
    # name. Real-lab file names (extensioned, timestamped) cover the
    # tokens through the result manifest's explicit required_return_files
    # mapping -- the operator's declaration, never guessed (v1.1).
    base, adapter = handoff
    dispatch_id = _dispatch_default(
        adapter, required_return=("yield_pct", "product_photo")
    ).dispatch_id
    files = ("yield_pct.txt", "2026-08-15_product_photo.jpg")
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(
            files=files,
            required_return_files={
                "yield_pct": "yield_pct.txt",
                "product_photo": "2026-08-15_product_photo.jpg",
            },
        ),
        files={
            "yield_pct.txt": "0.984\n",
            "2026-08-15_product_photo.jpg": b"jpeg",
        },
    )
    result = adapter.collect(dispatch_id)
    assert result.collected_files == tuple(sorted(files))
    assert result.manifest.required_return_files == (
        ("product_photo", "2026-08-15_product_photo.jpg"),
        ("yield_pct", "yield_pct.txt"),
    )


def test_collect_ac03_mapped_return_file_missing_detected(handoff):
    # AC-03: a required return whose mapped file is declared but absent
    # from the handoff is refused with BOTH records -- the missing
    # declared file and the uncovered required return.
    base, adapter = handoff
    dispatch_id = _dispatch_default(
        adapter, required_return=("yield_pct",)
    ).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(
            files=("yield_pct.txt",),
            required_return_files={"yield_pct": "yield_pct.txt"},
        ),
        files={},
    )
    with pytest.raises(CollectionRejectedError) as exc:
        adapter.collect(dispatch_id)
    assessment = exc.value.assessment
    assert assessment.missing_files == ("yield_pct.txt",)
    assert assessment.unmet_required_returns == ("yield_pct",)
    assert assessment.valid is False
    assert "yield_pct" in str(exc.value)


def test_collect_ac03_collect_before_result_raises_not_available(handoff):
    # Collecting a dispatch that has not returned a Result Package yet is
    # a defined "not available" state, not a collection.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    assert not (base / "incoming" / RUN_ID).exists()
    with pytest.raises(ResultNotAvailableError) as exc:
        adapter.collect(dispatch_id)
    assert "no result package returned" in str(exc.value)
    assert "result-manifest.json" in str(exc.value)


# ---------------------------------------------------------------------------
# Boundary behavior
# ---------------------------------------------------------------------------


def test_collect_unknown_dispatch_raises(handoff):
    base, adapter = handoff
    assert not (base / "outgoing").exists()
    with pytest.raises(DispatchNotFoundError) as exc:
        adapter.collect("sr_dispatch_unknown_00000000000000000000000000000000")
    assert "no dispatch with id" in str(exc.value)


def test_collect_corrupt_result_manifest_refused(handoff):
    # An unparseable returned result manifest is corrupt operator data:
    # refused with a stable adapter data error, nothing collected.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    incoming = base / "incoming" / RUN_ID
    incoming.mkdir(parents=True)
    (incoming / "result-manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LabAdapterDataError) as exc:
        adapter.collect(dispatch_id)
    assert "corrupt lab result manifest" in str(exc.value)


def test_collect_manifest_declaring_extra_unknown_file_is_not_required(handoff):
    # Extra files beyond the declaration are allowed: only what the
    # manifest declares must be present; a declared-and-present file
    # suffices.
    base, adapter = handoff
    dispatch_id = _dispatch_default(adapter).dispatch_id
    write_result_package(
        base,
        RUN_ID,
        make_result_manifest(files=("raw-data.csv",)),
        files={"raw-data.csv": "data\n", "unexpected.jpg": b"jpeg"},
    )
    result = adapter.collect(dispatch_id)
    assert result.collected_files == ("raw-data.csv", "unexpected.jpg")
    assert result.manifest.files == ("raw-data.csv",)

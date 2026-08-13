"""Tests for the LabAdapter contract and record vocabulary (DEV-M7-G01).

Interface shape -- the abstract ``LabAdapter`` contract declares exactly
the four minimum conceptual operations of 15-ADAPTER-SPEC.md SS2
(``dispatch`` / ``status`` / ``collect`` /
``request_missing_result``), every public boundary rejects wrong
argument types with ``TypeError``, and every record is a frozen
dataclass. Determinism -- the ``dispatch_id`` is a pure function of the
package identity, and the manifest-evaluation module is pure (source
inspection: no I/O, no randomness, no wall clock, no network anywhere).
"""

from __future__ import annotations

import inspect
from abc import ABC
from dataclasses import FrozenInstanceError

import pytest

from scientific_reproduction.adapters.lab import manifest as manifest_module
from scientific_reproduction.adapters.lab.base import (
    LAB_ADAPTER_CONTRACT_VERSION,
    CollectionRejectedError,
    DispatchRecord,
    DispatchState,
    DispatchStatus,
    DuplicateDispatchError,
    LabAdapter,
    LabAdapterDataError,
    LabAdapterError,
    ResultNotAvailableError,
    is_safe_path_segment,
)
from scientific_reproduction.adapters.lab.filesystem import FilesystemLabAdapter
from scientific_reproduction.adapters.lab.manifest import (
    ResultManifestAssessment,
    evaluate_result_manifest,
)
from tests.adapters.lab.lab_helpers import RUN_ID, make_package, make_result_manifest

# ---------------------------------------------------------------------------
# Interface shape (15-ADAPTER-SPEC.md SS2)
# ---------------------------------------------------------------------------


def test_contract_lab_adapter_is_abstract_interface():
    """LabAdapter is an ABC declaring the four conceptual operations."""
    assert issubclass(LabAdapter, ABC)
    assert LabAdapter.__abstractmethods__ == frozenset(
        {"dispatch", "status", "collect", "request_missing_result"}
    )
    with pytest.raises(TypeError):
        LabAdapter()  # type: ignore[abstract]


def test_contract_operations_match_the_frozen_adapter_spec():
    # 15-ADAPTER-SPEC.md SS2 lists exactly dispatch(execution_package),
    # status(dispatch_id), collect(dispatch_id) and
    # request_missing_result(dispatch_id, manifest_requirements).
    for name in ("dispatch", "status", "collect", "request_missing_result"):
        assert name in LabAdapter.__abstractmethods__
    contract_version = LAB_ADAPTER_CONTRACT_VERSION
    assert contract_version == "1.0"


def test_contract_filesystem_adapter_declares_stable_identity():
    adapter = FilesystemLabAdapter("x", outgoing="outgoing", incoming="incoming")
    assert adapter.adapter_id == "filesystem"
    assert adapter.version == "1.0"
    assert adapter.description


# ---------------------------------------------------------------------------
# Error hierarchy (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


def test_contract_errors_are_valueerror_subclasses():
    for error in (
        LabAdapterError,
        LabAdapterDataError,
        DuplicateDispatchError,
        ResultNotAvailableError,
        CollectionRejectedError,
    ):
        assert issubclass(error, ValueError)
    for error in (
        LabAdapterDataError,
        DuplicateDispatchError,
        ResultNotAvailableError,
        CollectionRejectedError,
    ):
        assert issubclass(error, LabAdapterError)


# ---------------------------------------------------------------------------
# Records (frozen dataclasses)
# ---------------------------------------------------------------------------


def test_contract_records_are_frozen():
    record = DispatchRecord(
        dispatch_id="sr_dispatch_x",
        package_id="pkg",
        project_id="prj",
        goal_id="goal",
        run_id="run",
        outgoing_path="/handoff/outgoing/run",
    )
    with pytest.raises(FrozenInstanceError):
        setattr(record, "run_id", "other")
    status = DispatchStatus(
        dispatch_id="sr_dispatch_x", run_id="run", state=DispatchState.RUNNING_EXTERNAL
    )
    with pytest.raises(FrozenInstanceError):
        setattr(status, "state", DispatchState.RESULT_AVAILABLE)
    assessment = evaluate_result_manifest(
        make_package(),
        expected_run_id=RUN_ID,
        expected_package_id="sr_package_reference_flow_0001",
        required_returns=(),
        present_files=(),
    )
    with pytest.raises(FrozenInstanceError):
        setattr(assessment, "valid", True)


def test_contract_dispatch_state_values_are_frozen_lifecycle_values():
    # DispatchState values come from core.models.LifecycleState --
    # nothing invented here.
    assert DispatchState.RUNNING_EXTERNAL.value == "RUNNING_EXTERNAL"
    assert DispatchState.RESULT_AVAILABLE.value == "RESULT_AVAILABLE"


# ---------------------------------------------------------------------------
# Boundaries (TypeError for wrong argument types)
# ---------------------------------------------------------------------------


def test_contract_boundaries_reject_wrong_types(tmp_path):
    adapter = FilesystemLabAdapter(tmp_path / "lab")
    with pytest.raises(TypeError, match="base_dir must be a str or Path"):
        FilesystemLabAdapter(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dispatch expects a LabExecutionPackage"):
        adapter.dispatch("not a package")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dispatch expects a LabExecutionPackage"):
        adapter.dispatch(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dispatch_id must be a str"):
        adapter.status(7)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="dispatch_id must be a str"):
        adapter.collect(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="manifest_requirements must be a sequence"):
        adapter.request_missing_result("sr_dispatch_x", "raw-data.csv")
    with pytest.raises(TypeError, match="artifacts must be a mapping"):
        adapter.dispatch(make_package(), artifacts=["a.csv"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_contract_dispatch_id_is_a_pure_function_of_package_identity(tmp_path):
    # Identical packages dispatched through two independent adapters on
    # two independent handoff roots yield the same dispatch_id.
    first = FilesystemLabAdapter(tmp_path / "a")
    second = FilesystemLabAdapter(tmp_path / "b")
    package = make_package()
    record_a = first.dispatch(package)
    record_b = second.dispatch(package)
    assert record_a.dispatch_id == record_b.dispatch_id
    assert record_a.dispatch_id.startswith("sr_dispatch_")
    assert record_a.run_id == record_b.run_id == RUN_ID


def test_contract_safe_path_segment_vocabulary():
    for safe in ("run-1", "run_1", "raw-data.csv", "a"):
        assert is_safe_path_segment(safe), safe
    for unsafe in ("", ".", "..", "a/b", "a\\b", "a*b", "a?b", "a[b]"):
        assert not is_safe_path_segment(unsafe), unsafe


# ---------------------------------------------------------------------------
# Purity (the decision layer has no I/O, no randomness, no wall clock)
# ---------------------------------------------------------------------------


def test_contract_manifest_module_is_pure_no_io_no_randomness():
    source = inspect.getsource(manifest_module)
    for forbidden in (
        "open(",
        "os.",
        "pathlib",
        "random",
        "time.",
        "datetime",
        "urllib",
        "requests",
        "socket",
        "atomic_write",
        "import json",
    ):
        assert forbidden not in source, forbidden
    for name in ("os", "sys", "time", "datetime", "random", "socket", "json", "pathlib"):
        assert name not in manifest_module.__dict__, name


def test_contract_filesystem_adapter_only_touches_injected_paths():
    # The adapter derives every path from the injected base_dir and the
    # outgoing/incoming directory names; nothing hidden, no wall clock,
    # no randomness, no network.
    import scientific_reproduction.adapters.lab.filesystem as filesystem_module

    source = inspect.getsource(filesystem_module)
    for forbidden in (
        "random.",
        "time.time",
        "datetime.now",
        "timezone",
        "os.getcwd",
        "Path.home",
        "environ",
        "getenv",
        "urllib",
        "import requests",
        "requests.",
        "socket",
        "tempfile",
    ):
        assert forbidden not in source, forbidden


def test_contract_new_modules_have_no_ignores_or_noqa():
    import scientific_reproduction.adapters.lab.base as base_module
    import scientific_reproduction.adapters.lab.filesystem as filesystem_module

    for module in (base_module, manifest_module, filesystem_module):
        source = inspect.getsource(module)
        assert "type: ignore" not in source, module.__name__
        assert "# noqa" not in source, module.__name__


def test_contract_assessment_rejects_wrong_types():
    kwargs = dict(
        expected_run_id="run",
        expected_package_id="pkg",
        required_returns=(),
        present_files=(),
    )
    with pytest.raises(TypeError, match="Mapping manifest"):
        evaluate_result_manifest("not a manifest", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expected_run_id must be a str"):
        evaluate_result_manifest(
            make_result_manifest(),
            expected_run_id=7,  # type: ignore[arg-type]
            expected_package_id="pkg",
            required_returns=(),
            present_files=(),
        )
    with pytest.raises(TypeError, match="expected_package_id must be a str"):
        evaluate_result_manifest(
            make_result_manifest(),
            expected_run_id="run",
            expected_package_id=None,  # type: ignore[arg-type]
            required_returns=(),
            present_files=(),
        )
    with pytest.raises(TypeError, match="required_returns must be a sequence"):
        evaluate_result_manifest(
            make_result_manifest(),
            expected_run_id="run",
            expected_package_id="pkg",
            required_returns="csv",  # type: ignore[arg-type]
            present_files=(),
        )
    with pytest.raises(TypeError, match="present_files must be a sequence"):
        evaluate_result_manifest(
            make_result_manifest(),
            expected_run_id="run",
            expected_package_id="pkg",
            required_returns=(),
            present_files="csv",  # type: ignore[arg-type]
        )


def test_contract_assessment_is_a_stable_decision_record():
    manifest = make_result_manifest(files=("raw-data.csv",))
    assessment = evaluate_result_manifest(
        manifest,
        expected_run_id=RUN_ID,
        expected_package_id="sr_package_reference_flow_0001",
        required_returns=("raw-data.csv",),
        present_files=("raw-data.csv",),
    )
    assert isinstance(assessment, ResultManifestAssessment)
    assert assessment.valid is True
    assert assessment.ruleset_version == "1.0"
    assert assessment.run_matched is True
    assert assessment.package_matched is True
    assert assessment.missing_fields == ()
    assert assessment.missing_files == ()
    assert assessment.unmet_required_returns == ()
    assert assessment.run_id == RUN_ID
    assert assessment.expected_run_id == RUN_ID
    # Identical inputs -> identical decision records.
    again = evaluate_result_manifest(
        manifest,
        expected_run_id=RUN_ID,
        expected_package_id="sr_package_reference_flow_0001",
        required_returns=("raw-data.csv",),
        present_files=("raw-data.csv",),
    )
    assert again == assessment
    assert repr(again) == repr(assessment)

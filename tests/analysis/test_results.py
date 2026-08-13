"""Analysis Result Package records with trace links (DEV-M9-G02).

Every test name contains "result" so ``python -m pytest -q
tests/analysis -k result`` selects the whole suite. The ``ac01``/``ac02``/
``ac03`` sections map one-to-one to the acceptance criteria of DEV-M9-G02:

* ``ac01`` -- the result record names the exact input Run/artifact refs:
  the stored ``run_ref`` equals the input Run record's ``run_id`` and the
  ``input_artifact_ids`` are the exact ids of the registered artifact
  manifests the analysis consumed; a drifted reference that does not
  resolve to a registered entity (artifact, protocol version or
  acceptance record) is rejected with a stable
  ``UnresolvedResultReferenceError`` at registration;
* ``ac02`` -- the protocol version is immutable in the result record: the
  frozen dataclass rejects any mutation of ``protocol_version`` and the
  registry writes each ``result_id`` exactly once -- re-registration, even
  with a different ``protocol_version``, raises ``DuplicateResultError``
  and the original file stays byte untouched;
* ``ac03`` -- registering a result never closes a Requirement: the record
  only carries ``requirement_refs`` (pure linkage), the registration never
  touches requirement state, and the referenced requirement record stays
  byte untouched with its outcome ``OPEN``.

The deterministic path mirrors ``protocol_helpers``: every fixture uses
fixed identities/timestamps (``FROZEN_AT``), so all records are
deterministic. Fixtures are self-contained in this file.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from protocol_helpers import FROZEN_AT, init_project, make_protocol

import scientific_reproduction.analysis.results as results_module
from scientific_reproduction.analysis.protocols import (
    freeze_primary_protocol,
    list_protocol_versions,
    read_protocol_version,
    register_analysis_record,
)
from scientific_reproduction.analysis.results import (
    ARTIFACTS_STATE_DIR,
    RESULTS_STATE_DIR,
    DuplicateResultError,
    InvalidResultIdError,
    ResultNotFoundError,
    ResultRecord,
    ResultRecordError,
    ResultRegistryError,
    UnresolvedResultReferenceError,
    list_results,
    read_result,
    register_result,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    ArtifactManifest,
    Criticality,
    DecisionMode,
    InventoryItemType,
    LifecycleState,
    MappingStatus,
    PrimaryOrExploratory,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    Run,
    RunType,
)
from scientific_reproduction.planning.init import ProjectNotInitializedError
from scientific_reproduction.planning.inventory import (
    read_requirement,
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    read_acceptance,
    read_analysis_protocol,
    register_acceptance,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_manifest(artifact_id: str, *, run_id: str = "RUN-001") -> ArtifactManifest:
    """Build a schema-valid artifact manifest (no file access)."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=f"file:///raw/{artifact_id}.csv",
        sha256="a" * 64,
        size_bytes=1024,
        created_at="2026-01-01T00:00:00Z",
        run_id=run_id,
    )


def make_acceptance(
    acceptance_id: str, *, goal_id: str = "G-1"
) -> AcceptanceCriteria:
    """Build a schema-valid acceptance criteria record."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=goal_id,
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "batch_level_uptake", "margin": 0.1}],
    )


def make_run(run_id: str = "RUN-001") -> Run:
    """Build the input Run record whose ``run_id`` a result must name."""
    return Run(
        run_id=run_id,
        goal_id="G-1",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.RESULT_AVAILABLE,
        goal_version="v1",
        artifacts=["ART-001"],
    )


def make_result_record(result_id: str, **kwargs: Any) -> ResultRecord:
    """Build a schema-valid result record with compact defaults."""
    defaults: dict[str, Any] = {
        "analysis_id": "ANL-1",
        "protocol_version": "v1",
        "run_ref": "RUN-001",
        "input_artifact_ids": ["ART-001"],
        "primary_or_exploratory": PrimaryOrExploratory.PRIMARY,
        "acceptance_ref": "ACC-1",
    }
    defaults.update(kwargs)
    return ResultRecord(result_id=result_id, **defaults)


def build_result_workspace(tmp_path: Path) -> Path:
    """Initialize a project with the registered entities a result references.

    Registers, deterministically: the frozen PRIMARY protocol ``ANL-1``
    ``v1`` (DEV-M9-G01 registry), the raw artifact manifest ``ART-001``
    (the project ``manifests/`` artifact registry) and the acceptance
    criteria ``ACC-1``.
    """
    root = init_project(tmp_path)
    register_analysis_record(root, make_protocol("ANL-1"))
    draft = read_analysis_protocol(root, "ANL-1")
    freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    ArtifactRegistry(root / ARTIFACTS_STATE_DIR).register(make_manifest("ART-001"))
    register_acceptance(root, make_acceptance("ACC-1"))
    return root


def register_raw_artifact(root: Path, artifact_id: str) -> None:
    """Register one raw artifact manifest under ``manifests/``."""
    ArtifactRegistry(root / ARTIFACTS_STATE_DIR).register(
        make_manifest(artifact_id, run_id="RUN-001")
    )


def register_requirement_fixture(root: Path, requirement_id: str = "REQ-1") -> Path:
    """Register an OPEN requirement (and its inventory item) at ``requirements/``.

    Returns the requirement state file path so tests can prove the bytes
    are untouched by result registration (AC-03).
    """
    register_inventory_item(
        root,
        ReproductionInventoryItem(
            inventory_id="INV-1",
            source_id="SRC-1",
            item_type=InventoryItemType.DATASET,
            formal_report=True,
            description="raw isotherm dataset",
            mapping_status=MappingStatus.UNMAPPED,
        ),
    )
    register_requirement(
        root,
        ReproductionRequirement(
            requirement_id=requirement_id,
            statement="reproduce the reported uptake isotherm",
            inventory_items=["INV-1"],
            criticality=Criticality.CRITICAL,
            goal_ids=["G-1"],
            outcome=RequirementOutcome.OPEN,
        ),
    )
    return root / "requirements" / f"{requirement_id}.json"


def _canonical(data: dict) -> str:
    """The registry's canonical JSON serialization."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# AC-01: exact input Run/artifact refs (validated against registered entities)
# ---------------------------------------------------------------------------


def test_result_ac01_names_exact_registered_artifact_refs(tmp_path):
    root = build_result_workspace(tmp_path)
    register_raw_artifact(root, "ART-002")
    registered = register_result(
        root, make_result_record("RES-1", input_artifact_ids=["ART-002", "ART-001"])
    )
    stored = read_result(root, "RES-1")
    # The record names the exact artifact ids of the registered manifests
    # the analysis consumed -- not copied-by-value strings that drift.
    assert registered.input_artifact_ids == ["ART-002", "ART-001"]
    assert stored == registered
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    for artifact_id in stored.input_artifact_ids:
        assert registry.get(artifact_id).artifact_id == artifact_id
    raw = (root / RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    assert '"input_artifact_ids": [' in raw
    assert "ART-001" in raw and "ART-002" in raw


def test_result_ac01_run_ref_is_the_exact_input_run_id(tmp_path):
    root = build_result_workspace(tmp_path)
    run = make_run("RUN-001")
    registered = register_result(root, make_result_record("RES-1", run_ref=run.run_id))
    # AC-01: the result names the exact Run ref -- the run_id of the Run
    # record the analysis consumed.
    assert registered.run_ref == run.run_id
    stored = read_result(root, "RES-1")
    assert stored.run_ref == "RUN-001"
    raw = (root / RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    assert '"run_ref": "RUN-001"' in raw


def test_result_ac01_unregistered_artifact_ref_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(UnresolvedResultReferenceError) as exc:
        register_result(
            root, make_result_record("RES-1", input_artifact_ids=["ART-NOPE"])
        )
    message = str(exc.value)
    assert "RES-1" in message
    assert "ART-NOPE" in message
    assert "not registered" in message
    # Nothing was written.
    assert not (root / RESULTS_STATE_DIR / "RES-1.json").exists()


def test_result_ac01_unregistered_output_artifact_ref_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(UnresolvedResultReferenceError) as exc:
        register_result(
            root, make_result_record("RES-1", output_artifact_ids=["ART-GHOST"])
        )
    assert "ART-GHOST" in str(exc.value)


def test_result_ac01_registered_output_artifact_ref_accepted(tmp_path):
    root = build_result_workspace(tmp_path)
    register_raw_artifact(root, "FIG-1")
    registered = register_result(
        root,
        make_result_record(
            "RES-1", input_artifact_ids=["ART-001"], output_artifact_ids=["FIG-1"]
        ),
    )
    assert registered.output_artifact_ids == ["FIG-1"]


def test_result_ac01_unregistered_protocol_version_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(UnresolvedResultReferenceError) as exc:
        register_result(
            root, make_result_record("RES-1", protocol_version="v9")
        )
    message = str(exc.value)
    assert "RES-1" in message
    assert "v9" in message
    assert "ANL-1" in message
    assert not (root / RESULTS_STATE_DIR / "RES-1.json").exists()


def test_result_ac01_result_kind_record_rejected_as_protocol_ref(tmp_path):
    root = build_result_workspace(tmp_path)
    # Hand-write a RESULT-kind record into the protocol lineage at ANL-R@v1
    # (canonical JSON, exactly like the versioned protocol registry).
    result_kind = make_protocol("ANL-R", protocol_version="v1", frozen=True).to_dict()
    result_kind["kind"] = AnalysisKind.RESULT.value
    versions_dir = root / "protocols" / "versions"
    versions_dir.mkdir(exist_ok=True)
    (versions_dir / "ANL-R@v1.json").write_text(_canonical(result_kind), encoding="utf-8")
    assert read_protocol_version(root, "ANL-R", "v1").record.kind is AnalysisKind.RESULT
    with pytest.raises(UnresolvedResultReferenceError) as exc:
        register_result(
            root,
            make_result_record("RES-1", analysis_id="ANL-R", protocol_version="v1"),
        )
    message = str(exc.value)
    assert "ANL-R" in message
    assert "not an analysis protocol record" in message


def test_result_ac01_unregistered_acceptance_ref_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(UnresolvedResultReferenceError) as exc:
        register_result(
            root, make_result_record("RES-1", acceptance_ref="ACC-NOPE")
        )
    message = str(exc.value)
    assert "RES-1" in message
    assert "ACC-NOPE" in message
    assert not (root / RESULTS_STATE_DIR / "RES-1.json").exists()


def test_result_ac01_registered_acceptance_ref_resolved(tmp_path):
    root = build_result_workspace(tmp_path)
    registered = register_result(root, make_result_record("RES-1"))
    assert registered.acceptance_ref == "ACC-1"
    assert read_acceptance(root, registered.acceptance_ref).acceptance_id == "ACC-1"


def test_result_ac01_without_acceptance_ref_accepted(tmp_path):
    root = build_result_workspace(tmp_path)
    registered = register_result(root, make_result_record("RES-1", acceptance_ref=None))
    assert registered.acceptance_ref is None
    # None is omitted from the canonical stored bytes (to_dict convention).
    raw = (root / RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    assert '"acceptance_ref"' not in raw


# ---------------------------------------------------------------------------
# AC-02: protocol version is immutable in the result record
# ---------------------------------------------------------------------------


def test_result_ac02_protocol_version_immutable_in_memory(tmp_path):
    root = build_result_workspace(tmp_path)
    record = make_result_record("RES-1")
    with pytest.raises(FrozenInstanceError):
        record.protocol_version = "v2"
    registered = register_result(root, record)
    with pytest.raises(FrozenInstanceError):
        registered.protocol_version = "v9"
    stored = read_result(root, "RES-1")
    with pytest.raises(FrozenInstanceError):
        stored.protocol_version = "v2"
    with pytest.raises(FrozenInstanceError):
        stored.run_ref = "RUN-2"
    assert stored.protocol_version == "v1"


def test_result_ac02_duplicate_registration_rejected_bytes_untouched(tmp_path):
    root = build_result_workspace(tmp_path)
    register_result(root, make_result_record("RES-1"))
    state_path = root / RESULTS_STATE_DIR / "RES-1.json"
    before = state_path.read_bytes()
    with pytest.raises(DuplicateResultError) as exc:
        register_result(root, make_result_record("RES-1"))
    assert "RES-1" in str(exc.value)
    assert "already registered" in str(exc.value)
    # AC-02: the original record is never rewritten (no-clobber handling).
    assert state_path.read_bytes() == before
    assert read_result(root, "RES-1").protocol_version == "v1"


def test_result_ac02_re_registration_with_different_version_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    register_result(root, make_result_record("RES-1", protocol_version="v1"))
    state_path = root / RESULTS_STATE_DIR / "RES-1.json"
    before = state_path.read_bytes()
    with pytest.raises(DuplicateResultError) as exc:
        register_result(root, make_result_record("RES-1", protocol_version="v9"))
    message = str(exc.value)
    assert "RES-1" in message
    assert "exactly once" in message
    # AC-02: the registered protocol version cannot be mutated by
    # re-registration -- the stored record keeps the original version.
    assert state_path.read_bytes() == before
    assert read_result(root, "RES-1").protocol_version == "v1"


def test_result_ac02_duplicate_error_is_stable_across_attempts(tmp_path):
    root = build_result_workspace(tmp_path)
    register_result(root, make_result_record("RES-1"))
    with pytest.raises(DuplicateResultError) as first:
        register_result(root, make_result_record("RES-1"))
    with pytest.raises(DuplicateResultError) as second:
        register_result(root, make_result_record("RES-1"))
    assert str(first.value) == str(second.value)


def test_result_ac02_stored_record_is_immutable_replacement_never_written(tmp_path):
    root = build_result_workspace(tmp_path)
    register_result(root, make_result_record("RES-1", protocol_version="v1"))
    state_path = root / RESULTS_STATE_DIR / "RES-1.json"
    # Any write path that would replace the record is rejected up front:
    # the registry is exactly-once per result id.
    tampered = replace(read_result(root, "RES-1"), protocol_version="v2")
    with pytest.raises(DuplicateResultError):
        register_result(root, tampered)
    assert state_path.read_bytes() == _canonical(
        make_result_record("RES-1", protocol_version="v1").to_dict()
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# AC-03: registering a result never closes a Requirement
# ---------------------------------------------------------------------------


def test_result_ac03_registration_leaves_referenced_requirement_open(tmp_path):
    root = build_result_workspace(tmp_path)
    requirement_path = register_requirement_fixture(root, "REQ-1")
    before = requirement_path.read_bytes()
    register_result(
        root,
        make_result_record("RES-1", requirement_refs=["REQ-1"]),
    )
    # AC-03: the requirement record is byte untouched and still OPEN --
    # registering a result performs no closure by itself.
    assert requirement_path.read_bytes() == before
    stored_requirement = read_requirement(root, "REQ-1")
    assert stored_requirement.requirement_id == "REQ-1"
    assert stored_requirement.outcome is RequirementOutcome.OPEN


def test_result_ac03_exposes_requirement_linkage_without_closure(tmp_path):
    root = build_result_workspace(tmp_path)
    register_requirement_fixture(root, "REQ-1")
    registered = register_result(
        root,
        make_result_record("RES-1", requirement_refs=["REQ-1", "REQ-2"]),
    )
    # The linkage is exposed on the record for a later closure flow.
    assert registered.requirement_refs == ["REQ-1", "REQ-2"]
    stored = read_result(root, "RES-1")
    assert stored.requirement_refs == ["REQ-1", "REQ-2"]
    # The module performs no closure: the result package never imports the
    # requirement/outcome layers (close semantics stay with the
    # requirement/outcome layer; only the linkage is exposed).
    source = inspect.getsource(results_module)
    assert "register_requirement" not in source
    assert "from scientific_reproduction.core.rules" not in source
    assert "from scientific_reproduction.planning.inventory" not in source


def test_result_ac03_result_requires_no_requirement_at_all(tmp_path):
    root = build_result_workspace(tmp_path)
    registered = register_result(root, make_result_record("RES-1"))
    assert registered.requirement_refs == []
    assert "requirement_refs" in json.loads(
        (root / RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Paradigm: registration, canonical bytes, deterministic listing, roundtrip
# ---------------------------------------------------------------------------


def test_result_registry_canonical_json_and_roundtrip(tmp_path):
    root = build_result_workspace(tmp_path)
    record = make_result_record(
        "RES-1",
        requirement_refs=["REQ-1"],
        environment={"python": "3.11", "lib": "isothermlib 1.2"},
        qc_findings=["no outliers"],
        metrics=[{"metric": "batch_level_uptake", "value": 12.4}],
        uncertainty={"method": "monte_carlo", "n": 500},
        warnings=["baseline drift flagged"],
        scripts=["scripts/fit_isotherms.py"],
    )
    register_result(root, record)
    state_path = root / RESULTS_STATE_DIR / "RES-1.json"
    raw = state_path.read_text(encoding="utf-8")
    assert raw == _canonical(record.to_dict())
    assert json.loads(raw) == record.to_dict()
    stored = read_result(root, "RES-1")
    assert stored == record
    assert ResultRecord.from_dict(stored.to_dict()) == record


def test_result_mapping_input_coerced_with_defaults(tmp_path):
    root = build_result_workspace(tmp_path)
    registered = register_result(
        root,
        {
            "result_id": "RES-1",
            "analysis_id": "ANL-1",
            "protocol_version": "v1",
            "run_ref": "RUN-001",
            "input_artifact_ids": ["ART-001"],
            "primary_or_exploratory": "PRIMARY",
        },
    )
    assert registered.acceptance_ref is None
    assert registered.requirement_refs == []
    assert registered.output_artifact_ids == []
    assert registered.environment == {}
    stored = read_result(root, "RES-1")
    assert stored == registered
    assert stored.primary_or_exploratory is PrimaryOrExploratory.PRIMARY


def test_result_mapping_invalid_enum_value_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(ResultRecordError) as exc:
        register_result(
            root,
            {
                "result_id": "RES-1",
                "analysis_id": "ANL-1",
                "protocol_version": "v1",
                "run_ref": "RUN-001",
                "input_artifact_ids": ["ART-001"],
                "primary_or_exploratory": "SOMETIMES",
            },
        )
    assert "primary_or_exploratory" in str(exc.value)


def test_result_deterministic_listing_sorted_by_id(tmp_path):
    root = build_result_workspace(tmp_path)
    register_raw_artifact(root, "ART-002")
    register_raw_artifact(root, "ART-003")
    for result_id in ("RES-3", "RES-1", "RES-2"):
        register_result(
            root,
            make_result_record(
                result_id, input_artifact_ids=["ART-001", "ART-002", "ART-003"]
            ),
        )
    listed = list_results(root)
    assert [r.result_id for r in listed] == ["RES-1", "RES-2", "RES-3"]
    assert all(r == read_result(root, r.result_id) for r in listed)


def test_result_deterministic_same_state_same_bytes(tmp_path):
    root_a = build_result_workspace(tmp_path / "a")
    root_b = build_result_workspace(tmp_path / "b")
    for root in (root_a, root_b):
        register_result(root, make_result_record("RES-1"))
        register_result(root, make_result_record("RES-2"))
    assert list_results(root_a) == list_results(root_b)
    for result_id in ("RES-1", "RES-2"):
        path_a = root_a / RESULTS_STATE_DIR / f"{result_id}.json"
        path_b = root_b / RESULTS_STATE_DIR / f"{result_id}.json"
        assert path_a.read_bytes() == path_b.read_bytes()


def test_result_read_roundtrip_and_not_found(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(ResultNotFoundError) as exc:
        read_result(root, "RES-1")
    assert "RES-1" in str(exc.value)
    register_result(root, make_result_record("RES-1"))
    assert read_result(root, "RES-1") == make_result_record("RES-1")
    assert list_results(root) == (make_result_record("RES-1"),)


# ---------------------------------------------------------------------------
# Paradigm: purity, boundaries, error hierarchy, corrupt state
# ---------------------------------------------------------------------------


def test_result_type_error_boundaries(tmp_path):
    root = build_result_workspace(tmp_path)
    record = make_result_record("RES-1")
    with pytest.raises(TypeError):
        register_result(123, record)
    with pytest.raises(TypeError):
        register_result(root, "not-a-record")
    with pytest.raises(TypeError):
        register_result(root, 42)
    with pytest.raises(TypeError):
        read_result(root, 5)
    with pytest.raises(TypeError):
        read_result(3, "RES-1")
    with pytest.raises(TypeError):
        list_results(3)
    # Record construction boundaries: wrong types are TypeError.
    with pytest.raises(TypeError):
        ResultRecord(
            result_id="RES-1",
            analysis_id="ANL-1",
            protocol_version="v1",
            run_ref="RUN-001",
            input_artifact_ids=("ART-001",),  # tuple, not list
            primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        )
    with pytest.raises(TypeError):
        make_result_record("RES-1", primary_or_exploratory="PRIMARY")
    with pytest.raises(TypeError):
        make_result_record("RES-1", acceptance_ref=5)
    with pytest.raises(TypeError):
        make_result_record("RES-1", run_ref=None)
    with pytest.raises(TypeError):
        make_result_record("RES-1", environment=[1])
    with pytest.raises(TypeError):
        make_result_record("RES-1", qc_findings=["ok", 5])
    with pytest.raises(TypeError):
        make_result_record("RES-1", metrics=[5])
    with pytest.raises(TypeError):
        ResultRecord.from_dict([1, 2])


def test_result_record_shape_validation_rejects_bad_values():
    with pytest.raises(ResultRecordError):
        make_result_record("")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", analysis_id="../escape")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", protocol_version="2.0")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", protocol_version="")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", run_ref="")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", run_ref="R/UN-1")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", run_ref="RUN*")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", input_artifact_ids=[])
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", input_artifact_ids=["ART-1", "ART-1"])
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", input_artifact_ids=["ART-1", ""])
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", output_artifact_ids=["FIG-1", "FIG-1"])
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", requirement_refs=["REQ-1", "REQ-1"])
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", acceptance_ref="")
    with pytest.raises(ResultRecordError):
        make_result_record("RES-1", warnings=[""])
    with pytest.raises(ResultRecordError):
        ResultRecord.from_dict({"result_id": "RES-1"})


def test_result_error_hierarchy_and_stable_messages(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(ResultNotFoundError) as first:
        read_result(root, "RES-1")
    with pytest.raises(ResultNotFoundError) as second:
        read_result(root, "RES-1")
    assert str(first.value) == str(second.value)
    assert isinstance(first.value, ResultRegistryError)
    assert isinstance(first.value, ValueError)
    for error_type in (
        ResultRegistryError,
        ResultRecordError,
        DuplicateResultError,
        InvalidResultIdError,
        ResultNotFoundError,
        UnresolvedResultReferenceError,
    ):
        assert issubclass(error_type, ValueError)


def test_result_requires_initialized_project(tmp_path):
    bare = tmp_path / "bare"
    record = make_result_record("RES-1")
    with pytest.raises(ProjectNotInitializedError):
        register_result(bare, record)
    with pytest.raises(ProjectNotInitializedError):
        read_result(bare, "RES-1")
    with pytest.raises(ProjectNotInitializedError):
        list_results(bare)


def test_result_rejects_unsafe_ids_at_every_boundary(tmp_path):
    root = build_result_workspace(tmp_path)
    for unsafe in ("../escape", "a/b", "a\\b", "ANL*", "ANL?", "ANL[1", "ANL]1"):
        with pytest.raises(InvalidResultIdError):
            register_result(root, make_result_record(unsafe))
        with pytest.raises(InvalidResultIdError):
            read_result(root, unsafe)
    with pytest.raises(InvalidResultIdError) as exc:
        read_result(root, ".")
    assert "." in str(exc.value)


def test_result_corrupt_registry_records_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    results_dir = root / RESULTS_STATE_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "RES-1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt analysis result record"):
        read_result(root, "RES-1")
    with pytest.raises(ValueError, match="corrupt analysis result record"):
        list_results(root)
    # A non-object JSON document is corrupt.
    (results_dir / "RES-1.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt analysis result record"):
        read_result(root, "RES-1")
    # A valid record missing a required field is corrupt.
    incomplete = make_result_record("RES-1").to_dict()
    del incomplete["run_ref"]
    (results_dir / "RES-1.json").write_text(_canonical(incomplete), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt analysis result record"):
        read_result(root, "RES-1")
    # A stored result_id contradicting its file name is corrupt.
    (results_dir / "RES-1.json").write_text(
        _canonical(make_result_record("RES-OTHER").to_dict()), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt analysis result record"):
        read_result(root, "RES-1")
    with pytest.raises(ValueError, match="corrupt analysis result record"):
        list_results(root)


def test_result_protocol_and_result_registries_are_isolated(tmp_path):
    root = build_result_workspace(tmp_path)
    register_result(root, make_result_record("RES-1"))
    # The result registry is invisible to the protocol registries: the
    # DEV-M4-G04 protocol listing and the DEV-M9-G01 lineage see exactly
    # the protocol records, and the result registry sees exactly the
    # results.
    from scientific_reproduction.planning.plan import list_analysis_protocols

    assert [a.analysis_id for a in list_analysis_protocols(root)] == ["ANL-1"]
    assert [v.record.protocol_version for v in list_protocol_versions(root, "ANL-1")] == [
        "v1-draft",
        "v1",
    ]
    assert [r.result_id for r in list_results(root)] == ["RES-1"]
    # A protocol id is never a result id: the registries are disjoint.
    with pytest.raises(ResultNotFoundError):
        read_result(root, "ANL-1")

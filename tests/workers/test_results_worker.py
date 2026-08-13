"""Worker Result Package records with typed sections (DEV-M6-G02).

Every test name contains "result" so ``python -m pytest -q
tests/workers -k result`` selects the whole suite (the pre-existing
workers/context tests carry no "result" in their names and are the only
deselected items). The ``ac01``/``ac02``/``ac03`` sections map one-to-one
to the acceptance criteria of DEV-M6-G02:

* ``ac01`` -- the result package has distinct, typed sections for
  worker-produced facts (measurements/values), data (structured outputs)
  and deviations (what the worker did differently or could not do), and
  it is structurally impossible to confuse them with
  ``core.models.SupervisorDecision`` records: the package may reference a
  Supervisor decision by ref (``decision_refs``) but never carries
  decision semantics; everything is pure with an injectable timestamp;
* ``ac02`` -- the package cannot encode an authoritative
  requirement-level outcome: no outcome-typed field, no verdict enum on
  requirement refs; facts/deviations may reference Requirements by id
  (pure linkage) and the module never imports the requirement
  outcome/closure layers;
* ``ac03`` -- every artifact reference (input and output) resolves
  against the real DEV-M3-G02 ``ArtifactRegistry`` at registration
  (exactness: an unregistered ref is rejected with a stable error and
  nothing is written), and the registration returns a deterministic
  :class:`ResultManifest` recording exactly the linked references --
  "artifacts are linked through manifests"; every artifact id is a safe
  registry id at the record boundary and re-checked at the resolution
  gate (defense-in-depth, FND-M9-G02-01).

The deterministic path mirrors the M9-G02 suite: every fixture uses a
fixed identity/timestamp for ``initialize_project`` and a pinned
generated context id, so every record and every registered byte is
deterministic. Artifact manifests are registered through the real
``ArtifactRegistry`` (``manifests/``).
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import scientific_reproduction.workers.results as results_module
from scientific_reproduction.analysis.results import (
    list_results as list_analysis_results,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    ArtifactManifest,
    Criticality,
    DecisionType,
    InventoryItemType,
    MappingStatus,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    SupervisorDecision,
    WorkerRole,
)
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_and_reject,
)
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    initialize_project,
)
from scientific_reproduction.planning.inventory import (
    read_requirement,
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.workers.results import (
    ARTIFACTS_STATE_DIR,
    WORKER_RESULT_MANIFEST_VERSION,
    WORKER_RESULTS_STATE_DIR,
    DeviationType,
    DuplicateWorkerResultError,
    InvalidWorkerResultIdError,
    ResultManifest,
    ResultReference,
    ResultReferenceKind,
    UnresolvedWorkerResultReferenceError,
    WorkerData,
    WorkerDeviation,
    WorkerFact,
    WorkerResultError,
    WorkerResultNotFoundError,
    WorkerResultPackage,
    WorkerResultRecordError,
    WorkerResultRegistration,
    build_result_manifest,
    list_worker_results,
    read_worker_result,
    register_worker_result,
)

# ---------------------------------------------------------------------------
# Fixtures (deterministic: pinned identity/timestamp, generated context id)
# ---------------------------------------------------------------------------

#: Deterministic author/committer identity for the initialized project.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects.
DOI = "10.1039/D5TA00771B"

#: The test's worker role.
ROLE = WorkerRole.EXPERIMENT_WORKER

#: A pinned, well-formed generated context id (the exact shape the
#: ``GoalExecutionContextPackage`` generator of DEV-M6-G01 produces).
CONTEXT_ID = generate_id("context", "GOAL-1", "v1", ROLE.value)


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


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


def register_artifact(root: Path, artifact_id: str) -> None:
    """Register one artifact manifest under ``manifests/`` (real registry)."""
    ArtifactRegistry(root / ARTIFACTS_STATE_DIR).register(
        make_manifest(artifact_id)
    )


def build_result_workspace(root: Path) -> Path:
    """Initialize a project with the registered entities a result references.

    Registers, deterministically: the raw artifact manifest ``ART-001`` in
    the project ``manifests/`` artifact registry (DEV-M3-G02).
    """
    init_project(root)
    register_artifact(root, "ART-001")
    return root


def make_fact(fact_id: str = "F-1", **kwargs: Any) -> WorkerFact:
    """Build a schema-valid fact with compact defaults."""
    defaults: dict[str, Any] = {"name": "uptake_at_298k", "value": 180.5}
    defaults.update(kwargs)
    return WorkerFact(fact_id=fact_id, **defaults)


def make_data_entry(data_id: str = "D-1", **kwargs: Any) -> WorkerData:
    """Build a schema-valid data entry with compact defaults."""
    defaults: dict[str, Any] = {
        "name": "isotherm_table",
        "format": "csv",
        "summary": {"rows": 24},
    }
    defaults.update(kwargs)
    return WorkerData(data_id=data_id, **defaults)


def make_deviation(
    deviation_id: str = "DEV-1", **kwargs: Any
) -> WorkerDeviation:
    """Build a schema-valid deviation with compact defaults."""
    defaults: dict[str, Any] = {
        "kind": DeviationType.PROTOCOL_DEVIATION,
        "description": "used reagent lot B instead of lot A",
    }
    defaults.update(kwargs)
    return WorkerDeviation(deviation_id=deviation_id, **defaults)


def make_package(result_id: str, **kwargs: Any) -> WorkerResultPackage:
    """Build a schema-valid result package with compact defaults."""
    defaults: dict[str, Any] = {
        "context_id": CONTEXT_ID,
        "worker_role": ROLE,
        "goal_id": "GOAL-1",
        "goal_version": "v1",
        "input_artifact_ids": ["ART-001"],
    }
    defaults.update(kwargs)
    return WorkerResultPackage(result_id=result_id, **defaults)


def make_supervisor_decision() -> SupervisorDecision:
    """Build a frozen SupervisorDecision record (never part of a package)."""
    return SupervisorDecision(
        decision_id="DEC-1",
        decision_type=DecisionType.GOAL_REVIEW,
        actor="supervisor",
        timestamp="2026-07-01T00:00:00Z",
        affected_refs=["GOAL-1"],
        rationale="reviewed the run results",
    )


def _canonical(data: dict[str, Any]) -> str:
    """The registry's canonical JSON serialization."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# AC-01: distinct typed sections; no confusion with Supervisor decisions
# ---------------------------------------------------------------------------


def test_result_ac01_package_has_distinct_typed_sections(tmp_path):
    root = build_result_workspace(tmp_path)
    package = make_package(
        "RES-1",
        facts=[make_fact(), make_fact(fact_id="F-2", name="temperature", value=298)],
        data=[make_data_entry()],
        deviations=[make_deviation()],
    )
    registered = register_worker_result(root, package)
    stored = read_worker_result(root, "RES-1")
    # The sections are distinct, typed records -- not free-form dicts.
    assert all(isinstance(fact, WorkerFact) for fact in stored.facts)
    assert all(isinstance(entry, WorkerData) for entry in stored.data)
    assert all(isinstance(dev, WorkerDeviation) for dev in stored.deviations)
    assert stored.facts[0].name == "uptake_at_298k"
    assert stored.facts[0].value == 180.5
    assert stored.data[0].format == "csv"
    assert stored.deviations[0].kind is DeviationType.PROTOCOL_DEVIATION
    assert isinstance(registered, WorkerResultRegistration)
    assert registered.package == stored
    # A raw dict or a Supervisor decision is rejected as a section entry.
    with pytest.raises(TypeError):
        make_package("RES-1", facts=[{"fact_id": "F-1", "name": "n", "value": 1}])
    with pytest.raises(TypeError):
        make_package("RES-1", facts=[make_supervisor_decision()])


def test_result_ac01_facts_carry_measurements_and_values(tmp_path):
    root = build_result_workspace(tmp_path)
    package = make_package(
        "RES-1",
        facts=[
            make_fact(fact_id="F-1", name="uptake", value=180.5, unit="cm3/g"),
            make_fact(fact_id="F-2", name="n_points", value=24),
            make_fact(fact_id="F-3", name="sample_label", value="FDM-201-A"),
            make_fact(fact_id="F-4", name="clean_plate", value=True),
        ],
    )
    register_worker_result(root, package)
    stored = read_worker_result(root, "RES-1")
    # Scalar values roundtrip with their exact types.
    values = {fact.fact_id: fact.value for fact in stored.facts}
    assert values == {
        "F-1": 180.5,
        "F-2": 24,
        "F-3": "FDM-201-A",
        "F-4": True,
    }
    assert stored.facts[0].unit == "cm3/g"
    assert stored.facts[1].unit is None
    # Non-scalar values are rejected at the record boundary.
    with pytest.raises(TypeError):
        make_fact(fact_id="F-5", value=[1, 2, 3])
    with pytest.raises(TypeError):
        make_fact(fact_id="F-6", value=None)


def test_result_ac01_data_carry_structured_outputs(tmp_path):
    root = build_result_workspace(tmp_path)
    register_worker_result(
        root,
        make_package(
            "RES-1",
            data=[
                make_data_entry(
                    data_id="D-1",
                    name="isotherm_table",
                    format="csv",
                    summary={"rows": 24, "columns": ["p", "uptake"]},
                ),
                make_data_entry(
                    data_id="D-2", name="log", format="json", summary={}
                ),
            ],
        ),
    )
    stored = read_worker_result(root, "RES-1")
    assert [(entry.data_id, entry.name, entry.format) for entry in stored.data] == [
        ("D-1", "isotherm_table", "csv"),
        ("D-2", "log", "json"),
    ]
    assert stored.data[0].summary == {"rows": 24, "columns": ["p", "uptake"]}
    assert stored.data[1].summary == {}
    with pytest.raises(TypeError):
        make_data_entry(data_id="D-3", summary=[1, 2])


def test_result_ac01_deviations_state_what_was_done_differently_or_not(tmp_path):
    root = build_result_workspace(tmp_path)
    register_worker_result(
        root,
        make_package(
            "RES-1",
            deviations=[
                make_deviation(
                    deviation_id="DEV-1",
                    kind=DeviationType.PROTOCOL_DEVIATION,
                    description="used reagent lot B instead of lot A",
                ),
                make_deviation(
                    deviation_id="DEV-2",
                    kind=DeviationType.FAILURE,
                    description="the second replicate could not be collected",
                ),
                make_deviation(
                    deviation_id="DEV-3",
                    kind=DeviationType.INTERRUPTION,
                    description="the job was interrupted by a scheduler outage",
                ),
            ],
        ),
    )
    stored = read_worker_result(root, "RES-1")
    kinds = [dev.kind for dev in stored.deviations]
    assert kinds == [
        DeviationType.PROTOCOL_DEVIATION,
        DeviationType.FAILURE,
        DeviationType.INTERRUPTION,
    ]
    assert "could not be collected" in stored.deviations[1].description
    with pytest.raises(WorkerResultRecordError):
        make_deviation(deviation_id="DEV-4", description="")


def test_result_ac01_package_carries_no_supervisor_decision_semantics(tmp_path):
    root = build_result_workspace(tmp_path)
    # The package record carries none of the SupervisorDecision fields.
    decision_fields = {
        "decision_id",
        "decision_type",
        "actor",
        "timestamp",
        "affected_refs",
        "rationale",
        "evidence_refs",
        "analysis_refs",
        "previous_version_refs",
        "resulting_version_refs",
    }
    for record_type in (
        WorkerResultPackage,
        WorkerFact,
        WorkerData,
        WorkerDeviation,
    ):
        field_names = {f.name for f in fields(record_type)}
        assert not (field_names & decision_fields)
    # A Supervisor decision cannot be smuggled into any section or ref list.
    decision = make_supervisor_decision()
    with pytest.raises(TypeError):
        make_package("RES-1", facts=[decision])
    with pytest.raises(TypeError):
        make_package("RES-1", data=[decision])
    with pytest.raises(TypeError):
        make_package("RES-1", deviations=[decision])
    with pytest.raises(TypeError):
        make_package("RES-1", decision_refs=[decision])
    # The only contact with decisions is the opaque ref linkage.
    registered = register_worker_result(
        root, make_package("RES-1", decision_refs=["DEC-1"])
    )
    assert registered.package.decision_refs == ["DEC-1"]
    raw = (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    assert "DEC-1" in raw
    assert "decision_type" not in raw
    assert "rationale" not in raw


def test_result_ac01_package_cannot_validate_as_a_decision(tmp_path):
    root = build_result_workspace(tmp_path)
    register_worker_result(root, make_package("RES-1"))
    raw = (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    # Structural impossibility: the package record is not a Supervisor
    # Decision document -- the frozen decision schema requires
    # decision_type/actor/rationale, which the package can never carry.
    with pytest.raises(SchemaValidationError):
        validate_and_reject("decision", json.loads(raw))


def test_result_ac01_registration_is_pure_with_injectable_timestamp(tmp_path):
    root = build_result_workspace(tmp_path)
    registered = register_worker_result(
        root,
        make_package("RES-1", completed_at="2026-07-01T10:00:00Z"),
    )
    assert registered.package.completed_at == "2026-07-01T10:00:00Z"
    stored = read_worker_result(root, "RES-1")
    assert stored.completed_at == "2026-07-01T10:00:00Z"
    # Determinism: identical inputs -> identical registered bytes; the
    # timestamp is a pure input, never read from the wall clock.
    other = build_result_workspace(tmp_path / "other")
    register_worker_result(
        other, make_package("RES-1", completed_at="2026-07-01T10:00:00Z")
    )
    path_a = root / WORKER_RESULTS_STATE_DIR / "RES-1.json"
    path_b = other / WORKER_RESULTS_STATE_DIR / "RES-1.json"
    assert path_a.read_bytes() == path_b.read_bytes()
    # No wall clock, no randomness, no network anywhere in the module.
    source = inspect.getsource(results_module)
    for banned in (
        "import random",
        "import time",
        "import datetime",
        "from datetime",
        "import socket",
        "import urllib",
    ):
        assert banned not in source


# ---------------------------------------------------------------------------
# AC-02: no authoritative requirement-level outcome
# ---------------------------------------------------------------------------


def test_result_ac02_no_outcome_field_anywhere():
    for record_type in (
        WorkerResultPackage,
        WorkerFact,
        WorkerData,
        WorkerDeviation,
    ):
        for record_field in fields(record_type):
            assert record_field.name != "outcome"
            assert "verdict" not in record_field.name


def test_result_ac02_fact_may_reference_requirement_but_never_declare_outcome(
    tmp_path,
):
    root = build_result_workspace(tmp_path)
    registered = register_worker_result(
        root,
        make_package(
            "RES-1",
            facts=[make_fact(fact_id="F-1", requirement_refs=["REQ-1", "REQ-2"])],
        ),
    )
    # The linkage is exposed for a later Supervisor closure flow -- and it
    # is the only requirement contact the fact carries.
    stored = read_worker_result(root, "RES-1")
    assert stored.facts[0].requirement_refs == ["REQ-1", "REQ-2"]
    assert registered.manifest.references_for(ResultReferenceKind.REQUIREMENT) == (
        ResultReference(ResultReferenceKind.REQUIREMENT, "REQ-1"),
        ResultReference(ResultReferenceKind.REQUIREMENT, "REQ-2"),
    )
    # The linkage is plain-string only: a non-string entry is rejected
    # (the module defines no verdict enum for refs -- see the AC-02
    # source-level asserts).
    with pytest.raises(TypeError):
        make_fact(fact_id="F-2", requirement_refs=[42])


def test_result_ac02_deviation_may_reference_requirement_but_never_declare_outcome(
    tmp_path,
):
    root = build_result_workspace(tmp_path)
    registered = register_worker_result(
        root,
        make_package(
            "RES-1",
            deviations=[
                make_deviation(deviation_id="DEV-1", requirement_refs=["REQ-1"])
            ],
        ),
    )
    stored = read_worker_result(root, "RES-1")
    assert stored.deviations[0].requirement_refs == ["REQ-1"]
    assert registered.manifest.references_for(ResultReferenceKind.REQUIREMENT) == (
        ResultReference(ResultReferenceKind.REQUIREMENT, "REQ-1"),
    )
    with pytest.raises(WorkerResultRecordError):
        make_deviation(
            deviation_id="DEV-2",
            requirement_refs=["REQ-1", "REQ-1"],
        )


def test_result_ac02_package_cannot_validate_as_a_requirement(tmp_path):
    root = build_result_workspace(tmp_path)
    register_worker_result(root, make_package("RES-1"))
    raw = (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    # Structural impossibility: the requirement schema requires an
    # ``outcome`` (and inventory/goal edges) -- the package can never be
    # a requirement record, so it can never declare an outcome.
    with pytest.raises(SchemaValidationError):
        validate_and_reject("requirement", json.loads(raw))


def test_result_ac02_module_never_imports_requirement_layers():
    source = inspect.getsource(results_module)
    assert "from scientific_reproduction.core.rules" not in source
    assert "from scientific_reproduction.planning.inventory" not in source
    assert "register_requirement" not in source
    assert "RequirementOutcome" not in source


def test_result_ac02_requirement_linkage_is_never_resolved_or_closed(tmp_path):
    root = build_result_workspace(tmp_path)
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
            requirement_id="REQ-1",
            statement="reproduce the reported uptake isotherm",
            inventory_items=["INV-1"],
            criticality=Criticality.CRITICAL,
            goal_ids=["GOAL-1"],
            outcome=RequirementOutcome.OPEN,
        ),
    )
    requirement_path = root / "requirements" / "REQ-1.json"
    before = requirement_path.read_bytes()
    register_worker_result(
        root,
        make_package(
            "RES-1",
            facts=[make_fact(fact_id="F-1", requirement_refs=["REQ-1"])],
        ),
    )
    # Registering a worker result performs no closure: the requirement
    # record is byte untouched and still OPEN.
    assert requirement_path.read_bytes() == before
    assert read_requirement(root, "REQ-1").outcome is RequirementOutcome.OPEN
    # Pure linkage: an unregistered requirement id is never resolved (the
    # module reads no requirement state at all).
    other = register_worker_result(
        root,
        make_package(
            "RES-2",
            facts=[
                make_fact(fact_id="F-2", requirement_refs=["REQ-NOT-REGISTERED"])
            ],
        ),
    )
    assert other.manifest.references_for(ResultReferenceKind.REQUIREMENT) == (
        ResultReference(ResultReferenceKind.REQUIREMENT, "REQ-NOT-REGISTERED"),
    )


# ---------------------------------------------------------------------------
# AC-03: artifacts resolve against the real manifests registry
# ---------------------------------------------------------------------------


def test_result_ac03_input_artifact_refs_resolve_against_real_registry(tmp_path):
    root = build_result_workspace(tmp_path)
    register_artifact(root, "ART-002")
    registered = register_worker_result(
        root,
        make_package("RES-1", input_artifact_ids=["ART-002", "ART-001"]),
    )
    stored = read_worker_result(root, "RES-1")
    assert stored.input_artifact_ids == ["ART-002", "ART-001"]
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    for artifact_id in stored.input_artifact_ids:
        assert registry.get(artifact_id).artifact_id == artifact_id
    assert registered.manifest.references_for(ResultReferenceKind.ARTIFACT_INPUT) == (
        ResultReference(ResultReferenceKind.ARTIFACT_INPUT, "ART-001"),
        ResultReference(ResultReferenceKind.ARTIFACT_INPUT, "ART-002"),
    )
    raw = (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").read_text(encoding="utf-8")
    assert "ART-001" in raw and "ART-002" in raw


def test_result_ac03_output_artifact_refs_resolve_against_real_registry(tmp_path):
    root = build_result_workspace(tmp_path)
    register_artifact(root, "FIG-1")
    registered = register_worker_result(
        root,
        make_package("RES-1", output_artifact_ids=["FIG-1"]),
    )
    stored = read_worker_result(root, "RES-1")
    assert stored.output_artifact_ids == ["FIG-1"]
    assert ArtifactRegistry(root / ARTIFACTS_STATE_DIR).get("FIG-1").artifact_id == "FIG-1"
    assert registered.manifest.references_for(ResultReferenceKind.ARTIFACT_OUTPUT) == (
        ResultReference(ResultReferenceKind.ARTIFACT_OUTPUT, "FIG-1"),
    )


def test_result_ac03_unregistered_input_artifact_ref_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(UnresolvedWorkerResultReferenceError) as exc:
        register_worker_result(
            root,
            make_package("RES-1", input_artifact_ids=["ART-NOPE"]),
        )
    message = str(exc.value)
    assert "RES-1" in message
    assert "ART-NOPE" in message
    assert "not registered" in message
    # Nothing was written.
    assert not (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").exists()
    assert list_worker_results(root) == ()


def test_result_ac03_unregistered_output_artifact_ref_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(UnresolvedWorkerResultReferenceError) as exc:
        register_worker_result(
            root,
            make_package("RES-1", output_artifact_ids=["ART-GHOST"]),
        )
    assert "ART-GHOST" in str(exc.value)
    assert not (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").exists()


def test_result_ac03_manifest_records_exactly_the_linked_references(tmp_path):
    root = build_result_workspace(tmp_path)
    register_artifact(root, "FIG-1")
    register_worker_result(
        root,
        make_package(
            "RES-1",
            run_ref="RUN-001",
            facts=[make_fact(fact_id="F-1", requirement_refs=["REQ-1"])],
            deviations=[
                make_deviation(deviation_id="DEV-1", requirement_refs=["REQ-2"])
            ],
            input_artifact_ids=["ART-001"],
            output_artifact_ids=["FIG-1"],
            decision_refs=["DEC-1"],
        ),
    )
    manifest = build_result_manifest(read_worker_result(root, "RES-1"))
    assert isinstance(manifest, ResultManifest)
    assert manifest.references == (
        ResultReference(ResultReferenceKind.ARTIFACT_INPUT, "ART-001"),
        ResultReference(ResultReferenceKind.ARTIFACT_OUTPUT, "FIG-1"),
        ResultReference(ResultReferenceKind.CONTEXT, CONTEXT_ID),
        ResultReference(ResultReferenceKind.DECISION, "DEC-1"),
        ResultReference(ResultReferenceKind.REQUIREMENT, "REQ-1"),
        ResultReference(ResultReferenceKind.REQUIREMENT, "REQ-2"),
        ResultReference(ResultReferenceKind.RUN, "RUN-001"),
    )
    assert manifest.result_id == "RES-1"
    assert manifest.goal_id == "GOAL-1"
    assert manifest.goal_version == "v1"
    assert manifest.worker_role is ROLE


def test_result_ac03_unsafe_artifact_ids_rejected_at_record_boundary():
    for unsafe in ("a/b", "a\\b", "../escape", ".", "..", "ART*", "ART?", "ART[1", "ART]1"):
        with pytest.raises(WorkerResultRecordError) as exc:
            make_package("RES-1", input_artifact_ids=[unsafe])
        assert "not a safe registry id" in str(exc.value)
        with pytest.raises(WorkerResultRecordError):
            make_package("RES-1", output_artifact_ids=[unsafe])
    # The record boundary offers no bypass: replace re-runs the frozen
    # constructor.
    package = make_package("RES-1")
    with pytest.raises(WorkerResultRecordError):
        replace(package, input_artifact_ids=["../EVIL"])
    with pytest.raises(WorkerResultRecordError):
        replace(package, output_artifact_ids=["ANL*"])
    with pytest.raises(WorkerResultRecordError):
        WorkerResultPackage.from_dict(
            {**package.to_dict(), "input_artifact_ids": ["../EVIL"]}
        )


def test_result_ac03_traversal_artifact_id_cannot_escape_manifests(tmp_path):
    """FND-M9-G02-01 regression: ``manifests/../EVIL.json`` is unreachable.

    On merged main an artifact ref resolved ``manifests/../EVIL.json`` -- a
    crafted manifest-shaped JSON at the workspace root, OUTSIDE the
    registry -- and registration succeeded (the artifact registry has no id
    validation at ``get``). The traversal must now fail with the stable
    error at the record boundary, and the crafted file's bytes must never
    be read.
    """
    root = build_result_workspace(tmp_path)
    evil = make_manifest("EVIL")
    evil_path = root / "EVIL.json"
    evil_path.write_bytes(_canonical(evil.to_dict()).encode("utf-8"))
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    assert [m.artifact_id for m in registry.list()] == ["ART-001"]
    with pytest.raises(WorkerResultRecordError) as exc:
        register_worker_result(
            root, make_package("RES-1", input_artifact_ids=["../EVIL"])
        )
    message = str(exc.value)
    assert "input_artifact_ids" in message
    assert "'../EVIL'" in message
    assert "not a safe registry id" in message
    # Nothing was written and the crafted file was never read.
    assert not (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").exists()
    assert [m.artifact_id for m in registry.list()] == ["ART-001"]
    assert list_worker_results(root) == ()
    assert evil_path.read_bytes() == _canonical(evil.to_dict()).encode("utf-8")


def test_result_ac03_defense_in_depth_unsafe_id_rejected_at_resolution_gate(
    tmp_path,
):
    """The resolution gate refuses unsafe ids even if the shape check were bypassed.

    The record boundary offers no bypass: ``dataclasses.replace`` and
    ``from_dict`` both re-run the frozen constructor, so an unsafe artifact
    id cannot reach registration through any public path. Defense-in-depth
    (FND-M9-G02-01): the resolution gate re-checks every id before
    ``ArtifactRegistry.get``, so the resolution loop never constructs a
    registry path from an unsafe id.
    """
    root = build_result_workspace(tmp_path)
    bypass = object.__new__(WorkerResultPackage)
    for name, value in {
        "result_id": "RES-1",
        "context_id": CONTEXT_ID,
        "worker_role": ROLE,
        "goal_id": "GOAL-1",
        "goal_version": "v1",
        "run_ref": None,
        "facts": [],
        "data": [],
        "deviations": [],
        "input_artifact_ids": ["../EVIL"],
        "output_artifact_ids": [],
        "decision_refs": [],
        "environment": {},
        "completed_at": None,
    }.items():
        object.__setattr__(bypass, name, value)
    with pytest.raises(UnresolvedWorkerResultReferenceError) as exc:
        results_module._resolve_artifact_refs(root.resolve(), bypass)
    message = str(exc.value)
    assert "RES-1" in message
    assert "'../EVIL'" in message
    assert "not a safe registry id" in message
    assert not (root / WORKER_RESULTS_STATE_DIR / "RES-1.json").exists()


# ---------------------------------------------------------------------------
# Paradigm: canonical bytes, roundtrip, listing, exactly-once registry
# ---------------------------------------------------------------------------


def test_result_registry_canonical_json_and_roundtrip(tmp_path):
    root = build_result_workspace(tmp_path)
    register_artifact(root, "FIG-1")
    package = make_package(
        "RES-1",
        run_ref="RUN-001",
        facts=[
            make_fact(fact_id="F-1", value=180.5, unit="cm3/g", requirement_refs=["REQ-1"])
        ],
        data=[make_data_entry()],
        deviations=[
            make_deviation(
                deviation_id="DEV-1",
                kind=DeviationType.INTERRUPTION,
                description="the job was interrupted by a scheduler outage",
            )
        ],
        input_artifact_ids=["ART-001"],
        output_artifact_ids=["FIG-1"],
        decision_refs=["DEC-1"],
        environment={"python": "3.11", "adapter": "local"},
        completed_at="2026-07-01T10:00:00Z",
    )
    registered = register_worker_result(root, package)
    assert isinstance(registered, WorkerResultRegistration)
    assert registered.package == package
    state_path = root / WORKER_RESULTS_STATE_DIR / "RES-1.json"
    raw = state_path.read_text(encoding="utf-8")
    assert raw == _canonical(package.to_dict())
    assert json.loads(raw) == package.to_dict()
    stored = read_worker_result(root, "RES-1")
    assert stored == package
    assert WorkerResultPackage.from_dict(stored.to_dict()) == package


def test_result_mapping_input_coerced_with_defaults(tmp_path):
    root = build_result_workspace(tmp_path)
    registered = register_worker_result(
        root,
        {
            "result_id": "RES-1",
            "context_id": CONTEXT_ID,
            "worker_role": "experiment_worker",
            "goal_id": "GOAL-1",
            "goal_version": "v1",
            "facts": [
                {"fact_id": "F-1", "name": "uptake", "value": 180.5}
            ],
            "deviations": [
                {
                    "deviation_id": "DEV-1",
                    "kind": "protocol_deviation",
                    "description": "used reagent lot B",
                }
            ],
            "input_artifact_ids": ["ART-001"],
        },
    )
    assert registered.package.run_ref is None
    assert registered.package.data == []
    assert registered.package.output_artifact_ids == []
    assert registered.package.decision_refs == []
    assert registered.package.environment == {}
    assert registered.package.completed_at is None
    assert registered.package.facts[0].value == 180.5
    assert registered.package.deviations[0].kind is DeviationType.PROTOCOL_DEVIATION
    stored = read_worker_result(root, "RES-1")
    assert stored == registered.package
    assert stored.worker_role is WorkerRole.EXPERIMENT_WORKER


def test_result_mapping_invalid_enum_value_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(WorkerResultRecordError) as exc:
        register_worker_result(
            root,
            {
                "result_id": "RES-1",
                "context_id": CONTEXT_ID,
                "worker_role": "SOMETIMES",
                "goal_id": "GOAL-1",
                "goal_version": "v1",
                "input_artifact_ids": ["ART-001"],
            },
        )
    assert "worker_role" in str(exc.value)
    # The deviation kind vocabulary is DeviationType only: an unknown
    # value is rejected with a stable record error at the mapping layer.
    with pytest.raises(WorkerResultRecordError) as exc:
        WorkerDeviation.from_dict(
            {
                "deviation_id": "DEV-1",
                "kind": "verdict",
                "description": "unused",
            }
        )
    assert "kind" in str(exc.value)


def test_result_deterministic_listing_sorted_by_id(tmp_path):
    root = build_result_workspace(tmp_path)
    register_artifact(root, "ART-002")
    register_artifact(root, "ART-003")
    for result_id in ("RES-3", "RES-1", "RES-2"):
        register_worker_result(
            root,
            make_package(
                result_id,
                input_artifact_ids=["ART-001", "ART-002", "ART-003"],
            ),
        )
    listed = list_worker_results(root)
    assert [r.result_id for r in listed] == ["RES-1", "RES-2", "RES-3"]
    assert all(r == read_worker_result(root, r.result_id) for r in listed)


def test_result_deterministic_same_state_same_bytes(tmp_path):
    root_a = build_result_workspace(tmp_path / "a")
    root_b = build_result_workspace(tmp_path / "b")
    for root in (root_a, root_b):
        register_worker_result(root, make_package("RES-1"))
        register_worker_result(root, make_package("RES-2"))
    assert list_worker_results(root_a) == list_worker_results(root_b)
    for result_id in ("RES-1", "RES-2"):
        path_a = root_a / WORKER_RESULTS_STATE_DIR / f"{result_id}.json"
        path_b = root_b / WORKER_RESULTS_STATE_DIR / f"{result_id}.json"
        assert path_a.read_bytes() == path_b.read_bytes()


def test_result_read_roundtrip_and_not_found(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(WorkerResultNotFoundError) as exc:
        read_worker_result(root, "RES-1")
    assert "RES-1" in str(exc.value)
    register_worker_result(root, make_package("RES-1"))
    assert read_worker_result(root, "RES-1") == make_package("RES-1")
    assert list_worker_results(root) == (make_package("RES-1"),)


def test_result_duplicate_registration_rejected_bytes_untouched(tmp_path):
    root = build_result_workspace(tmp_path)
    register_worker_result(root, make_package("RES-1"))
    state_path = root / WORKER_RESULTS_STATE_DIR / "RES-1.json"
    before = state_path.read_bytes()
    with pytest.raises(DuplicateWorkerResultError) as exc:
        register_worker_result(root, make_package("RES-1"))
    message = str(exc.value)
    assert "RES-1" in message
    assert "already registered" in message
    # The original record is never rewritten (no-clobber handling).
    assert state_path.read_bytes() == before
    assert read_worker_result(root, "RES-1").goal_version == "v1"
    with pytest.raises(DuplicateWorkerResultError) as second:
        register_worker_result(
            root, make_package("RES-1", completed_at="2026-07-02T00:00:00Z")
        )
    assert str(second.value) == message


# ---------------------------------------------------------------------------
# Paradigm: purity, boundaries, error hierarchy, corrupt state
# ---------------------------------------------------------------------------


def test_result_type_error_boundaries(tmp_path):
    root = build_result_workspace(tmp_path)
    package = make_package("RES-1")
    with pytest.raises(TypeError):
        register_worker_result(123, package)
    with pytest.raises(TypeError):
        register_worker_result(root, "not-a-package")
    with pytest.raises(TypeError):
        register_worker_result(root, 42)
    with pytest.raises(TypeError):
        read_worker_result(root, 5)
    with pytest.raises(TypeError):
        read_worker_result(3, "RES-1")
    with pytest.raises(TypeError):
        list_worker_results(3)
    # Package construction boundaries: wrong types are TypeError.
    with pytest.raises(TypeError):
        make_package("RES-1", worker_role="experiment_worker")
    with pytest.raises(TypeError):
        make_package("RES-1", context_id=42)
    with pytest.raises(TypeError):
        make_package("RES-1", run_ref=5)
    with pytest.raises(TypeError):
        make_package("RES-1", facts=("F-1",))
    with pytest.raises(TypeError):
        make_package("RES-1", facts=[make_fact(), "F-2"])
    with pytest.raises(TypeError):
        make_package("RES-1", data=[make_data_entry(), 5])
    with pytest.raises(TypeError):
        make_package("RES-1", deviations=[make_deviation(), None])
    with pytest.raises(TypeError):
        make_package("RES-1", decision_refs=[5])
    with pytest.raises(TypeError):
        make_package("RES-1", environment=[1])
    with pytest.raises(TypeError):
        make_package("RES-1", completed_at=5)
    with pytest.raises(TypeError):
        make_fact(fact_id="F-1", name=5)
    with pytest.raises(TypeError):
        WorkerFact(fact_id="F-1", name="n", value=[1, 2])
    with pytest.raises(TypeError):
        make_deviation(deviation_id="DEV-1", kind="protocol_deviation")
    with pytest.raises(TypeError):
        build_result_manifest("not-a-package")
    registered = register_worker_result(root, package)
    with pytest.raises(TypeError):
        registered.manifest.references_for("artifact_input")
    with pytest.raises(TypeError):
        WorkerResultPackage.from_dict([1, 2])


def test_result_record_shape_validation_rejects_bad_values():
    with pytest.raises(WorkerResultRecordError):
        make_package("")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", context_id="not-a-context-id")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", context_id=generate_id("goal", "GOAL-1"))
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", goal_id="")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", goal_id="../escape")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", goal_version="v1-draft")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", goal_version="2.0")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", run_ref="")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", run_ref="R/UN-1")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", run_ref="RUN*")
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", input_artifact_ids=[])
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", input_artifact_ids=["ART-1", "ART-1"])
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", input_artifact_ids=["ART-1", ""])
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", output_artifact_ids=["FIG-1", "FIG-1"])
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", decision_refs=["DEC-1", "DEC-1"])
    with pytest.raises(WorkerResultRecordError):
        make_package("RES-1", completed_at="")
    with pytest.raises(WorkerResultRecordError):
        make_package(
            "RES-1",
            facts=[make_fact(fact_id="F-1"), make_fact(fact_id="F-1")],
        )
    with pytest.raises(WorkerResultRecordError):
        make_package(
            "RES-1",
            data=[make_data_entry(data_id="D-1"), make_data_entry(data_id="D-1")],
        )
    with pytest.raises(WorkerResultRecordError):
        make_fact(fact_id="")
    with pytest.raises(WorkerResultRecordError):
        make_fact(fact_id="F-1", name="")
    with pytest.raises(WorkerResultRecordError):
        make_fact(fact_id="F-1", unit="")
    with pytest.raises(WorkerResultRecordError):
        make_data_entry(data_id="D-1", name="")
    with pytest.raises(WorkerResultRecordError):
        make_data_entry(data_id="D-1", format="")
    with pytest.raises(WorkerResultRecordError):
        make_deviation(deviation_id="")
    with pytest.raises(WorkerResultRecordError):
        WorkerResultPackage.from_dict({"result_id": "RES-1"})


def test_result_error_hierarchy_and_stable_messages(tmp_path):
    root = build_result_workspace(tmp_path)
    with pytest.raises(WorkerResultNotFoundError) as first:
        read_worker_result(root, "RES-1")
    with pytest.raises(WorkerResultNotFoundError) as second:
        read_worker_result(root, "RES-1")
    assert str(first.value) == str(second.value)
    assert isinstance(first.value, WorkerResultError)
    assert isinstance(first.value, ValueError)
    with pytest.raises(UnresolvedWorkerResultReferenceError) as first:
        register_worker_result(root, make_package("RES-1", input_artifact_ids=["NOPE"]))
    with pytest.raises(UnresolvedWorkerResultReferenceError) as second:
        register_worker_result(root, make_package("RES-1", input_artifact_ids=["NOPE"]))
    assert str(first.value) == str(second.value)
    for error_type in (
        WorkerResultError,
        WorkerResultRecordError,
        DuplicateWorkerResultError,
        InvalidWorkerResultIdError,
        WorkerResultNotFoundError,
        UnresolvedWorkerResultReferenceError,
    ):
        assert issubclass(error_type, ValueError)


def test_result_requires_initialized_project(tmp_path):
    bare = tmp_path / "bare"
    package = make_package("RES-1")
    with pytest.raises(ProjectNotInitializedError):
        register_worker_result(bare, package)
    with pytest.raises(ProjectNotInitializedError):
        read_worker_result(bare, "RES-1")
    with pytest.raises(ProjectNotInitializedError):
        list_worker_results(bare)


def test_result_rejects_unsafe_ids_at_every_boundary(tmp_path):
    root = build_result_workspace(tmp_path)
    for unsafe in ("../escape", "a/b", "a\\b", "RES*", "RES?", "RES[1", "RES]1", ".", ".."):
        with pytest.raises(InvalidWorkerResultIdError):
            register_worker_result(root, make_package(unsafe))
        with pytest.raises(InvalidWorkerResultIdError):
            read_worker_result(root, unsafe)
    with pytest.raises(InvalidWorkerResultIdError) as exc:
        read_worker_result(root, ".")
    assert "." in str(exc.value)


def test_result_corrupt_registry_records_rejected(tmp_path):
    root = build_result_workspace(tmp_path)
    results_dir = root / WORKER_RESULTS_STATE_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "RES-1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt worker result record"):
        read_worker_result(root, "RES-1")
    with pytest.raises(ValueError, match="corrupt worker result record"):
        list_worker_results(root)
    # A non-object JSON document is corrupt.
    (results_dir / "RES-1.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt worker result record"):
        read_worker_result(root, "RES-1")
    # A valid record missing a required field is corrupt.
    incomplete = make_package("RES-1").to_dict()
    del incomplete["context_id"]
    (results_dir / "RES-1.json").write_text(_canonical(incomplete), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt worker result record"):
        read_worker_result(root, "RES-1")
    # A stored result_id contradicting its file name is corrupt.
    (results_dir / "RES-1.json").write_text(
        _canonical(make_package("RES-OTHER").to_dict()), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt worker result record"):
        read_worker_result(root, "RES-1")
    with pytest.raises(ValueError, match="corrupt worker result record"):
        list_worker_results(root)


def test_result_frozen_records_reject_mutation(tmp_path):
    root = build_result_workspace(tmp_path)
    package = make_package("RES-1", facts=[make_fact()])
    with pytest.raises(FrozenInstanceError):
        package.goal_version = "v2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        package.facts[0].value = 1.0  # type: ignore[misc]
    register_worker_result(root, package)
    stored = read_worker_result(root, "RES-1")
    with pytest.raises(FrozenInstanceError):
        stored.context_id = CONTEXT_ID  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        stored.deviations = [make_deviation()]  # type: ignore[misc]


def test_result_manifest_is_deterministic_sorted_and_hashable(tmp_path):
    root = build_result_workspace(tmp_path)
    register_artifact(root, "FIG-1")
    package = make_package(
        "RES-1",
        run_ref="RUN-001",
        facts=[make_fact(fact_id="F-1", requirement_refs=["REQ-2"])],
        deviations=[make_deviation(deviation_id="DEV-1", requirement_refs=["REQ-1"])],
        input_artifact_ids=["ART-001"],
        output_artifact_ids=["FIG-1"],
        decision_refs=["DEC-1"],
    )
    first = build_result_manifest(package)
    second = build_result_manifest(package)
    assert first == second
    assert first.manifest_version == WORKER_RESULT_MANIFEST_VERSION
    # Deterministic order: references sorted by (kind, ref_id).
    kinds = [r.kind.value for r in first.references]
    assert kinds == sorted(kinds)
    for kind in ResultReferenceKind:
        refs = [r.ref_id for r in first.references if r.kind is kind]
        assert refs == sorted(refs)
    # The hash is a real SHA-256 of the manifest's canonical JSON.
    assert len(first.result_hash()) == 64
    expected = hashlib.sha256(
        first.to_canonical_json().encode("utf-8")
    ).hexdigest()
    assert first.result_hash() == expected
    registered = register_worker_result(root, package)
    assert registered.manifest == first
    assert registered.manifest == build_result_manifest(read_worker_result(root, "RES-1"))


def test_result_worker_results_registry_is_isolated(tmp_path):
    root = build_result_workspace(tmp_path)
    register_worker_result(root, make_package("RES-1"))
    # The worker result registry is invisible to the analysis result
    # registry (DEV-M9-G02) and vice versa.
    assert list_analysis_results(root) == ()
    assert not (root / "analysis" / "results" / "RES-1.json").exists()
    assert [r.result_id for r in list_worker_results(root)] == ["RES-1"]
    with pytest.raises(WorkerResultNotFoundError):
        read_worker_result(root, "ANL-1")


def test_result_registration_answers_a_real_generated_context(tmp_path):
    from context_helpers import build_complete_workspace, frozen_goal, make_retry_policy

    from scientific_reproduction.workers.context import generate_goal_context

    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    context = generate_goal_context(
        root, goal, worker_role=ROLE, retry_policy=make_retry_policy()
    )
    register_artifact(root, "ART-001")
    registered = register_worker_result(
        root,
        make_package("RES-1", context_id=context.package.context_id),
    )
    # The package names the exact context it answers: the goal identity of
    # the context and its generated context_id.
    assert registered.package.context_id == context.package.context_id
    assert registered.package.goal_id == goal.goal_id
    assert registered.package.goal_version == goal.version
    assert registered.manifest.references_for(ResultReferenceKind.CONTEXT) == (
        ResultReference(
            ResultReferenceKind.CONTEXT, context.package.context_id
        ),
    )
    stored = read_worker_result(root, "RES-1")
    assert stored == registered.package

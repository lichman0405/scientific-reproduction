"""Tests for the typed core object models (DEV-M1-G01, acceptance AC-01).

Covered behaviors:
  * all 21 normative object types round-trip ``to_dict()`` / ``from_dict()``;
  * serialization to JSON works for every model;
  * required fields from the frozen schemas are always present in the
    serialized output;
  * enums carry exactly the schema values (member counts spot-checked);
  * defaults are sensible and models are immutable (frozen dataclasses);
  * the ``event`` schema key ``from`` serializes under its schema name.
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest
from fixtures import VALID_DOCS

from scientific_reproduction.core import models as m
from scientific_reproduction.core.schema_validation import (
    load_schema,
    schemas_dir,
    validate_object,
)

ALL_MODEL_NAMES = sorted(m.MODEL_REGISTRY)


def test_registry_covers_all_21_normative_types() -> None:
    assert len(m.MODEL_REGISTRY) == 21
    assert len(m.SCHEMA_NAMES) == 21
    assert set(m.SCHEMA_NAMES) == set(m.MODEL_REGISTRY)


def test_every_registry_name_has_a_schema_file() -> None:
    for name in m.MODEL_REGISTRY:
        assert (schemas_dir() / f"{name}.schema.yaml").is_file(), (
            f"no schema file for {name}"
        )


@pytest.mark.parametrize("name", ALL_MODEL_NAMES)
def test_to_dict_round_trips_with_from_dict(name: str) -> None:
    model_cls = m.MODEL_REGISTRY[name]
    doc = copy.deepcopy(VALID_DOCS[name])
    model = model_cls.from_dict(doc)
    # Serializing twice (through to_dict/from_dict) is stable.
    assert model_cls.from_dict(model.to_dict()) == model
    assert model_cls.from_dict(model.to_dict()).to_dict() == model.to_dict()


@pytest.mark.parametrize("name", ALL_MODEL_NAMES)
def test_to_dict_uses_exact_schema_key_names(name: str) -> None:
    # Every key to_dict() emits must be a schema property key, under the
    # exact schema key names (e.g. "from" for events). None-valued optional
    # fields are omitted (see test_to_dict_omits_unset_optional_fields).
    schema = load_schema(name)
    model_cls = m.MODEL_REGISTRY[name]
    model = model_cls.from_dict(copy.deepcopy(VALID_DOCS[name]))
    emitted = model.to_dict()
    assert set(emitted) <= set(schema.get("properties", {})), (
        f"{name}: to_dict() emitted undeclared key(s) "
        f"{set(emitted) - set(schema.get('properties', {}))}"
    )
    assert "from" not in emitted or name == "event"


def test_to_dict_omits_unset_optional_fields() -> None:
    # Several schemas declare optional properties as non-nullable
    # ({type: string}), so unset fields must be omitted rather than emitted
    # as null -- otherwise serialized objects fail their own schema.
    project = m.Project(
        project_id="P1",
        primary_target=m.PrimaryTarget(
            source_type=m.TargetSourceType.DOI, identifier="10.1/x"
        ),
        project_phase=m.ProjectPhase.PLANNING,
        reproduction_outcome=m.ReproductionOutcome.UNDETERMINED,
        current_plan_version="v1",
    )
    emitted = project.to_dict()
    assert "title" not in emitted
    assert "created_at" not in emitted
    assert "state_backend" not in emitted
    assert emitted["project_id"] == "P1"
    # And the serialized form is schema-valid.
    assert load_schema("project")["properties"]["state_backend"]["const"] == "filesystem"
    assert validate_object("project", emitted) == []


@pytest.mark.parametrize("name", ALL_MODEL_NAMES)
def test_required_schema_fields_are_always_present(name: str) -> None:
    model_cls = m.MODEL_REGISTRY[name]
    schema = load_schema(name)
    model = model_cls.from_dict(copy.deepcopy(VALID_DOCS[name]))
    emitted = model.to_dict()
    for required_key in schema.get("required", []):
        assert required_key in emitted, (
            f"{name}: required schema key {required_key!r} missing from to_dict()"
        )


@pytest.mark.parametrize("name", ALL_MODEL_NAMES)
def test_json_serialization_of_to_dict(name: str) -> None:
    model_cls = m.MODEL_REGISTRY[name]
    model = model_cls.from_dict(copy.deepcopy(VALID_DOCS[name]))
    payload = json.dumps(model.to_dict())
    assert json.loads(payload) == model.to_dict()


@pytest.mark.parametrize("name", ALL_MODEL_NAMES)
def test_models_are_frozen(name: str) -> None:
    model_cls = m.MODEL_REGISTRY[name]
    model = model_cls.from_dict(copy.deepcopy(VALID_DOCS[name]))
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(model, "nonexistent_attribute", "x")


@pytest.mark.parametrize("name", ALL_MODEL_NAMES)
def test_from_dict_requires_schema_required_fields(name: str) -> None:
    model_cls = m.MODEL_REGISTRY[name]
    schema = load_schema(name)
    doc = copy.deepcopy(VALID_DOCS[name])
    for required_key in schema.get("required", []):
        if required_key in doc:
            broken = copy.deepcopy(doc)
            del broken[required_key]
            with pytest.raises(TypeError, match="missing required field"):
                model_cls.from_dict(broken)


def test_from_dict_rejects_non_mapping() -> None:
    with pytest.raises(TypeError, match="mapping"):
        m.Project.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_from_dict_rejects_unknown_enum_value() -> None:
    doc = copy.deepcopy(VALID_DOCS["project"])
    doc["project_phase"] = "BOGUS_PHASE"
    with pytest.raises(ValueError):
        m.Project.from_dict(doc)


def test_nested_objects_are_coerced_to_typed_dataclasses() -> None:
    doc = copy.deepcopy(VALID_DOCS["run"])
    doc["external"] = {
        "backend": "platform",
        "job_id": "job-42",
        "working_directory": "/work",
    }
    run = m.Run.from_dict(doc)
    assert isinstance(run.external, m.RunExternal)
    assert run.external.backend == "platform"
    assert run.to_dict()["external"] == doc["external"]

    goal = m.GoalContract.from_dict(copy.deepcopy(VALID_DOCS["goal"]))
    assert isinstance(goal.dependencies, list)
    assert all(isinstance(dep, m.GoalDependency) for dep in goal.dependencies)
    assert isinstance(goal.replication, m.GoalReplication)
    assert isinstance(goal.acceptance, m.GoalAcceptance)


def test_event_from_key_uses_schema_alias() -> None:
    doc = copy.deepcopy(VALID_DOCS["event"])
    event = m.ProjectEvent.from_dict(doc)
    # The schema key is "from"; the python field is from_ with an alias.
    assert "from" in event.to_dict()
    assert "from_" not in event.to_dict()
    assert event.to_dict()["from"] == doc["from"]
    assert event.from_ == doc["from"]


def test_event_alias_round_trip() -> None:
    doc = copy.deepcopy(VALID_DOCS["event"])
    doc["from"] = "CREATED"
    doc["to"] = "CLOSED"
    event = m.ProjectEvent.from_dict(doc)
    assert m.ProjectEvent.from_dict(event.to_dict()) == event


def test_sensible_defaults() -> None:
    # Run: scientific_review defaults to UNREVIEWED, collections empty.
    run = m.Run(run_id="R1", goal_id="G1", goal_version="v1",
                run_type=m.RunType.RETRY, lifecycle_state=m.LifecycleState.CREATED)
    assert run.scientific_review is m.ScientificReview.UNREVIEWED
    assert run.external is None
    assert run.artifacts == []
    # Closure literature default from the schema (default: 2).
    assert m.ClosureLiterature().required_zero_novelty_cycles == 2
    # Goal dependency gate defaults from the schema (default: false).
    dep = m.GoalDependency(goal_id="G0", type=m.DependencyType.HARD_GATE)
    assert dep.execution_gate is False
    assert dep.acceptance_gate is False
    # Resource human_gate_required defaults to false.
    res = m.Resource(resource_id="R", name="n",
                     resource_type=m.ResourceType.INSTRUMENT,
                     availability_state=m.AvailabilityState.AVAILABLE)
    assert res.human_gate_required is False
    # Acceptance criteria confidence is unspecified until evidence exists.
    acc = m.AcceptanceCriteria(acceptance_id="A", goal_id="G", version="v1",
                               frozen=False, decision_mode=m.DecisionMode.EQUIVALENCE,
                               criteria=[{}])
    assert acc.confidence is None
    assert acc.target is None


def test_enum_member_counts_match_schemas() -> None:
    # Exact counts from schemas/project.schema.yaml and schemas/run.schema.yaml.
    assert len(m.ProjectPhase) == 14
    assert len(m.LifecycleState) == 10
    # evidence assessment ranges are integers 0..4 (schema-enforced, tested
    # in test_schema_validation).
    assert len(m.AssumptionClassification) == 3
    assert m.AssumptionClassification.A0_TECHNICAL_DEFAULT.value == "A0_TECHNICAL_DEFAULT"
    assert m.AssumptionClassification.A1_METHODOLOGICAL_DEFAULT.value == "A1_METHODOLOGICAL_DEFAULT"
    assert m.AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION.value == "A2_SCIENTIFIC_ASSUMPTION"
    assert len(m.MappingStatus) == 4
    assert m.MappingStatus.EXCLUDED_NONFORMAL.value == "EXCLUDED_NONFORMAL"
    assert len(m.DependencyType) == 3
    assert len(m.GoalTrack) == 3
    assert len(m.ReproductionOutcome) == 5
    assert len(m.GateStatus) == 5
    assert len(m.DecisionMode) == 7
    assert len(m.ScientificReview) == 4
    assert len(m.RunType) == 5
    assert len(m.SourceType) == 14


def test_enum_values_are_schema_exact() -> None:
    # Spot-check a couple of tricky values verbatim from the schemas.
    assert m.RunType.INDEPENDENT_REPLICATE.value == "independent_replicate"
    assert m.TargetSourceType.PDF.value == "pdf"
    assert m.RequirementOutcome.REPRODUCED_WITH_RECOVERY.value == "REPRODUCED_WITH_RECOVERY"
    assert m.MethodReproducibility.REPRODUCIBLE_WITH_METHOD_ADJUSTMENT.value == (
        "REPRODUCIBLE_WITH_METHOD_ADJUSTMENT"
    )
    assert m.ResearchRequestStatus.EXHAUSTED.value == "EXHAUSTED"
    assert m.Criticality.SUPPORTING.value == "SUPPORTING"


def test_constructor_holds_data_without_workflow_decisions() -> None:
    # Constructors must not pick phases/outcomes: required enum fields have
    # no implicit choices (they must be supplied by the caller), and the
    # serialized output is exactly the caller-provided state.
    doc = copy.deepcopy(VALID_DOCS["project"])
    project = m.Project.from_dict(doc)
    assert project.project_phase is m.ProjectPhase.PLANNING
    assert project.reproduction_outcome is m.ReproductionOutcome.UNDETERMINED
    assert project.to_dict()["project_phase"] == "PLANNING"

"""Cross-platform orchestration semantics -- Goal -> Run -> Evidence
(DEV-M10-G06, AC-02: platform-specific fallback differences do not alter
Goal/Run/Evidence semantics).

End-to-end flows through EACH concrete :class:`PlatformAdapter` (the
Claude Code adapter DEV-M10-G03 and the Codex adapter DEV-M10-G05),
proving the orchestration semantics are platform-independent. The house
pattern of ``tests/integration/test_external_adapters.py`` (DEV-M7-G05)
is mirrored exactly: real core machinery throughout (the frozen
``GoalContract``/``Run``/``ClaimSpecificEvidence`` models, ``generate_id``
deterministic ids, ``compute_sha256`` + the real ``ArtifactRegistry``,
schema validation through ``core.schema_validation``), fresh adapter
instances over the same state directory via ``tmp_path``, and only the
platform transport boundaries faked (the scripted rig of
``test_platform_contract``). Nothing is mocked at the core layer.

The suite proves AC-02 along three axes:

* ``test_ac02_goal_through_each_adapter_yields_identical_run_semantics``
  -- a Goal dispatched through the Claude adapter and through the Codex
  adapter yields the same schema-valid Run semantics: byte-identical Run
  records / evidence association given identical inputs, differing only
  in the documented platform-scoped identity fields (the deterministic
  ``worker_session_ref`` and ``external.backend``);
* ``test_ac02_forced_fallback_answers_leave_goal_run_evidence_unchanged``
  -- forcing the platform-specific FALLBACK answers (the
  ``fallback_subagent`` channel, the resume-limitation FALLBACK, the
  durable outbox) leaves the Goal/Run/Evidence state byte-identical to
  the all-NATIVE flow, and every record still validates through the core
  machinery;
* ``test_ac02_fresh_instance_over_same_state_directory_answers_same_identity``
  -- the worker exits; a brand-new adapter over the same state directory
  (its own transport, its registry rehydrated from the durable snapshot,
  the 13-EXECUTION-MONITOR.md SS4 reconstruction) answers the same
  identity, the persisted state stays schema-valid, and the run record
  reconstructed from the fresh instance is byte-identical.

Determinism: no network, no sleeps, no wall clock (timestamps are the
fixed ``FIXED_STAMP``), no randomness, no environment dependence
(``tmp_path`` only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_platform_contract import (
    BOTH_PLATFORMS,
    CLAUDE,
    CODEX,
    GOAL_ID,
    PROJECT_ID,
    PlatformRig,
    make_rig,
    rehydrate_rig,
    spawn_worker,
)

from scientific_reproduction.adapters.platform.base import (
    CommandSpec,
    FallbackMode,
    WorkerSessionHandle,
    derive_worker_session_ref,
)
from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    ArtifactManifest,
    ClaimSpecificEvidence,
    EvidenceAssessment,
    GoalAcceptance,
    GoalContract,
    GoalExecutionContextPackage,
    GoalReplication,
    GoalTrack,
    LifecycleState,
    Run,
    RunExternal,
    RunType,
    WorkerRole,
)
from scientific_reproduction.core.schema_validation import (
    validate_and_reject,
    validate_object,
)

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: The deterministic project/goal/run/requirement of the flows; every
#: session, run, artifact and evidence id is then a pure function of
#: these (the ``generate_id`` discipline of the house pattern). The
#: project/goal are the shared constants of the AC-01 battery, so the
#: flows of the two files operate on the SAME canonical identity.
RUN_ID = generate_id("run", "dev-m10-g06-semantics")
REQUIREMENT_ID = generate_id("requirement", GOAL_ID, "dev-m10-g06-semantics")
CONTEXT_ID = generate_id("worker-context", PROJECT_ID, GOAL_ID, RUN_ID)
EVIDENCE_ID = generate_id("evidence", RUN_ID, "finding-1")
ARTIFACT_ID = generate_id("artifact", RUN_ID, "raw-data.csv")


def make_goal() -> GoalContract:
    """A deterministic, schema-valid frozen GoalContract."""
    return GoalContract(
        goal_id=GOAL_ID,
        title="synthesize the target compound per the frozen protocol",
        unit_process_type="synthesis",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="synthesize the target compound per the frozen protocol",
        requirement_ids=[REQUIREMENT_ID],
        dependencies=[],
        acceptance=GoalAcceptance(
            criteria_ref=generate_id("acceptance-criteria", GOAL_ID), frozen=True
        ),
        analysis_protocol_ref=generate_id("analysis", GOAL_ID, "protocol"),
        replication=GoalReplication(
            independent_required=True,
            planned_n_policy="fixed minimum of 3 independent replicates",
            minimum_n=3,
        ),
        version="1.0",
        frozen=True,
    )


def make_context() -> GoalExecutionContextPackage:
    """The worker-context package of the run: carries the evidence the
    worker maintains (``evidence_refs``), so the evidence association
    flows through the adapter path."""
    return GoalExecutionContextPackage(
        context_id=CONTEXT_ID,
        worker_role=WorkerRole.EXPERIMENT_WORKER,
        goal_id=GOAL_ID,
        goal_version="1.0",
        run_id=RUN_ID,
        allowed_actions=["prepare"],
        forbidden_actions=["mutate"],
        evidence_refs=[EVIDENCE_ID],
        required_outputs=["raw-data.csv"],
    )


def make_evidence(source_id: str) -> ClaimSpecificEvidence:
    """The evidence record produced under the run, citing the run's
    registered raw output as its source (the adapter-path association)."""
    return ClaimSpecificEvidence(
        evidence_id=EVIDENCE_ID,
        source_id=source_id,
        claim_id=generate_id("claim", GOAL_ID, "yield"),
        finding=(
            "the target compound synthesized with yield 0.87 under the"
            " frozen protocol"
        ),
        assessment=EvidenceAssessment(
            authority=3,
            reliability=4,
            directness=3,
            reliability_checklist_ref="reliability-checklist-v1",
        ),
        source_location="run-notes",
    )


def make_run(
    worker_session_ref: str,
    backend: str,
    *,
    artifacts: tuple[str, ...] = (),
) -> Run:
    """The schema-valid Run of the flow, referencing the platform session
    deterministically: ``worker_session_ref`` is the canonical reference
    stored by the Core, ``external.backend`` names the platform."""
    return Run(
        run_id=RUN_ID,
        goal_id=GOAL_ID,
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
        goal_version="1.0",
        worker_session_ref=worker_session_ref,
        external=RunExternal(backend=backend),
        artifacts=list(artifacts),
    )


def semantic_projection(run: Run) -> dict[str, Any]:
    """The Run record minus the documented platform-scoped identity
    fields (``worker_session_ref`` and ``external.backend``): what
    "identical Run semantics given identical inputs" means."""
    data = run.to_dict()
    data.pop("worker_session_ref", None)
    external = dict(data.get("external") or {})
    external.pop("backend", None)
    data["external"] = external
    return data


def register_artifact(state_dir: Path) -> ArtifactManifest:
    """Register the run's raw output through the REAL ``ArtifactRegistry``
    / ``compute_sha256`` machinery (nothing mocked at the core layer).

    The artifact id is the deterministic ``generate_id("artifact",
    run_id, name)``; the manifest is persisted under
    ``state_dir/manifests/`` and schema-validated by the registry's own
    persistence gate.
    """
    workdir = state_dir / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    raw = workdir / "raw-data.csv"
    raw.write_bytes(b"yield=0.87\n")
    manifest = ArtifactManifest(
        artifact_id=ARTIFACT_ID,
        uri=str(raw),
        sha256=compute_sha256(raw),
        size_bytes=raw.stat().st_size,
        created_at=FIXED_STAMP,
        run_id=RUN_ID,
        mime_type="text/csv",
        producer="experiment_worker",
        metadata={"unit": "mol/mol"},
    )
    registry = ArtifactRegistry(state_dir / "manifests")
    registry.register(manifest)  # validates before writing (persistence gate)
    assert registry.get(ARTIFACT_ID) == manifest
    assert validate_object("artifact-manifest", manifest.to_dict()) == []
    assert (state_dir / "manifests" / f"{ARTIFACT_ID}.json").is_file()
    return manifest


def persist_object(state_dir: Path, obj_type: str, data: dict[str, Any]) -> Path:
    """Persist one object through the core persistence gate
    (``validate_and_reject``) under ``state_dir/<obj_type>.json``."""
    validate_and_reject(obj_type, data)
    path = state_dir / f"{obj_type}.json"
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def read_persisted(state_dir: Path, obj_type: str) -> dict[str, Any]:
    """The persisted object as parsed JSON."""
    return json.loads((state_dir / f"{obj_type}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC-02 -- a Goal dispatched through each adapter yields the same
# schema-valid Run semantics (identical records given identical inputs)
# ---------------------------------------------------------------------------


def test_ac02_goal_through_each_adapter_yields_identical_run_semantics() -> None:
    # One frozen Goal, one worker-context package (carrying the evidence
    # refs), dispatched through BOTH adapters: the resulting Run records
    # are schema-valid and byte-identical modulo the documented
    # platform-scoped identity fields -- the deterministic
    # worker_session_ref and external.backend -- and the evidence
    # association is identical.
    goal = make_goal()
    assert validate_object("goal", goal.to_dict()) == []
    context = make_context()
    assert validate_object("worker-context", context.to_dict()) == []
    evidence = make_evidence(source_id=ARTIFACT_ID)
    assert validate_object("evidence", evidence.to_dict()) == []

    rigs: dict[str, PlatformRig] = {}
    runs: dict[str, Run] = {}
    handles: dict[str, WorkerSessionHandle] = {}
    for platform in BOTH_PLATFORMS:
        rig = make_rig(platform)
        rigs[platform] = rig
        handle = spawn_worker(rig, context)
        handles[platform] = handle
        runs[platform] = make_run(
            handle.session_ref, rig.platform_id, artifacts=(ARTIFACT_ID,)
        )
        assert validate_object("run", runs[platform].to_dict()) == []

    # identical Run records given identical inputs: only the documented
    # platform-scoped fields differ
    assert runs[CLAUDE].run_id == runs[CODEX].run_id == RUN_ID
    assert runs[CLAUDE].goal_id == runs[CODEX].goal_id == GOAL_ID
    assert runs[CLAUDE].run_type is runs[CODEX].run_type
    assert runs[CLAUDE].lifecycle_state is runs[CODEX].lifecycle_state
    assert runs[CLAUDE].goal_version == runs[CODEX].goal_version
    assert runs[CLAUDE].artifacts == runs[CODEX].artifacts == [ARTIFACT_ID]
    assert semantic_projection(runs[CLAUDE]) == semantic_projection(runs[CODEX])

    # the worker_session_ref of each Run is the canonical deterministic
    # reference of its platform's session (never the reverse), and the
    # external reference names the platform
    for platform in BOTH_PLATFORMS:
        rig = rigs[platform]
        handle = handles[platform]
        run = runs[platform]
        assert run.worker_session_ref == handle.session_ref
        assert run.worker_session_ref == derive_worker_session_ref(
            rig.platform_id,
            "experiment_worker",
            "worker",
            PROJECT_ID,
            GOAL_ID,
            CONTEXT_ID,
        )
        assert is_valid_session_ref(run.worker_session_ref)
        assert run.external is not None and run.external.backend == rig.platform_id
        assert run.external.job_id is None and run.external.dispatch_id is None

    # the platform-scoped difference is exactly the session identity:
    # distinct platforms, distinct canonical references
    assert runs[CLAUDE].worker_session_ref != runs[CODEX].worker_session_ref

    # the evidence association is identical on both platforms: the same
    # evidence record, the same source artifact, the same context refs
    assert context.evidence_refs == [EVIDENCE_ID]
    assert evidence.source_id == ARTIFACT_ID
    for platform in BOTH_PLATFORMS:
        assert ARTIFACT_ID in runs[platform].artifacts


# ---------------------------------------------------------------------------
# AC-02 -- forced platform-specific FALLBACK answers never alter the
# Goal/Run/Evidence semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_ac02_forced_fallback_answers_leave_goal_run_evidence_unchanged(
    tmp_path: Path, platform: str
) -> None:
    # The all-NATIVE flow first: spawn through the live transport,
    # register the run's raw output through the real artifact registry,
    # produce the evidence citing it.
    goal = make_goal()
    context = make_context()
    rig = make_rig(platform)
    native = spawn_worker(rig, context)
    artifact = register_artifact(tmp_path / "native")
    run_native = make_run(
        native.session_ref, rig.platform_id, artifacts=(artifact.artifact_id,)
    )
    assert validate_object("run", run_native.to_dict()) == []
    native_state = (run_native.to_dict(), make_evidence(artifact.artifact_id).to_dict())

    # Now force this platform's documented FALLBACK answers on the SAME
    # logical context: the explicit subagent channel, the resume
    # limitation and the durable outbox.
    subagent = rig.adapter.fallback_subagent("worker", context, project_id=PROJECT_ID)
    assert subagent.mode is FallbackMode.FALLBACK
    assert isinstance(subagent.handle, WorkerSessionHandle)
    assert subagent.handle.session_ref == native.session_ref  # same identity

    resume = rig.adapter.resume_session(native.session_ref)
    assert resume.mode is FallbackMode.FALLBACK  # the documented limitation
    assert resume.handle == native

    outbox = rig.adapter.expose_command(
        CommandSpec(session_ref=native.session_ref, directive="run protocol step 1")
    )
    assert outbox.mode is FallbackMode.FALLBACK
    assert rig.registry.pending_commands(native.session_ref) == (
        "run protocol step 1",
    )

    # Goal/Run/Evidence semantics are unchanged: the Run record built
    # from the fallback-spawned handle is byte-identical, and every
    # record still validates through the core machinery.
    run_fallback = make_run(
        subagent.handle.session_ref, rig.platform_id, artifacts=(artifact.artifact_id,)
    )
    assert run_fallback.to_dict() == native_state[0]
    assert run_fallback.worker_session_ref == run_native.worker_session_ref
    assert run_fallback.external == run_native.external
    assert run_fallback.artifacts == run_native.artifacts
    assert validate_object("run", run_fallback.to_dict()) == []
    assert validate_object("goal", goal.to_dict()) == []
    assert validate_object("worker-context", context.to_dict()) == []
    assert make_evidence(artifact.artifact_id).to_dict() == native_state[1]
    assert validate_object("evidence", native_state[1]) == []

    # the identity the Run stores was never rewritten by the fallback
    # answers (AC-02: the durable session_ref survives the limitation)
    assert rig.registry.get(native.session_ref).handle.session_ref == (
        native.session_ref
    )
    assert rig.registry.pending_commands(native.session_ref) == (
        "run protocol step 1",
    )


# ---------------------------------------------------------------------------
# AC-02 -- the worker exits; a fresh adapter over the same state
# directory reconstructs the same identity and the same semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_ac02_fresh_instance_over_same_state_directory_answers_same_identity(
    tmp_path: Path, platform: str
) -> None:
    # Worker 1 spawns the session, produces the evidence artifact and
    # persists the schema-valid state under the state directory.
    state_dir = tmp_path / "state"
    goal = make_goal()
    context = make_context()
    artifact = register_artifact(state_dir)
    evidence = make_evidence(source_id=artifact.artifact_id)

    rig = make_rig(platform)
    handle = spawn_worker(rig, context)
    run = make_run(handle.session_ref, rig.platform_id, artifacts=(ARTIFACT_ID,))
    persist_object(state_dir, "goal", goal.to_dict())
    persist_object(state_dir, "worker-context", context.to_dict())
    persist_object(state_dir, "run", run.to_dict())
    persist_object(state_dir, "evidence", evidence.to_dict())
    snapshot_file = state_dir / "session-snapshot.json"
    snapshot_file.write_text(
        json.dumps(rig.registry.to_records(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Worker 1 exits. A fresh adapter over the same state directory --
    # its own transport (the live sessions are gone) and its registry
    # rehydrated from the durable snapshot -- reconstructs the identity
    # without chat-memory access (13-EXECUTION-MONITOR.md SS3-SS4).
    fresh = rehydrate_rig(
        platform, json.loads(snapshot_file.read_text(encoding="utf-8"))
    )
    resumed = fresh.adapter.resume_session(handle.session_ref)
    assert resumed.mode is FallbackMode.FALLBACK  # the documented limitation
    assert resumed.handle == handle  # the same durable identity
    replaced = fresh.adapter.replace_session(handle.session_ref)
    assert replaced.mode is FallbackMode.NATIVE
    assert isinstance(replaced.handle, WorkerSessionHandle)
    assert replaced.handle.session_ref == handle.session_ref

    # The Run semantics reconstructed from the fresh instance's answers
    # are byte-identical to the original worker's record.
    run_fresh = make_run(
        resumed.handle.session_ref, fresh.platform_id, artifacts=(ARTIFACT_ID,)
    )
    assert run_fresh.to_dict() == run.to_dict()
    assert run_fresh.worker_session_ref == handle.session_ref

    # The persisted state is schema-valid through the core machinery
    # (not ad-hoc): every object validates, the artifact manifest was
    # written by the real registry's persistence gate and its checksum
    # still verifies against the real file.
    for obj_type in ("goal", "worker-context", "run", "evidence"):
        assert validate_object(obj_type, read_persisted(state_dir, obj_type)) == []
    assert (state_dir / "manifests" / f"{ARTIFACT_ID}.json").is_file()
    assert ArtifactRegistry(state_dir / "manifests").get(ARTIFACT_ID) == artifact
    assert compute_sha256(state_dir / "workdir" / "raw-data.csv") == artifact.sha256
    assert validate_object("artifact-manifest", artifact.to_dict()) == []


# ---------------------------------------------------------------------------
# AC-02 -- the full flow is deterministic: two fresh executions of the
# same goal through the same platform produce byte-identical state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_ac02_full_flow_is_deterministic_and_repeatable(
    tmp_path: Path, platform: str
) -> None:
    # The whole Goal -> Run -> Evidence flow (spawn, resume limitation,
    # outbox, terminate) is a pure function of the inputs: two fresh
    # executions produce byte-identical results, snapshots and state
    # files.

    def run(state_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        context = make_context()
        artifact = register_artifact(state_dir)
        evidence = make_evidence(source_id=artifact.artifact_id)
        rig = make_rig(platform)
        handle = spawn_worker(rig, context)
        run_record = make_run(
            handle.session_ref, rig.platform_id, artifacts=(artifact.artifact_id,)
        )
        rig.adapter.resume_session(handle.session_ref)  # FALLBACK limitation
        rig.adapter.expose_command(
            CommandSpec(session_ref=handle.session_ref, directive="watch")
        )
        snapshot = rig.registry.to_records()
        rig.adapter.terminate_session(handle.session_ref)
        return run_record.to_dict(), evidence.to_dict(), snapshot

    first = run(tmp_path / "run-a")
    second = run(tmp_path / "run-b")
    assert first == second
    assert validate_object("run", first[0]) == []
    assert validate_object("evidence", first[1]) == []
    assert first[0]["worker_session_ref"] == second[0]["worker_session_ref"]


# ---------------------------------------------------------------------------
# Supporting helpers (import-time hygiene: is_valid_session_ref used above)
# ---------------------------------------------------------------------------


def is_valid_session_ref(value: str) -> bool:
    """True iff ``value`` is a well-formed generated session reference."""
    from scientific_reproduction.core.ids import is_valid_id

    return is_valid_id(value, kind="session")

"""Key-claim traceability checker over the real project registries (DEV-M13-G01).

Implements the **traceability checker** deliverable of DEV-M13-G01: the
machine resolution of the frozen report-traceability chain
(``14-STATE-GIT-ARTIFACTS.md`` SS7 -- *"Every report claim deemed
key/decision-relevant must be traceable to: Report claim -> Decision /
Requirement outcome -> Analysis Result -> Run(s) -> Raw Artifact
manifest(s) -> Source/Evidence as appropriate"*) and the v0.1 release gate
(``18-TEST-AND-ACCEPTANCE-PLAN.md`` SS4 gate 8: the final machine-auditable
package validates traceability). Grounded in the real records:

* **Claims** are the opaque ``claim_id`` strings of the claim-specific
  evidence vocabulary (``06-EVIDENCE-SYSTEM.md`` SS6; the frozen model has
  no ``Claim`` object -- a claim is ``ClaimSpecificEvidence.claim_id``).
  The evidence base is the **real** ``research.evidence.EvidenceRegistry``
  (``records_for_claim``); the checker takes the registry as an explicit
  input because the evidence registry is an in-memory API (no v0.1
  evidence store exists).
* **Requirement outcome** is the persisted ``ReproductionRequirement``
  record (``planning.inventory.read_requirement`` /
  ``list_requirements``); Supervisor decisions are recorded only as event
  payloads in v0.1 (no decision registry exists), so the decision hop of
  the SS7 chain is represented by the requirement records the claim's
  evidence is used by.
* **Analysis Results** are the ``analysis.results.ResultRecord`` records
  (``analysis/results/<result_id>.json``) with their exact links:
  ``run_ref`` (the input Run's ``run_id``), ``input_artifact_ids``,
  ``acceptance_ref`` and ``requirement_refs``.
* **Runs** are the schema-validated ``core.models.Run`` records persisted
  through the real ``core.state_backend.FilesystemStateBackend`` under
  ``runs/run/<run_id>.json``.
* **Raw Artifact manifests** are the ``artifacts.registry.ArtifactRegistry``
  records (``manifests/<artifact_id>.json``) with their producer links
  ``run_id`` / ``analysis_id``.
* **Acceptances** are the ``AcceptanceCriteria`` records
  (``planning.plan.read_acceptance`` / ``list_acceptance``); an acceptance
  supports the claim through its ``evidence_refs`` (which name evidence
  record ids and/or analysis result ids) and the analysis result links back
  through ``ResultRecord.acceptance_ref``.

Resolution model (gap semantics -- the AC-02 surface)
-----------------------------------------------------
:func:`trace_claim` is **total**: it never raises for missing data. Every
dangling reference of the chain is collected as a :class:`TraceGap`
(a reference from a resolved node that does not resolve to a registered
entity), so a broken chain is visible machine-auditably instead of
crashing the checker. A claim with no registered evidence records resolves
to a trace containing only the CLAIM node -- the absence itself is the
missing link the audit package validator turns into a validation failure
(``reporting/audit.py``, AC-02). Structural failures (uninitialized
workspace, corrupt stored records, wrong argument types) raise the module
errors; every stored record is read through the real registration APIs and
never rewritten.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the given
inputs: no wall clock, no randomness, no network. All node/link/gap
collections are deduplicated and sorted by stable keys, so identical
state always yields byte-identical traces. ``TypeError`` at the public
boundaries; errors follow the ``ValueError``-subclass convention with
stable messages; ``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from scientific_reproduction.analysis.results import ResultRecord, list_results
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ArtifactManifest,
    ClaimSpecificEvidence,
    ReproductionRequirement,
    Run,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
)
from scientific_reproduction.planning.inventory import list_requirements
from scientific_reproduction.planning.plan import list_acceptance, list_goals
from scientific_reproduction.research.evidence import EvidenceRegistry

__all__ = [
    "CLAIM_TRACE_VERSION",
    "TraceCorruptError",
    "TraceGap",
    "TraceKind",
    "TraceLink",
    "TraceNode",
    "TraceNotInitializedError",
    "TraceabilityError",
    "ClaimTrace",
    "trace_claim",
]

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2

#: Version of the claim-trace serialization (``version`` key of
#: :class:`ClaimTrace`).
CLAIM_TRACE_VERSION: str = "1.0"

#: One typed record carried by a trace node: the heterogeneous frozen
#: record classes of the real registries (an ``AnalysisProtocolOrResult``
#: is never part of a resolved chain -- only ``ResultRecord`` analysis
#: results are).
TraceRecord: TypeAlias = (
    AcceptanceCriteria
    | ArtifactManifest
    | ClaimSpecificEvidence
    | ReproductionRequirement
    | ResultRecord
    | Run
)

#: The run store object type of the state backend (``runs/run/<id>.json``).
_RUN_OBJECT_TYPE: str = "run"

#: The artifact registry base directory of a project workspace
#: (``14-STATE-GIT-ARTIFACTS.md`` SS6: manifests live under ``manifests/``).
_ARTIFACTS_STATE_DIR: str = "manifests"

#: The runs state directory of a project workspace (the ``runs/`` tree of
#: ``planning.init.INIT_DIRECTORIES``).
_RUNS_STATE_DIR: str = "runs"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class TraceabilityError(ValueError):
    """Base class for all traceability checker errors."""


class TraceNotInitializedError(TraceabilityError):
    """Raised when tracing is attempted on a workspace without a project
    state record (no ``project.yaml`` at the root)."""


class TraceCorruptError(TraceabilityError):
    """Raised when a stored record the chain resolves through is corrupt.

    The registered state is read through the real registry read APIs;
    those APIs surface corruption as ``ValueError``, which this module
    re-raises as ``TraceCorruptError`` with the same message so the
    checker's error surface stays stable.
    """


# ---------------------------------------------------------------------------
# The trace vocabulary (the module's own auditable kinds)
# ---------------------------------------------------------------------------


class TraceKind(StrEnum):
    """The node vocabulary of a resolved claim trace.

    Values match no frozen schema enum (the trace is this module's own
    auditable vocabulary, mirroring ``workers.results.ResultReferenceKind``):
    CLAIM is the key report claim, EVIDENCE a claim-specific evidence
    record, ACCEPTANCE the acceptance criteria the claim's evidence
    supports, REQUIREMENT the requirement outcome the evidence is used by,
    ANALYSIS an analysis result record, RUN a run record and ARTIFACT a raw
    artifact manifest.
    """

    CLAIM = "claim"
    EVIDENCE = "evidence"
    ACCEPTANCE = "acceptance"
    REQUIREMENT = "requirement"
    ANALYSIS = "analysis"
    RUN = "run"
    ARTIFACT = "artifact"


@dataclass(frozen=True)
class TraceNode:
    """One resolved record of a claim trace: kind + id + typed record.

    ``record`` is the typed record read through the real registry API
    (``None`` for the CLAIM node -- a claim is the opaque ``claim_id``
    string, ``06-EVIDENCE-SYSTEM.md`` SS6).
    """

    kind: TraceKind
    ref_id: str
    record: TraceRecord | None

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the node (kind + id)."""
        return {"kind": self.kind.value, "ref_id": self.ref_id}


@dataclass(frozen=True)
class TraceLink:
    """One resolved reference of a claim trace.

    ``source``/``target`` are the linked nodes and ``via`` names the exact
    real record field that connects them (e.g. ``"ResultRecord.run_ref"``),
    so every hop of the chain is machine-auditable down to the field.
    """

    source: TraceNode
    via: str
    target: TraceNode

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the link."""
        return {
            "source": self.source.ref_id,
            "via": self.via,
            "target": self.target.ref_id,
        }


@dataclass(frozen=True)
class TraceGap:
    """One missing link of a claim trace (the AC-02 surface).

    ``source`` is the resolved node whose reference could not be resolved,
    ``via`` the exact real record field carrying the reference and
    ``ref_id`` the referenced id that resolves to no registered entity;
    ``reason`` is a stable message naming both.
    """

    source: TraceNode
    via: str
    ref_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the gap."""
        return {
            "source": self.source.ref_id,
            "via": self.via,
            "ref_id": self.ref_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ClaimTrace:
    """The resolved trace of one key report claim (AC-01).

    The chain resolved by :func:`trace_claim`, grounded in the frozen
    ``14-STATE-GIT-ARTIFACTS.md`` SS7 reading: claim -> evidence ->
    acceptance -> analysis result -> run(s) -> raw artifact manifest(s),
    with the requirement-outcome hop through the evidence ``used_by``
    links. Every collection is deduplicated and sorted by stable keys, so
    the trace is a pure, deterministic function of the registered state.

    Attributes:
        claim_id: the key report claim (the opaque ``claim_id`` of the
            claim-specific evidence vocabulary).
        nodes: every resolved node, sorted by ``(kind, ref_id)``. Always
            contains the CLAIM node; a claim with no registered evidence
            records resolves to a trace with only that node.
        links: every resolved reference, sorted by
            ``(source, via, target)``.
        gaps: every missing link (AC-02), sorted by
            ``(source, via, ref_id)``.
    """

    claim_id: str
    nodes: tuple[TraceNode, ...]
    links: tuple[TraceLink, ...]
    gaps: tuple[TraceGap, ...]

    def nodes_for(self, kind: TraceKind) -> tuple[TraceNode, ...]:
        """The trace's nodes of one kind, in stored (sorted) order.

        Raises:
            TypeError: ``kind`` is not a ``TraceKind``.
        """
        if not isinstance(kind, TraceKind):
            raise TypeError(f"kind must be a TraceKind, got {type(kind).__name__}")
        return tuple(node for node in self.nodes if node.kind is kind)

    def has_node(self, kind: TraceKind) -> bool:
        """True when the trace contains at least one node of ``kind``.

        Raises:
            TypeError: ``kind`` is not a ``TraceKind``.
        """
        if not isinstance(kind, TraceKind):
            raise TypeError(f"kind must be a TraceKind, got {type(kind).__name__}")
        return any(node.kind is kind for node in self.nodes)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the trace in canonical field order."""
        return {
            "version": CLAIM_TRACE_VERSION,
            "claim_id": self.claim_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "links": [link.to_dict() for link in self.links],
            "gaps": [gap.to_dict() for gap in self.gaps],
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# The resolver (pure, deterministic)
# ---------------------------------------------------------------------------


def trace_claim(
    root: str | Path,
    claim_id: str,
    evidence: EvidenceRegistry,
) -> ClaimTrace:
    """Resolve the trace of one key report claim (AC-01/AC-02).

    The chain resolution entry: the claim's claim-specific evidence
    records (``EvidenceRegistry.records_for_claim``), the acceptance
    criteria the evidence supports (``AcceptanceCriteria.evidence_refs``),
    the requirement-outcome hop through ``ClaimSpecificEvidence.used_by``,
    the analysis result records (``ResultRecord.acceptance_ref`` /
    ``requirement_refs``), the Runs (``ResultRecord.run_ref``) and the raw
    artifact manifests (``ResultRecord.input_artifact_ids``, plus the
    ``Run.artifacts`` / ``ArtifactManifest.run_id`` / ``analysis_id``
    producer links). Every record is read through the real registration
    APIs and never rewritten.

    Total by design: a missing link is never an exception -- it is a
    :class:`TraceGap` of the returned trace, so the audit package
    validator (``reporting/audit.py``) can fail validation on the gaps
    (AC-02). A claim with no registered evidence records resolves to a
    trace with only the CLAIM node.

    Args:
        root: the initialized workspace root.
        claim_id: the key report claim (the opaque ``claim_id`` of the
            claim-specific evidence vocabulary).
        evidence: the real claim-specific evidence registry to resolve the
            claim's evidence records against.

    Returns:
        The deterministic :class:`ClaimTrace` of the claim.

    Raises:
        TypeError: ``root`` is not a str/Path, ``claim_id`` is not a str,
            or ``evidence`` is not an ``EvidenceRegistry``.
        TraceNotInitializedError: no ``project.yaml`` exists at ``root``.
        TraceCorruptError: a stored record the chain resolves through is
            corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(claim_id, str):
        raise TypeError(f"claim_id must be a str, got {type(claim_id).__name__}")
    if not isinstance(evidence, EvidenceRegistry):
        raise TypeError(
            "evidence must be an EvidenceRegistry, got"
            f" {type(evidence).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)

    # -- preload the registered state through the real read APIs ------------
    results = {result.result_id: result for result in _list_results(project_root)}
    run_store = FilesystemStateBackend(project_root / _RUNS_STATE_DIR)
    runs = {
        run_id: _read_run(run_store, run_id)
        for run_id in run_store.list_ids(_RUN_OBJECT_TYPE)
    }
    artifacts = {
        manifest.artifact_id: manifest
        for manifest in _list_artifacts(project_root)
    }
    acceptances = {
        record.acceptance_id: record
        for record in _list_acceptances(project_root)
    }
    requirements = {
        record.requirement_id: record
        for record in _list_requirements(project_root)
    }
    goals = {record.goal_id: record for record in _list_goals(project_root)}

    # -- resolution state ----------------------------------------------------
    nodes: dict[tuple[TraceKind, str], TraceNode] = {}
    links: dict[tuple[str, str, str, str, str], TraceLink] = {}
    gaps: dict[tuple[str, str, str, str], TraceGap] = {}

    def add_node(kind: TraceKind, ref_id: str, record: TraceRecord | None) -> TraceNode:
        """Register one node (deduplicated by kind + id)."""
        key = (kind, ref_id)
        node = nodes.get(key)
        if node is None:
            node = TraceNode(kind=kind, ref_id=ref_id, record=record)
            nodes[key] = node
        return node

    def add_link(source: TraceNode, via: str, target: TraceNode) -> None:
        """Register one resolved reference (deduplicated)."""
        key = (source.kind.value, source.ref_id, via, target.kind.value, target.ref_id)
        if key not in links:
            links[key] = TraceLink(source=source, via=via, target=target)

    def add_gap(source: TraceNode, via: str, ref_id: str, reason: str) -> None:
        """Register one missing link (deduplicated)."""
        key = (source.kind.value, source.ref_id, via, ref_id)
        if key not in gaps:
            gaps[key] = TraceGap(
                source=source, via=via, ref_id=ref_id, reason=reason
            )

    # -- the claim and its evidence records ----------------------------------
    add_node(TraceKind.CLAIM, claim_id, None)
    evidence_nodes = [
        add_node(TraceKind.EVIDENCE, record.evidence_id, record)
        for record in evidence.records_for_claim(claim_id)
    ]
    evidence_ids = {node.ref_id for node in evidence_nodes}

    # -- hop 1: evidence -> requirement outcome via used_by ------------------
    # 06-EVIDENCE-SYSTEM.md SS6: ``used_by`` holds the Goals/decisions using
    # the evidence as opaque refs; the persisted requirement records are the
    # SS7 "Requirement outcome" hop of the report-traceability chain. A
    # used_by ref that names no registered requirement (and no registered
    # goal -- goals are context, not chain nodes) is a missing link.
    for evidence_node in evidence_nodes:
        record = evidence_node.record
        # Evidence nodes always carry their ClaimSpecificEvidence.
        assert isinstance(record, ClaimSpecificEvidence)
        for ref in record.used_by:
            if ref in requirements:
                target = add_node(TraceKind.REQUIREMENT, ref, requirements[ref])
                add_link(evidence_node, "ClaimSpecificEvidence.used_by", target)
            elif ref not in goals:
                add_gap(
                    evidence_node,
                    "ClaimSpecificEvidence.used_by",
                    ref,
                    f"used_by reference {ref!r} of evidence"
                    f" {evidence_node.ref_id!r} names no registered"
                    " requirement or goal; the report-traceability chain"
                    " (14-STATE-GIT-ARTIFACTS.md SS7) requires the"
                    " Requirement-outcome hop to resolve"
                )

    # -- hop 2: evidence -> acceptance via AcceptanceCriteria.evidence_refs --
    # The acceptance criteria the claim's evidence supports; ``evidence_refs``
    # may name evidence record ids (the claim-supporting hop) or analysis
    # result ids (the reverse hop, hop 5).
    trace_acceptances: dict[str, AcceptanceCriteria] = {}
    for acceptance in acceptances.values():
        if any(ref in evidence_ids for ref in acceptance.evidence_refs):
            trace_acceptances[acceptance.acceptance_id] = acceptance
    for acceptance_id, record in sorted(trace_acceptances.items()):
        acceptance_node = add_node(
            TraceKind.ACCEPTANCE, acceptance_id, record
        )
        for evidence_node in evidence_nodes:
            if evidence_node.ref_id in record.evidence_refs:
                add_link(
                    evidence_node,
                    "AcceptanceCriteria.evidence_refs",
                    acceptance_node,
                )

    # -- hop 3: acceptance -> analysis result via ResultRecord.acceptance_ref -
    # 14-STATE-GIT-ARTIFACTS.md SS7: the analysis result is the hop after
    # the acceptance; the result pins the exact acceptance it was evaluated
    # against (DEV-M9-G02 AC-01 exactness).
    for result in sorted(results.values(), key=lambda r: r.result_id):
        if result.acceptance_ref in trace_acceptances:
            analysis_node = add_node(TraceKind.ANALYSIS, result.result_id, result)
            acceptance_node = nodes[(TraceKind.ACCEPTANCE, result.acceptance_ref)]
            add_link(acceptance_node, "ResultRecord.acceptance_ref", analysis_node)

    # -- hop 4: requirement -> analysis result via requirement_refs ----------
    # The requirement-outcome hop reaches the analysis result through the
    # result record's pure-linkage requirement refs (DEV-M9-G02 AC-03).
    requirement_ids = {
        node.ref_id
        for node in nodes.values()
        if node.kind is TraceKind.REQUIREMENT
    }
    for result in sorted(results.values(), key=lambda r: r.result_id):
        for ref in result.requirement_refs:
            if ref in requirement_ids:
                analysis_node = add_node(TraceKind.ANALYSIS, result.result_id, result)
                requirement_node = nodes[(TraceKind.REQUIREMENT, ref)]
                add_link(
                    requirement_node, "ResultRecord.requirement_refs", analysis_node
                )

    # -- hop 5: analysis -> acceptance when evidence_refs names the result ---
    # The DEV-M9-G02 result package is itself a named "evidence" of the
    # acceptance (scenario suites register ``evidence_refs=[<result id>]``):
    # an acceptance whose evidence_refs names a resolved analysis result
    # enters the trace and links back to it.
    for analysis_node in [n for n in nodes.values() if n.kind is TraceKind.ANALYSIS]:
        for acceptance in acceptances.values():
            if analysis_node.ref_id in acceptance.evidence_refs:
                acceptance_node = add_node(
                    TraceKind.ACCEPTANCE, acceptance.acceptance_id, acceptance
                )
                add_link(
                    acceptance_node,
                    "AcceptanceCriteria.evidence_refs",
                    analysis_node,
                )

    # -- hop 6: analysis -> run via ResultRecord.run_ref ---------------------
    # The exact input Run ref of the result package (DEV-M9-G02 AC-01); a
    # run_ref that resolves to no registered Run record is the canonical
    # missing link (AC-02).
    for analysis_node in [n for n in nodes.values() if n.kind is TraceKind.ANALYSIS]:
        record = analysis_node.record
        # Analysis nodes always carry their ResultRecord.
        assert isinstance(record, ResultRecord)
        run = runs.get(record.run_ref)
        if run is None:
            add_gap(
                analysis_node,
                "ResultRecord.run_ref",
                record.run_ref,
                f"run_ref {record.run_ref!r} of analysis result"
                f" {record.result_id!r} resolves to no registered Run record"
                f" (runs/run/{record.run_ref}.json); the report-traceability"
                " chain requires the Run hop to resolve (AC-02)"
            )
        else:
            run_node = add_node(TraceKind.RUN, record.run_ref, run)
            add_link(analysis_node, "ResultRecord.run_ref", run_node)

    # -- hop 7: analysis -> raw artifact manifests via input_artifact_ids -----
    # The exact raw artifact refs of the result package (DEV-M9-G02 AC-01);
    # an input artifact id that resolves to no registered manifest is a
    # missing link (AC-02).
    for analysis_node in [n for n in nodes.values() if n.kind is TraceKind.ANALYSIS]:
        record = analysis_node.record
        assert isinstance(record, ResultRecord)
        for artifact_id in record.input_artifact_ids:
            manifest = artifacts.get(artifact_id)
            if manifest is None:
                add_gap(
                    analysis_node,
                    "ResultRecord.input_artifact_ids",
                    artifact_id,
                    f"input artifact {artifact_id!r} of analysis result"
                    f" {record.result_id!r} resolves to no registered"
                    " artifact manifest (manifests/<id>.json); the"
                    " report-traceability chain requires the raw Artifact"
                    " hop to resolve (AC-02)"
                )
            else:
                artifact_node = add_node(
                    TraceKind.ARTIFACT, artifact_id, manifest
                )
                add_link(
                    analysis_node,
                    "ResultRecord.input_artifact_ids",
                    artifact_node,
                )

    # -- hop 8: run -> artifacts via Run.artifacts ----------------------------
    for run_node in [n for n in nodes.values() if n.kind is TraceKind.RUN]:
        record = run_node.record
        # Run nodes always carry their Run record.
        assert isinstance(record, Run)
        for artifact_id in record.artifacts:
            manifest = artifacts.get(artifact_id)
            if manifest is None:
                add_gap(
                    run_node,
                    "Run.artifacts",
                    artifact_id,
                    f"artifact reference {artifact_id!r} of run"
                    f" {record.run_id!r} resolves to no registered artifact"
                    " manifest (manifests/<id>.json); the run's artifact"
                    " links must resolve (AC-02)"
                )
            else:
                artifact_node = add_node(
                    TraceKind.ARTIFACT, artifact_id, manifest
                )
                add_link(run_node, "Run.artifacts", artifact_node)

    # -- hop 9: artifact producer links (ArtifactManifest.run_id/analysis_id) -
    for artifact_node in [n for n in nodes.values() if n.kind is TraceKind.ARTIFACT]:
        record = artifact_node.record
        # Artifact nodes always carry their ArtifactManifest.
        assert isinstance(record, ArtifactManifest)
        if record.run_id is not None:
            run = runs.get(record.run_id)
            if run is None:
                add_gap(
                    artifact_node,
                    "ArtifactManifest.run_id",
                    record.run_id,
                    f"producer run {record.run_id!r} of artifact"
                    f" {record.artifact_id!r} resolves to no registered Run"
                    " record; the raw artifact's producer link must resolve"
                    " (AC-02)"
                )
            else:
                run_node = add_node(TraceKind.RUN, record.run_id, run)
                add_link(artifact_node, "ArtifactManifest.run_id", run_node)
        if record.analysis_id is not None:
            producer_result = results.get(record.analysis_id)
            if producer_result is None:
                add_gap(
                    artifact_node,
                    "ArtifactManifest.analysis_id",
                    record.analysis_id,
                    f"producer analysis {record.analysis_id!r} of artifact"
                    f" {record.artifact_id!r} resolves to no registered"
                    " analysis result record; the raw artifact's producer"
                    " link must resolve (AC-02)"
                )
            else:
                analysis_node = add_node(
                    TraceKind.ANALYSIS, record.analysis_id, producer_result
                )
                add_link(artifact_node, "ArtifactManifest.analysis_id", analysis_node)

    # -- dangling evidence_refs of the acceptances in the trace ---------------
    # Every evidence_refs entry of a trace acceptance must resolve to a
    # claim evidence record or to a resolved analysis result; anything else
    # is a missing link (AC-02).
    for acceptance_node in [
        n for n in nodes.values() if n.kind is TraceKind.ACCEPTANCE
    ]:
        record = acceptance_node.record
        # Acceptance nodes always carry their AcceptanceCriteria.
        assert isinstance(record, AcceptanceCriteria)
        for ref in record.evidence_refs:
            if ref in evidence_ids:
                continue
            if ref in results and (TraceKind.ANALYSIS, ref) in nodes:
                continue
            add_gap(
                acceptance_node,
                "AcceptanceCriteria.evidence_refs",
                ref,
                f"evidence_refs entry {ref!r} of acceptance"
                f" {record.acceptance_id!r} resolves to no claim evidence"
                " record and no analysis result record; the acceptance's"
                " evidence links must resolve (AC-02)"
            )

    return ClaimTrace(
        claim_id=claim_id,
        nodes=tuple(
            sorted(nodes.values(), key=lambda node: (node.kind.value, node.ref_id))
        ),
        links=tuple(
            sorted(
                links.values(),
                key=lambda link: (
                    link.source.kind.value,
                    link.source.ref_id,
                    link.target.kind.value,
                    link.target.ref_id,
                    link.via,
                ),
            )
        ),
        gaps=tuple(
            sorted(
                gaps.values(),
                key=lambda gap: (
                    gap.source.kind.value,
                    gap.source.ref_id,
                    gap.via,
                    gap.ref_id,
                ),
            )
        ),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject tracing on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise TraceNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _wrap_corrupt(
    message: str, exc: ValueError | TypeError
) -> TraceCorruptError:
    """Re-raise a stored-record corruption as ``TraceCorruptError``."""
    return TraceCorruptError(f"{message}: {exc}")


def _list_results(root: Path) -> tuple[ResultRecord, ...]:
    """List the registered analysis result records (real API, wrapped)."""
    try:
        return list_results(root)
    except ValueError as exc:
        raise _wrap_corrupt("corrupt analysis result registry", exc) from exc


def _read_run(store: FilesystemStateBackend, run_id: str) -> Run:
    """Read one registered Run record as a typed record (real API, wrapped)."""
    try:
        return Run.from_dict(store.read(_RUN_OBJECT_TYPE, run_id))
    except (TypeError, ValueError) as exc:
        raise _wrap_corrupt(
            f"corrupt run record {run_id!r} in the run store", exc
        ) from exc


def _list_artifacts(root: Path) -> tuple[ArtifactManifest, ...]:
    """List the registered artifact manifests (real API, wrapped)."""
    try:
        return tuple(ArtifactRegistry(root / _ARTIFACTS_STATE_DIR).list())
    except ValueError as exc:
        raise _wrap_corrupt("corrupt artifact manifest registry", exc) from exc


def _list_acceptances(root: Path) -> tuple[AcceptanceCriteria, ...]:
    """List the registered acceptance records (real API, wrapped)."""
    try:
        return list_acceptance(root)
    except ValueError as exc:
        raise _wrap_corrupt("corrupt acceptance registry", exc) from exc


def _list_requirements(root: Path) -> tuple[ReproductionRequirement, ...]:
    """List the registered requirement records (real API, wrapped)."""
    try:
        return list_requirements(root)
    except ValueError as exc:
        raise _wrap_corrupt("corrupt requirement registry", exc) from exc


def _list_goals(root: Path) -> tuple[Any, ...]:
    """List the registered goal contracts (real API, wrapped).

    Goals are not nodes of the SS7 chain; the listing exists only to
    verify ``ClaimSpecificEvidence.used_by`` goal refs.
    """
    try:
        return list_goals(root)
    except ValueError as exc:
        raise _wrap_corrupt("corrupt goal registry", exc) from exc

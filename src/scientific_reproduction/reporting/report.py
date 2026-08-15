"""Human-readable reproduction report generator (DEV-M13-G02).

Implements the **report generator** deliverable of DEV-M13-G02: the
human-readable markdown projection of the registered project state. The
report assembles its content **from the real registered state through the
real read APIs** -- the run store
(``core.state_backend.FilesystemStateBackend`` over the workspace root,
resolving the canonical ``runs/`` tree directory), the analysis result
registry (``analysis.results.list_results``), the
artifact manifest registry, the planning registries (project state,
goals, requirements, inventory items, acceptance criteria, closure
contracts) and the analysis protocol lineage
(``analysis.protocols.list_protocol_versions``) -- plus the DEV-M13-G01
read surfaces: ``reporting.audit.build_audit_package`` (every run of the
run store with its derived :class:`reporting.audit.RunStatus`, failed
runs included, AC-02) and the key-claim traces of
``reporting.traceability`` (AC-03). No validation is re-implemented
here: the machine-auditable package and the claim traces are the source
of truth (``14-STATE-GIT-ARTIFACTS.md`` SS7 -- every key report claim
must trace through Report claim -> Requirement outcome -> Analysis
Result -> Run(s) -> Raw Artifact manifest(s) -> Source/Evidence; v0.1
release gate 8 of ``18-TEST-AND-ACCEPTANCE-PLAN.md`` SS4: the final
machine-auditable package validates traceability); the report is its
human-readable projection.

Sections (fixed order, covering the DEV-M13-G02 objective)
----------------------------------------------------------
1. Scope -- goals, requirements, inventory items, acceptance criteria.
2. Methods -- the registered analysis protocol lineage: every version
   with its frozen/draft status, profile and method/artifact counts.
3. Statistics -- the registered analysis result records: the metrics,
   uncertainty and warnings of each result package.
4. Strict/recovery history -- the v0.1 recovery representation
   (``08-STRICT-RECOVERY-CLOSURE.md``): goal tracks (``GoalTrack``),
   requirements with the recovery outcome
   (``RequirementOutcome.REPRODUCED_WITH_RECOVERY``,
   ``05-GOAL-RUN-SCHEMA.md`` SS2), closure-contract recovery progress
   (``ClosureContract.recovery``) and per-run engineering retries /
   deviations records. v0.1 has no dedicated recovery registry;
   Supervisor recovery decisions exist only as event payloads.
5. Failures and deviations -- every run of the run store with its
   derived ``RunStatus`` (AC-02): failed runs (``CANCELLED`` /
   ``INVALIDATED`` or a FAIL scientific review, ``05-GOAL-RUN-SCHEMA.md``
   SS7 -- scientific PASS/FAIL is a review decision stored separately
   from the lifecycle) are summarized explicitly, never hidden.
6. Outcomes -- the scientific outcome (AC-01): the requirement outcomes
   (``RequirementOutcome``) and the run scientific reviews
   (``ScientificReview``).
7. Method reproducibility -- the method reproducibility outcome (AC-01):
   ``MethodReproducibility`` per requirement, protocol adherence of the
   analysis results and run lifecycle coverage.
8. Key claims and traceability -- the auditable object ids of every key
   claim (AC-03): evidence/requirement/acceptance/result/run/artifact
   ids from the claim traces, plus the structured :class:`ClaimReport`
   surface of the report.
9. Limitations -- the recorded limitations of the registered state:
   evidence-recorded limitations (``ClaimSpecificEvidence.limitations``),
   claim trace gaps, unresolved run artifact refs and analysis result
   warnings.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the given
inputs: no wall clock, no randomness, no network. Every collection is
sorted by stable keys, so identical state always yields byte-identical
markdown and canonical JSON. ``TypeError`` at the public boundaries;
errors follow the ``ValueError``-subclass convention with stable
messages; ``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from scientific_reproduction.analysis.protocols import list_protocol_versions
from scientific_reproduction.analysis.results import ResultRecord
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ClaimSpecificEvidence,
    ClosureContract,
    GoalContract,
    GoalTrack,
    LifecycleState,
    Project,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    Run,
    ScientificReview,
)
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.inventory import list_inventory_items
from scientific_reproduction.planning.plan import (
    list_analysis_protocols,
    list_closure_contracts,
    list_goals,
)
from scientific_reproduction.reporting.audit import (
    AuditCorruptError,
    AuditNotInitializedError,
    AuditPackage,
    RunEntry,
    RunStatus,
    build_audit_package,
)
from scientific_reproduction.reporting.traceability import ClaimTrace, TraceKind
from scientific_reproduction.research.evidence import EvidenceRegistry

if TYPE_CHECKING:
    from scientific_reproduction.analysis.protocols import ProtocolVersion

__all__ = [
    "REPORT_VERSION",
    "ClaimReport",
    "Report",
    "ReportCorruptError",
    "ReportError",
    "ReportNotInitializedError",
    "ReportSection",
    "build_report",
]

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2

#: Version of the report serialization (``version`` key of :class:`Report`).
REPORT_VERSION: str = "1.0"

#: The fixed section titles of the report, in order (the DEV-M13-G02
#: objective's coverage plus the AC-03 key-claims section).
_SECTION_TITLES: tuple[str, ...] = (
    "Scope",
    "Methods",
    "Statistics",
    "Strict/recovery history",
    "Failures and deviations",
    "Outcomes",
    "Method reproducibility",
    "Key claims and traceability",
    "Limitations",
)


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ReportError(ValueError):
    """Base class for all report generator errors."""


class ReportNotInitializedError(ReportError):
    """Raised when report generation is attempted on a workspace without a
    project state record (no ``project.yaml`` at the root)."""


class ReportCorruptError(ReportError):
    """Raised when a stored record the report reads is corrupt.

    The registered state is read through the real registry read APIs;
    those APIs surface corruption as ``ValueError``, which this module
    re-raises as ``ReportCorruptError`` with the same message so the
    report's error surface stays stable.
    """


# ---------------------------------------------------------------------------
# The report surfaces (AC-03: auditable object ids; AC-02: failures data)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimReport:
    """The auditable-object-id surface of one key claim (AC-03).

    Every attribute holds the real object ids of the registered records
    the claim's trace resolves to (``reporting.traceability.ClaimTrace``
    nodes), sorted and deduplicated: ``evidence_ids`` (``evidence_id``),
    ``requirement_ids`` (``requirement_id``), ``acceptance_ids``
    (``acceptance_id``), ``result_ids`` (``result_id``), ``run_ids``
    (``run_id``) and ``artifact_ids`` (``artifact_id``). ``gap_count``
    is the number of missing links of the claim's trace and
    ``gap_ref_ids`` the dangling reference ids of those gaps (AC-02:
    gaps are surfaced, never hidden).
    """

    claim_id: str
    evidence_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    acceptance_ids: tuple[str, ...]
    result_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    gap_count: int
    gap_ref_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the claim surface in canonical field order."""
        return {
            "claim_id": self.claim_id,
            "evidence_ids": list(self.evidence_ids),
            "requirement_ids": list(self.requirement_ids),
            "acceptance_ids": list(self.acceptance_ids),
            "result_ids": list(self.result_ids),
            "run_ids": list(self.run_ids),
            "artifact_ids": list(self.artifact_ids),
            "gap_count": self.gap_count,
            "gap_ref_ids": list(self.gap_ref_ids),
        }


@dataclass(frozen=True)
class ReportSection:
    """One section of the report: a title and its markdown body."""

    title: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the section."""
        return {"title": self.title, "body": self.body}


@dataclass(frozen=True)
class Report:
    """The human-readable reproduction report.

    Attributes:
        project_id: the registered project id (``project.yaml``).
        primary_target: the registered primary target (DOI or
            identifier).
        project_phase: the registered project phase.
        plan_version: the registered current plan version.
        sections: the report sections in fixed order
            (see ``_SECTION_TITLES``).
        claims: the AC-03 claim surfaces, sorted by ``claim_id``.
    """

    project_id: str
    primary_target: str
    project_phase: str
    plan_version: str
    sections: tuple[ReportSection, ...]
    claims: tuple[ClaimReport, ...]

    def section(self, title: str) -> ReportSection | None:
        """The section with ``title``, or None when absent.

        Raises:
            TypeError: ``title`` is not a str.
        """
        if not isinstance(title, str):
            raise TypeError(f"title must be a str, got {type(title).__name__}")
        for section in self.sections:
            if section.title == title:
                return section
        return None

    def to_markdown(self) -> str:
        """Render the report as deterministic markdown text."""
        parts: list[str] = [
            "# Reproduction Report",
            "",
            f"Project: {self.project_id}",
            f"Primary target: {self.primary_target}",
            f"Project phase: {self.project_phase}",
            f"Plan version: {self.plan_version}",
            "",
            "This report is the human-readable projection of the",
            "registered project state; the machine-auditable package and",
            "the claim traces are the source of truth",
            "(14-STATE-GIT-ARTIFACTS.md SS7).",
        ]
        for number, section in enumerate(self.sections, start=1):
            parts.extend(("", f"## {number}. {section.title}", "", section.body))
        return "\n".join(parts) + "\n"

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the report in canonical field order."""
        return {
            "version": REPORT_VERSION,
            "project_id": self.project_id,
            "primary_target": self.primary_target,
            "project_phase": self.project_phase,
            "plan_version": self.plan_version,
            "sections": [section.to_dict() for section in self.sections],
            "claims": [claim.to_dict() for claim in self.claims],
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Report assembly (pure, deterministic)
# ---------------------------------------------------------------------------


def build_report(
    root: str | Path,
    evidence: EvidenceRegistry,
    key_claims: Sequence[str],
) -> Report:
    """Assemble the human-readable report from the real registered state.

    Reads every record through the real registration APIs: the project
    state (``planning.init.read_project_state``), the goal / inventory /
    closure registries and the analysis protocol lineage
    (``analysis.protocols.list_protocol_versions``) directly, and the
    run store, analysis results, artifact manifests, acceptances,
    requirements and claim traces through the DEV-M13-G01 read surface
    ``reporting.audit.build_audit_package`` (the package is used as a
    read API: every run carries its derived ``RunStatus``, failed runs
    included -- AC-02 -- and every claim trace its resolved nodes --
    AC-03). The report renders these records; it never re-implements
    validation.

    Args:
        root: the initialized workspace root.
        evidence: the real claim-specific evidence registry of the
            project.
        key_claims: the report's key claims (the opaque ``claim_id``
            strings whose auditable object ids the report cites).
            Entries are deduplicated and sorted by id; an empty sequence
            builds a report with no claims (the key-claims section then
            states that no key claims were specified).

    Returns:
        The deterministic :class:`Report`.

    Raises:
        TypeError: ``root`` is not a str/Path, ``evidence`` is not an
            ``EvidenceRegistry``, or ``key_claims`` is not a sequence of
            non-empty strings.
        ReportNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        ReportCorruptError: a stored record the report reads is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(evidence, EvidenceRegistry):
        raise TypeError(
            "evidence must be an EvidenceRegistry, got"
            f" {type(evidence).__name__}"
        )
    _normalize_key_claims(key_claims)

    package = _build_audit_package_wrapped(root, evidence, key_claims)
    project = _read_project_wrapped(Path(root).resolve())
    goals = _read_goals_wrapped(Path(root).resolve())
    inventory = _read_inventory_wrapped(Path(root).resolve())
    closure = _read_closure_wrapped(Path(root).resolve())
    protocol_ids, lineages = _read_protocol_lineages_wrapped(
        Path(root).resolve()
    )

    requirements = package.requirements
    runs = tuple(sorted((entry.run for entry in package.runs), key=lambda r: r.run_id))
    claims = tuple(_claim_report(trace) for trace in package.claims)

    builders = (
        _scope_section(
            goals, requirements, inventory, package.acceptances
        ),
        _methods_section(protocol_ids, lineages),
        _statistics_section(package.analyses),
        _recovery_history_section(goals, requirements, closure, runs),
        _failures_section(package.runs),
        _outcomes_section(requirements, runs),
        _method_reproducibility_section(
            requirements, package.analyses, lineages, runs
        ),
        _key_claims_section(claims),
        _limitations_section(
            package.evidence, claims, package.runs, package.analyses
        ),
    )
    sections = tuple(
        ReportSection(title=title, body=body)
        for title, body in zip(_SECTION_TITLES, builders, strict=True)
    )

    return Report(
        project_id=project.project_id,
        primary_target=project.primary_target.doi
        or project.primary_target.identifier,
        project_phase=project.project_phase.value,
        plan_version=project.current_plan_version,
        sections=sections,
        claims=claims,
    )


# ---------------------------------------------------------------------------
# Section renderers (deterministic markdown bodies)
# ---------------------------------------------------------------------------


def _scope_section(
    goals: tuple[GoalContract, ...],
    requirements: tuple[ReproductionRequirement, ...],
    inventory: tuple[ReproductionInventoryItem, ...],
    acceptances: tuple[AcceptanceCriteria, ...],
) -> str:
    """The scope section: goals, requirements, inventory, acceptances."""
    lines: list[str] = [f"Goals ({len(goals)})"]
    for goal in goals:
        lines.append(
            f"- {goal.goal_id} [{goal.track.value}] {goal.title}"
            f" (frozen: {_yes_no(goal.frozen)},"
            f" acceptance: {goal.acceptance.criteria_ref},"
            f" protocol: {goal.analysis_protocol_ref},"
            f" requirements: {_id_list(goal.requirement_ids)})"
        )
    lines.extend(["", f"Requirements ({len(requirements)})"])
    for requirement in requirements:
        lines.append(
            f"- {requirement.requirement_id}"
            f" [{requirement.criticality.value}] {requirement.statement}"
            f" (outcome: {requirement.outcome.value},"
            f" method reproducibility:"
            f" {_maybe(requirement.method_reproducibility)})"
        )
    lines.extend(["", f"Inventory items ({len(inventory)})"])
    for item in inventory:
        lines.append(
            f"- {item.inventory_id} [{item.item_type.value}]"
            f" {item.description}"
            f" (formal report: {_yes_no(item.formal_report)},"
            f" mapping: {item.mapping_status.value})"
        )
    lines.extend(["", f"Acceptance criteria ({len(acceptances)})"])
    for acceptance in acceptances:
        lines.append(
            f"- {acceptance.acceptance_id} [decision mode"
            f" {acceptance.decision_mode.value}] goal"
            f" {acceptance.goal_id}, evidence refs:"
            f" {_id_list(acceptance.evidence_refs)}"
        )
    return "\n".join(lines)


def _methods_section(
    protocol_ids: tuple[str, ...],
    lineages: dict[str, tuple[ProtocolVersion, ...]],
) -> str:
    """The methods section: the registered analysis protocol lineage."""
    lines: list[str] = [f"Analysis protocols ({len(protocol_ids)})"]
    for analysis_id in protocol_ids:
        versions = lineages[analysis_id]
        lines.append(f"- {analysis_id}")
        if not versions:
            lines.append("  - (no protocol versions registered)")
        for version in versions:
            record = version.record
            state = "frozen" if record.frozen else "draft"
            stamp = ""
            if record.frozen and version.metadata.frozen_at is not None:
                stamp = f" frozen at {version.metadata.frozen_at}"
            profile = (
                record.profile.value if record.profile is not None else "unset"
            )
            lines.append(
                f"  - {record.protocol_version} ({state}{stamp}):"
                f" profile {profile}, methods {len(record.methods)},"
                f" input artifacts {len(record.input_artifact_ids)}"
            )
    return "\n".join(lines)


def _statistics_section(results: tuple[ResultRecord, ...]) -> str:
    """The statistics section: the registered analysis result records."""
    lines: list[str] = [f"Analysis results ({len(results)})"]
    for result in results:
        lines.append(
            f"- {result.result_id} [protocol {result.analysis_id}"
            f" {result.protocol_version}] run {result.run_ref},"
            f" acceptance {result.acceptance_ref or 'none'},"
            f" requirements {_id_list(result.requirement_refs)}"
        )
        metrics = f"{len(result.metrics)} metrics" if result.metrics else "none"
        lines.append(
            f"  - metrics: {metrics}; uncertainty:"
            f" {_compact_json(result.uncertainty)}"
        )
        if result.warnings:
            lines.append(
                f"  - warnings: {len(result.warnings)}"
                f" (first: {result.warnings[0]!r})"
            )
    return "\n".join(lines)


def _recovery_history_section(
    goals: tuple[GoalContract, ...],
    requirements: tuple[ReproductionRequirement, ...],
    closure: tuple[ClosureContract, ...],
    runs: tuple[Run, ...],
) -> str:
    """The strict/recovery history section (the v0.1 representation).

    Recovery history is carried by the real registered records: goal
    tracks (``GoalTrack``), requirement outcomes
    (``RequirementOutcome.REPRODUCED_WITH_RECOVERY``), closure-contract
    recovery progress (``ClosureContract.recovery``) and per-run
    engineering retries / deviations. No dedicated recovery registry
    exists in v0.1; Supervisor recovery decisions are recorded only as
    event payloads (``08-STRICT-RECOVERY-CLOSURE.md``).
    """
    lines: list[str] = []
    track_counts = {track.value: 0 for track in GoalTrack}
    for goal in goals:
        track_counts[goal.track.value] += 1
    summary = ", ".join(
        f"{name.lower()} {count}"
        for name, count in sorted(track_counts.items())
    )
    lines.append(f"Goal tracks: {summary}")
    recovery_goals = [
        goal for goal in goals if goal.track is not GoalTrack.STRICT_REPRODUCTION
    ]
    lines.append(f"Recovery/method-redesign goals: {len(recovery_goals)}")
    for goal in recovery_goals:
        lines.append(f"- {goal.goal_id} [{goal.track.value}] {goal.title}")
    recovery_requirements = [
        requirement
        for requirement in requirements
        if requirement.outcome is RequirementOutcome.REPRODUCED_WITH_RECOVERY
    ]
    lines.append(
        f"Requirements with recovery outcome: {len(recovery_requirements)}"
    )
    for requirement in recovery_requirements:
        lines.append(f"- {requirement.requirement_id}")
    lines.append(f"Closure contracts ({len(closure)})")
    for contract in closure:
        recovery = contract.recovery
        lines.append(
            f"- {contract.closure_id} (frozen: {_yes_no(contract.frozen)}):"
            f" recovery hypotheses eligible"
            f" {_maybe_int(recovery.eligible_hypotheses_total)},"
            f" tested or ruled out {_maybe_int(recovery.tested_or_ruled_out)},"
            f" remaining {_maybe_int(recovery.remaining)}"
        )
    retried = [run for run in runs if run.engineering_retries]
    deviating = [run for run in runs if run.deviations]
    lines.append(
        f"Runs with engineering retries: {len(retried)};"
        f" runs with deviations: {len(deviating)}"
    )
    for run in retried:
        lines.append(
            f"- {run.run_id}: {len(run.engineering_retries)} engineering"
            " retries"
        )
    for run in deviating:
        lines.append(f"- {run.run_id}: {len(run.deviations)} deviations")
    return "\n".join(lines)


def _failures_section(entries: tuple[RunEntry, ...]) -> str:
    """The failures and deviations section (AC-02).

    Every run of the run store is rendered with its derived
    ``RunStatus`` (``reporting.audit.run_status``); failed runs
    (``CANCELLED`` / ``INVALIDATED`` lifecycle states or a FAIL
    scientific review, ``05-GOAL-RUN-SCHEMA.md`` SS7) are summarized
    explicitly -- never hidden.
    """
    status_counts = {status.value: 0 for status in RunStatus}
    for entry in entries:
        status_counts[entry.status.value] += 1
    summary = ", ".join(
        f"{name} {count}" for name, count in sorted(status_counts.items())
    )
    lines = [
        f"Runs ({len(entries)} total): {summary}",
        "",
        "Run table (all runs, failed runs included)",
    ]
    for entry in entries:
        run = entry.run
        lines.append(
            f"- {run.run_id} [{entry.status.value}]"
            f" lifecycle {run.lifecycle_state.value},"
            f" review {run.scientific_review.value},"
            f" goal {run.goal_id}, type {run.run_type.value},"
            f" artifacts {len(run.artifacts)}"
        )
    failed = [entry for entry in entries if entry.status is RunStatus.FAILED]
    lines.extend(["", f"Failed runs ({len(failed)} -- summarized, not hidden)"])
    for entry in failed:
        run = entry.run
        reason = (
            "FAIL scientific review"
            if run.scientific_review is ScientificReview.FAIL
            else f"lifecycle state {run.lifecycle_state.value}"
        )
        lines.append(
            f"- {run.run_id} [{reason}]"
            f" review {run.scientific_review.value},"
            f" goal {run.goal_id}, type {run.run_type.value},"
            f" deviations {len(run.deviations)},"
            f" engineering retries {len(run.engineering_retries)}"
        )
    deviations_total = sum(len(entry.run.deviations) for entry in entries)
    retries_total = sum(len(entry.run.engineering_retries) for entry in entries)
    lines.append(
        f"Deviations: {deviations_total} total; engineering retries:"
        f" {retries_total} total"
    )
    return "\n".join(lines)


def _outcomes_section(
    requirements: tuple[ReproductionRequirement, ...],
    runs: tuple[Run, ...],
) -> str:
    """The scientific outcome section (AC-01).

    The scientific outcome is rendered from the real records:
    ``ReproductionRequirement.outcome`` (``RequirementOutcome``,
    ``05-GOAL-RUN-SCHEMA.md`` SS2) and the run scientific reviews
    (``ScientificReview``, stored separately from the run lifecycle,
    SS7). This is distinct from the method reproducibility outcome of
    the "Method reproducibility" section.
    """
    lines = [
        "Requirement outcomes (scientific outcome,"
        " 05-GOAL-RUN-SCHEMA.md SS2)"
    ]
    for requirement in requirements:
        lines.append(
            f"- {requirement.requirement_id}"
            f" [{requirement.criticality.value}]:"
            f" {requirement.outcome.value}"
        )
    review_counts = {review.value: 0 for review in ScientificReview}
    for run in runs:
        review_counts[run.scientific_review.value] += 1
    summary = ", ".join(
        f"{name} {count}" for name, count in sorted(review_counts.items())
    )
    lines.append(f"Run scientific reviews: {summary}")
    return "\n".join(lines)


def _method_reproducibility_section(
    requirements: tuple[ReproductionRequirement, ...],
    results: tuple[ResultRecord, ...],
    lineages: dict[str, tuple[ProtocolVersion, ...]],
    runs: tuple[Run, ...],
) -> str:
    """The method reproducibility section (AC-01).

    The method reproducibility outcome is rendered from the real
    records: ``ReproductionRequirement.method_reproducibility``
    (``MethodReproducibility``, ``05-GOAL-RUN-SCHEMA.md`` SS2), the
    protocol adherence of the registered analysis results (execution
    against a frozen protocol version of the registered lineage) and
    the run lifecycle coverage. Distinct from the scientific outcome of
    the "Outcomes" section.
    """
    lines = [
        "Requirement method reproducibility outcomes"
        " (05-GOAL-RUN-SCHEMA.md SS2, distinct from the scientific"
        " outcome)"
    ]
    for requirement in requirements:
        lines.append(
            f"- {requirement.requirement_id}:"
            f" {_maybe(requirement.method_reproducibility)}"
        )
    lines.extend(
        [
            "",
            "Protocol adherence (results executed against a frozen",
            "protocol version)",
        ]
    )
    for result in results:
        frozen_versions = lineages.get(result.analysis_id, ())
        frozen = any(
            version.record.protocol_version == result.protocol_version
            and version.record.frozen
            for version in frozen_versions
        )
        lines.append(
            f"- {result.result_id}: protocol {result.analysis_id}"
            f" {result.protocol_version} (frozen: {_yes_no(frozen)})"
        )
    lines.extend(["", "Run lifecycle coverage"])
    closed = sum(1 for run in runs if run.lifecycle_state is LifecycleState.CLOSED)
    lines.append(f"- closed runs: {closed} of {len(runs)}")
    return "\n".join(lines)


def _key_claims_section(claims: tuple[ClaimReport, ...]) -> str:
    """The key claims section (AC-03).

    Every cited id is the auditable object id of the registered record
    the claim's trace resolved to (``14-STATE-GIT-ARTIFACTS.md`` SS7).
    """
    lines = [
        "Key claims and their traceability"
        " (14-STATE-GIT-ARTIFACTS.md SS7)"
    ]
    if not claims:
        lines.append("- (no key claims specified)")
    for claim in claims:
        lines.append(f"- {claim.claim_id}")
        lines.append(f"  - evidence: {_id_list(claim.evidence_ids)}")
        lines.append(f"  - requirements: {_id_list(claim.requirement_ids)}")
        lines.append(f"  - acceptances: {_id_list(claim.acceptance_ids)}")
        lines.append(
            f"  - analysis results: {_id_list(claim.result_ids)}"
        )
        lines.append(f"  - runs: {_id_list(claim.run_ids)}")
        lines.append(f"  - artifacts: {_id_list(claim.artifact_ids)}")
        lines.append(f"  - trace gaps: {claim.gap_count}")
    return "\n".join(lines)


def _limitations_section(
    evidence_records: tuple[ClaimSpecificEvidence, ...],
    claims: tuple[ClaimReport, ...],
    entries: tuple[RunEntry, ...],
    results: tuple[ResultRecord, ...],
) -> str:
    """The limitations section: the recorded limitations of the registered
    state -- evidence-recorded limitations
    (``ClaimSpecificEvidence.limitations``), claim trace gaps,
    unresolved run artifact refs and analysis result warnings."""
    lines: list[str] = []
    recorded: list[str] = []
    for record in evidence_records:
        for limitation in record.limitations:
            recorded.append(f"- evidence {record.evidence_id}: {limitation}")
    if recorded:
        lines.append(f"Recorded evidence limitations ({len(recorded)})")
        lines.extend(recorded)
    for claim in claims:
        if claim.gap_count:
            lines.append(
                f"- key claim {claim.claim_id}: trace has"
                f" {claim.gap_count} gap(s)"
                f" (dangling refs: {_id_list(claim.gap_ref_ids)}); the"
                " claim is not fully traceable through Analysis -> Run ->"
                " Artifact/Evidence"
            )
    unresolved = sorted(
        {
            artifact_id
            for entry in entries
            for artifact_id in entry.unresolved_artifact_ids
        }
    )
    if unresolved:
        lines.append(f"- unresolved run artifact refs: {_id_list(unresolved)}")
    for result in results:
        for warning in result.warnings:
            lines.append(
                f"- analysis result {result.result_id} warning: {warning}"
            )
    if not lines:
        lines.append("No limitations recorded in the registered state.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _wrap_corrupt(exc: ValueError | TypeError) -> ReportCorruptError:
    """Re-raise a stored-record corruption as ``ReportCorruptError``."""
    return ReportCorruptError(f"corrupt registered state: {exc}")


def _normalize_key_claims(key_claims: Sequence[str]) -> tuple[str, ...]:
    """Validate, deduplicate and sort the key claims (deterministic)."""
    if isinstance(key_claims, (str, bytes)) or not isinstance(
        key_claims, Sequence
    ):
        raise TypeError(
            "key_claims must be a sequence of claim id strings, got"
            f" {type(key_claims).__name__}"
        )
    normalized: list[str] = []
    for claim_id in key_claims:
        if not isinstance(claim_id, str):
            raise TypeError(
                "key_claims entries must be str claim ids, got"
                f" {type(claim_id).__name__}"
            )
        if not claim_id.strip():
            raise ValueError(
                "key_claims entries must be non-empty strings, got an empty"
                " string"
            )
        if claim_id not in normalized:
            normalized.append(claim_id)
    return tuple(sorted(normalized))


def _build_audit_package_wrapped(
    root: str | Path,
    evidence: EvidenceRegistry,
    key_claims: Sequence[str],
) -> AuditPackage:
    """Read the audit package as a read API, re-raising as report errors."""
    try:
        return build_audit_package(root, evidence, key_claims)
    except AuditNotInitializedError as exc:
        raise ReportNotInitializedError(str(exc)) from exc
    except AuditCorruptError as exc:
        raise ReportCorruptError(str(exc)) from exc


def _read_project_wrapped(root: Path) -> Project:
    """Read the project state record (real API, wrapped)."""
    try:
        return read_project_state(root)
    except ProjectNotInitializedError as exc:
        raise ReportNotInitializedError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise _wrap_corrupt(exc) from exc


def _read_goals_wrapped(root: Path) -> tuple[GoalContract, ...]:
    """List the registered goal contracts (real API, wrapped)."""
    try:
        return tuple(sorted(list_goals(root), key=lambda goal: goal.goal_id))
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc


def _read_inventory_wrapped(
    root: Path,
) -> tuple[ReproductionInventoryItem, ...]:
    """List the registered inventory items (real API, wrapped)."""
    try:
        return tuple(
            sorted(list_inventory_items(root), key=lambda item: item.inventory_id)
        )
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc


def _read_closure_wrapped(root: Path) -> tuple[ClosureContract, ...]:
    """List the registered closure contracts (real API, wrapped)."""
    try:
        return tuple(
            sorted(list_closure_contracts(root), key=lambda c: c.closure_id)
        )
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc


def _read_protocol_lineages_wrapped(
    root: Path,
) -> tuple[tuple[str, ...], dict[str, tuple[ProtocolVersion, ...]]]:
    """Read every registered analysis protocol id and its lineage.

    The ids come from the registered analysis protocol records
    (``planning.plan.list_analysis_protocols``); each lineage is read
    with ``analysis.protocols.list_protocol_versions``. A protocol id
    with no registered version is a structurally inconsistent state and
    raises ``ReportCorruptError`` (stable message) instead of rendering
    a partial methods section.
    """
    try:
        protocol_ids = tuple(
            sorted(
                record.analysis_id for record in list_analysis_protocols(root)
            )
        )
        lineages: dict[str, tuple[ProtocolVersion, ...]] = {}
        for analysis_id in protocol_ids:
            lineages[analysis_id] = list_protocol_versions(root, analysis_id)
        return protocol_ids, lineages
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc


def _claim_report(trace: ClaimTrace) -> ClaimReport:
    """Build the AC-03 claim surface from one resolved claim trace."""
    return ClaimReport(
        claim_id=trace.claim_id,
        evidence_ids=_trace_ids(trace, TraceKind.EVIDENCE),
        requirement_ids=_trace_ids(trace, TraceKind.REQUIREMENT),
        acceptance_ids=_trace_ids(trace, TraceKind.ACCEPTANCE),
        result_ids=_trace_ids(trace, TraceKind.ANALYSIS),
        run_ids=_trace_ids(trace, TraceKind.RUN),
        artifact_ids=_trace_ids(trace, TraceKind.ARTIFACT),
        gap_count=len(trace.gaps),
        gap_ref_ids=tuple(sorted({gap.ref_id for gap in trace.gaps})),
    )


def _trace_ids(trace: ClaimTrace, kind: TraceKind) -> tuple[str, ...]:
    """The sorted, deduplicated node ref ids of one trace kind."""
    return tuple(sorted({node.ref_id for node in trace.nodes_for(kind)}))


def _yes_no(value: bool) -> str:
    """Render a boolean as ``yes``/``no``."""
    return "yes" if value else "no"


def _maybe(value: Any) -> str:
    """Render an optional enum (``value.value``) or ``not recorded``."""
    if value is None:
        return "not recorded"
    return str(value.value)


def _maybe_int(value: int | None) -> str:
    """Render an optional integer or ``not recorded``."""
    return "not recorded" if value is None else str(value)


def _id_list(ids: Sequence[str]) -> str:
    """Render an id sequence as a comma-separated list or ``none``."""
    if not ids:
        return "none"
    return ", ".join(ids)


def _compact_json(value: dict[str, Any]) -> str:
    """Render a dict as compact canonical JSON (sorted keys)."""
    return json.dumps(value, sort_keys=True)

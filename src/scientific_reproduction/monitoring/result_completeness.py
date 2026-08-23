"""Result Package completeness validation (13-EXECUTION-MONITOR.md §2,
issue #155).

§2 lists "validate minimal Result Package completeness" among the
Execution Monitor's responsibilities, but until now the monitor had no
runtime primitive for it: ``register_worker_result``
(``workers/results.py``) does schema validation and artifact/run
reference resolution only -- nothing reconciles the package's artifacts
and facts against the frozen Goal contract's declared ``outputs``
(``core/models.py`` ``GoalContract.outputs``, a free-form
``list[dict[str, Any]]``), so the monitor had to hand-roll a
scientific-completeness judgment. This module ships that primitive: a
deterministic, pure verdict evaluator over the registered
:class:`~scientific_reproduction.workers.results.WorkerResultPackage`
and the frozen :class:`~scientific_reproduction.core.models.GoalContract`,
following the ordered rule-table pattern of ``planning/audit.py``
(versioned rules, first match wins, ``matched_rule_id`` trace, stable
verdict vocabulary), plus the monitor-facing gate that resolves both
records and consults the evaluator.

Matching rule (deterministic, test-locked)
------------------------------------------
Goal ``outputs`` is free-form, so coverage is defined by an explicit,
deterministic matching rule:

* **Declared-output key** -- every output declaration is matched by its
  first usable stable key, in this precedence order:
  1. ``R-OUT-K1`` a non-empty-string ``id`` -> key kind ``id``;
  2. ``R-OUT-K2`` no usable ``id`` but a non-empty-string ``name`` ->
     key kind ``name``;
  3. ``R-OUT-U1`` neither (the entry is not a mapping, or carries no
     non-empty-string ``id``/``name``) -> the declaration is unkeyable
     and counts as **uncovered**, with the reason in its detail.
* **Package coverage identifiers** -- a declared output is covered iff
  its key value equals one of the package's artifact identifiers or
  facts: every ``input_artifact_ids`` / ``output_artifact_ids`` entry,
  every fact ``fact_id`` and every fact ``name``. The ``data`` section
  and the deviations do not participate: ``data`` entries are
  structured outputs referenced through the artifact ids, and
  deviations are protocol facts, not declared outputs (the
  "artifacts/facts" reconciliation surface of issue #155).
* Matching is exact string equality (no normalization); ``matched_by``
  lists every matching identifier of a covered output, sorted.

Verdict rules (ordered, first match wins)
-----------------------------------------
1. ``R-RPC-I1`` at least one declared output is uncovered -> INCOMPLETE,
   with per-output coverage records naming exactly which declared
   outputs are uncovered and why.
2. ``R-RPC-C1`` every declared output is covered (default) -> COMPLETE.
   A goal declaring **zero outputs** passes vacuously: the coverage
   obligation ranges over the declared outputs only, mirroring the
   ``planning/audit.py`` convention.

The monitor-facing consumer
---------------------------
The consultation is deliberately **not** wired into
``monitoring/reconcile.py``'s ``RESULT_AVAILABLE`` transition path: the
reconcile engine observes the *external* completion signal, at which
point the registered Result Package may legitimately not exist yet
(registration is the separate worker-side result handover), and the
engine is bound to the monitor state directory and injected stores --
it holds neither the package nor the contract, and blocking the
exactly-once completion transition (DEV-M8-G02 AC-01) on a package that
arrives later would break the reconciliation semantics. Instead,
:func:`validate_result_package_completeness` is the monitor's documented
gate: it resolves the run's registered Result Package (by ``run_ref``)
and the frozen Goal Contract, evaluates the verdict, and is consulted
**before the run advances to analysis** -- before the
``RESULT_AVAILABLE -> ANALYZING`` transition through
``workers.run_helpers.transition_run`` and before the analysis follow-up
is issued through ``monitoring.triggers.TriggerRegistry``. An INCOMPLETE
verdict names the uncovered declared outputs; the monitor reports or
remediates instead of advancing.

Determinism and discipline
--------------------------
The verdict is a pure function of the two typed inputs (the package and
the frozen contract): no registry access, no randomness, no wall clock,
no LLM, and the result does not depend on the order of the package's
sections (the coverage identifiers are sets; the per-output records
follow the contract's declared order). ``TypeError`` at the public type
boundaries; stable :class:`ResultCompletenessError` (a
``MonitoringError``, the package's ``ValueError``-based hierarchy)
otherwise. The monitoring subsystem never imports the adapters package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from scientific_reproduction.core.models import GoalContract, LifecycleState, Run
from scientific_reproduction.monitoring.registry import MonitoringError
from scientific_reproduction.planning.plan import read_goal
from scientific_reproduction.workers.results import (
    WorkerResultPackage,
    list_worker_results,
)
from scientific_reproduction.workers.run_helpers import read_run

__all__ = [
    "COMPLETENESS_RULES",
    "COMPLETENESS_RULESET_VERSION",
    "GoalReader",
    "OutputCoverage",
    "ResultCompletenessAudit",
    "ResultCompletenessDecision",
    "ResultCompletenessError",
    "ResultCompletenessInput",
    "ResultCompletenessRule",
    "ResultCompletenessVerdict",
    "RunReader",
    "RunResultsReader",
    "evaluate_result_package_completeness",
    "validate_result_package_completeness",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the completeness rule table. Bumped whenever a rule
#: changes; recorded in every verdict so old verdicts stay
#: interpretable (the ``planning/audit.py`` convention).
COMPLETENESS_RULESET_VERSION: str = "1.0"

#: The stable key kinds of a declared output (R-OUT-K1/R-OUT-K2): the
#: key is the first usable one in this precedence order.
_DECLARED_OUTPUT_KEY_KINDS: tuple[str, ...] = ("id", "name")

# ---------------------------------------------------------------------------
# Verdict vocabulary and errors
# ---------------------------------------------------------------------------


class ResultCompletenessVerdict(StrEnum):
    """The deterministic completeness verdict values.

    ``COMPLETE`` -- every declared output of the frozen Goal is covered
    by the Result Package's artifacts or facts (R-RPC-C1).
    ``INCOMPLETE`` -- at least one declared output is uncovered
    (R-RPC-I1); the per-output coverage records name which ones.
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class ResultCompletenessError(MonitoringError):
    """Raised for completeness-validation contract violations: the
    package and the contract name different goals (a verdict against
    the wrong contract is meaningless), the contract is not frozen, the
    run cannot carry a result, no registered Result Package resolves
    for the run, or more than one does (the gate cannot pick)."""


# ---------------------------------------------------------------------------
# The per-output coverage record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputCoverage:
    """The coverage record of one declared Goal output.

    ``output_index`` is the 0-based position of the declaration in the
    frozen contract's ``outputs`` list (its deterministic identity).
    ``key_kind`` / ``key_value`` are the stable key the declaration was
    matched by (``id`` or ``name``; both None when the declaration is
    unkeyable -- R-OUT-U1). ``covered`` is True iff the key value
    equals a package artifact id, fact id or fact name; ``matched_by``
    lists every matching identifier as ``"artifact:<id>"`` /
    ``"fact_id:<id>"`` / ``"fact_name:<name>"`` (sorted). ``detail``
    is None when covered and the uncovered reason otherwise.
    """

    output_index: int
    key_kind: str | None
    key_value: str | None
    covered: bool
    matched_by: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.output_index, bool) or not isinstance(
            self.output_index, int
        ):
            raise TypeError(
                "OutputCoverage.output_index must be an int, got"
                f" {type(self.output_index).__name__}"
            )
        if self.output_index < 0:
            raise ResultCompletenessError(
                "OutputCoverage.output_index must be >= 0, got"
                f" {self.output_index}"
            )
        if self.key_kind is not None and self.key_kind not in (
            _DECLARED_OUTPUT_KEY_KINDS
        ):
            raise ResultCompletenessError(
                f"OutputCoverage.key_kind {self.key_kind!r} is not a known"
                f" declared-output key kind (expected one of"
                f" {list(_DECLARED_OUTPUT_KEY_KINDS)})"
            )
        if (self.key_kind is None) != (self.key_value is None):
            raise ResultCompletenessError(
                "OutputCoverage.key_kind and key_value must both be set or"
                f" both be None, got {self.key_kind!r} / {self.key_value!r}"
            )
        if self.key_value is not None and (
            not isinstance(self.key_value, str) or not self.key_value.strip()
        ):
            raise ResultCompletenessError(
                "OutputCoverage.key_value must be a non-empty string when"
                f" set, got {self.key_value!r}"
            )
        if not isinstance(self.covered, bool):
            raise TypeError(
                "OutputCoverage.covered must be a bool, got"
                f" {type(self.covered).__name__}"
            )
        if not isinstance(self.matched_by, tuple) or any(
            not isinstance(ref, str) for ref in self.matched_by
        ):
            raise TypeError(
                "OutputCoverage.matched_by must be a tuple of str, got"
                f" {self.matched_by!r}"
            )
        if self.covered != bool(self.matched_by):
            raise ResultCompletenessError(
                "OutputCoverage.covered must be True exactly when"
                f" matched_by is non-empty, got covered={self.covered},"
                f" matched_by={self.matched_by!r}"
            )
        if self.detail is not None and not isinstance(self.detail, str):
            raise TypeError(
                "OutputCoverage.detail must be a str or None, got"
                f" {type(self.detail).__name__}"
            )


# ---------------------------------------------------------------------------
# The ordered rule table (the ``planning/audit.py`` paradigm)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultCompletenessInput:
    """The state a completeness verdict is a pure function of.

    Frozen and hashable so "same state -> same verdict" is directly
    testable. ``output_coverage`` holds one record per declared output
    in declaration order; ``uncovered_outputs`` is the uncovered
    subset in the same order.
    """

    goal_id: str
    result_id: str
    goal_version: str
    output_coverage: tuple[OutputCoverage, ...]
    uncovered_outputs: tuple[OutputCoverage, ...]


@dataclass(frozen=True)
class ResultCompletenessRule:
    """One entry of the ordered completeness verdict rule table."""

    rule_id: str
    description: str
    verdict: ResultCompletenessVerdict
    predicate: Callable[[ResultCompletenessInput], bool]


@dataclass(frozen=True)
class ResultCompletenessDecision:
    """Record of one verdict rule evaluation for a given state (auditability)."""

    rule_id: str
    description: str
    verdict: ResultCompletenessVerdict
    matched: bool


#: The ordered verdict rule table. First match wins; order is normative
#: (see the module docstring). Predicates are pure functions of the
#: :class:`ResultCompletenessInput` only.
COMPLETENESS_RULES: tuple[ResultCompletenessRule, ...] = (
    ResultCompletenessRule(
        rule_id="R-RPC-I1",
        description=(
            "at least one declared output is uncovered by the Result"
            " Package's artifacts or facts; a missing declared output"
            " makes the package INCOMPLETE"
        ),
        verdict=ResultCompletenessVerdict.INCOMPLETE,
        predicate=lambda i: bool(i.uncovered_outputs),
    ),
    ResultCompletenessRule(
        rule_id="R-RPC-C1",
        description=(
            "every declared output is covered by the Result Package's"
            " artifacts or facts (default; a goal declaring no outputs"
            " passes vacuously)"
        ),
        verdict=ResultCompletenessVerdict.COMPLETE,
        predicate=lambda i: True,
    ),
)


# ---------------------------------------------------------------------------
# The verdict record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultCompletenessAudit:
    """The completeness verdict record of one Result Package.

    ``verdict`` is the deterministic decision as the stable
    :class:`ResultCompletenessVerdict` (COMPLETE/INCOMPLETE).
    ``goal_id`` / ``result_id`` / ``goal_version`` identify what was
    validated: the frozen Goal Contract's id, the Result Package's
    result id, and the frozen goal version the package answers.
    ``output_coverage`` carries one record per declared output in
    declaration order; ``uncovered_outputs`` is the uncovered subset
    (the per-output detail of R-RPC-I1). ``decisions`` /
    ``matched_rule_id`` record the verdict rule trace.
    """

    verdict: ResultCompletenessVerdict
    goal_id: str
    result_id: str
    goal_version: str
    output_coverage: tuple[OutputCoverage, ...]
    uncovered_outputs: tuple[OutputCoverage, ...]
    decisions: tuple[ResultCompletenessDecision, ...]
    matched_rule_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ResultCompletenessVerdict):
            raise TypeError(
                "ResultCompletenessAudit.verdict must be a"
                " ResultCompletenessVerdict, got"
                f" {type(self.verdict).__name__}"
            )
        for name in ("goal_id", "result_id", "goal_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise TypeError(
                    f"ResultCompletenessAudit.{name} must be a non-empty"
                    f" str, got {value!r}"
                )
        _require_coverage_tuple(self, "output_coverage")
        _require_coverage_tuple(self, "uncovered_outputs")
        coverage_by_index = {
            record.output_index: record for record in self.output_coverage
        }
        for record in self.uncovered_outputs:
            declared = coverage_by_index.get(record.output_index)
            if declared is None or declared != record:
                raise ResultCompletenessError(
                    "ResultCompletenessAudit.uncovered_outputs must be the"
                    " uncovered subset of output_coverage; record at"
                    f" output_index {record.output_index} is not part of"
                    " the coverage set"
                )
            if record.covered:
                raise ResultCompletenessError(
                    "ResultCompletenessAudit.uncovered_outputs must contain"
                    f" only uncovered records, got a covered record at"
                    f" output_index {record.output_index}"
                )
        if not isinstance(self.decisions, tuple) or any(
            not isinstance(decision, ResultCompletenessDecision)
            for decision in self.decisions
        ):
            raise TypeError(
                "ResultCompletenessAudit.decisions must be a tuple of"
                " ResultCompletenessDecision, got"
                f" {self.decisions!r}"
            )
        if not isinstance(self.matched_rule_id, str):
            raise TypeError(
                "ResultCompletenessAudit.matched_rule_id must be a str, got"
                f" {type(self.matched_rule_id).__name__}"
            )
        if not any(
            decision.matched and decision.rule_id == self.matched_rule_id
            for decision in self.decisions
        ):
            raise ResultCompletenessError(
                "ResultCompletenessAudit.matched_rule_id must name a"
                f" matched decision, got {self.matched_rule_id!r}"
            )

    @property
    def complete(self) -> bool:
        """True iff the verdict is COMPLETE (the analysis-advance gate)."""
        return self.verdict is ResultCompletenessVerdict.COMPLETE


# ---------------------------------------------------------------------------
# The pure validator (the runtime primitive)
# ---------------------------------------------------------------------------


def evaluate_result_package_completeness(
    package: WorkerResultPackage,
    goal: GoalContract,
) -> ResultCompletenessAudit:
    """Evaluate the completeness of one Result Package against the
    frozen Goal Contract's declared outputs.

    Pure and deterministic: the verdict is a pure function of the two
    typed inputs (no registry access, no wall clock, no randomness, no
    LLM). Every declared output of ``goal.outputs`` is matched by its
    first usable stable key (``id``, then ``name`` -- R-OUT-K1/K2) and
    covered iff the key value equals a package artifact id
    (``input_artifact_ids`` / ``output_artifact_ids``), a fact
    ``fact_id`` or a fact ``name``; an unkeyable declaration
    (R-OUT-U1) is uncovered. The verdict follows the ordered rule table
    (``COMPLETENESS_RULES``): one uncovered output -> INCOMPLETE with
    per-output detail; everything covered -> COMPLETE (a goal declaring
    zero outputs passes vacuously). The result does not depend on the
    order of the package's sections.

    Args:
        package: the registered Result Package (typed
            ``WorkerResultPackage`` -- the package the worker returned
            for the run).
        goal: the frozen Goal Contract whose declared ``outputs`` the
            package must cover (typed ``GoalContract``).

    Returns:
        The frozen :class:`ResultCompletenessAudit` record (verdict +
        per-output coverage + rule trace).

    Raises:
        TypeError: ``package`` is not a ``WorkerResultPackage``,
            ``goal`` is not a ``GoalContract``, or ``goal.outputs`` is
            not a list.
        ResultCompletenessError: the package and the contract name
            different goals, or the contract is not frozen.
    """
    if not isinstance(package, WorkerResultPackage):
        raise TypeError(
            "evaluate_result_package_completeness expects a"
            f" WorkerResultPackage, got {type(package).__name__}"
        )
    if not isinstance(goal, GoalContract):
        raise TypeError(
            "evaluate_result_package_completeness expects a GoalContract,"
            f" got {type(goal).__name__}"
        )
    if package.goal_id != goal.goal_id:
        raise ResultCompletenessError(
            f"result package {package.result_id!r} answers goal"
            f" {package.goal_id!r} but the contract names goal"
            f" {goal.goal_id!r}; validating a package against a different"
            " goal's declared outputs is meaningless"
        )
    if not goal.frozen:
        raise ResultCompletenessError(
            f"goal {goal.goal_id!r} is not frozen; completeness is"
            " validated against the frozen Goal Contract only (a draft's"
            " declared outputs are not the frozen contract)"
        )
    if not isinstance(goal.outputs, list):
        raise TypeError(
            f"GoalContract.outputs must be a list, got"
            f" {type(goal.outputs).__name__}"
        )
    artifact_ids = frozenset(package.input_artifact_ids) | frozenset(
        package.output_artifact_ids
    )
    fact_ids = frozenset(fact.fact_id for fact in package.facts)
    fact_names = frozenset(fact.name for fact in package.facts)
    coverage: list[OutputCoverage] = []
    for index, declared in enumerate(goal.outputs):
        key_kind, key_value, detail = _declared_output_key(declared)
        matched_by: tuple[str, ...] = ()
        if key_value is not None:
            matched_by = tuple(
                sorted(
                    _matching_refs(
                        key_value, artifact_ids, fact_ids, fact_names
                    )
                )
            )
        covered = bool(matched_by)
        if not covered and detail is None:
            detail = (
                f"no package artifact id, fact id or fact name equals"
                f" {key_value!r}"
            )
        coverage.append(
            OutputCoverage(
                output_index=index,
                key_kind=key_kind,
                key_value=key_value,
                covered=covered,
                matched_by=matched_by,
                detail=detail,
            )
        )
    uncovered = tuple(record for record in coverage if not record.covered)
    verdict_input = ResultCompletenessInput(
        goal_id=goal.goal_id,
        result_id=package.result_id,
        goal_version=package.goal_version,
        output_coverage=tuple(coverage),
        uncovered_outputs=uncovered,
    )
    decisions: list[ResultCompletenessDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = ResultCompletenessVerdict.COMPLETE  # unreachable default
    for rule in COMPLETENESS_RULES:
        matched = rule.predicate(verdict_input)
        decisions.append(
            ResultCompletenessDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
    # R-RPC-C1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return ResultCompletenessAudit(
        verdict=matched_verdict,
        goal_id=goal.goal_id,
        result_id=package.result_id,
        goal_version=package.goal_version,
        output_coverage=tuple(coverage),
        uncovered_outputs=uncovered,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# The monitor-facing gate (the consumer the Monitor consults before
# advancing a run to analysis)
# ---------------------------------------------------------------------------

#: A run reader resolving a run id to its durable record (mirrors
#: ``workers.run_helpers.read_run``).
RunReader: TypeAlias = Callable[[str], Run]

#: A results reader resolving a run id to its registered Result
#: Package(s) (the packages whose ``run_ref`` names the run, in
#: ``result_id`` order -- ``workers.results.list_worker_results`` is
#: sorted by ``result_id``).
RunResultsReader: TypeAlias = Callable[[str], tuple[WorkerResultPackage, ...]]

#: A goal reader resolving a goal id to its registered Goal Contract
#: (mirrors ``planning.plan.read_goal``).
GoalReader: TypeAlias = Callable[[str], GoalContract]

#: Lifecycle states in which the durable Run record records the result
#: (``RESULT_AVAILABLE`` or later, mirroring the reconcile engine /
#: recovery procedure / trigger registry): the only runs whose Result
#: Package completeness can be validated. A run that records no result
#: has no Result Package to validate.
_RESULT_RECORDED_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
        LifecycleState.INVALIDATED,
    }
)


def validate_result_package_completeness(
    root: str | Path,
    run_id: str,
    *,
    run_reader: RunReader | None = None,
    results_reader: RunResultsReader | None = None,
    goal_reader: GoalReader | None = None,
) -> ResultCompletenessAudit:
    """The monitor-facing completeness gate of one run (issue #155).

    Resolves the run's registered Result Package and the frozen Goal
    Contract through the registries, then evaluates the deterministic
    completeness verdict (``evaluate_result_package_completeness``).
    The Execution Monitor consults this gate **before advancing the run
    to analysis** -- before the ``RESULT_AVAILABLE -> ANALYZING``
    transition through ``workers.run_helpers.transition_run`` and
    before the analysis follow-up is issued through
    ``monitoring.triggers.TriggerRegistry``: an INCOMPLETE verdict
    names the uncovered declared outputs, and the run must not advance
    while they are missing.

    Referential integrity (house discipline): the run must record a
    result (``RESULT_AVAILABLE`` or later); exactly one registered
    Result Package must resolve for the run (its ``run_ref``); the
    package and the run must name the same goal and the same frozen
    goal version; the resolved contract must be the frozen contract of
    that goal. Every violation raises the stable
    :class:`ResultCompletenessError` and nothing is written.

    Args:
        root: the initialized workspace root (unused when all three
            readers are injected).
        run_id: the id of the run whose Result Package completeness is
            validated.
        run_reader: the reader resolving the run (default:
            ``workers.run_helpers.read_run`` over ``root``).
        results_reader: the reader resolving the run's registered
            Result Package(s) by ``run_ref`` (default:
            ``workers.results.list_worker_results`` over ``root``).
        goal_reader: the reader resolving the frozen Goal Contract
            (default: ``planning.plan.read_goal`` over ``root``).

    Returns:
        The frozen :class:`ResultCompletenessAudit` record for the
        run's registered Result Package against the frozen contract.

    Raises:
        TypeError: ``root`` is not a str/Path, ``run_id`` is not a str,
            a reader is not callable, or a reader returns a record of
            the wrong type.
        RunNotFoundError: no run with that id is registered (the
            default reader; an injected reader raises its own
            not-found error).
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root`` (default readers).
        GoalNotFoundError: no goal with the package's ``goal_id`` is
            registered (default reader).
        ResultCompletenessError: the run cannot carry a result, no
            registered Result Package resolves for the run, more than
            one does, the package and the run name different goals or
            goal versions, or the resolved contract is not frozen.
        ValueError: a stored record is corrupt (default readers).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    if run_reader is not None and not callable(run_reader):
        raise TypeError(
            f"run_reader must be callable, got {type(run_reader).__name__}"
        )
    if results_reader is not None and not callable(results_reader):
        raise TypeError(
            f"results_reader must be callable, got {type(results_reader).__name__}"
        )
    if goal_reader is not None and not callable(goal_reader):
        raise TypeError(
            f"goal_reader must be callable, got {type(goal_reader).__name__}"
        )
    project_root = Path(root).resolve()
    run = (
        run_reader(run_id)
        if run_reader is not None
        else read_run(project_root, run_id)
    )
    if not isinstance(run, Run):
        raise TypeError(
            f"run_reader must return a Run, got {type(run).__name__}"
        )
    if run.lifecycle_state not in _RESULT_RECORDED_RUN_STATES:
        raise ResultCompletenessError(
            f"run {run_id!r} cannot have its Result Package completeness"
            f" validated in lifecycle state"
            f" {run.lifecycle_state.value!r}: the durable run record does"
            " not record a result (RESULT_AVAILABLE or later)"
        )
    packages = (
        results_reader(run_id)
        if results_reader is not None
        else _list_results_for_run(project_root, run_id)
    )
    if not isinstance(packages, tuple) or any(
        not isinstance(package, WorkerResultPackage) for package in packages
    ):
        raise TypeError(
            "results_reader must return a tuple of WorkerResultPackage, got"
            f" {type(packages).__name__}"
        )
    if len(packages) > 1:
        raise ResultCompletenessError(
            f"{len(packages)} registered Result Packages resolve for run"
            f" {run_id!r} ("
            + ", ".join(package.result_id for package in packages)
            + "); the completeness gate cannot pick one deterministically"
        )
    if not packages:
        raise ResultCompletenessError(
            f"no registered Result Package resolves for run {run_id!r}"
            " (no worker result package names the run in its run_ref);"
            " register the result package before validating completeness"
        )
    package = packages[0]
    if package.goal_id != run.goal_id:
        raise ResultCompletenessError(
            f"result package {package.result_id!r} answers goal"
            f" {package.goal_id!r} but run {run_id!r} names goal"
            f" {run.goal_id!r}; the run and its Result Package must name"
            " the same goal"
        )
    if package.goal_version != run.goal_version:
        raise ResultCompletenessError(
            f"result package {package.result_id!r} answers goal version"
            f" {package.goal_version!r} but run {run_id!r} names"
            f" {run.goal_version!r}; the run and its Result Package must"
            " name the same frozen goal version"
        )
    goal = (
        goal_reader(package.goal_id)
        if goal_reader is not None
        else read_goal(project_root, package.goal_id)
    )
    if not isinstance(goal, GoalContract):
        raise TypeError(
            f"goal_reader must return a GoalContract, got {type(goal).__name__}"
        )
    return evaluate_result_package_completeness(package, goal)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _declared_output_key(
    declared: Any,
) -> tuple[str | None, str | None, str | None]:
    """Extract the stable key of one declared output (R-OUT-K1/K2/U1).

    Returns ``(key_kind, key_value, detail)``: the first usable stable
    key in the precedence order ``id``, ``name`` (a non-empty-string
    value), or ``(None, None, reason)`` when the declaration is
    unkeyable. ``detail`` carries the unkeyable reason; it is None for
    a usable key.
    """
    if not isinstance(declared, Mapping):
        return None, None, "declared output is not a mapping"
    unusable_reason: str | None = None
    for kind in _DECLARED_OUTPUT_KEY_KINDS:
        if kind not in declared:
            continue
        value = declared[kind]
        if isinstance(value, str) and value.strip():
            return kind, value, None
        # The key exists but is unusable: fall through to the next
        # kind (an unusable ``id`` does not block a usable ``name``,
        # R-OUT-K2); keep the first unusable key's reason for the
        # unkeyable detail (``id`` precedes ``name``).
        if unusable_reason is None:
            unusable_reason = (
                f"declared output {kind!r} is not a non-empty string"
                f" (got {value!r})"
            )
    if unusable_reason is not None:
        return None, None, unusable_reason
    return (
        None,
        None,
        "declared output has no 'id' or 'name' key",
    )


def _matching_refs(
    key_value: str,
    artifact_ids: frozenset[str],
    fact_ids: frozenset[str],
    fact_names: frozenset[str],
) -> set[str]:
    """The identifier refs of the package that carry ``key_value``.

    One ref per matching identifier kind: ``"artifact:<id>"``,
    ``"fact_id:<id>"`` and ``"fact_name:<name>"`` (the documented
    ``matched_by`` vocabulary).
    """
    refs: set[str] = set()
    if key_value in artifact_ids:
        refs.add(f"artifact:{key_value}")
    if key_value in fact_ids:
        refs.add(f"fact_id:{key_value}")
    if key_value in fact_names:
        refs.add(f"fact_name:{key_value}")
    return refs


def _list_results_for_run(
    root: Path, run_id: str
) -> tuple[WorkerResultPackage, ...]:
    """The registered Result Packages of one run (``run_ref`` match),
    in ``result_id`` order (``list_worker_results`` is sorted)."""
    return tuple(
        package
        for package in list_worker_results(root)
        if package.run_ref == run_id
    )


def _require_coverage_tuple(
    record: ResultCompletenessAudit, field_name: str
) -> None:
    """Reject a coverage field that is not a tuple of OutputCoverage."""
    value = getattr(record, field_name)
    if not isinstance(value, tuple) or any(
        not isinstance(entry, OutputCoverage) for entry in value
    ):
        raise TypeError(
            f"ResultCompletenessAudit.{field_name} must be a tuple of"
            f" OutputCoverage, got {value!r}"
        )

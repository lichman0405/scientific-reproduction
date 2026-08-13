"""Automatic retry authorization evaluator (DEV-M6-G04).

Implements the **automatic retry policy evaluator** deliverable of
DEV-M6-G04: the deterministic decision layer that the Goal Execution
Context Package's ``retry:<failure>`` actions (``workers/context.py``)
point at. For one frozen ``core.models.AutomaticRetryPolicy`` and one
reported failure, it decides whether the retry is **pre-authorized for
automatic worker action** (the whitelist of
``allowed_engineering_failures``, and identical checkpoint continuation
within ``max_identical_retries``), whether the failure **invalidates
the run** instead of retrying (``invalidate_run_on``), or whether the
failure is a **scientific parameter modification that must route to
the Supervisor** (``supervisor_required_changes`` -- the explicit
prohibitions of ``05-GOAL-RUN-SCHEMA.md`` SS8).

Normative grounding (locked readings)
-------------------------------------
* ``11-COMPUTATION-SUBSYSTEM.md`` SS5 -- the engineering/scientific
  retry split: SSH transient failures, scheduler node failures,
  identical resubmissions, network timeouts and checkpoint
  continuation with unchanged scientific parameters are automatic
  retries; changing functional / force field / k-point mesh / cutoff
  or convergence tolerance / thermostat / integration / model
  parameters / molecular charges / GCMC mixing rules are
  Supervisor-required changes.
* ``03-ROLE-AND-PERMISSION-SPEC.md`` SS1 ("Workers report facts.
  Supervisor makes scientific decisions.") and SS4 -- the Monitor may
  execute preauthorized engineering retries but may not change
  scientific parameters: the evaluator's AUTHORIZED verdict is exactly
  the pre-authorized retry of ``core/permissions.py``
  ``Action.ENGINEERING_RETRY``, and its SUPERVISOR routing is exactly
  the ``SCIENTIFIC_PARAMETER_CHANGE`` boundary (Supervisor-only).
* ``05-GOAL-RUN-SCHEMA.md`` SS4/SS8 and
  ``schemas/retry-policy.schema.yaml`` -- the goal's automatic retry
  policy is the contract: ``allowed_engineering_failures`` are the
  retries the worker may take on its own,
  ``supervisor_required_changes`` the retries explicitly prohibited
  without a Supervisor change.
* ``examples/fdm-201`` simulated scenario S5 -- the FDM-201
  counter-case: "changing smearing/mixing/convergence policy would
  alter the method", so a scientific parameter modification of that
  kind is never auto-authorized (AC-03).

Decision model (determinism)
----------------------------
Evaluation is the ordered, versioned ``RETRY_DECISION_RULES`` table:
first match wins; the trailing total default ``R-RET-D1`` rejects
every failure no earlier rule authorized -- a failure not whitelisted
is not authorized, even if it looks transient (the whitelist is the
contract). Scientific classification is decided first (``R-RET-S1``
supervisor-required changes, ``R-RET-V1`` the frozen detection
vocabulary): a scientific parameter modification is never authorized
for automatic worker action, even when a policy also whitelists it
(AC-03). Invalidation (``R-RET-I1``) beats the whitelist: those
failure kinds invalidate the run instead of retrying. The whitelist
``R-RET-A1`` authorizes the pre-authorized engineering retries of
AC-01; the checkpoint rules ``R-RET-C1`` / ``R-RET-C2`` gate identical
checkpoint continuation by ``max_identical_retries`` (``None`` =
unlimited, an int = hard ceiling) per AC-02. Every assessment records
the full decision record (every rule evaluation, the matched rule id,
the matched policy entries and the frozen reasoning ids), and the
matched rule's verdict/routing are post-asserted against the
assessment -- the verdict always matches the recorded reason.

Determinism and boundaries
--------------------------
Pure functions of the frozen policy record and the injected
``RetryEvaluationInput`` only: no I/O, no wall clock, no network, no
mutation. The module exposes pure evaluation only -- there is no
execution API, so the Supervisor-routed changes have no path to be
executed here (AC-03 boundary). ``TypeError`` at the public type
boundaries (never ``ValueError`` for wrong types); value/rule
violations follow the ``ValueError``-subclass convention with stable
one-line messages (``RetryEvaluationError`` for input value
violations, ``MalformedRetryPolicyError`` for malformed policy usage,
``RetryRulesetError`` for rule-table shape violations);
``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from scientific_reproduction.core.models import AutomaticRetryPolicy

__all__ = [
    "CHECKPOINT_CONTINUATION_KIND",
    "REASON_ALLOWED_ENGINEERING_FAILURE",
    "REASON_CEILING_NOT_REACHED",
    "REASON_CEILING_REACHED",
    "REASON_CEILING_UNLIMITED",
    "REASON_IDENTICAL_CHECKPOINT_CONTINUATION",
    "REASON_IDS",
    "REASON_INVALIDATE_RUN",
    "REASON_NO_POLICY_ENTRY",
    "REASON_SCIENTIFIC_CHANGE_VOCABULARY",
    "REASON_SUPERVISOR_REQUIRED_CHANGE",
    "RETRY_AUTHORIZATION_RULESET_VERSION",
    "RETRY_DECISION_RULES",
    "SCIENTIFIC_CHANGE_FAILURES",
    "MalformedRetryPolicyError",
    "RetryAssessment",
    "RetryAuthorization",
    "RetryEvaluationError",
    "RetryEvaluationInput",
    "RetryPolicyError",
    "RetryRouting",
    "RetryRule",
    "RetryRuleDecision",
    "RetryRulesetError",
    "evaluate_automatic_retry",
    "validate_retry_ruleset",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class RetryPolicyError(ValueError):
    """Base class for all automatic retry policy evaluation errors."""


class RetryEvaluationError(RetryPolicyError):
    """Raised for value/rule violations of a retry evaluation input.

    Stable one-line messages name the offending value and the violated
    rule; wrong *types* are rejected with ``TypeError`` at the public
    boundaries, never with this class.
    """


class MalformedRetryPolicyError(RetryPolicyError):
    """Raised when the frozen policy record cannot serve as a contract.

    The whitelist is the contract, so the contract must be well-formed:
    the failure-kind lists must be lists of non-empty strings and
    ``max_identical_retries`` must be None or a non-negative int
    (``schemas/retry-policy.schema.yaml``). Stable messages name the
    offending policy and field.
    """


class RetryRulesetError(RetryPolicyError):
    """Raised when a retry decision rule table violates the frozen shape.

    Covers empty tables, duplicate rule ids and a trailing rule that is
    not a total default (stable messages naming the violation).
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the retry authorization rule table; recorded in every
#: assessment so old decisions stay interpretable. Bumped whenever a
#: rule changes.
RETRY_AUTHORIZATION_RULESET_VERSION: str = "1.0"

#: The failure kind of an identical checkpoint continuation
#: (11-COMPUTATION-SUBSYSTEM.md SS5: "checkpoint continuation with
#: unchanged scientific parameters").
CHECKPOINT_CONTINUATION_KIND: str = "checkpoint_continuation"

#: Frozen detection vocabulary of known scientific parameter
#: modifications (11-COMPUTATION-SUBSYSTEM.md SS5 Supervisor-required
#: examples, plus the FDM-201 counter-case of the simulated scenario
#: suite -- smearing/mixing/convergence-policy changes). Detection aid
#: only: the authoritative list is the policy's
#: ``supervisor_required_changes``; a kind the vocabulary names is
#: never auto-authorized even when a policy also whitelists it (AC-03).
SCIENTIFIC_CHANGE_FAILURES: frozenset[str] = frozenset(
    {
        "convergence_policy_change",
        "convergence_tolerance_change",
        "cutoff_change",
        "force_field_change",
        "functional_change",
        "integration_parameter_change",
        "kpoint_mesh_change",
        "mixing_parameter_change",
        "mixing_rule_change",
        "model_parameter_change",
        "molecular_charge_change",
        "smearing_parameter_change",
        "thermostat_parameter_change",
    }
)

#: Frozen reasoning-id vocabulary recorded in every assessment. The ids
#: name exactly which contract entry or state decided the verdict (the
#: whitelist entry, the supervisor-required change, the invalidation
#: trigger, the ceiling state); no other id can appear in an
#: assessment.
REASON_ALLOWED_ENGINEERING_FAILURE: str = "allowed_engineering_failures"
REASON_SUPERVISOR_REQUIRED_CHANGE: str = "supervisor_required_changes"
REASON_SCIENTIFIC_CHANGE_VOCABULARY: str = "scientific_change_vocabulary"
REASON_INVALIDATE_RUN: str = "invalidate_run_on"
REASON_IDENTICAL_CHECKPOINT_CONTINUATION: str = (
    "identical_checkpoint_continuation"
)
REASON_CEILING_UNLIMITED: str = "max_identical_retries_unlimited"
REASON_CEILING_NOT_REACHED: str = "max_identical_retries_not_reached"
REASON_CEILING_REACHED: str = "max_identical_retries_reached"
REASON_NO_POLICY_ENTRY: str = "no_policy_entry"

REASON_IDS: frozenset[str] = frozenset(
    {
        REASON_ALLOWED_ENGINEERING_FAILURE,
        REASON_SUPERVISOR_REQUIRED_CHANGE,
        REASON_SCIENTIFIC_CHANGE_VOCABULARY,
        REASON_INVALIDATE_RUN,
        REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
        REASON_CEILING_UNLIMITED,
        REASON_CEILING_NOT_REACHED,
        REASON_CEILING_REACHED,
        REASON_NO_POLICY_ENTRY,
    }
)


class RetryAuthorization(StrEnum):
    """The frozen verdict vocabulary of a retry authorization decision.

    AUTHORIZED: the automatic worker action is pre-authorized (the
    pre-authorized automatic retry of ``core.permissions.py``
    ``Action.ENGINEERING_RETRY``). REJECTED: the automatic worker
    action is not authorized (the failure is routed or invalidated, or
    no contract entry authorizes it).
    """

    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"


class RetryRouting(StrEnum):
    """The frozen routing vocabulary of a retry authorization decision.

    AUTOMATIC: the failure stays in the automatic/operational
    machinery (the pre-authorized retry runs, or the run is
    invalidated, or nothing is authorized -- no Supervisor change is
    needed). SUPERVISOR: the failure is a scientific parameter
    modification routed to the Supervisor (``03-ROLE-AND-PERMISSION-SPEC.md``
    SS2 -- the Supervisor alone makes scientific decisions); the
    routing is a decision record, never an execution.
    """

    AUTOMATIC = "AUTOMATIC"
    SUPERVISOR = "SUPERVISOR"


# ---------------------------------------------------------------------------
# The evaluation input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryEvaluationInput:
    """The state a retry authorization decision is a pure function of.

    ``policy`` is the goal's frozen ``AutomaticRetryPolicy`` record
    (the contract); ``failure_kind`` is the stable failure-kind string
    of the reported failure -- the exact suffix of the context
    package's ``"retry:<failure>"`` actions (e.g.
    ``"ssh_connection_lost"``, ``"network_timeout"``,
    ``"scheduler_node_failure"``, ``"checkpoint_continuation"``,
    ``"smearing_parameter_change"``); ``identical_retry_count`` is the
    number of identical retries performed so far (the assessment
    decides the next attempt); ``checkpoint_continuation`` declares
    that the failure is an **identical checkpoint continuation** -- a
    rerun from the same checkpoint with zero scientific change
    (AC-02). The flag must be True exactly for the
    ``checkpoint_continuation`` kind: the evaluator cannot observe
    scientific change, it can only accept or refuse the caller's
    declaration.
    """

    policy: AutomaticRetryPolicy
    failure_kind: str
    identical_retry_count: int = 0
    checkpoint_continuation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.policy, AutomaticRetryPolicy):
            raise TypeError(
                "RetryEvaluationInput.policy must be an"
                " AutomaticRetryPolicy, got"
                f" {type(self.policy).__name__}"
            )
        _validate_policy_shape(self.policy)
        if not isinstance(self.failure_kind, str):
            raise TypeError(
                "RetryEvaluationInput.failure_kind must be a str, got"
                f" {type(self.failure_kind).__name__}"
            )
        if not self.failure_kind.strip():
            raise RetryEvaluationError(
                "RetryEvaluationInput.failure_kind must be a non-empty"
                f" string, got {self.failure_kind!r}"
            )
        if isinstance(self.identical_retry_count, bool) or not isinstance(
            self.identical_retry_count, int
        ):
            raise TypeError(
                "RetryEvaluationInput.identical_retry_count must be an"
                " int, got"
                f" {type(self.identical_retry_count).__name__}"
            )
        if self.identical_retry_count < 0:
            raise RetryEvaluationError(
                "RetryEvaluationInput.identical_retry_count must be"
                f" >= 0, got {self.identical_retry_count}"
            )
        if not isinstance(self.checkpoint_continuation, bool):
            raise TypeError(
                "RetryEvaluationInput.checkpoint_continuation must be a"
                " bool, got"
                f" {type(self.checkpoint_continuation).__name__}"
            )
        if (self.failure_kind == CHECKPOINT_CONTINUATION_KIND) != (
            self.checkpoint_continuation
        ):
            raise RetryEvaluationError(
                "RetryEvaluationInput.checkpoint_continuation must be"
                " True exactly for failure_kind"
                f" {CHECKPOINT_CONTINUATION_KIND!r} (an identical"
                " checkpoint continuation reruns from the same"
                " checkpoint with zero scientific change), got"
                f" failure_kind={self.failure_kind!r}"
                f" checkpoint_continuation={self.checkpoint_continuation}"
            )


def _validate_policy_shape(policy: AutomaticRetryPolicy) -> None:
    """Reject a malformed policy record before it is consulted.

    The evaluator consumes the three failure-kind lists and the
    ceiling; a record whose entries are not non-empty strings, or whose
    ceiling is not None or a non-negative int, cannot be a contract and
    is rejected loudly with a stable one-line message. ``TypeError`` is
    reserved for the public boundaries; the contents of a policy record
    are value-level (``MalformedRetryPolicyError``), mirroring how the
    replication hook treats invalid criteria contents.
    """
    for field_name in (
        "allowed_engineering_failures",
        "supervisor_required_changes",
        "invalidate_run_on",
    ):
        entries = getattr(policy, field_name)
        if not isinstance(entries, list) or not all(
            isinstance(entry, str) and entry.strip() for entry in entries
        ):
            raise MalformedRetryPolicyError(
                f"retry policy {policy.policy_id!r} field {field_name!r}"
                " must be a list of non-empty strings, got"
                f" {entries!r}"
            )
    ceiling = policy.max_identical_retries
    if ceiling is not None:
        if isinstance(ceiling, bool) or not isinstance(ceiling, int):
            raise MalformedRetryPolicyError(
                f"retry policy {policy.policy_id!r} field"
                " 'max_identical_retries' must be an int or None, got"
                f" {ceiling!r}"
            )
        if ceiling < 0:
            raise MalformedRetryPolicyError(
                f"retry policy {policy.policy_id!r} field"
                " 'max_identical_retries' must be a non-negative int"
                f" (schema minimum 0), got {ceiling}"
            )


# ---------------------------------------------------------------------------
# The ordered rule table (first match wins, total default)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryRule:
    """One entry of the ordered retry authorization decision rule table."""

    rule_id: str
    description: str
    verdict: RetryAuthorization
    routing: RetryRouting
    predicate: Callable[[RetryEvaluationInput], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"RetryRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise RetryRulesetError(
                    f"RetryRule.{field_name} must be a non-empty string,"
                    f" got {value!r}"
                )
        if not isinstance(self.verdict, RetryAuthorization):
            raise TypeError(
                "RetryRule.verdict must be a RetryAuthorization member,"
                f" got {type(self.verdict).__name__}"
            )
        if not isinstance(self.routing, RetryRouting):
            raise TypeError(
                "RetryRule.routing must be a RetryRouting member, got"
                f" {type(self.routing).__name__}"
            )
        if not callable(self.predicate):
            raise TypeError(
                "RetryRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


#: The ordered retry authorization decision rule table. First match
#: wins; order is normative. Scientific classification is decided first
#: (``R-RET-S1`` -- the policy's ``supervisor_required_changes``, the
#: authoritative list; ``R-RET-V1`` -- the frozen detection vocabulary):
#: a scientific parameter modification is never authorized for
#: automatic worker action, even when a policy also whitelists it
#: (AC-03). Invalidation (``R-RET-I1``) beats the whitelist: those
#: failure kinds invalidate the run instead of retrying. The whitelist
#: ``R-RET-A1`` authorizes the pre-authorized engineering retries of
#: AC-01; the checkpoint rules ``R-RET-C1`` / ``R-RET-C2`` gate
#: identical checkpoint continuation by ``max_identical_retries``
#: (AC-02). The trailing total default ``R-RET-D1`` rejects everything
#: no rule authorized (the whitelist is the contract).
RETRY_DECISION_RULES: tuple[RetryRule, ...] = (
    RetryRule(
        rule_id="R-RET-S1",
        description=(
            "the failure kind is in the policy's"
            " supervisor_required_changes: the retry is explicitly"
            " prohibited without a Supervisor change"
            " (05-GOAL-RUN-SCHEMA.md SS8, 11-COMPUTATION-SUBSYSTEM.md"
            " SS5): reject automatic action and route the change to the"
            " Supervisor (AC-03 -- scientific parameter modifications"
            " are never authorized for automatic worker action)"
        ),
        verdict=RetryAuthorization.REJECTED,
        routing=RetryRouting.SUPERVISOR,
        predicate=lambda i: i.failure_kind in i.policy.supervisor_required_changes,
    ),
    RetryRule(
        rule_id="R-RET-V1",
        description=(
            "the failure kind names a known scientific parameter"
            " modification (the frozen SCIENTIFIC_CHANGE_FAILURES"
            " vocabulary -- functional / force field / k-point mesh /"
            " cutoff / convergence / thermostat / integration / model /"
            " charge / mixing changes of 11-COMPUTATION-SUBSYSTEM.md"
            " SS5 and the FDM-201 simulated scenario S5): reject"
            " automatic action and route to the Supervisor; the"
            " vocabulary only aids detection -- the policy's"
            " supervisor_required_changes stay authoritative (AC-03)"
        ),
        verdict=RetryAuthorization.REJECTED,
        routing=RetryRouting.SUPERVISOR,
        predicate=lambda i: i.failure_kind in SCIENTIFIC_CHANGE_FAILURES,
    ),
    RetryRule(
        rule_id="R-RET-I1",
        description=(
            "the failure kind is in the policy's invalidate_run_on: the"
            " failure invalidates the run instead of retrying (AC-02 --"
            " an invalidate_run_on situation is never an authorized"
            " retry)"
        ),
        verdict=RetryAuthorization.REJECTED,
        routing=RetryRouting.AUTOMATIC,
        predicate=lambda i: i.failure_kind in i.policy.invalidate_run_on,
    ),
    RetryRule(
        rule_id="R-RET-A1",
        description=(
            "the failure kind is in the policy's"
            " allowed_engineering_failures (and is not a checkpoint"
            " continuation, which the ceiling rules govern): the"
            " transient engineering failure is pre-authorized for"
            " automatic worker action -- AC-01, the whitelist is the"
            " contract (11-COMPUTATION-SUBSYSTEM.md SS5: SSH transient"
            " failure, scheduler node failure, network timeout)"
        ),
        verdict=RetryAuthorization.AUTHORIZED,
        routing=RetryRouting.AUTOMATIC,
        predicate=lambda i: (
            i.failure_kind in i.policy.allowed_engineering_failures
            and i.failure_kind != CHECKPOINT_CONTINUATION_KIND
        ),
    ),
    RetryRule(
        rule_id="R-RET-C1",
        description=(
            "identical checkpoint continuation (failure kind"
            " 'checkpoint_continuation', declared identical -- rerun"
            " from the same checkpoint with zero scientific change)"
            " within the policy's max_identical_retries ceiling (None ="
            " unlimited): authorize the automatic retry -- AC-02"
        ),
        verdict=RetryAuthorization.AUTHORIZED,
        routing=RetryRouting.AUTOMATIC,
        predicate=lambda i: (
            i.failure_kind == CHECKPOINT_CONTINUATION_KIND
            and i.checkpoint_continuation
            and (
                i.policy.max_identical_retries is None
                or i.identical_retry_count < i.policy.max_identical_retries
            )
        ),
    ),
    RetryRule(
        rule_id="R-RET-C2",
        description=(
            "identical checkpoint continuation at or over the policy's"
            " max_identical_retries hard ceiling: reject the automatic"
            " retry (the ceiling is exhausted -- AC-02)"
        ),
        verdict=RetryAuthorization.REJECTED,
        routing=RetryRouting.AUTOMATIC,
        predicate=lambda i: (
            i.failure_kind == CHECKPOINT_CONTINUATION_KIND
            and i.checkpoint_continuation
            and i.policy.max_identical_retries is not None
            and i.identical_retry_count >= i.policy.max_identical_retries
        ),
    ),
    RetryRule(
        rule_id="R-RET-D1",
        description=(
            "the failure kind matches no policy entry and no known"
            " scientific change: reject -- a failure not whitelisted is"
            " not authorized for automatic worker action, even if it"
            " looks transient (the whitelist is the contract;"
            " AC-01/AC-02/AC-03)"
        ),
        verdict=RetryAuthorization.REJECTED,
        routing=RetryRouting.AUTOMATIC,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class RetryRuleDecision:
    """Record of one rule-table evaluation for a given failure."""

    rule_id: str
    description: str
    verdict: RetryAuthorization
    routing: RetryRouting
    matched: bool

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"RetryRuleDecision.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
        if not isinstance(self.verdict, RetryAuthorization):
            raise TypeError(
                "RetryRuleDecision.verdict must be a RetryAuthorization"
                f" member, got {type(self.verdict).__name__}"
            )
        if not isinstance(self.routing, RetryRouting):
            raise TypeError(
                "RetryRuleDecision.routing must be a RetryRouting"
                f" member, got {type(self.routing).__name__}"
            )
        if not isinstance(self.matched, bool):
            raise TypeError(
                "RetryRuleDecision.matched must be a bool, got"
                f" {type(self.matched).__name__}"
            )


@dataclass(frozen=True)
class RetryAssessment:
    """Full, auditable result of one retry authorization decision.

    ``input`` is the exact failure state the decision was computed
    from; ``verdict`` is the authorization (``RetryAuthorization``);
    ``routing`` is where the failure goes (``RetryRouting``: AUTOMATIC
    -- the pre-authorized retry runs, or the run is invalidated, or
    nothing is authorized; SUPERVISOR -- the scientific parameter
    change is routed to the Supervisor as a decision record, never an
    execution); ``decisions`` records the outcome of every rule in the
    table (in evaluation order); ``matched_rule_id`` names the deciding
    rule (``None`` is impossible: the trailing total default
    ``R-RET-D1`` always matches); ``matched_policy_entries`` names the
    exact policy-list entries (or vocabulary terms) that matched;
    ``reasoning_ids`` are the frozen reason ids of the decision (which
    whitelist entry matched, ceiling state, invalidation trigger);
    ``ceiling`` / ``ceiling_reached`` record the identical retry count
    against the policy's ``max_identical_retries`` (``ceiling`` is the
    policy ceiling verbatim, ``None`` = unlimited); ``ruleset_version``
    records the rule table version.
    """

    input: RetryEvaluationInput
    verdict: RetryAuthorization
    routing: RetryRouting
    decisions: tuple[RetryRuleDecision, ...]
    matched_rule_id: str
    matched_policy_entries: tuple[str, ...]
    reasoning_ids: tuple[str, ...]
    ruleset_version: str = RETRY_AUTHORIZATION_RULESET_VERSION
    ceiling: int | None = None
    ceiling_reached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.input, RetryEvaluationInput):
            raise TypeError(
                "RetryAssessment.input must be a RetryEvaluationInput,"
                f" got {type(self.input).__name__}"
            )
        if not isinstance(self.verdict, RetryAuthorization):
            raise TypeError(
                "RetryAssessment.verdict must be a RetryAuthorization"
                f" member, got {type(self.verdict).__name__}"
            )
        if not isinstance(self.routing, RetryRouting):
            raise TypeError(
                "RetryAssessment.routing must be a RetryRouting member,"
                f" got {type(self.routing).__name__}"
            )
        if not isinstance(self.decisions, tuple) or not all(
            isinstance(decision, RetryRuleDecision)
            for decision in self.decisions
        ):
            raise TypeError(
                "RetryAssessment.decisions must be a tuple of"
                " RetryRuleDecision, got"
                f" {type(self.decisions).__name__}"
            )
        if not isinstance(self.matched_rule_id, str):
            raise TypeError(
                "RetryAssessment.matched_rule_id must be a str, got"
                f" {type(self.matched_rule_id).__name__}"
            )
        if not isinstance(self.matched_policy_entries, tuple) or not all(
            isinstance(entry, str) for entry in self.matched_policy_entries
        ):
            raise TypeError(
                "RetryAssessment.matched_policy_entries must be a tuple"
                " of str, got"
                f" {type(self.matched_policy_entries).__name__}"
            )
        if not isinstance(self.reasoning_ids, tuple) or not all(
            isinstance(reason, str) for reason in self.reasoning_ids
        ):
            raise TypeError(
                "RetryAssessment.reasoning_ids must be a tuple of str,"
                f" got {type(self.reasoning_ids).__name__}"
            )
        if not isinstance(self.ruleset_version, str):
            raise TypeError(
                "RetryAssessment.ruleset_version must be a str, got"
                f" {type(self.ruleset_version).__name__}"
            )
        if self.ceiling is not None and (
            isinstance(self.ceiling, bool) or not isinstance(self.ceiling, int)
        ):
            raise TypeError(
                "RetryAssessment.ceiling must be an int or None, got"
                f" {type(self.ceiling).__name__}"
            )
        if not isinstance(self.ceiling_reached, bool):
            raise TypeError(
                "RetryAssessment.ceiling_reached must be a bool, got"
                f" {type(self.ceiling_reached).__name__}"
            )
        # Value and integrity rules (stable messages).
        if not self.decisions:
            raise RetryEvaluationError(
                "a retry assessment must record at least one rule"
                " decision"
            )
        if not self.matched_rule_id.strip():
            raise RetryEvaluationError(
                "a retry assessment must record the matched rule id"
            )
        if not self.ruleset_version.strip():
            raise RetryEvaluationError(
                "a retry assessment must record a non-empty ruleset"
                " version"
            )
        if self.ceiling is not None and self.ceiling < 0:
            raise RetryEvaluationError(
                "a retry assessment ceiling must be a non-negative int,"
                f" got {self.ceiling}"
            )
        if self.ceiling is None and self.ceiling_reached:
            raise RetryEvaluationError(
                "a retry assessment with no ceiling cannot record"
                " ceiling_reached"
            )
        if self.ceiling is not None and self.ceiling != (
            self.input.policy.max_identical_retries
        ):
            raise RetryEvaluationError(
                "a retry assessment ceiling must equal the policy's"
                " max_identical_retries verbatim, got"
                f" {self.ceiling}"
            )
        if not self.reasoning_ids:
            raise RetryEvaluationError(
                "a retry assessment must record at least one reasoning"
                " id"
            )
        unknown_reasons = sorted(
            reason
            for reason in self.reasoning_ids
            if reason not in REASON_IDS
        )
        if unknown_reasons:
            raise RetryEvaluationError(
                "a retry assessment records reasoning ids of the frozen"
                " vocabulary only, got:"
                f" {', '.join(unknown_reasons)}"
            )
        empty_entries = [
            entry for entry in self.matched_policy_entries if not entry.strip()
        ]
        if empty_entries:
            raise RetryEvaluationError(
                "matched policy entries must be non-empty strings"
            )
        matched = [
            decision
            for decision in self.decisions
            if decision.rule_id == self.matched_rule_id
        ]
        if len(matched) != 1:
            raise RetryEvaluationError(
                "the matched rule id must name exactly one recorded rule"
                " decision, got"
                f" {len(matched)} for {self.matched_rule_id!r}"
            )
        matched_decision = matched[0]
        if not matched_decision.matched:
            raise RetryEvaluationError(
                "the matched rule decision must be recorded as matched"
            )
        if matched_decision.verdict is not self.verdict:
            raise RetryEvaluationError(
                "the assessment verdict must equal the matched rule's"
                " verdict"
            )
        if matched_decision.routing is not self.routing:
            raise RetryEvaluationError(
                "the assessment routing must equal the matched rule's"
                " routing"
            )
        if (
            self.verdict is RetryAuthorization.AUTHORIZED
            and self.routing is not RetryRouting.AUTOMATIC
        ):
            raise RetryEvaluationError(
                "an authorized automatic retry is never routed to the"
                " Supervisor"
            )
        expected_ceiling_reached = (
            self.input.failure_kind == CHECKPOINT_CONTINUATION_KIND
            and self.ceiling is not None
            and self.input.identical_retry_count >= self.ceiling
        )
        if self.ceiling_reached != expected_ceiling_reached:
            raise RetryEvaluationError(
                "ceiling_reached must record whether the identical"
                " retry count of the checkpoint continuation is at or"
                " over the ceiling"
            )


# ---------------------------------------------------------------------------
# Evaluation (pure and deterministic)
# ---------------------------------------------------------------------------


def evaluate_automatic_retry(input_: RetryEvaluationInput) -> RetryAssessment:
    """Decide the retry authorization for one reported failure.

    Pure and deterministic: the decision is a pure function of the
    frozen policy record and the injected failure state, decided by the
    ordered ``RETRY_DECISION_RULES`` table (first match wins; the
    trailing total default ``R-RET-D1`` rejects anything no rule
    authorized). The full assessment records every rule evaluation, the
    matched rule id, the matched policy entries, the frozen reasoning
    ids and the ceiling state; the matched rule's verdict and routing
    are post-asserted to be the assessment's own -- the verdict always
    matches the recorded reason.

    Raises:
        TypeError: ``input_`` is not a ``RetryEvaluationInput``.
    """
    if not isinstance(input_, RetryEvaluationInput):
        raise TypeError(
            "evaluate_automatic_retry expects a RetryEvaluationInput,"
            f" got {type(input_).__name__}"
        )
    decisions: list[RetryRuleDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = RetryAuthorization.REJECTED  # unreachable default
    matched_routing = RetryRouting.AUTOMATIC  # unreachable default
    for rule in RETRY_DECISION_RULES:
        matched = rule.predicate(input_)
        decisions.append(
            RetryRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                routing=rule.routing,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
            matched_routing = rule.routing
    # R-RET-D1 (the total default) always matches, so this can never be None.
    assert matched_rule_id is not None
    matched_entries, reasoning_ids = _reason_record(matched_rule_id, input_)
    # Post-asserted invariant: the verdict matches the recorded reason.
    matched_decision = next(
        decision for decision in decisions if decision.rule_id == matched_rule_id
    )
    assert matched_decision.matched
    assert matched_decision.verdict is matched_verdict
    assert matched_decision.routing is matched_routing
    ceiling = input_.policy.max_identical_retries
    ceiling_reached = (
        input_.failure_kind == CHECKPOINT_CONTINUATION_KIND
        and ceiling is not None
        and input_.identical_retry_count >= ceiling
    )
    return RetryAssessment(
        input=input_,
        verdict=matched_verdict,
        routing=matched_routing,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
        matched_policy_entries=matched_entries,
        reasoning_ids=reasoning_ids,
        ceiling=ceiling,
        ceiling_reached=ceiling_reached,
    )


# ---------------------------------------------------------------------------
# Reasoning record (frozen and reproducible)
# ---------------------------------------------------------------------------


def _reason_record(
    rule_id: str, input_: RetryEvaluationInput
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The matched policy entries and reasoning ids of the deciding rule.

    Deterministic and frozen: the possible reasoning ids are exactly
    the ``REASON_IDS`` vocabulary, and the matched policy entries are
    the exact policy-list entries (or vocabulary terms) the deciding
    rule's predicate matched on -- the same decision always records the
    same record.
    """
    if rule_id == "R-RET-S1":
        return (input_.failure_kind,), (REASON_SUPERVISOR_REQUIRED_CHANGE,)
    if rule_id == "R-RET-V1":
        return (input_.failure_kind,), (REASON_SCIENTIFIC_CHANGE_VOCABULARY,)
    if rule_id == "R-RET-I1":
        return (input_.failure_kind,), (REASON_INVALIDATE_RUN,)
    if rule_id == "R-RET-A1":
        return (input_.failure_kind,), (REASON_ALLOWED_ENGINEERING_FAILURE,)
    if rule_id == "R-RET-C1":
        if input_.policy.max_identical_retries is None:
            return (), (
                REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
                REASON_CEILING_UNLIMITED,
            )
        return (), (
            REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
            REASON_CEILING_NOT_REACHED,
        )
    if rule_id == "R-RET-C2":
        return (), (
            REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
            REASON_CEILING_REACHED,
        )
    if rule_id == "R-RET-D1":
        return (), (REASON_NO_POLICY_ENTRY,)
    raise RetryRulesetError(f"unknown retry decision rule {rule_id!r}")


# ---------------------------------------------------------------------------
# Ruleset integrity (frozen, complete, total)
# ---------------------------------------------------------------------------

#: Probe inputs covering every rule trigger, used by
#: :func:`validate_retry_ruleset` to prove the trailing rule is a total
#: default (a decision exists for every rule-triggering shape).
_RULESET_PROBES: tuple[RetryEvaluationInput, ...] = (
    # R-RET-S1 trigger: a supervisor-required change.
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-1",
            allowed_engineering_failures=[],
            supervisor_required_changes=["functional_change"],
        ),
        failure_kind="functional_change",
    ),
    # R-RET-V1 trigger: a vocabulary-known scientific change the policy
    # does not list (the vocabulary is a detection aid).
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-2",
            allowed_engineering_failures=[],
            supervisor_required_changes=[],
        ),
        failure_kind="mixing_rule_change",
    ),
    # R-RET-I1 trigger: an invalidation.
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-3",
            allowed_engineering_failures=[],
            supervisor_required_changes=[],
            invalidate_run_on=["sample_loss"],
        ),
        failure_kind="sample_loss",
    ),
    # R-RET-A1 trigger: a whitelisted engineering failure.
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-4",
            allowed_engineering_failures=["ssh_connection_lost"],
            supervisor_required_changes=[],
        ),
        failure_kind="ssh_connection_lost",
    ),
    # R-RET-C1 trigger: identical checkpoint continuation, no ceiling.
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-5",
            allowed_engineering_failures=[],
            supervisor_required_changes=[],
        ),
        failure_kind="checkpoint_continuation",
        checkpoint_continuation=True,
    ),
    # R-RET-C2 trigger: identical checkpoint continuation at the ceiling.
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-6",
            allowed_engineering_failures=[],
            supervisor_required_changes=[],
            max_identical_retries=2,
        ),
        failure_kind="checkpoint_continuation",
        identical_retry_count=2,
        checkpoint_continuation=True,
    ),
    # R-RET-D1 trigger: an arbitrary failure no rule authorizes.
    RetryEvaluationInput(
        policy=AutomaticRetryPolicy(
            policy_id="RETRY-PROBE-7",
            allowed_engineering_failures=[],
            supervisor_required_changes=[],
        ),
        failure_kind="arbitrary_unknown_failure",
    ),
)


def validate_retry_ruleset(
    rules: Sequence[RetryRule] | None = None,
) -> tuple[str, ...]:
    """Validate a retry decision rule table's integrity; return its ids.

    The frozen module table ``RETRY_DECISION_RULES`` is validated by
    default; an explicit candidate table can be passed (e.g. a
    versioned replacement). A valid table is non-empty, has unique rule
    ids, and its trailing rule matches every probe input of the
    rule-triggering shapes -- the total default that guarantees
    first-match evaluation is total (a decision always exists).
    ``evaluate_automatic_retry`` post-asserts this invariant; this
    validator surfaces it loudly and early.

    Raises:
        TypeError: ``rules`` is neither a sequence of ``RetryRule`` nor
            None, or an entry is not a ``RetryRule``.
        RetryRulesetError: the table is empty, carries duplicate rule
            ids, or its trailing rule is not a total default (stable
            messages).
    """
    table = RETRY_DECISION_RULES if rules is None else rules
    if not isinstance(table, Sequence) or isinstance(table, (str, bytes)):
        raise TypeError(
            "rules must be a sequence of RetryRule or None, got"
            f" {type(table).__name__}"
        )
    rules_tuple = tuple(table)
    for rule in rules_tuple:
        if not isinstance(rule, RetryRule):
            raise TypeError(
                "retry rule table entries must be RetryRule instances,"
                f" got {type(rule).__name__}"
            )
    if not rules_tuple:
        raise RetryRulesetError(
            "the retry decision rule table must not be empty: at least"
            " the total default rule is required"
        )
    rule_ids = tuple(rule.rule_id for rule in rules_tuple)
    duplicates = sorted(
        {rule_id for rule_id in rule_ids if rule_ids.count(rule_id) > 1}
    )
    if duplicates:
        raise RetryRulesetError(
            "duplicate rule id(s) in the retry decision rule table:"
            f" {', '.join(duplicates)}"
        )
    default_rule = rules_tuple[-1]
    for probe in _RULESET_PROBES:
        if not default_rule.predicate(probe):
            raise RetryRulesetError(
                f"the trailing rule {default_rule.rule_id!r} is not a"
                " total default: it does not match the"
                f" {probe.failure_kind!r} probe input"
            )
    return rule_ids

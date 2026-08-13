"""Frozen Primary Analysis Protocol records (DEV-M9-G01).

Implements **analysis protocol persistence**, the **freeze/version rules**
and the **PRIMARY vs EXPLORATORY separation** deliverables of DEV-M9-G01
over the ``planning/plan.py`` goal-contract family registry (DEV-M4-G04)
and the frozen ``core.models.AnalysisProtocolOrResult`` model, grounded
in:

* ``12-ANALYSIS-SUBSYSTEM.md`` SS3 (the Primary Analysis Protocol is
  frozen before data generation) and SS4 (Exploratory analysis must never
  overwrite or replace the Primary Analysis result; a Supervisor may later
  version the formal protocol, but past Primary Analysis remains
  preserved);
* ``07-STATISTICS-AND-ACCEPTANCE.md`` SS9 (freeze the analysis protocol
  before data generation; changes after data are observed require a
  versioned Supervisor decision and cannot retroactively erase the
  original Primary Analysis);
* ``01-PRODUCT-REQUIREMENTS.md`` SS5 step 7 (the Supervisor creates the
  primary analysis protocols) preceding step 8 (Plan v1 is frozen) -- the
  protocol drafts live in the ``protocols/`` registry of DEV-M4-G04;
* ``14-STATE-GIT-ARTIFACTS.md`` SS5 ("Analysis Protocol revision" is a
  governance checkpoint; ``frozen_commit`` records the pre-freeze ``git
  HEAD`` like ``planning/freeze.py``);
* ``05-GOAL-RUN-SCHEMA.md`` SS4/SS8 (goals reference a Primary Analysis
  Protocol; workers receive frozen analysis references).

Registry model (normative reading, locked here)
-----------------------------------------------
The DEV-M4-G04 registry keys drafts by id (``protocols/<analysis_id>.json``,
``planning.plan.register_analysis_protocol``; ``list_analysis_protocols``
globs ``*.json`` non-recursively). This module **extends** that registry
with a versioned registry of frozen/revised records in a sibling
subdirectory, ``protocols/versions/<analysis_id>@<protocol_version>.json``:

* the id-keyed draft file keeps its DEV-M4-G04 meaning (the initial
  ``v1-draft`` record, ``frozen`` False) and is never rewritten here;
* versioned records are one file per protocol version, mirroring the
  plan registry's version-keyed ``plans/<version>.json`` storage;
* the subdirectory keeps versioned files invisible to
  ``planning.plan.list_analysis_protocols`` (its ``*.json`` glob does not
  descend), so the DEV-M4-G04 plan-freeze flow keeps seeing exactly the
  drafts it expects even after protocols are frozen;
* analysis ids are validated at this module's boundary as safe single
  path segments without ``@`` and without glob metacharacters (``*``,
  ``?``, ``[``, ``]``): the versioned listing is built with
  ``glob("<analysis_id>@*.json")``, so a wildcard id would silently
  return records of *other* analyses (and registration of such an id
  would leak a platform-dependent ``OSError`` on Windows).

Freeze metadata (normative reading, locked here)
------------------------------------------------
The frozen ``AnalysisProtocolOrResult`` model declares only ``frozen``
(no ``frozen_at``/``frozen_commit``/parent fields -- ``core/models.py``,
``schemas/analysis.schema.yaml``), so the freeze stamp follows the
DEV-M4-G04 convention for models without freeze fields: the metadata is
carried by the :class:`ProtocolFreezeResult` / :class:`ProtocolVersion`
objects and persisted as extra keys of the versioned record file. The
schema explicitly allows extra properties (``additionalProperties:
true``), so versioned files stay schema-validated and canonical; the
``to_dict()``/``from_dict()`` model layer simply ignores the extra keys,
which only this module reads (``metadata_version``,
``parent_protocol_version``, ``frozen_at``, ``frozen_commit``).
``frozen_commit`` is ``None`` outside a Git repository (documented, never
fabricated) -- the ``plan.freeze``/``Analysis Protocol revision``
checkpoint commits themselves are owned by the Supervisor flow.

AC-01 -- primary protocol frozen before data analysis acceptance
-----------------------------------------------------------------
:func:`evaluate_acceptance_gate` / :func:`assert_acceptance_eligible`
model the gate: data analysis acceptance for an ``analysis_id`` is
allowed **iff** a PRIMARY analysis protocol is registered *and* frozen
(decided by the ordered, versioned ``ACCEPTANCE_GATE_RULES`` table with a
trailing total default). :func:`freeze_primary_protocol` produces the
frozen PRIMARY protocol record (formal ``protocol_version`` ``v<N>``,
``frozen`` True, freeze metadata, schema-validated, canonical, atomic)
from the registered draft; the draft is never rewritten and a second
freeze of the same formal version is rejected.

AC-02 -- exploratory analysis cannot overwrite/replace the primary result
-------------------------------------------------------------------------
The primary record (the registered PRIMARY protocol and any PRIMARY
result record) is **immutable and authoritative**: the primary-authority
rule table (``PRIMARY_AUTHORITY_RULES``, evaluated by
:func:`evaluate_primary_authority` and enforced at every record write in
:func:`register_analysis_record`) rejects any write that would clobber or
replace it (a stable ``PrimaryRecordReplaceProhibitedError``), while
EXPLORATORY records are accepted as long as they live **alongside** it
under their own analysis id -- an exploratory write can never land in the
primary record's file, and the registry is exactly-once per id.

AC-03 -- formal protocol revision is versioned
----------------------------------------------
:func:`revise_protocol` revises a registered, frozen, formal PRIMARY
protocol into the next draft version (``v<N>`` -> ``v<N+1>-draft``, per
the plan convention) with ``parent_protocol_version`` = the frozen
version; :func:`freeze_primary_protocol` then freezes the revision draft
into ``v<N+1>`` (parent link preserved). The old record stays **byte
untouched** (it is a separate file; never rewritten). The versioned
``PROTOCOL_STATUS_RULES`` table computes the effective lineage status
(``PlanStatus.DRAFT``/``FROZEN``/``SUPERSEDED``) -- ``SUPERSEDED`` is a
computed lineage status, never a stored mutation.

Determinism and boundaries
--------------------------
All checks and derived records are pure functions of the registered
state plus the injectable ``timestamp`` (naive datetimes rejected, like
``planning/init.py``); no LLM, no randomness, no wall clock. Version
semantics reuse ``planning.plan`` (``v<N>`` / ``v<N>-draft``,
``formal_version``, ``next_version``). ``TypeError`` at the public
boundaries; errors follow the ``ValueError``-subclass convention with
stable messages; ``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias

from scientific_reproduction.audit.git import NotARepositoryError, current_head
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import (
    AnalysisKind,
    AnalysisProtocolOrResult,
    PlanStatus,
    PrimaryOrExploratory,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)
from scientific_reproduction.planning.plan import (
    PROTOCOLS_STATE_DIR,
    formal_version,
    is_draft_version,
    is_formal_version,
    next_version,
)

__all__ = [
    "ACCEPTANCE_GATE_RULES",
    "ACCEPTANCE_GATE_RULESET_VERSION",
    "PROTOCOL_METADATA_VERSION",
    "PROTOCOL_STATUS_RULES",
    "PROTOCOL_STATUS_RULESET_VERSION",
    "PRIMARY_AUTHORITY_RULES",
    "PRIMARY_AUTHORITY_RULESET_VERSION",
    "VERSIONS_STATE_DIR",
    "AcceptanceGateAssessment",
    "AcceptanceGateDecision",
    "AcceptanceGateInput",
    "AcceptanceGateProhibitedError",
    "AcceptanceGateRule",
    "AnalysisProtocolError",
    "AnalysisRecordInput",
    "DuplicateProtocolVersionError",
    "InvalidProtocolIdError",
    "InvalidProtocolVersionError",
    "PrimaryAuthorityAssessment",
    "PrimaryAuthorityDecision",
    "PrimaryAuthorityInput",
    "PrimaryAuthorityRule",
    "PrimaryRecordReplaceProhibitedError",
    "ProtocolAlreadyFrozenError",
    "ProtocolFreezeResult",
    "ProtocolLineageEntry",
    "ProtocolNotDraftError",
    "ProtocolNotFoundError",
    "ProtocolNotFrozenError",
    "ProtocolNotPrimaryError",
    "ProtocolStateMismatchError",
    "ProtocolStatusAssessment",
    "ProtocolStatusDecision",
    "ProtocolStatusInput",
    "ProtocolStatusRule",
    "ProtocolVersion",
    "ProtocolVersionMetadata",
    "assert_acceptance_eligible",
    "evaluate_acceptance_gate",
    "evaluate_primary_authority",
    "evaluate_protocol_status",
    "freeze_primary_protocol",
    "list_protocol_versions",
    "protocol_lineage",
    "read_protocol_version",
    "register_analysis_record",
    "revise_protocol",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class AnalysisProtocolError(ValueError):
    """Base class for all analysis protocol registry errors."""


class ProtocolNotFoundError(AnalysisProtocolError):
    """Raised when reading an analysis protocol record that is not registered."""


class DuplicateProtocolVersionError(AnalysisProtocolError):
    """Raised when a protocol version is registered a second time (no clobbering)."""


class InvalidProtocolVersionError(AnalysisProtocolError):
    """Raised when a protocol version is not ``v<N>`` or ``v<N>-draft``."""


class InvalidProtocolIdError(AnalysisProtocolError):
    """Raised when an analysis id is not a safe single registry path segment."""


class ProtocolNotDraftError(AnalysisProtocolError):
    """Raised when the protocol to freeze is not a pre-freeze record."""


class ProtocolStateMismatchError(AnalysisProtocolError):
    """Raised when the given record is not the registered record of its id.

    Guards against stale record objects: the record must equal the
    registered record at its version (draft or frozen).
    """


class ProtocolAlreadyFrozenError(AnalysisProtocolError):
    """Raised when the formal version of a protocol is already frozen."""


class ProtocolNotFrozenError(AnalysisProtocolError):
    """Raised when revising a protocol that is not registered and frozen."""


class ProtocolNotPrimaryError(AnalysisProtocolError):
    """Raised when a PRIMARY-only operation receives a non-PRIMARY record.

    The freeze and revision flows apply to PRIMARY analysis protocols only
    (``12-ANALYSIS-SUBSYSTEM.md`` SS3-4); exploratory records are
    registered, isolated and never frozen.
    """


class PrimaryRecordReplaceProhibitedError(AnalysisProtocolError):
    """Raised when a write would overwrite or replace the primary record.

    AC-02: the primary record is immutable and authoritative; exploratory
    analysis can never overwrite or replace it.
    """


class AcceptanceGateProhibitedError(AnalysisProtocolError):
    """Raised when the acceptance gate rejects data analysis acceptance.

    AC-01: data analysis acceptance is prohibited until the PRIMARY
    analysis protocol is frozen.
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Subdirectory of ``protocols/`` holding the versioned protocol records
#: (``protocols/versions/<analysis_id>@<version>.json``). A sibling of the
#: DEV-M4-G04 id-keyed draft registry; invisible to its ``*.json`` glob.
VERSIONS_STATE_DIR: str = "versions"

#: Version of the protocol-status (supersession) rule table. Bumped
#: whenever a rule changes; recorded in every assessment.
PROTOCOL_STATUS_RULESET_VERSION: str = "1.0"

#: Version of the acceptance-gate rule table; recorded in every assessment.
ACCEPTANCE_GATE_RULESET_VERSION: str = "1.0"

#: Version of the primary-authority rule table; recorded in every assessment.
PRIMARY_AUTHORITY_RULESET_VERSION: str = "1.0"

#: Version of the persisted freeze metadata schema
#: (``metadata_version`` key of versioned record files).
PROTOCOL_METADATA_VERSION: str = "1.0"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: Version syntax: ``v<N>`` (formal) or ``v<N>-draft`` (draft).
_VERSION_RE = re.compile(r"^v(?P<number>\d+)(?P<suffix>-draft)?$")

#: A user-supplied analysis record: the typed model or a schema-shaped dict.
AnalysisRecordInput: TypeAlias = AnalysisProtocolOrResult | Mapping[str, Any]


# ---------------------------------------------------------------------------
# Protocol version semantics (v<N> / v<N>-draft, mirroring planning.plan)
# ---------------------------------------------------------------------------


def _validate_protocol_version(version: str) -> None:
    """Reject non-str and malformed version values with stable messages."""
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    if _VERSION_RE.fullmatch(version) is None:
        raise InvalidProtocolVersionError(
            f"invalid protocol version {version!r}: expected 'v<N>' or"
            " 'v<N>-draft'"
        )


def _version_sort_key(version: str) -> tuple[int, int]:
    """Sort key: version number first, draft before formal of the same number."""
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise InvalidProtocolVersionError(
            f"invalid protocol version {version!r}: expected 'v<N>' or"
            " 'v<N>-draft'"
        )
    return (int(match.group("number")), 0 if match.group("suffix") else 1)


def _is_safe_registry_id(value: str) -> bool:
    """True iff ``value`` is a safe single registry path segment."""
    return (
        value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
        and not any(char in value for char in "*?[]")
    )


def _validate_protocol_id(value: str) -> None:
    """Reject ids that would escape the registry, glob, or break versioned naming.

    Ids must be safe single path segments **without** ``@``: versioned
    records are named ``<analysis_id>@<version>.json`` and this module is
    the only writer of versioned files, so ``@`` in an id would make the
    naming ambiguous. Glob metacharacters (``*``, ``?``, ``[``, ``]``) are
    rejected as well: the versioned listing is built with
    ``glob("<analysis_id>@*.json")``, so a wildcard id would silently
    return records of *other* analyses, and registering such an id would
    leak a platform-dependent ``OSError`` on Windows instead of a clean
    error. The DEV-M4-G04 draft registry (id-keyed) is untouched; this
    module rejects such ids at its own boundary before any versioned
    write or glob.
    """
    if not _is_safe_registry_id(value) or "@" in value:
        raise InvalidProtocolIdError(
            f"invalid analysis protocol id {value!r}: ids must be non-empty"
            " single path segments (no '/', no '\\', not '.' or '..') without"
            " glob metacharacters '*', '?', '[' or ']' and without '@'"
            " (versioned records are named '<id>@<version>.json')"
        )


# ---------------------------------------------------------------------------
# Effective protocol status: the supersession rule table (AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolStatusInput:
    """The state an effective protocol status is a pure function of.

    Frozen and hashable so "same state -> same status" is directly
    testable. ``has_newer_version`` is True iff the protocol lineage holds
    a later version (a higher number, or the formal record of the same
    number when the evaluated record is a draft).
    """

    frozen: bool
    has_newer_version: bool


@dataclass(frozen=True)
class ProtocolStatusRule:
    """One entry of the ordered protocol-status (supersession) rule table."""

    rule_id: str
    description: str
    status: PlanStatus
    predicate: Callable[[ProtocolStatusInput], bool]


@dataclass(frozen=True)
class ProtocolStatusDecision:
    """Record of one rule evaluation for a given state (auditability)."""

    rule_id: str
    description: str
    status: PlanStatus
    matched: bool


#: The ordered protocol-status rule table. First match wins; order is
#: normative. Predicates are pure functions of the :class:`ProtocolStatusInput`
#: only. The stored record bytes are never rewritten: ``SUPERSEDED`` is a
#: computed lineage status, never a stored mutation (AC-03 -- revision
#: must never mutate the old record in place).
PROTOCOL_STATUS_RULES: tuple[ProtocolStatusRule, ...] = (
    ProtocolStatusRule(
        rule_id="R-PROT-D1",
        description=(
            "the stored record is not frozen: pre-freeze records stay DRAFT"
            " -- drafts are never superseded"
        ),
        status=PlanStatus.DRAFT,
        predicate=lambda i: not i.frozen,
    ),
    ProtocolStatusRule(
        rule_id="R-PROT-P1",
        description=(
            "the stored record is frozen and a newer version of the protocol"
            " lineage is registered: the formal revision supersedes the old"
            " frozen record (AC-03)"
        ),
        status=PlanStatus.SUPERSEDED,
        predicate=lambda i: i.frozen and i.has_newer_version,
    ),
    ProtocolStatusRule(
        rule_id="R-PROT-F1",
        description=(
            "the stored record is frozen and no newer version is registered"
            " (default, total)"
        ),
        status=PlanStatus.FROZEN,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class ProtocolStatusAssessment:
    """Full, auditable result of an effective-status decision.

    ``input`` is the exact state the status was computed from;
    ``decisions`` records the outcome of every rule in the table (in
    evaluation order); ``matched_rule_id`` names the deciding rule (``None``
    is impossible: the final default rule always matches);
    ``ruleset_version`` records the rule table version
    (``PROTOCOL_STATUS_RULESET_VERSION``).
    """

    input: ProtocolStatusInput
    status: PlanStatus
    decisions: tuple[ProtocolStatusDecision, ...]
    matched_rule_id: str
    ruleset_version: str = PROTOCOL_STATUS_RULESET_VERSION


def evaluate_protocol_status(frozen: bool, has_newer_version: bool) -> ProtocolStatusAssessment:
    """Decide the effective protocol status with the ordered rule table.

    Pure and deterministic: the effective status is a pure function of the
    stored ``frozen`` flag and whether a newer version of the lineage is
    registered. A non-frozen record is always ``DRAFT`` (R-PROT-D1); a
    frozen record with a newer version registered is ``SUPERSEDED``
    (R-PROT-P1); a frozen record without one stays ``FROZEN`` (R-PROT-F1,
    the total default).

    Raises:
        TypeError: ``frozen`` is not a bool, or ``has_newer_version`` is
            not a bool.
    """
    if not isinstance(frozen, bool):
        raise TypeError(f"frozen must be a bool, got {type(frozen).__name__}")
    if not isinstance(has_newer_version, bool):
        raise TypeError(
            "has_newer_version must be a bool, got"
            f" {type(has_newer_version).__name__}"
        )
    audit_input = ProtocolStatusInput(
        frozen=frozen, has_newer_version=has_newer_version
    )
    decisions: list[ProtocolStatusDecision] = []
    matched_rule_id: str | None = None
    matched_status = PlanStatus.FROZEN  # unreachable default
    for rule in PROTOCOL_STATUS_RULES:
        matched = rule.predicate(audit_input)
        decisions.append(
            ProtocolStatusDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                status=rule.status,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_status = rule.status
    # R-PROT-F1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return ProtocolStatusAssessment(
        input=audit_input,
        status=matched_status,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# AC-01: the acceptance gate (frozen primary protocol required)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceGateInput:
    """The registered state the acceptance gate is a pure function of.

    ``protocol_registered`` -- any analysis record is registered for the
    id; ``primary_protocol_registered`` -- a record of kind PROTOCOL and
    class PRIMARY is registered; ``primary_protocol_frozen`` -- such a
    record is registered with ``frozen`` True (the frozen record produced
    by :func:`freeze_primary_protocol`).
    """

    analysis_id: str
    protocol_registered: bool
    primary_protocol_registered: bool
    primary_protocol_frozen: bool


@dataclass(frozen=True)
class AcceptanceGateRule:
    """One entry of the ordered acceptance-gate rule table."""

    rule_id: str
    description: str
    allowed: bool
    predicate: Callable[[AcceptanceGateInput], bool]


@dataclass(frozen=True)
class AcceptanceGateDecision:
    """Record of one gate-rule evaluation for a given state."""

    rule_id: str
    description: str
    allowed: bool
    matched: bool


#: The ordered acceptance-gate rule table. First match wins; order is
#: normative. AC-01: data analysis acceptance is allowed only when a
#: PRIMARY analysis protocol is registered *and* frozen -- the gate is the
#: rule that freezing the primary protocol is a prerequisite for data
#: analysis acceptance (``12-ANALYSIS-SUBSYSTEM.md`` SS3).
ACCEPTANCE_GATE_RULES: tuple[AcceptanceGateRule, ...] = (
    AcceptanceGateRule(
        rule_id="R-ACC-N1",
        description=(
            "no analysis protocol is registered for this analysis id:"
            " acceptance requires a registered protocol"
        ),
        allowed=False,
        predicate=lambda i: not i.protocol_registered,
    ),
    AcceptanceGateRule(
        rule_id="R-ACC-E1",
        description=(
            "no PRIMARY analysis protocol is registered (only exploratory"
            " records): acceptance requires a PRIMARY protocol"
        ),
        allowed=False,
        predicate=lambda i: not i.primary_protocol_registered,
    ),
    AcceptanceGateRule(
        rule_id="R-ACC-U1",
        description=(
            "the PRIMARY analysis protocol is not frozen: AC-01 requires"
            " the primary protocol to be frozen before data analysis"
            " acceptance"
        ),
        allowed=False,
        predicate=lambda i: not i.primary_protocol_frozen,
    ),
    AcceptanceGateRule(
        rule_id="R-ACC-A1",
        description=(
            "a frozen PRIMARY analysis protocol is registered: data"
            " analysis acceptance is allowed (AC-01, total default)"
        ),
        allowed=True,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class AcceptanceGateAssessment:
    """Full, auditable result of an acceptance-gate decision (AC-01).

    ``input`` is the exact registered state the gate was evaluated from;
    ``decisions`` records the outcome of every rule in the table;
    ``matched_rule_id`` names the deciding rule (never ``None``: the
    trailing total default always matches); ``ruleset_version`` records
    the rule table version (``ACCEPTANCE_GATE_RULESET_VERSION``).
    """

    input: AcceptanceGateInput
    allowed: bool
    decisions: tuple[AcceptanceGateDecision, ...]
    matched_rule_id: str
    ruleset_version: str = ACCEPTANCE_GATE_RULESET_VERSION


def evaluate_acceptance_gate(
    root: str | Path, analysis_id: str
) -> AcceptanceGateAssessment:
    """Evaluate the data-analysis acceptance gate for ``analysis_id`` (AC-01).

    Pure function of the registered protocol lineage at ``root``: the gate
    allows data analysis acceptance iff a PRIMARY analysis protocol is
    registered and frozen (``ACCEPTANCE_GATE_RULES``). No record is
    written.

    Args:
        root: the initialized workspace root.
        analysis_id: the analysis protocol id the acceptance applies to.

    Returns:
        The :class:`AcceptanceGateAssessment` (``allowed`` True iff the
        primary protocol is frozen).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``analysis_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidProtocolIdError: ``analysis_id`` is not a safe id.
        InvalidProtocolVersionError: a stored record carries a malformed
            version.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(analysis_id, str):
        raise TypeError(
            f"analysis_id must be a str, got {type(analysis_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_protocol_id(analysis_id)
    lineage = list_protocol_versions(project_root, analysis_id)
    protocol_registered = bool(lineage)
    primary_protocol_registered = any(
        v.record.kind is AnalysisKind.PROTOCOL
        and v.record.primary_or_exploratory is PrimaryOrExploratory.PRIMARY
        for v in lineage
    )
    primary_protocol_frozen = any(
        v.record.kind is AnalysisKind.PROTOCOL
        and v.record.primary_or_exploratory is PrimaryOrExploratory.PRIMARY
        and v.record.frozen
        for v in lineage
    )
    audit_input = AcceptanceGateInput(
        analysis_id=analysis_id,
        protocol_registered=protocol_registered,
        primary_protocol_registered=primary_protocol_registered,
        primary_protocol_frozen=primary_protocol_frozen,
    )
    return _evaluate_gate_rules(audit_input)


def assert_acceptance_eligible(root: str | Path, analysis_id: str) -> None:
    """Raise unless data analysis acceptance is allowed for ``analysis_id``.

    The enforcement side of AC-01: raises ``AcceptanceGateProhibitedError``
    (stable message naming the deciding gate rule) when no frozen PRIMARY
    protocol is registered; returns silently when the gate allows.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``analysis_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidProtocolIdError: ``analysis_id`` is not a safe id.
        AcceptanceGateProhibitedError: the gate rejects (AC-01).
        ValueError: a stored record is corrupt.
    """
    assessment = evaluate_acceptance_gate(root, analysis_id)
    if not assessment.allowed:
        matched = next(
            d for d in assessment.decisions if d.rule_id == assessment.matched_rule_id
        )
        raise AcceptanceGateProhibitedError(
            f"data analysis acceptance for analysis {analysis_id!r} is"
            f" prohibited ({matched.rule_id}): {matched.description}"
        )


def _evaluate_gate_rules(audit_input: AcceptanceGateInput) -> AcceptanceGateAssessment:
    """Run the acceptance-gate rule table over an input (shared helper)."""
    decisions: list[AcceptanceGateDecision] = []
    matched_rule_id: str | None = None
    matched_allowed = True  # unreachable default
    for rule in ACCEPTANCE_GATE_RULES:
        matched = rule.predicate(audit_input)
        decisions.append(
            AcceptanceGateDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                allowed=rule.allowed,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_allowed = rule.allowed
    # R-ACC-A1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return AcceptanceGateAssessment(
        input=audit_input,
        allowed=matched_allowed,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# AC-02: the primary-authority rule table (no overwrite / no replace)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrimaryAuthorityInput:
    """The state a record-write authority decision is a pure function of.

    ``candidate_class`` is the ``primary_or_exploratory`` class of the
    record being written; ``primary_record_registered`` is True iff any
    PRIMARY record (protocol or result) is registered for the id;
    ``record_registered`` is True iff any record is registered for the id.
    """

    candidate_class: PrimaryOrExploratory
    primary_record_registered: bool
    record_registered: bool


@dataclass(frozen=True)
class PrimaryAuthorityRule:
    """One entry of the ordered primary-authority rule table."""

    rule_id: str
    description: str
    allowed: bool
    predicate: Callable[[PrimaryAuthorityInput], bool]


@dataclass(frozen=True)
class PrimaryAuthorityDecision:
    """Record of one primary-authority rule evaluation for a given state."""

    rule_id: str
    description: str
    allowed: bool
    matched: bool


#: The ordered primary-authority rule table. First match wins; order is
#: normative. AC-02: the primary record is immutable and authoritative --
#: an EXPLORATORY write can never land where a primary record is
#: registered (exploratory analysis lives alongside, under its own id),
#: and a PRIMARY write can never overwrite or replace the registered
#: primary record. Non-primary ids are exactly-once (no clobbering).
PRIMARY_AUTHORITY_RULES: tuple[PrimaryAuthorityRule, ...] = (
    PrimaryAuthorityRule(
        rule_id="R-AUTH-P1",
        description=(
            "exploratory analysis cannot overwrite or replace the primary"
            " record: the primary record is immutable and authoritative, and"
            " exploratory records must live alongside it under their own"
            " analysis id (AC-02)"
        ),
        allowed=False,
        predicate=lambda i: (
            i.candidate_class is PrimaryOrExploratory.EXPLORATORY
            and i.primary_record_registered
        ),
    ),
    PrimaryAuthorityRule(
        rule_id="R-AUTH-P2",
        description=(
            "the primary record is immutable and authoritative: replacing or"
            " overwriting it is prohibited (AC-02)"
        ),
        allowed=False,
        predicate=lambda i: (
            i.candidate_class is PrimaryOrExploratory.PRIMARY
            and i.primary_record_registered
        ),
    ),
    PrimaryAuthorityRule(
        rule_id="R-AUTH-D1",
        description=(
            "records are immutable and each analysis id is written exactly"
            " once (no clobbering)"
        ),
        allowed=False,
        predicate=lambda i: i.record_registered,
    ),
    PrimaryAuthorityRule(
        rule_id="R-AUTH-A1",
        description=(
            "first registration of an isolated analysis record (primary or"
            " exploratory, total default)"
        ),
        allowed=True,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class PrimaryAuthorityAssessment:
    """Full, auditable result of a record-write authority decision (AC-02).

    ``input`` is the exact state the decision was computed from;
    ``decisions`` records the outcome of every rule in the table;
    ``matched_rule_id`` names the deciding rule (never ``None``: the
    trailing total default always matches); ``ruleset_version`` records
    the rule table version (``PRIMARY_AUTHORITY_RULESET_VERSION``).
    """

    input: PrimaryAuthorityInput
    allowed: bool
    decisions: tuple[PrimaryAuthorityDecision, ...]
    matched_rule_id: str
    ruleset_version: str = PRIMARY_AUTHORITY_RULESET_VERSION


def evaluate_primary_authority(
    candidate_class: PrimaryOrExploratory,
    primary_record_registered: bool,
    record_registered: bool,
) -> PrimaryAuthorityAssessment:
    """Decide whether a record write is allowed by the primary-authority table.

    Pure and deterministic: the decision is a pure function of the
    candidate's PRIMARY/EXPLORATORY class and whether a primary record
    (and any record) is already registered for the id. Any write that
    would clobber or replace the primary record is rejected (AC-02);
    isolated first registrations are allowed (R-AUTH-A1, the total
    default).

    Raises:
        TypeError: ``candidate_class`` is not a ``PrimaryOrExploratory``,
            or a boolean argument is not a bool.
    """
    if not isinstance(candidate_class, PrimaryOrExploratory):
        raise TypeError(
            "candidate_class must be a PrimaryOrExploratory, got"
            f" {type(candidate_class).__name__}"
        )
    if not isinstance(primary_record_registered, bool):
        raise TypeError(
            "primary_record_registered must be a bool, got"
            f" {type(primary_record_registered).__name__}"
        )
    if not isinstance(record_registered, bool):
        raise TypeError(
            f"record_registered must be a bool, got {type(record_registered).__name__}"
        )
    audit_input = PrimaryAuthorityInput(
        candidate_class=candidate_class,
        primary_record_registered=primary_record_registered,
        record_registered=record_registered,
    )
    decisions: list[PrimaryAuthorityDecision] = []
    matched_rule_id: str | None = None
    matched_allowed = True  # unreachable default
    for rule in PRIMARY_AUTHORITY_RULES:
        matched = rule.predicate(audit_input)
        decisions.append(
            PrimaryAuthorityDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                allowed=rule.allowed,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_allowed = rule.allowed
    # R-AUTH-A1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return PrimaryAuthorityAssessment(
        input=audit_input,
        allowed=matched_allowed,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Versioned registry records and metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolVersionMetadata:
    """The freeze/version metadata of one protocol version.

    Mirrors the ``plan`` model's version metadata (``parent_plan_version``
    / ``frozen_at`` / ``frozen_commit``) which the frozen
    ``AnalysisProtocolOrResult`` model does not declare: the metadata is
    persisted as extra schema-permitted keys of the versioned record file
    and surfaced here. ``frozen_at`` / ``frozen_commit`` are set only on
    frozen records (``frozen_commit`` None outside a Git repository,
    documented); ``parent_protocol_version`` is the version a record was
    revised from (AC-03) -- None for the first frozen version.
    """

    parent_protocol_version: str | None = None
    frozen_at: str | None = None
    frozen_commit: str | None = None
    metadata_version: str = PROTOCOL_METADATA_VERSION


@dataclass(frozen=True)
class ProtocolVersion:
    """One version of a protocol lineage: the record and its metadata.

    ``record`` is the typed ``AnalysisProtocolOrResult`` (schema fields
    only); ``metadata`` carries the persisted freeze/version metadata.
    """

    record: AnalysisProtocolOrResult
    metadata: ProtocolVersionMetadata


@dataclass(frozen=True)
class ProtocolFreezeResult:
    """The frozen PRIMARY protocol of one freeze (AC-01/AC-02).

    ``frozen_record`` is the frozen ``AnalysisProtocolOrResult`` (formal
    ``protocol_version`` ``v<N>``, ``frozen`` True) persisted at
    ``protocols/versions/<analysis_id>@<vN>.json``; ``frozen_at`` /
    ``frozen_commit`` are the freeze stamp (M4-G04 convention);
    ``parent_protocol_version`` is the version the frozen record was
    revised from (None for the initial ``v1``).
    """

    frozen_record: AnalysisProtocolOrResult
    frozen_at: str
    frozen_commit: str | None
    parent_protocol_version: str | None


@dataclass(frozen=True)
class ProtocolLineageEntry:
    """One version of a protocol lineage with its effective status.

    ``record`` is the exact stored record; ``metadata`` its version
    metadata; ``status`` is the recomputed effective status (``SUPERSEDED``
    when a newer version exists, decided by the ``PROTOCOL_STATUS_RULES``
    table); ``assessment`` records the rule trace. The stored record bytes
    are never rewritten (AC-03).
    """

    record: AnalysisProtocolOrResult
    status: PlanStatus
    assessment: ProtocolStatusAssessment
    metadata: ProtocolVersionMetadata


# ---------------------------------------------------------------------------
# Registration (AC-02 gate at every write)
# ---------------------------------------------------------------------------


def register_analysis_record(
    root: str | Path, record: AnalysisRecordInput
) -> AnalysisProtocolOrResult:
    """Register one analysis record draft at ``protocols/<id>.json``.

    The analysis-subsystem registration entry: schema-validated
    (``validate_and_reject`` ``"analysis"``) canonical JSON via
    ``core.atomic.atomic_write``, id path-escape validated, exactly-once
    per id (no clobbering), and **gated by the primary-authority rule
    table** (AC-02): any write that would overwrite or replace a
    registered primary record is rejected with a stable
    ``PrimaryRecordReplaceProhibitedError`` before anything is written,
    while an EXPLORATORY record under a fresh id is accepted and isolated
    (its own file, never the primary's). The registry holds pre-freeze
    records only: ``frozen`` True records are rejected (freezing happens
    through :func:`freeze_primary_protocol`).

    Args:
        root: the initialized workspace root.
        record: the analysis record as a typed ``AnalysisProtocolOrResult``
            or a schema-shaped mapping (missing ``protocol_version``
            defaults to ``INITIAL_PLAN_VERSION``, missing ``frozen`` to
            False).

    Returns:
        The registered record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``record`` is neither an
            ``AnalysisProtocolOrResult`` nor a mapping.
        ValueError: the record is schema-invalid (subclass
            ``SchemaValidationError``) or a required field is missing.
        InvalidProtocolIdError: the ``analysis_id`` is not a safe single
            path segment (no path separators, not ``.``/``..``, no glob
            metacharacters ``*``/``?``/``[``/``]``, no ``@``).
        ProtocolAlreadyFrozenError: the record carries ``frozen`` True.
        PrimaryRecordReplaceProhibitedError: the write would overwrite or
            replace the registered primary record (AC-02).
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_record(record)
    analysis_id = model.analysis_id
    _validate_protocol_id(analysis_id)
    if model.frozen:
        raise ProtocolAlreadyFrozenError(
            f"analysis record {analysis_id!r} carries frozen=True; the"
            " registry holds pre-freeze records only -- freezing happens"
            " through freeze_primary_protocol(root, protocol)"
        )
    lineage = list_protocol_versions(project_root, analysis_id)
    primary_registered = any(
        v.record.primary_or_exploratory is PrimaryOrExploratory.PRIMARY
        for v in lineage
    )
    authority = evaluate_primary_authority(
        model.primary_or_exploratory,
        primary_registered,
        bool(lineage),
    )
    if not authority.allowed:
        matched = next(
            d for d in authority.decisions if d.rule_id == authority.matched_rule_id
        )
        raise PrimaryRecordReplaceProhibitedError(
            f"analysis record {analysis_id!r} rejected ({matched.rule_id}):"
            f" {matched.description}"
        )
    validate_and_reject("analysis", model.to_dict())
    state_path = project_root / PROTOCOLS_STATE_DIR / f"{analysis_id}.json"
    atomic_write(state_path, _canonical_json(model.to_dict()))
    return model


# ---------------------------------------------------------------------------
# Freeze (AC-01) and versioned revision (AC-03)
# ---------------------------------------------------------------------------


def freeze_primary_protocol(
    root: str | Path,
    protocol: AnalysisProtocolOrResult,
    *,
    timestamp: datetime | None = None,
) -> ProtocolFreezeResult:
    """Freeze the registered PRIMARY protocol draft (AC-01).

    The freeze applies to a **PRIMARY** analysis protocol only
    (``ProtocolNotPrimaryError`` otherwise) and requires the registered
    pre-freeze draft of the workspace (``protocols/<id>.json`` for
    ``v1-draft``, the versioned draft file for later drafts;
    ``ProtocolStateMismatchError`` / ``ProtocolNotFoundError`` otherwise).
    On success the frozen record -- formal ``protocol_version``
    (``"v1-draft"`` -> ``"v1"``), ``frozen`` True, freeze metadata
    (``frozen_at``, ``frozen_commit`` = pre-freeze ``git HEAD`` or None
    outside a Git repository) -- is persisted at
    ``protocols/versions/<id>@<vN>.json``: schema-validated, canonical,
    atomic. The draft file is never rewritten; a second freeze of the same
    formal version is rejected (``ProtocolAlreadyFrozenError``). The
    frozen record is the AC-01 prerequisite the acceptance gate requires.

    Args:
        root: the initialized workspace root.
        protocol: the registered PRIMARY protocol draft to freeze
            (``TypeError`` otherwise).
        timestamp: injectable freeze timestamp (defaults to now-UTC).
            Naive datetimes are rejected.

    Returns:
        The :class:`ProtocolFreezeResult` with the frozen record and the
        freeze stamp.

    Raises:
        TypeError: ``root`` is not a str/Path, ``protocol`` is not an
            ``AnalysisProtocolOrResult``, or ``timestamp`` is not a
            datetime.
        ValueError: ``timestamp`` is naive.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ProtocolNotPrimaryError: ``protocol`` is not a PRIMARY protocol
            record.
        ProtocolNotDraftError: ``protocol`` is already frozen.
        ProtocolAlreadyFrozenError: the formal version is already frozen.
        InvalidProtocolVersionError: ``protocol.protocol_version`` is not
            a draft version (``v<N>-draft``).
        ProtocolNotFoundError: no draft is registered for the id.
        ProtocolStateMismatchError: ``protocol`` is not the registered
            draft.
        ValueError: a stored registry record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(protocol, AnalysisProtocolOrResult):
        raise TypeError(
            "protocol must be an AnalysisProtocolOrResult, got"
            f" {type(protocol).__name__}"
        )
    project_root = Path(root).resolve()
    resolved_timestamp = _resolve_timestamp(timestamp, name="timestamp")
    _require_initialized(project_root)
    _validate_protocol_id(protocol.analysis_id)
    _require_primary_protocol(protocol, operation="freeze")
    if protocol.frozen:
        raise ProtocolNotDraftError(
            f"protocol freeze requires a pre-freeze record, got"
            f" frozen=True for analysis {protocol.analysis_id!r}"
        )
    if not is_draft_version(protocol.protocol_version):
        raise _freeze_expected_draft_version(protocol.protocol_version)

    draft_metadata = _read_registered_draft(project_root, protocol)
    formal = formal_version(protocol.protocol_version)
    formal_path = _versioned_path(project_root, protocol.analysis_id, formal)
    if formal_path.is_file():
        raise ProtocolAlreadyFrozenError(
            f"protocol version {formal!r} of analysis {protocol.analysis_id!r}"
            " is already frozen; a formal protocol version is written"
            " exactly once"
        )

    frozen_at = _format_iso(resolved_timestamp)
    frozen_commit = _resolve_frozen_commit(project_root)
    parent = draft_metadata.parent_protocol_version

    frozen_record = replace(
        protocol, protocol_version=formal, frozen=True
    )
    _write_versioned(
        project_root,
        frozen_record,
        ProtocolVersionMetadata(
            parent_protocol_version=parent,
            frozen_at=frozen_at,
            frozen_commit=frozen_commit,
        ),
    )
    return ProtocolFreezeResult(
        frozen_record=frozen_record,
        frozen_at=frozen_at,
        frozen_commit=frozen_commit,
        parent_protocol_version=parent,
    )


def revise_protocol(
    root: str | Path, protocol: AnalysisProtocolOrResult
) -> ProtocolVersion:
    """Revise a registered FROZEN formal PRIMARY protocol (AC-03).

    The protocol must be the **registered** frozen record of the workspace
    (``ProtocolNotFoundError`` / ``ProtocolStateMismatchError`` /
    ``ProtocolNotFrozenError`` otherwise), a PRIMARY protocol
    (``ProtocolNotPrimaryError``) carrying a formal version (``v<N>``;
    ``InvalidProtocolVersionError`` otherwise). The revision creates the
    next draft version (``v1`` -> ``v2-draft``) with
    ``parent_protocol_version`` set to the frozen version, copies the
    frozen record's content as the revision baseline, writes the new draft
    record at ``protocols/versions/<id>@<vN+1>-draft.json`` and leaves the
    old record **byte untouched** -- the old version is reported
    ``SUPERSEDED`` by :func:`protocol_lineage` (computed lineage status,
    never a stored mutation). No timestamp is taken: the revision produces
    a working DRAFT (no freeze metadata); the subsequent freeze stamps it.

    Args:
        root: the initialized workspace root.
        protocol: the registered FROZEN formal PRIMARY protocol to revise
            (``TypeError`` otherwise).

    Returns:
        The new draft :class:`ProtocolVersion` (``protocol_version``
        ``v<N+1>-draft``, ``frozen`` False, ``parent_protocol_version`` =
        the frozen version), persisted at the versioned draft file.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``protocol`` is not an
            ``AnalysisProtocolOrResult``.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ProtocolNotFoundError: no record with the protocol's version is
            registered.
        ProtocolStateMismatchError: ``protocol`` is not the registered
            record of its version.
        ProtocolNotFrozenError: the registered record is not frozen.
        ProtocolNotPrimaryError: ``protocol`` is not a PRIMARY protocol.
        InvalidProtocolVersionError: ``protocol.protocol_version`` is not
            a formal ``v<N>``.
        DuplicateProtocolVersionError: the next version is already
            registered.
        ValueError: a stored registry record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(protocol, AnalysisProtocolOrResult):
        raise TypeError(
            "protocol must be an AnalysisProtocolOrResult, got"
            f" {type(protocol).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_protocol_id(protocol.analysis_id)
    _require_primary_protocol(protocol, operation="revision")

    registered = read_protocol_version(
        project_root, protocol.analysis_id, protocol.protocol_version
    ).record
    if registered != protocol:
        raise ProtocolStateMismatchError(
            f"protocol {protocol.protocol_version!r} of {protocol.analysis_id!r}"
            " is not the registered record of the workspace; re-read it with"
            " read_protocol_version(root, analysis_id, version)"
        )
    if not registered.frozen:
        raise ProtocolNotFrozenError(
            f"revision requires a FROZEN protocol, got frozen=False for"
            f" version {protocol.protocol_version!r} of {protocol.analysis_id!r}"
        )
    if not is_formal_version(protocol.protocol_version):
        raise _revision_expected_formal_version(protocol.protocol_version)

    next_draft = f"{next_version(protocol.protocol_version)}-draft"
    if _versioned_path(project_root, protocol.analysis_id, next_draft).is_file():
        raise DuplicateProtocolVersionError(
            f"protocol version {next_draft!r} of {protocol.analysis_id!r} is"
            " already registered; protocol records are immutable and each"
            " version is written exactly once"
        )

    new_draft = replace(protocol, protocol_version=next_draft, frozen=False)
    metadata = ProtocolVersionMetadata(
        parent_protocol_version=protocol.protocol_version
    )
    _write_versioned(project_root, new_draft, metadata)
    return ProtocolVersion(record=new_draft, metadata=metadata)


# ---------------------------------------------------------------------------
# Reads and the lineage view
# ---------------------------------------------------------------------------


def read_protocol_version(
    root: str | Path, analysis_id: str, version: str
) -> ProtocolVersion:
    """Read one registered protocol version as a typed record.

    The versioned registry path (``protocols/versions/<id>@<version>.json``)
    for every version except the initial draft, which is the DEV-M4-G04
    id-keyed file (``protocols/<id>.json``, read at version ``"v1-draft"``
    only). The returned record is the exact stored record (bytes -> model);
    stored files are never rewritten by revision, so this read is stable
    across ``revise_protocol`` calls (AC-03: the old record is preserved
    untouched).

    Raises:
        TypeError: ``root`` is not a str/Path, ``analysis_id`` is not a
            str, or ``version`` is not a str.
        InvalidProtocolIdError: ``analysis_id`` is not a safe id.
        InvalidProtocolVersionError: ``version`` is not ``v<N>`` /
            ``v<N>-draft``.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ProtocolNotFoundError: no record with that version is registered.
        ValueError: the stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(analysis_id, str):
        raise TypeError(
            f"analysis_id must be a str, got {type(analysis_id).__name__}"
        )
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_protocol_id(analysis_id)
    _validate_protocol_version(version)
    versioned = _read_versioned(project_root, analysis_id, version)
    if versioned is not None:
        return versioned
    if version == "v1-draft":
        draft = _read_id_keyed(project_root, analysis_id)
        if draft is not None:
            return ProtocolVersion(
                record=draft, metadata=ProtocolVersionMetadata()
            )
    raise ProtocolNotFoundError(
        f"no analysis protocol version {version!r} of {analysis_id!r} is"
        f" registered at {project_root}"
    )


def list_protocol_versions(
    root: str | Path, analysis_id: str
) -> tuple[ProtocolVersion, ...]:
    """List every registered version of one protocol, sorted by version.

    Order: version number ascending, draft before formal of the same
    number (``"v1-draft"``, ``"v1"``, ``"v2-draft"``, ...). Includes the
    DEV-M4-G04 id-keyed draft (when registered) and all versioned records.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``analysis_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidProtocolIdError: ``analysis_id`` is not a safe id.
        InvalidProtocolVersionError: a stored record carries a malformed
            version.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(analysis_id, str):
        raise TypeError(
            f"analysis_id must be a str, got {type(analysis_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_protocol_id(analysis_id)
    records: list[ProtocolVersion] = []
    draft = _read_id_keyed(project_root, analysis_id)
    if draft is not None:
        records.append(ProtocolVersion(record=draft, metadata=ProtocolVersionMetadata()))
    versions_dir = project_root / PROTOCOLS_STATE_DIR / VERSIONS_STATE_DIR
    if versions_dir.is_dir():
        for path in versions_dir.glob(f"{analysis_id}@*.json"):
            records.append(_read_versioned_file(path))
    return tuple(
        sorted(records, key=lambda v: _version_sort_key(v.record.protocol_version))
    )


def protocol_lineage(root: str | Path, analysis_id: str) -> tuple[ProtocolLineageEntry, ...]:
    """Return every version of a protocol with its recomputed effective status.

    The supersession view of the versioned registry: a stored frozen
    record is reported ``SUPERSEDED`` iff a newer version of the lineage
    is registered (the formal revision supersedes the old, AC-03), by the
    ``PROTOCOL_STATUS_RULES`` table -- the stored record itself is never
    rewritten. Non-frozen records always report ``DRAFT`` (R-PROT-D1).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``analysis_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidProtocolIdError: ``analysis_id`` is not a safe id.
        InvalidProtocolVersionError: a stored record carries a malformed
            version.
        ValueError: a stored record is corrupt.
    """
    versions = list_protocol_versions(root, analysis_id)
    entries: list[ProtocolLineageEntry] = []
    for version in versions:
        has_newer = any(
            _version_sort_key(other.record.protocol_version)
            > _version_sort_key(version.record.protocol_version)
            for other in versions
            if other is not version
        )
        assessment = evaluate_protocol_status(
            version.record.frozen, has_newer
        )
        entries.append(
            ProtocolLineageEntry(
                record=version.record,
                status=assessment.status,
                assessment=assessment,
                metadata=version.metadata,
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _coerce_record(record: AnalysisRecordInput) -> AnalysisProtocolOrResult:
    """Return a typed record from either input form (drafts get defaults)."""
    if isinstance(record, AnalysisProtocolOrResult):
        return record
    if isinstance(record, Mapping):
        data = dict(record)
        data.setdefault("protocol_version", INITIAL_PLAN_VERSION)
        data.setdefault("frozen", False)
        return AnalysisProtocolOrResult.from_dict(data)
    raise TypeError(
        "analysis record must be an AnalysisProtocolOrResult or a mapping,"
        f" got {type(record).__name__}"
    )


def _require_primary_protocol(
    protocol: AnalysisProtocolOrResult, *, operation: str
) -> None:
    """Reject freeze/revision of anything but a PRIMARY protocol record."""
    if protocol.kind is not AnalysisKind.PROTOCOL:
        raise ProtocolNotPrimaryError(
            f"protocol {operation} applies to analysis protocol records only,"
            f" got kind {protocol.kind.value!r} for {protocol.analysis_id!r}"
        )
    if (
        protocol.primary_or_exploratory is not PrimaryOrExploratory.PRIMARY
    ):
        raise ProtocolNotPrimaryError(
            f"protocol {operation} applies to PRIMARY analysis protocols only"
            f" (12-ANALYSIS-SUBSYSTEM.md SS3-4); analysis {protocol.analysis_id!r}"
            f" is {protocol.primary_or_exploratory.value}"
        )


def _read_registered_draft(
    root: Path, protocol: AnalysisProtocolOrResult
) -> ProtocolVersionMetadata:
    """Return the registered draft's metadata, checking the record is registered.

    The ``v1-draft`` draft is the DEV-M4-G04 id-keyed file; later drafts
    are versioned files. Raises ``ProtocolNotFoundError`` when the draft is
    not registered and ``ProtocolStateMismatchError`` when it differs from
    ``protocol``.
    """
    if protocol.protocol_version == "v1-draft":
        stored = _read_id_keyed(root, protocol.analysis_id)
        if stored is None:
            raise ProtocolNotFoundError(
                f"no analysis protocol {protocol.analysis_id!r} is registered"
                f" at {root}"
            )
        if stored != protocol:
            raise ProtocolStateMismatchError(
                f"protocol {protocol.protocol_version!r} of"
                f" {protocol.analysis_id!r} is not the registered draft of the"
                " workspace; re-read it with read_analysis_protocol(root,"
                " analysis_id)"
            )
        return ProtocolVersionMetadata()
    versioned = _read_versioned(root, protocol.analysis_id, protocol.protocol_version)
    if versioned is None:
        raise ProtocolNotFoundError(
            f"no analysis protocol version {protocol.protocol_version!r} of"
            f" {protocol.analysis_id!r} is registered at {root}"
        )
    if versioned.record != protocol:
        raise ProtocolStateMismatchError(
            f"protocol {protocol.protocol_version!r} of {protocol.analysis_id!r}"
            " is not the registered draft of the workspace; re-read it with"
            " read_protocol_version(root, analysis_id, version)"
        )
    return versioned.metadata


def _write_versioned(
    root: Path, record: AnalysisProtocolOrResult, metadata: ProtocolVersionMetadata
) -> None:
    """Persist one versioned protocol record (schema-validated, canonical).

    The versioned record file carries the schema fields plus the freeze/
    version metadata as extra schema-permitted keys
    (``schemas/analysis.schema.yaml`` ``additionalProperties: true``);
    ``None`` metadata values are omitted (the ``to_dict()`` convention).
    """
    data = record.to_dict()
    data["metadata_version"] = metadata.metadata_version
    if metadata.parent_protocol_version is not None:
        data["parent_protocol_version"] = metadata.parent_protocol_version
    if metadata.frozen_at is not None:
        data["frozen_at"] = metadata.frozen_at
    if metadata.frozen_commit is not None:
        data["frozen_commit"] = metadata.frozen_commit
    validate_and_reject("analysis", data)
    state_path = _versioned_path(root, record.analysis_id, record.protocol_version)
    atomic_write(state_path, _canonical_json(data))


def _versioned_path(root: Path, analysis_id: str, version: str) -> Path:
    """The versioned registry path of one protocol version."""
    return root / PROTOCOLS_STATE_DIR / VERSIONS_STATE_DIR / f"{analysis_id}@{version}.json"


def _read_id_keyed(root: Path, analysis_id: str) -> AnalysisProtocolOrResult | None:
    """Read the DEV-M4-G04 id-keyed draft record, or None when absent.

    Corrupt records raise ``ValueError``; the stored ``protocol_version``
    is validated (the lineage sorts on it).
    """
    state_path = root / PROTOCOLS_STATE_DIR / f"{analysis_id}.json"
    if not state_path.is_file():
        return None
    raw = _read_json_object(state_path, "analysis protocol")
    record = AnalysisProtocolOrResult.from_dict(raw)
    _validate_protocol_version(record.protocol_version)
    return record


def _read_versioned(
    root: Path, analysis_id: str, version: str
) -> ProtocolVersion | None:
    """Read one versioned record, or None when the file is absent."""
    state_path = _versioned_path(root, analysis_id, version)
    if not state_path.is_file():
        return None
    return _read_versioned_file(state_path)


def _read_versioned_file(state_path: Path) -> ProtocolVersion:
    """Parse one versioned record file.

    The filename is ``<analysis_id>@<version>.json``; the version is
    validated (lineage sorting needs it) and the stored metadata keys
    (``metadata_version`` / ``parent_protocol_version`` / ``frozen_at`` /
    ``frozen_commit``) are surfaced in :class:`ProtocolVersionMetadata`
    while the model layer sees only the schema fields. An unparseable or
    malformed file raises ``ValueError``.
    """
    name = state_path.name
    if "@" not in name or not name.endswith(".json"):
        raise ValueError(
            f"corrupt analysis protocol record at {state_path}: expected"
            " '<analysis_id>@<version>.json'"
        )
    version = name.rsplit("@", 1)[1].removesuffix(".json")
    raw = _read_json_object(state_path, "analysis protocol")
    _validate_protocol_version(version)
    record = AnalysisProtocolOrResult.from_dict(raw)
    if record.protocol_version != version:
        raise ValueError(
            f"corrupt analysis protocol record at {state_path}: stored"
            f" protocol_version {record.protocol_version!r} does not match"
            f" the file version {version!r}"
        )
    metadata = ProtocolVersionMetadata(
        parent_protocol_version=raw.get("parent_protocol_version"),
        frozen_at=raw.get("frozen_at"),
        frozen_commit=raw.get("frozen_commit"),
        metadata_version=str(raw.get("metadata_version", PROTOCOL_METADATA_VERSION)),
    )
    return ProtocolVersion(record=record, metadata=metadata)


def _read_json_object(path: Path, kind: str) -> dict[str, Any]:
    """Load and type a record file, rejecting corrupt state with a stable error."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt {kind} record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt {kind} record at {path}: expected a JSON object"
        )
    return raw


def _canonical_json(data: dict[str, Any]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _resolve_timestamp(timestamp: datetime | None, *, name: str) -> datetime:
    """Return the injectable timestamp (default now-UTC); reject naive."""
    if timestamp is None:
        return datetime.now(timezone.utc)
    if not isinstance(timestamp, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(timestamp).__name__}")
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp


def _format_iso(value: datetime) -> str:
    """Format a timezone-aware datetime as git-style UTC ISO-8601 (``Z``)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_frozen_commit(project_root: Path) -> str | None:
    """Return the pre-freeze ``git HEAD``, or None outside a Git repo.

    The ``Analysis Protocol revision`` checkpoint commit itself is
    created by the Supervisor flow (``audit/git.py`` CHECKPOINTS); this
    module only records the commit the freeze is based on. Outside a Git
    repository the field is ``None`` -- documented in the record (no
    fabrication).
    """
    try:
        return current_head(project_root)
    except NotARepositoryError:
        return None


def _freeze_expected_draft_version(version: str) -> InvalidProtocolVersionError:
    return InvalidProtocolVersionError(
        f"protocol freeze expects a draft version 'v<N>-draft', got {version!r}"
    )


def _revision_expected_formal_version(version: str) -> InvalidProtocolVersionError:
    return InvalidProtocolVersionError(
        "protocol revision expects a formal frozen version 'v<N>', got"
        f" {version!r}"
    )

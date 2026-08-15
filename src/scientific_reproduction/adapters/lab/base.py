"""LabAdapter contract for experiment execution and result handoff (DEV-M7-G01).

Defines the **LabAdapter interface** deliverable: the contract a Lab
(Experiment Worker / Execution Monitor) uses to hand an experiment off
to a physical laboratory and to bring its result package back, grounded
in the frozen specs:

* ``15-ADAPTER-SPEC.md`` SS2 ("LabAdapter"): the minimum conceptual
  operations are ``dispatch(execution_package)``,
  ``status(dispatch_id)``, ``collect(dispatch_id)`` and
  ``request_missing_result(dispatch_id, manifest_requirements)``; the
  v0.1 reference implementation is filesystem/manual handoff
  (``10-EXPERIMENT-SUBSYSTEM.md`` SS2: ``lab/outgoing/<RUN_ID>/`` and
  ``lab/incoming/<RUN_ID>/``).
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS1 (execution model): the LabAdapter
  performs the **outgoing handoff**, after which the Run enters
  ``RUNNING_EXTERNAL`` and the Experiment Worker exits; the Execution
  Monitor later detects the returned Result Package and the Run enters
  ``RESULT_AVAILABLE``.
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS3/SS4: the dispatched Execution
  Package carries the project/Goal/Run IDs, the objective, the exact
  protocol steps and the required raw-data exports; the returned Result
  Package declares its returned data files in its result manifest.

Records and determinism
-----------------------
Every operation records its outcome as a frozen dataclass
(:class:`DispatchRecord`, :class:`DispatchStatus`,
:class:`CollectionResult`, :class:`MissingResultRequest`). Adapters are
pure functions of the injected paths and inputs: no randomness, no wall
clock (timestamps are caller-injected), no network. The ``dispatch_id``
is a pure function of the execution package identity
(``core.ids.generate_id``), so the same package always dispatches to
the same id. Errors follow the ``ValueError``-subclass convention with
stable messages; ``TypeError`` at the public boundaries.

Run-record linkage (ownership)
------------------------------
The adapter performs the handoff **only** and never touches the Run
record (``run.external.*``, lifecycle state) -- it has no knowledge of
the project's run store. The **dispatching orchestrator** (the
Experiment Worker of ``10-EXPERIMENT-SUBSYSTEM.md`` SS1) owns the
linkage and must perform it immediately after a successful ``dispatch``:
record the returned ``DispatchRecord.dispatch_id`` as
``run.external.dispatch_id`` (plus ``run.external.backend``) and
advance the Run to ``RUNNING_EXTERNAL`` through the real transition
machinery. The bundled helper
:func:`~scientific_reproduction.adapters.lab.linkage.link_run_to_dispatch`
performs both steps as one validated operation over an injected run
store (15-ADAPTER-SPEC.md SS2 "Run record linkage").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Mapping, Sequence, TypeAlias

from scientific_reproduction.adapters.lab.manifest import (
    LabResultManifest,
    ResultManifestAssessment,
)
from scientific_reproduction.core.models import LabExecutionPackage

__all__ = [
    "LAB_ADAPTER_CONTRACT_VERSION",
    "CollectionRejectedError",
    "CollectionResult",
    "DispatchNotFoundError",
    "DispatchRecord",
    "DispatchState",
    "DispatchStatus",
    "DuplicateDispatchError",
    "LabAdapter",
    "LabAdapterDataError",
    "LabAdapterError",
    "LabExecutionPackageInput",
    "MissingResultRequest",
    "ResultNotAvailableError",
    "is_safe_path_segment",
]

#: Version of the lab adapter contract. Bumped whenever a contract rule
#: changes; the same version always accepts the same adapter inputs and
#: yields the same handoff layout.
LAB_ADAPTER_CONTRACT_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class LabAdapterError(ValueError):
    """Base error of the lab adapter subsystem.

    Every message is stable: it names the failing operation and the
    reason, so callers and tests can rely on it.
    """


class LabAdapterDataError(LabAdapterError):
    """Raised when the adapter receives malformed data.

    Covers unsafe handoff ids, malformed requirement lists and corrupt
    files read from the handoff directory.
    """


class DuplicateDispatchError(LabAdapterError):
    """Raised when an execution package is dispatched a second time.

    A dispatch is exactly-once: re-dispatching the same package (the
    same deterministic dispatch_id) is refused loudly and the original
    handoff is never overwritten (AC-01).
    """


class DispatchNotFoundError(LabAdapterError):
    """Raised when status/collect/request refer to an unknown dispatch_id."""


class ResultNotAvailableError(LabAdapterError):
    """Raised when collect is called before a result package was returned."""


class CollectionRejectedError(LabAdapterError):
    """Raised when the returned result package fails the collection checks.

    The result package is present but is not accepted: the run/package
    reference is absent or does not match the dispatch (AC-02), or
    required result-manifest entries / declared data files / required
    returns are missing (AC-03). The exception carries the full
    :class:`ResultManifestAssessment` decision record -- exactly WHAT is
    missing -- and a returned package is never collected, guessed or
    silently matched to a Run when these checks fail.
    """

    def __init__(
        self, dispatch_id: str, run_id: str, assessment: ResultManifestAssessment
    ) -> None:
        self.dispatch_id = dispatch_id
        self.run_id = run_id
        self.assessment = assessment
        parts = [
            f"result package for dispatch {dispatch_id!r} (run {run_id!r})"
            " is rejected:"
        ]
        if assessment.missing_fields:
            parts.append(
                "missing required result manifest field(s):"
                f" {', '.join(assessment.missing_fields)}"
            )
        if not assessment.run_matched:
            parts.append(
                "the run reference"
                f" {assessment.run_id!r} does not match the run"
                f" {assessment.expected_run_id!r} of the dispatch; a returned"
                " package is never matched to a run implicitly"
            )
        if not assessment.package_matched:
            parts.append(
                "the package reference"
                f" {assessment.package_id!r} does not match the execution"
                f" package {assessment.expected_package_id!r} of the dispatch"
            )
        if assessment.missing_files:
            parts.append(
                "declared data file(s) missing:"
                f" {', '.join(assessment.missing_files)}"
            )
        if assessment.unmet_required_returns:
            parts.append(
                "required return(s) not covered by the returned files:"
                f" {', '.join(assessment.unmet_required_returns)}"
            )
        super().__init__(" ".join(parts))


# ---------------------------------------------------------------------------
# Shared vocabulary
# ---------------------------------------------------------------------------


class DispatchState(StrEnum):
    """Observable states of a dispatched Run (frozen LifecycleState values).

    After the outgoing handoff the Run is ``RUNNING_EXTERNAL``
    (10-EXPERIMENT-SUBSYSTEM.md SS1); once a Result Package is detected
    in the incoming handoff the Run is ``RESULT_AVAILABLE``. The
    filesystem reference adapter cannot observe finer-grained external
    states, so those are the only two it reports.
    """

    RUNNING_EXTERNAL = "RUNNING_EXTERNAL"
    RESULT_AVAILABLE = "RESULT_AVAILABLE"


def is_safe_path_segment(value: str) -> bool:
    """True iff ``value`` is a safe single handoff path segment.

    Handoff ids map to directory/file names under the injected handoff
    root, so path separators, ``.``/``..`` segments and glob
    metacharacters are rejected before any path is constructed
    (defense-in-depth, FND-M9-G02-01).
    """
    return (
        value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
        and not any(char in value for char in "*?[]")
    )


# ---------------------------------------------------------------------------
# Records (frozen; every operation's outcome)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchRecord:
    """The record of one outgoing execution package dispatch (AC-01).

    ``dispatch_id`` is a deterministic pure function of the execution
    package identity (``generate_id("dispatch", package_id, run_id)``);
    ``outgoing_path`` is the handoff directory the package was written
    to; ``dispatched_at`` is the caller-injected timestamp (never read
    from a wall clock by the adapter).
    """

    dispatch_id: str
    package_id: str
    project_id: str
    goal_id: str
    run_id: str
    outgoing_path: str
    dispatched_at: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "dispatch_id",
            "package_id",
            "project_id",
            "goal_id",
            "run_id",
            "outgoing_path",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(
                    f"DispatchRecord.{name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise LabAdapterDataError(
                    f"DispatchRecord.{name} must be a non-empty string, got"
                    f" {value!r}"
                )
        if self.dispatched_at is not None and (
            not isinstance(self.dispatched_at, str) or not self.dispatched_at.strip()
        ):
            raise LabAdapterDataError(
                "DispatchRecord.dispatched_at must be a non-empty string when"
                " set"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the dispatch record in canonical field order."""
        data: dict[str, Any] = {
            "dispatch_id": self.dispatch_id,
            "package_id": self.package_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "run_id": self.run_id,
            "outgoing_path": self.outgoing_path,
        }
        if self.dispatched_at is not None:
            data["dispatched_at"] = self.dispatched_at
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DispatchRecord:
        """Build a dispatch record from a plain dict (corrupt state is
        a stable ValueError -- dispatch records are adapter-written state)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "DispatchRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        required = (
            "dispatch_id",
            "package_id",
            "project_id",
            "goal_id",
            "run_id",
            "outgoing_path",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError(
                "corrupt lab dispatch record: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {name: data[name] for name in required}
        if "dispatched_at" in data:
            kwargs["dispatched_at"] = data["dispatched_at"]
        return cls(**kwargs)


@dataclass(frozen=True)
class DispatchStatus:
    """The observable status of one dispatch (status operation)."""

    dispatch_id: str
    run_id: str
    state: DispatchState

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id.strip():
            raise LabAdapterDataError(
                "DispatchStatus.dispatch_id must be a non-empty string"
            )
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise LabAdapterDataError(
                "DispatchStatus.run_id must be a non-empty string"
            )
        if not isinstance(self.state, DispatchState):
            raise TypeError(
                "DispatchStatus.state must be a DispatchState member, got"
                f" {self.state!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the status."""
        return {
            "dispatch_id": self.dispatch_id,
            "run_id": self.run_id,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class CollectionResult:
    """The outcome of one successful result package collection (AC-02).

    ``manifest`` is the typed :class:`LabResultManifest` of the returned
    Result Package -- its ``run_id`` matches the Run the dispatch
    belongs to (never guessed, never silently matched); ``collected_files``
    are the data files actually present in the incoming handoff, sorted;
    ``result_path`` is the incoming handoff directory.
    """

    dispatch_id: str
    run_id: str
    manifest: LabResultManifest
    collected_files: tuple[str, ...]
    result_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id.strip():
            raise LabAdapterDataError(
                "CollectionResult.dispatch_id must be a non-empty string"
            )
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise LabAdapterDataError(
                "CollectionResult.run_id must be a non-empty string"
            )
        if not isinstance(self.manifest, LabResultManifest):
            raise TypeError(
                "CollectionResult.manifest must be a LabResultManifest, got"
                f" {type(self.manifest).__name__}"
            )
        if not isinstance(self.collected_files, tuple):
            raise TypeError(
                "CollectionResult.collected_files must be a tuple, got"
                f" {type(self.collected_files).__name__}"
            )
        for name in self.collected_files:
            if not isinstance(name, str):
                raise TypeError(
                    "CollectionResult.collected_files entries must be str, got"
                    f" {type(name).__name__}"
                )
        if not isinstance(self.result_path, str) or not self.result_path.strip():
            raise LabAdapterDataError(
                "CollectionResult.result_path must be a non-empty string"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the collection result."""
        return {
            "dispatch_id": self.dispatch_id,
            "run_id": self.run_id,
            "manifest": self.manifest.to_dict(),
            "collected_files": list(self.collected_files),
            "result_path": self.result_path,
        }


@dataclass(frozen=True)
class MissingResultRequest:
    """The record of one missing-result request (request_missing_result).

    ``manifest_requirements`` is the canonical sorted requirement list
    the lab operator must satisfy; ``request_path`` is where the request
    was written; ``requested_at`` is the caller-injected timestamp.
    """

    dispatch_id: str
    manifest_requirements: tuple[str, ...]
    request_path: str
    requested_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id.strip():
            raise LabAdapterDataError(
                "MissingResultRequest.dispatch_id must be a non-empty string"
            )
        if not isinstance(self.manifest_requirements, tuple):
            raise TypeError(
                "MissingResultRequest.manifest_requirements must be a tuple,"
                f" got {type(self.manifest_requirements).__name__}"
            )
        for requirement in self.manifest_requirements:
            if not isinstance(requirement, str) or not requirement.strip():
                raise LabAdapterDataError(
                    "MissingResultRequest.manifest_requirements entries must"
                    " be non-empty strings"
                )
        if not isinstance(self.request_path, str) or not self.request_path.strip():
            raise LabAdapterDataError(
                "MissingResultRequest.request_path must be a non-empty string"
            )
        if self.requested_at is not None and (
            not isinstance(self.requested_at, str) or not self.requested_at.strip()
        ):
            raise LabAdapterDataError(
                "MissingResultRequest.requested_at must be a non-empty string"
                " when set"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the request in canonical field order."""
        data: dict[str, Any] = {
            "dispatch_id": self.dispatch_id,
            "manifest_requirements": list(self.manifest_requirements),
            "request_path": self.request_path,
        }
        if self.requested_at is not None:
            data["requested_at"] = self.requested_at
        return data


#: A user-supplied execution package: the typed model or a schema-shaped
#: mapping (schema-gated at dispatch time).
LabExecutionPackageInput: TypeAlias = LabExecutionPackage | Mapping[str, Any]


class LabAdapter(ABC):
    """Contract for experiment execution and result handoff through an adapter.

    Subclasses declare their identity as stable, versioned class
    constants (``adapter_id``, ``version``, ``description``). The four
    operations are the minimum conceptual vocabulary of
    ``15-ADAPTER-SPEC.md`` SS2:

    * ``dispatch`` -- perform the outgoing handoff of an Experiment
      Execution Package (AC-01);
    * ``status`` -- the observable state of a dispatch;
    * ``collect`` -- bring back the returned Result Package, associated
      with the correct Run (AC-02) and verified complete (AC-03);
    * ``request_missing_result`` -- ask the lab for the missing result
      manifest/data.

    Implementations must be deterministic and must only touch paths that
    are injected by the caller (constructor or call parameters): no
    hidden filesystem access, no wall clock, no network in the tested
    path.
    """

    adapter_id: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str] = ""

    @abstractmethod
    def dispatch(
        self,
        execution_package: LabExecutionPackageInput,
        *,
        artifacts: Mapping[str, str | bytes] | None = None,
        dispatched_at: str | None = None,
    ) -> DispatchRecord:
        """Dispatch one Experiment Execution Package to the outgoing path.

        Args:
            execution_package: the frozen ``LabExecutionPackage`` record
                or a schema-shaped mapping (validated against
                ``schemas/lab-execution-package.schema.yaml`` on the way
                out -- a malformed package is refused loudly, never
                silently written).
            artifacts: optional companion files (name -> content) written
                alongside the execution manifest into the dispatch
                directory (the package's "artifacts").
            dispatched_at: caller-injected dispatch timestamp (no wall
                clock in the adapter).

        Returns:
            The :class:`DispatchRecord` of the dispatch (AC-01). The
            adapter never touches the Run record: the caller owns the
            linkage and must record the returned ``dispatch_id`` on the
            Run (``run.external.dispatch_id``) and advance it to
            ``RUNNING_EXTERNAL`` -- perform it with
            :func:`scientific_reproduction.adapters.lab.linkage.link_run_to_dispatch`
            (15-ADAPTER-SPEC.md SS2 "Run record linkage").

        Raises:
            TypeError: ``execution_package`` is neither a
                ``LabExecutionPackage`` nor a mapping, or ``artifacts``
                is not a mapping.
            LabAdapterDataError: an id is not a safe handoff path
                segment, or ``dispatched_at`` is malformed.
            SchemaValidationError: the package fails the real
                lab-execution-package schema (nothing is written).
            DuplicateDispatchError: the package was already dispatched.
        """

    @abstractmethod
    def status(self, dispatch_id: str) -> DispatchStatus:
        """Return the observable state of one dispatch.

        Raises:
            TypeError: ``dispatch_id`` is not a str.
            DispatchNotFoundError: no dispatch with that id is recorded.
        """

    @abstractmethod
    def collect(self, dispatch_id: str) -> CollectionResult:
        """Collect the returned Result Package of one dispatch.

        The returned package is associated with the correct Run: its
        result manifest must carry a run reference that exactly matches
        the Run of the dispatch (AC-02; an absent or mismatched run
        reference is refused, never guessed). Required result-manifest
        entries, declared data files and the dispatched package's
        required returns are verified; anything missing is a rejected
        collection carrying a record of WHAT is missing (AC-03).

        Raises:
            TypeError: ``dispatch_id`` is not a str.
            DispatchNotFoundError: no dispatch with that id is recorded.
            ResultNotAvailableError: no Result Package has been returned
                yet.
            CollectionRejectedError: the returned package fails the
                collection checks (carries the assessment decision
                record).
            LabAdapterDataError: the returned result manifest is corrupt.
        """

    @abstractmethod
    def request_missing_result(
        self,
        dispatch_id: str,
        manifest_requirements: Sequence[str],
    ) -> MissingResultRequest:
        """Request the missing result manifest/data from the lab.

        Raises:
            TypeError: ``dispatch_id`` is not a str or
                ``manifest_requirements`` is not a sequence of strings.
            LabAdapterDataError: a requirement is empty or duplicated.
            DispatchNotFoundError: no dispatch with that id is recorded.
        """

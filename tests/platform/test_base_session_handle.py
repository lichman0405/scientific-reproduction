"""Worker session handle: durable session/worker identity for project
state (DEV-M10-G02 AC-02).

The platform adapter's spawn/resume must return an identity that is
durable enough for project state: the canonical ``session_ref`` is a
deterministic function of the logical worker/role context, so the value
a Run record stores in ``worker_session_ref`` is stable across process
restarts and across Monitor replacement sessions
(13-EXECUTION-MONITOR.md SS3-SS4 -- a brand-new session takes over
without chat-memory access). These tests pin:

* the identity fields are exactly the ones project state needs (session
  ref, platform backend id, worker id/name, contract role id, project /
  goal / context references);
* the handle round-trips losslessly through the state backend / event
  log serialization (dict -> rehydrate -> equality, canonical JSON
  byte-identical);
* ``session_ref`` is deterministic via ``core.ids.generate_id`` and
  fits ``Run.worker_session_ref``;
* the record is frozen, validates its own contract with
  ``TypeError``/``PlatformAdapterDataError`` at the boundaries, and
  contains no wall clock and no randomness.

The suite is pure: no file I/O, no wall clock, no randomness.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import FrozenInstanceError

import pytest

from scientific_reproduction.adapters.platform.base import (
    CommandSpec,
    PlatformAdapterDataError,
    PlatformAdapterError,
    WorkerSessionHandle,
    derive_worker_session_ref,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    CONTRACT_ROLE_IDS,
)
from scientific_reproduction.core.ids import ID_PATTERN, generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunType,
)

PLATFORM_ID = "mock_platform"
WORKER_ID = "experiment_worker"


def make_handle(
    *,
    platform_id: str = PLATFORM_ID,
    worker_id: str = WORKER_ID,
    role_id: str = "worker",
    project_id: str | None = None,
    goal_id: str | None = None,
    context_id: str | None = None,
    persistent: bool = False,
) -> WorkerSessionHandle:
    project_id = project_id or generate_id("project", "ac02-test")
    if not persistent and goal_id is None:
        goal_id = generate_id("goal", project_id, "g1")
    return WorkerSessionHandle(
        platform_id=platform_id,
        worker_id=worker_id,
        role_id=role_id,
        project_id=project_id,
        goal_id=goal_id,
        context_id=context_id,
    )


def test_ac02_handle_carries_exactly_the_identity_fields_project_state_needs():
    # The identity fields are exactly the ones project state needs:
    # canonical session ref, platform backend id, worker id/name, frozen
    # contract role id and the project/goal/context references.
    handle = make_handle()
    assert set(handle.to_dict()) == {
        "session_ref",
        "platform_id",
        "worker_id",
        "role_id",
        "project_id",
        "goal_id",
    }
    assert handle.session_ref == handle.to_dict()["session_ref"]
    assert handle.platform_id == PLATFORM_ID
    assert handle.worker_id == WORKER_ID
    assert handle.role_id in CONTRACT_ROLE_IDS
    assert handle.goal_id is not None
    assert handle.context_id is None  # omitted from dict when unset
    # With a worker-context reference the full 7-field shape appears.
    scoped = make_handle(context_id=generate_id("worker-context", "p", "g", "ctx"))
    assert set(scoped.to_dict()) == {
        "session_ref",
        "platform_id",
        "worker_id",
        "role_id",
        "project_id",
        "goal_id",
        "context_id",
    }
    # A persistent role handle carries no goal context.
    persistent = make_handle(role_id="execution_monitor", persistent=True)
    assert set(persistent.to_dict()) == {
        "session_ref",
        "platform_id",
        "worker_id",
        "role_id",
        "project_id",
    }


def test_ac02_handle_round_trips_losslessly_through_state_serialization():
    # The handle serializes canonically and rehydrates losslessly:
    # dict round-trip preserves equality, JSON is byte-identical for
    # equal records and parses back to the same plain dict.
    for handle in (
        make_handle(),
        make_handle(role_id="execution_monitor", persistent=True),
        make_handle(context_id=generate_id("worker-context", "p", "g", "ctx-2")),
    ):
        assert handle.to_dict() == handle.to_dict()
        restored = WorkerSessionHandle.from_dict(handle.to_dict())
        assert restored == handle
        assert restored.session_ref == handle.session_ref
        assert restored.to_json() == handle.to_json()
        assert json.loads(handle.to_json()) == handle.to_dict()


def test_ac02_handle_rehydrates_corrupt_stored_ref_self_correcting():
    # A stale or corrupt session_ref in persisted state self-corrects
    # on rehydration: the canonical ref is recomputed deterministically
    # from the identity fields (AC-02 durability).
    handle = make_handle()
    corrupt = handle.to_dict()
    corrupt["session_ref"] = "sr_session_" + "0" * 32
    restored = WorkerSessionHandle.from_dict(corrupt)
    assert restored == handle
    assert restored.session_ref == handle.session_ref


def test_ac02_session_ref_is_deterministic_and_fits_run_project_state():
    # The canonical ref is a pure function of the logical worker context
    # (generate_id("session", ...)) and is exactly the value a Run
    # record stores in worker_session_ref: durable enough for project
    # state.
    handle = make_handle()
    expected = derive_worker_session_ref(
        handle.platform_id,
        handle.worker_id,
        handle.role_id,
        handle.project_id,
        handle.goal_id,
        handle.context_id,
    )
    assert handle.session_ref == expected
    assert ID_PATTERN.fullmatch(handle.session_ref)
    assert is_valid_id(handle.session_ref, kind="session")
    # Repeated construction of the same logical context -> same ref.
    assert make_handle(project_id=handle.project_id, goal_id=handle.goal_id).session_ref == handle.session_ref
    # The ref fits the Run record's worker_session_ref field.
    run = Run(
        run_id=generate_id("run", handle.project_id, "r1"),
        goal_id=handle.goal_id or "",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.CREATED,
        goal_version="1.0",
        worker_session_ref=handle.session_ref,
    )
    assert run.worker_session_ref == handle.session_ref
    assert run.to_dict()["worker_session_ref"] == handle.session_ref


def test_ac02_session_ref_distinguishes_distinct_worker_contexts():
    # Different logical contexts derive different refs: distinct goals,
    # distinct runs of one goal (context_id), and persistent roles.
    project_id = generate_id("project", "ac02")
    goal_a = generate_id("goal", project_id, "a")
    goal_b = generate_id("goal", project_id, "b")
    refs = {
        make_handle(project_id=project_id, goal_id=goal_a).session_ref,
        make_handle(project_id=project_id, goal_id=goal_b).session_ref,
        make_handle(
            project_id=project_id,
            goal_id=goal_a,
            context_id=generate_id("worker-context", "ctx", "run-1"),
        ).session_ref,
        make_handle(
            project_id=project_id,
            goal_id=goal_a,
            context_id=generate_id("worker-context", "ctx", "run-2"),
        ).session_ref,
        make_handle(
            role_id="execution_monitor", project_id=project_id, persistent=True
        ).session_ref,
    }
    assert len(refs) == 5


def test_ac02_handle_is_a_frozen_validating_record():
    # House style: frozen dataclass; mutation raises FrozenInstanceError;
    # wrong types raise TypeError; invalid values raise the stable
    # ValueError-subclassed error with a one-line message.
    handle = make_handle()
    assert dataclasses.is_dataclass(handle)
    with pytest.raises(FrozenInstanceError):
        handle.session_ref = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        handle.worker_id = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        WorkerSessionHandle(
            platform_id=42,  # type: ignore[arg-type]
            worker_id=WORKER_ID,
            role_id="worker",
            project_id=generate_id("project", "p"),
        )
    with pytest.raises(PlatformAdapterDataError) as exc:
        WorkerSessionHandle(
            platform_id=PLATFORM_ID,
            worker_id=WORKER_ID,
            role_id="not_a_role",
            project_id=generate_id("project", "p"),
        )
    assert "unknown role_id" in str(exc.value)
    with pytest.raises(PlatformAdapterDataError):
        WorkerSessionHandle(
            platform_id=PLATFORM_ID,
            worker_id=WORKER_ID,
            role_id="worker",
            project_id="not-a-project-id",
        )
    with pytest.raises(PlatformAdapterDataError):
        WorkerSessionHandle(
            platform_id=PLATFORM_ID,
            worker_id=WORKER_ID,
            role_id="worker",
            project_id=generate_id("project", "p"),
            goal_id="not-a-goal-id",
        )
    with pytest.raises(PlatformAdapterDataError):
        WorkerSessionHandle(
            platform_id=PLATFORM_ID,
            worker_id=WORKER_ID,
            role_id="worker",
            project_id=generate_id("project", "p"),
            session_ref="sr_session_00000000000000000000000000000000",
        )
    assert issubclass(PlatformAdapterError, ValueError)
    assert issubclass(PlatformAdapterDataError, PlatformAdapterError)


def test_ac02_handle_from_dict_rejects_corrupt_state():
    # Corrupt state from the backend/event log surfaces as the stable
    # error, never a silent guess.
    with pytest.raises(TypeError):
        WorkerSessionHandle.from_dict("not a mapping")
    with pytest.raises(PlatformAdapterDataError) as exc:
        WorkerSessionHandle.from_dict({})
    assert "missing required field" in str(exc.value)
    with pytest.raises(PlatformAdapterDataError):
        WorkerSessionHandle.from_dict(
            {
                "platform_id": PLATFORM_ID,
                "worker_id": WORKER_ID,
                "role_id": "worker",
                "project_id": "corrupt",
            }
        )
    # CommandSpec carries the same deterministic-ref discipline.
    command = CommandSpec(
        session_ref=make_handle().session_ref, directive="report status"
    )
    assert is_valid_id(command.command_ref, kind="command")
    assert CommandSpec.from_dict(command.to_dict()) == command
    with pytest.raises(PlatformAdapterDataError):
        CommandSpec.from_dict({})


def test_ac02_handle_has_no_wall_clock_and_no_randomness_fields():
    # Determinism hygiene: the record carries no timestamp-like fields
    # and repeated serialization is byte-identical.
    handle = make_handle()
    keys = set(handle.to_dict())
    assert not any(
        key in keys for key in ("timestamp", "created_at", "updated_at")
    )
    assert handle.to_json() == handle.to_json()
    assert make_handle().to_json() == handle.to_json()
    # role_id vocabulary is the frozen contract role vocabulary.
    assert set(CONTRACT_ROLE_IDS) == {
        "supervisor",
        "research",
        "execution_monitor",
        "worker",
    }

"""Tests for the duplicate-trigger protection registry and the follow-up
worker request (DEV-M8-G05, deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac01_trigger_*`` -- AC-01: repeated scan of the same
  ``RESULT_AVAILABLE`` Run resolves to the single original trigger record
  and never spawns a duplicate semantic follow-up (counting-hook negative
  proof: one hook call total across repeated scans; durable bytes
  identical on re-scan; deterministic replay semantics). Only a Run whose
  durable state records ``RESULT_AVAILABLE`` (or later) can be triggered;
  anything else is observed and ignored -- never triggered, never
  fabricated.
* ``test_ac02_trigger_*`` -- AC-02: a **fresh registry** over the same
  state directory reconstructs the recorded trigger set from the durable
  state alone (no session state); re-scanning the same runs after the
  "restart" yields the same single trigger per run and zero duplicate
  hook calls, with byte-identical durable state. The trigger record
  carries enough for audit: run_id, triggered_at (injected clock), the
  trigger kind vocabulary, the deterministic record id and the follow-up
  receipt when the hook returns one.
* ``test_triggers_*`` -- the durable contracts: stable ``MonitoringError``
  subclasses (``TriggerContractError`` for triggering a run that cannot
  carry a follow-up, ``CorruptTriggerStateError`` for corrupt trigger
  records), ``TypeError`` at the public type boundaries, the injected
  clock, hook exceptions propagating loudly (nothing recorded, the
  decision stays re-issuable), the no-secrets discipline (walked over
  every persisted byte), deterministic replay semantics, and the
  no-adapters architectural boundary.

Determinism: every test injects a :class:`FakeClock` producing the fixed
``FIXED_STAMP`` timestamp (no wall clock), ``tmp_path`` state
directories and ``generate_id`` ids. No randomness, no network, no
sleeps anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.monitoring import MonitoringError
from scientific_reproduction.monitoring.triggers import (
    FOLLOWUP_TRIGGER_KIND,
    TRIGGER_ID_KIND,
    TRIGGER_KINDS,
    TRIGGER_RECORD_VERSION,
    TRIGGERED_STATE_DIR,
    CorruptTriggerStateError,
    ScanOutcome,
    TriggerContractError,
    TriggerError,
    TriggerRecord,
    TriggerRegistry,
)

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: Credential-shaped strings that must never appear in persisted bytes.
FORBIDDEN_SECRETS = ("password", "passphrase", "secret", "credential",
                     "token", "api_key")


class FakeClock:
    """Injectable clock: a single fixed stamp repeats forever and every
    read is recorded (mirrors the compute-adapter tests' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


class CountingFollowupHook:
    """Injected follow-up hook that counts every invocation and returns a
    deterministic receipt: the AC-01/AC-02 negative-proof seam -- repeated
    scans and restarted registries must invoke it at most once per run."""

    def __init__(self, receipt: str | None = "followup-analysis-1") -> None:
        self._receipt = receipt
        self.calls: list[Run] = []

    def __call__(self, run: Run) -> str | None:
        self.calls.append(run)
        return self._receipt


def make_run_id(index: int = 1) -> str:
    """A deterministic run id (``sr_run_<32 hex>``)."""
    return generate_id("run", f"goal-{index}", f"seq-{index}")


def make_external(
    *,
    backend: str = "slurm_ssh",
    job_id: str | None = None,
    dispatch_id: str | None = None,
    working_directory: str | None = None,
) -> RunExternal:
    """An external identity; by default a slurm-ssh-shaped one with a
    job id and a working directory."""
    return RunExternal(
        backend=backend,
        job_id=job_id,
        dispatch_id=dispatch_id,
        working_directory=working_directory,
    )


def make_run(
    index: int = 1,
    *,
    lifecycle_state: LifecycleState = LifecycleState.RUNNING_EXTERNAL,
    external: RunExternal | None = None,
) -> Run:
    """A deterministic durable Run record (``RUNNING_EXTERNAL`` by
    default, with the fixed created/updated stamps)."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return Run(
        run_id=run_id,
        goal_id=generate_id("goal", f"g{index}"),
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=lifecycle_state,
        goal_version="v1",
        external=external,
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def make_registry(
    state_dir: Path,
    *,
    followup: Callable[[Run], str | None] | None = None,
    clock: FakeClock | None = None,
    monitor_id: str | None = None,
) -> TriggerRegistry:
    """A registry over ``state_dir`` with the fixed clock and (optionally)
    an injected follow-up hook and monitor identity."""
    return TriggerRegistry(
        state_dir,
        now=clock or FakeClock(),
        monitor_id=monitor_id,
        followup=followup,
    )


def make_trigger_record(
    run_id: str,
    *,
    triggered_at: str = FIXED_STAMP,
    followup_id: str | None = None,
) -> TriggerRecord:
    """The deterministic trigger record of ``run_id`` (as the registry
    would build and persist it)."""
    return TriggerRecord(
        record_id=generate_id(TRIGGER_ID_KIND, run_id, FOLLOWUP_TRIGGER_KIND),
        run_id=run_id,
        trigger_kind=FOLLOWUP_TRIGGER_KIND,
        triggered_at=triggered_at,
        followup_id=followup_id,
    )


def tree_bytes(root: Path) -> list[tuple[str, bytes]]:
    """(relative path, bytes) of every file under ``root``, sorted."""
    if not root.is_dir():
        return []
    return sorted(
        (p.relative_to(root).as_posix(), p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    )


def trigger_dir_files(state_dir: Path) -> list[tuple[str, bytes]]:
    """The persisted trigger-record bytes of ``state_dir`` (empty when
    nothing was ever triggered)."""
    return tree_bytes(state_dir / TRIGGERED_STATE_DIR)


# ---------------------------------------------------------------------------
# AC-01: repeated scan of the same RESULT_AVAILABLE Run spawns no
# duplicate semantic follow-up
# ---------------------------------------------------------------------------


def test_ac01_trigger_repeated_scan_spawns_single_followup(tmp_path: Path) -> None:
    """AC-01: the first scan of a RESULT_AVAILABLE Run invokes the
    follow-up hook exactly once and persists the deterministic trigger
    record; every repeated scan resolves to that single original record
    (identical outcome, identical record) and never invokes the hook
    again -- one call total across repeated scans, one record on disk,
    byte-identical durable state."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    hook = CountingFollowupHook("followup-analysis-1")
    registry = make_registry(state, followup=hook)

    first = registry.scan(run)

    assert first == ScanOutcome(
        run_id=run.run_id,
        record=make_trigger_record(run.run_id, followup_id="followup-analysis-1"),
        triggered=True,
        replayed=False,
        ignored=False,
    )
    assert hook.calls == [run]  # the hook received the durable Run record
    assert registry.get(run.run_id) == first.record
    assert len(trigger_dir_files(state)) == 1

    # Repeated scans of the same run: the single original record is
    # resolved, never a duplicate follow-up.
    first_state_bytes = tree_bytes(state)
    for _ in range(3):
        again = registry.scan(run)
        assert again == ScanOutcome(
            run_id=run.run_id,
            record=first.record,
            triggered=False,
            replayed=True,
            ignored=False,
        )
    assert len(hook.calls) == 1  # zero additional follow-up calls
    assert len(trigger_dir_files(state)) == 1
    assert tree_bytes(state) == first_state_bytes  # byte-identical on re-scan


def test_ac01_trigger_later_result_states_also_trigger_exactly_once(
    tmp_path: Path,
) -> None:
    """AC-01: a run whose durable state records a result in any
    result-bearing state at or past RESULT_AVAILABLE (mirroring the
    recovery procedure's durably-completed set) triggers exactly once --
    repeated scans never spawn a second follow-up."""
    for index, state_value in enumerate(
        (
            LifecycleState.ANALYZING,
            LifecycleState.SUBMITTED_FOR_REVIEW,
            LifecycleState.CLOSED,
            LifecycleState.INVALIDATED,
        ),
        start=2,
    ):
        state_dir = tmp_path / f"state-{state_value.value}"
        run = make_run(index, lifecycle_state=state_value)
        hook = CountingFollowupHook()
        registry = make_registry(state_dir, followup=hook)
        first = registry.scan(run)
        assert first.triggered is True
        assert first.record == make_trigger_record(
            run.run_id, followup_id="followup-analysis-1"
        )
        again = registry.scan(run)
        assert again.replayed is True
        assert again.record == first.record
        assert len(hook.calls) == 1
        assert len(trigger_dir_files(state_dir)) == 1


def test_ac01_trigger_scan_ignores_every_non_result_run(tmp_path: Path) -> None:
    """AC-01 discipline: only a run whose durable state records a result
    can be triggered. Scanning any other run -- pre-external, still
    running externally, or cancelled -- is observed and ignored: the
    outcome reports the ignore, the hook is never invoked and nothing is
    ever persisted (never a fabricated trigger)."""
    for index, state_value in enumerate(
        (
            LifecycleState.CREATED,
            LifecycleState.READY,
            LifecycleState.DISPATCHED,
            LifecycleState.RUNNING_EXTERNAL,
            LifecycleState.CANCELLED,
        ),
        start=1,
    ):
        state_dir = tmp_path / f"state-{state_value.value}"
        run = make_run(index, lifecycle_state=state_value)
        hook = CountingFollowupHook()
        registry = make_registry(state_dir, followup=hook)
        for _ in range(2):  # repeated scans stay ignored
            outcome = registry.scan(run)
            assert outcome == ScanOutcome(
                run_id=run.run_id,
                record=None,
                triggered=False,
                replayed=False,
                ignored=True,
            )
        assert hook.calls == []
        assert trigger_dir_files(state_dir) == []
        assert registry.get(run.run_id) is None


# ---------------------------------------------------------------------------
# AC-02: the trigger record survives a Monitor restart
# ---------------------------------------------------------------------------


def test_ac02_trigger_fresh_registry_reconstructs_trigger_set_after_restart(
    tmp_path: Path,
) -> None:
    """AC-02: a FRESH registry over the same state directory -- no
    session state -- reconstructs the recorded trigger set from the
    durable records alone; re-scanning the same runs after the "restart"
    yields the same single trigger per run and zero duplicate hook calls
    (fresh hook instance), with byte-identical durable state."""
    state = tmp_path / "state"
    run_a = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    run_b = make_run(2, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    hook_a = CountingFollowupHook("followup-analysis-a")
    registry_a = make_registry(state, followup=hook_a)
    first_a = registry_a.scan(run_a)
    first_b = registry_a.scan(run_b)
    assert first_a.triggered is True and first_b.triggered is True
    assert len(hook_a.calls) == 2
    original_bytes = tree_bytes(state)

    # The Monitor restarts: a fresh registry over the same state
    # directory with a fresh hook instance -- no session state.
    hook_b = CountingFollowupHook("followup-analysis-b")
    registry_b = make_registry(state, followup=hook_b)

    # Reconstruction from the durable records alone.
    assert registry_b.get(run_a.run_id) == first_a.record
    assert registry_b.get(run_b.run_id) == first_b.record
    assert registry_b.list_triggered() == tuple(
        sorted((first_a.record, first_b.record), key=lambda r: r.run_id)
    )
    assert hook_b.calls == []

    # Re-scanning the same runs after the restart: the same single
    # trigger per run, zero duplicate hook calls, byte-identical state.
    rescan_a = registry_b.scan(run_a)
    rescan_b = registry_b.scan(run_b)
    assert rescan_a.replayed is True and rescan_b.replayed is True
    assert rescan_a.record == first_a.record
    assert rescan_b.record == first_b.record
    assert hook_b.calls == []
    assert len(trigger_dir_files(state)) == 2
    assert tree_bytes(state) == original_bytes


def test_ac02_trigger_restart_rescan_never_duplicates_existing_followup(
    tmp_path: Path,
) -> None:
    """AC-02: after a restart, re-scanning a run that was already
    triggered before the restart resolves the single original record
    (same deterministic record id, same receipt) and invokes the fresh
    hook zero times -- the restart never spawns a duplicate follow-up."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    original = make_registry(
        state, followup=CountingFollowupHook("followup-analysis-9")
    ).scan(run)
    assert original.triggered is True

    hook = CountingFollowupHook("followup-analysis-after-restart")
    restarted = make_registry(state, followup=hook)
    outcome = restarted.scan(run)

    assert outcome.replayed is True
    assert outcome.triggered is False
    assert outcome.record == original.record
    assert outcome.record.record_id == generate_id(
        TRIGGER_ID_KIND, run.run_id, FOLLOWUP_TRIGGER_KIND
    )
    assert outcome.record.followup_id == "followup-analysis-9"
    assert hook.calls == []
    assert len(trigger_dir_files(state)) == 1


def test_ac02_trigger_record_is_auditable_from_durable_state_alone(
    tmp_path: Path,
) -> None:
    """AC-02 audit: the persisted record carries everything needed --
    the deterministic record id, the run id, the trigger kind vocabulary,
    the injected-clock trigger stamp and the follow-up receipt -- as
    canonical sorted JSON readable by a fresh registry after a restart."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    hook = CountingFollowupHook("followup-analysis-42")
    registry = make_registry(state, followup=hook)
    outcome = registry.scan(run)
    assert outcome.triggered is True

    raw = json.loads(
        (state / TRIGGERED_STATE_DIR / f"{run.run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw == {
        "record_version": TRIGGER_RECORD_VERSION,
        "record_id": generate_id(
            TRIGGER_ID_KIND, run.run_id, FOLLOWUP_TRIGGER_KIND
        ),
        "run_id": run.run_id,
        "trigger_kind": FOLLOWUP_TRIGGER_KIND,
        "triggered_at": FIXED_STAMP,
        "followup_id": "followup-analysis-42",
    }
    assert is_valid_id(raw["record_id"], TRIGGER_ID_KIND)
    assert TRIGGER_KINDS == frozenset({FOLLOWUP_TRIGGER_KIND})
    assert registry.get(run.run_id) == make_trigger_record(
        run.run_id, followup_id="followup-analysis-42"
    )


# ---------------------------------------------------------------------------
# The trigger contracts
# ---------------------------------------------------------------------------


def test_triggers_trigger_of_non_result_run_raises_contract_error(
    tmp_path: Path,
) -> None:
    """Triggering a run that cannot carry a follow-up (its lifecycle
    state records no result) raises the stable TriggerContractError:
    nothing is persisted, the hook is never invoked -- a follow-up is
    never fabricated onto the run."""
    for index, state_value in enumerate(
        (
            LifecycleState.RUNNING_EXTERNAL,
            LifecycleState.DISPATCHED,
            LifecycleState.CANCELLED,
        ),
        start=1,
    ):
        state_dir = tmp_path / f"state-{state_value.value}"
        run = make_run(index, lifecycle_state=state_value)
        hook = CountingFollowupHook()
        registry = make_registry(state_dir, followup=hook)
        with pytest.raises(TriggerContractError):
            registry.trigger(run)
        assert hook.calls == []
        assert trigger_dir_files(state_dir) == []
        assert registry.get(run.run_id) is None


def test_triggers_hook_exception_propagates_and_decision_stays_issuable(
    tmp_path: Path,
) -> None:
    """A follow-up hook failure propagates loudly: nothing is recorded
    (the failure message never reaches persisted bytes) and the decision
    stays re-issuable -- a later scan with a working hook issues the
    single follow-up exactly once."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)

    class FailingHook:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _run: Run) -> str | None:
            self.calls += 1
            raise RuntimeError("analysis worker token expired")

    failing = FailingHook()
    registry = make_registry(state, followup=failing)
    with pytest.raises(RuntimeError, match="token expired"):
        registry.scan(run)
    assert failing.calls == 1
    assert trigger_dir_files(state) == []  # nothing recorded
    assert registry.get(run.run_id) is None

    # After the transient failure, a working hook issues the single
    # follow-up; the failure message is never persisted anywhere.
    hook = CountingFollowupHook("followup-analysis-2")
    registry = make_registry(state, followup=hook)
    outcome = registry.scan(run)
    assert outcome.triggered is True
    assert len(hook.calls) == 1
    assert outcome.record.followup_id == "followup-analysis-2"
    persisted = b"".join(
        p.read_bytes()
        for p in (state / TRIGGERED_STATE_DIR).rglob("*")
        if p.is_file()
    )
    assert b"token expired" not in persisted
    assert registry.scan(run).replayed is True


def test_triggers_hook_without_receipt_records_decision_without_receipt(
    tmp_path: Path,
) -> None:
    """A follow-up hook returning no receipt still records the durable
    at-most-once decision: the record carries no followup_id, and the
    re-scan resolves it without invoking the hook again."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    hook = CountingFollowupHook(receipt=None)
    registry = make_registry(state, followup=hook)

    first = registry.scan(run)

    assert first.triggered is True
    assert first.record == make_trigger_record(run.run_id)
    raw = json.loads(
        (state / TRIGGERED_STATE_DIR / f"{run.run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert "followup_id" not in raw
    assert registry.scan(run).replayed is True
    assert len(hook.calls) == 1


def test_triggers_default_configuration_records_decision_without_hook(
    tmp_path: Path,
) -> None:
    """With no hook injected the scan durably records the at-most-once
    decision (no receipt) and repeated scans resolve it: the default
    configuration can never invoke any adapter and never fabricates a
    follow-up."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    registry = make_registry(state)  # no hook

    first = registry.scan(run)

    assert first.triggered is True
    assert first.record == make_trigger_record(run.run_id)
    assert registry.scan(run).replayed is True
    assert registry.get(run.run_id) == first.record
    assert len(trigger_dir_files(state)) == 1


def test_triggers_invalid_hook_return_fails_loudly(tmp_path: Path) -> None:
    """A hook returning neither a str receipt nor None (or an empty
    receipt) is a hook contract violation: it fails loudly with a stable
    error and nothing is recorded -- never a fabricated success."""

    class NonStringHook:
        def __call__(self, _run: Run) -> object:
            return 42

    state = tmp_path / "state-non-str"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    registry = make_registry(state, followup=NonStringHook())
    with pytest.raises(TypeError):
        registry.scan(run)
    assert trigger_dir_files(state) == []

    class EmptyReceiptHook:
        def __call__(self, _run: Run) -> str | None:
            return ""

    state = tmp_path / "state-empty"
    registry = make_registry(state, followup=EmptyReceiptHook())
    with pytest.raises(TriggerError):
        registry.scan(run)
    assert trigger_dir_files(state) == []


def test_triggers_corrupt_record_fails_loudly(tmp_path: Path) -> None:
    """Corrupt persisted trigger state fails loudly with the stable
    CorruptTriggerStateError (a ValueError subclass) -- never silently
    skipped, never silently overwritten."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    registry = make_registry(state, followup=CountingFollowupHook())
    registry.scan(run)

    path = state / TRIGGERED_STATE_DIR / f"{run.run_id}.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptTriggerStateError):
        registry.get(run.run_id)
    with pytest.raises(CorruptTriggerStateError):
        registry.list_triggered()
    with pytest.raises(CorruptTriggerStateError):
        registry.scan(run)  # the corrupt record is never overwritten


def test_triggers_malformed_record_fails_loudly(tmp_path: Path) -> None:
    """Trigger records that are not well-formed durable records -- a JSON
    array, a missing required field, an unknown record version, a record
    naming a different run than its file -- fail loudly with the stable
    CorruptTriggerStateError."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    other_run = make_run(2, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    path = state / TRIGGERED_STATE_DIR / f"{run.run_id}.json"
    path.parent.mkdir(parents=True)

    # A JSON array instead of a JSON object.
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(CorruptTriggerStateError):
        make_registry(state).get(run.run_id)

    # Missing required fields.
    path.write_text(
        json.dumps({"record_version": TRIGGER_RECORD_VERSION}),
        encoding="utf-8",
    )
    with pytest.raises(CorruptTriggerStateError):
        make_registry(state).get(run.run_id)

    # An unknown record version.
    record = make_trigger_record(run.run_id)
    data = record.to_dict()
    data["record_version"] = "9.9"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CorruptTriggerStateError):
        make_registry(state).get(run.run_id)

    # A record whose run id disagrees with its file name.
    path.write_text(
        json.dumps(make_trigger_record(other_run.run_id).to_dict()),
        encoding="utf-8",
    )
    with pytest.raises(CorruptTriggerStateError):
        make_registry(state).get(run.run_id)


def test_triggers_scan_of_untouched_run_is_quiet(tmp_path: Path) -> None:
    """A run that was never triggered has no record: get returns None and
    the trigger set of a fresh state directory is empty, deterministically."""
    state = tmp_path / "state"
    registry = make_registry(state)
    assert registry.get(make_run_id(1)) is None
    assert registry.list_triggered() == ()
    assert trigger_dir_files(state) == []


def test_triggers_uses_injected_clock(tmp_path: Path) -> None:
    """The trigger stamp comes from the injected clock -- no wall clock
    anywhere -- and a replayed scan stamps nothing (a pure no-op)."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    clock = FakeClock(FIXED_STAMP)
    registry = make_registry(state, followup=CountingFollowupHook(), clock=clock)

    first = registry.scan(run)

    assert first.record is not None
    assert first.record.triggered_at == FIXED_STAMP
    assert clock.calls == [FIXED_STAMP]
    registry.scan(run)  # replay: no new stamp, no new write
    assert clock.calls == [FIXED_STAMP]


def test_triggers_scan_validates_the_run_contract(tmp_path: Path) -> None:
    """The scan enforces the durable-Run contract: an invalid run id in
    the Run record is refused loudly (stable TriggerError)."""
    invalid = Run(
        run_id="not-a-valid-run-id",
        goal_id=generate_id("goal", "g1"),
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.RESULT_AVAILABLE,
        goal_version="v1",
    )
    registry = make_registry(tmp_path / "state")
    with pytest.raises(TriggerError):
        registry.scan(invalid)
    with pytest.raises(TriggerError):
        registry.trigger(invalid)
    with pytest.raises(TriggerError):
        registry.get("not-a-valid-run-id")


def test_triggers_invalid_monitor_id_raises_stable_error(tmp_path: Path) -> None:
    """An injected monitor_id that is not a valid monitor id is refused
    with the stable TriggerError."""
    with pytest.raises(TriggerError):
        TriggerRegistry(tmp_path / "s", monitor_id="not-a-monitor-id")


def test_triggers_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public type boundaries."""
    with pytest.raises(TypeError):
        TriggerRegistry(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TriggerRegistry(tmp_path / "s", now="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TriggerRegistry(tmp_path / "s", followup="not callable")  # type: ignore[arg-type]
    registry = make_registry(tmp_path / "s")
    with pytest.raises(TypeError):
        registry.scan(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.scan("not a run")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.trigger(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.get(42)  # type: ignore[arg-type]


def test_triggers_error_hierarchy_is_value_error_based() -> None:
    """The trigger error hierarchy is ValueError-based with stable
    subclasses (the house paradigm for durable-state errors)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(TriggerError, MonitoringError)
    assert issubclass(TriggerContractError, TriggerError)
    assert issubclass(CorruptTriggerStateError, TriggerError)
    assert TriggerContractError is not CorruptTriggerStateError


def test_triggers_deterministic_records_for_identical_inputs(tmp_path: Path) -> None:
    """Identical injected inputs (fixed clock, deterministic ids, the
    same monitor identity, the same receipt) produce byte-identical
    durable trigger records -- canonical sorted JSON, no randomness, no
    wall clock; repeated scans never change the bytes."""
    monitor_id = generate_id("monitor", "identical")
    payloads: list[list[tuple[str, bytes]]] = []
    for variant in ("a", "b"):
        state = tmp_path / variant / "state"
        run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
        hook = CountingFollowupHook("followup-analysis-7")
        registry = make_registry(
            state, followup=hook, monitor_id=monitor_id
        )
        registry.scan(run)
        registry.scan(run)  # the steady-state re-scan
        payloads.append(trigger_dir_files(state))
    assert payloads[0] == payloads[1]


def test_triggers_persisted_state_never_carries_credentials(tmp_path: Path) -> None:
    """The no-secrets discipline: after a full scenario including a
    failing follow-up hook with a credential-shaped message, no persisted
    byte anywhere carries credential-shaped content (hook failure
    messages are never recorded)."""
    state = tmp_path / "state"
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)

    class FailingHook:
        def __call__(self, _run: Run) -> str | None:
            raise RuntimeError("analysis worker api_key authentication failed")

    with pytest.raises(RuntimeError, match="api_key"):
        make_registry(state, followup=FailingHook()).scan(run)
    make_registry(
        state, followup=CountingFollowupHook("followup-analysis-3")
    ).scan(run)
    make_registry(state).scan(run)  # default configuration, no receipt

    bytes_ = b"".join(
        p.read_bytes()
        for root in (state,)
        for p in root.rglob("*")
        if p.is_file()
    )
    lowered = bytes_.decode("utf-8", errors="replace").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered, (
            f"persisted state must never carry {forbidden!r}"
        )
    assert "authentication failed" not in lowered


def test_triggers_module_does_not_couple_to_adapters() -> None:
    """Importing the trigger registry never pulls in the adapters package
    (proven in a fresh interpreter): the follow-up hook is a plain
    documented ``Run``-shaped seam, not an adapter type."""
    code = (
        "import sys\n"
        "import scientific_reproduction.monitoring.triggers\n"
        "assert 'scientific_reproduction.adapters' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

"""Deterministic adapter-state -> monitor-vocabulary bridge
(issue #151).

The reconcile engine's completion vocabulary is
``{"RESULT_AVAILABLE"}`` -- the lab adapter's ``DispatchState`` value --
while the compute adapters report their ``JobState`` vocabulary
(``prepared`` / ``running`` / ``completed`` / ``failed`` /
``cancelled``, ``adapters/compute/local.py``). No deterministic runtime
component mapped those vocabularies to the monitor's external-state
vocabulary: the bridge was a mapping hand-written in the monitor
agent's own script, so a wrong or drifted mapping silently froze
completion detection (AC-02's exact-match discipline inverted into a
silent freeze).

This module ships the single deterministic bridge:

* :data:`ADAPTER_STATE_TO_EXTERNAL_STATE` -- the frozen mapping table
  covering **every** member of both adapter vocabularies (mirrored
  here as plain documented strings: the monitoring subsystem never
  imports the adapters package, locked by
  ``tests/monitoring/test_monitoring_surface.py``). Terminal success
  states map to the completion signal (``RESULT_AVAILABLE``); every
  other adapter state maps to a non-completion observation -- the
  mapping can never fabricate completion (the AC-02 safe default is
  unchanged).
* :func:`map_adapter_state` -- the deterministic mapping function.
* :func:`build_mapped_probe` -- wraps a raw adapter-state source (a
  callable returning ``JobState`` / ``DispatchState`` values) into a
  reconcile ``ExternalStateProbe`` returning the monitor vocabulary;
  the hand-written probe mapping is no longer needed.

The table's consistency against the real adapter vocabularies is
locked by ``tests/monitoring/test_adapter_state.py``: adding, removing
or renaming any ``JobState`` / ``DispatchState`` member without
updating the table fails that test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from scientific_reproduction.core.models import RunExternal

__all__ = [
    "ADAPTER_STATE_TO_EXTERNAL_STATE",
    "AdapterStateSource",
    "build_mapped_probe",
    "map_adapter_state",
]

# ---------------------------------------------------------------------------
# The frozen mapping table
# ---------------------------------------------------------------------------

#: The deterministic adapter-state -> monitor-vocabulary mapping table.
#:
#: Keys are the adapter-reported state strings, mirrored here as plain
#: documented constants (the monitoring subsystem never imports the
#: adapters package). The table covers **every** member of both adapter
#: vocabularies:
#:
#: * terminal success -- ``JobState.COMPLETED`` /
#:   ``DispatchState.RESULT_AVAILABLE`` -- maps to the completion
#:   signal (``RESULT_AVAILABLE``, the single member of the reconcile
#:   engine's ``COMPLETION_SIGNALS``);
#: * non-terminal -- ``JobState.PREPARED`` / ``JobState.RUNNING`` /
#:   ``DispatchState.RUNNING_EXTERNAL`` -- maps to the running
#:   observation (``RUNNING_EXTERNAL``);
#: * terminal non-success -- ``JobState.FAILED`` /
#:   ``JobState.CANCELLED`` -- keeps its own backend-specific string:
#:   observed and recorded, never treated as completion (AC-02).
#:
#: A vocabulary change in either adapter enum without a corresponding
#: table update fails the consistency test in
#: ``tests/monitoring/test_adapter_state.py`` (both vocabularies are
#: locked).
ADAPTER_STATE_TO_EXTERNAL_STATE: dict[str, str] = {
    # The compute adapters' JobState vocabulary
    # (adapters/compute/local.py).
    "prepared": "RUNNING_EXTERNAL",
    "running": "RUNNING_EXTERNAL",
    "completed": "RESULT_AVAILABLE",
    "failed": "failed",
    "cancelled": "cancelled",
    # The lab adapter's DispatchState vocabulary (adapters/lab/base.py).
    "RUNNING_EXTERNAL": "RUNNING_EXTERNAL",
    "RESULT_AVAILABLE": "RESULT_AVAILABLE",
}


# ---------------------------------------------------------------------------
# The deterministic mapping function
# ---------------------------------------------------------------------------


def map_adapter_state(adapter_state: str) -> str:
    """The deterministic adapter-state -> monitor-vocabulary mapping.

    Maps every member of the compute adapters' ``JobState`` vocabulary
    and the lab adapter's ``DispatchState`` vocabulary to the monitor's
    external-state vocabulary. Terminal success states map to the
    completion signal (``RESULT_AVAILABLE``); every other adapter
    state maps to a non-completion observation (``RUNNING_EXTERNAL``
    or the state's own backend-specific string). An unrecognized
    string is returned unchanged -- a backend-specific non-completion
    observation (AC-02: only an exact mapped value equal to the
    completion signal can ever trigger completion).

    Args:
        adapter_state: the adapter-reported state (a ``JobState`` /
            ``DispatchState`` value; both are string enums, so a
            member or its raw value works).

    Returns:
        The monitor-vocabulary external-state string.

    Raises:
        TypeError: ``adapter_state`` is not a str.
    """
    if not isinstance(adapter_state, str):
        raise TypeError(
            "adapter_state must be a str, got"
            f" {type(adapter_state).__name__}"
        )
    return ADAPTER_STATE_TO_EXTERNAL_STATE.get(adapter_state, adapter_state)


# ---------------------------------------------------------------------------
# The mapped probe factory
# ---------------------------------------------------------------------------

#: A raw adapter-state source: a callable over the external identity
#: returning the adapter-reported state string (a ``JobState`` /
#: ``DispatchState`` value -- e.g. a closure calling the adapter's
#: ``status`` operation).
AdapterStateSource: TypeAlias = Callable[[RunExternal], str]


def build_mapped_probe(raw_probe: AdapterStateSource) -> AdapterStateSource:
    """Wrap a raw adapter-state source into a reconcile external-status
    probe routed through :func:`map_adapter_state`.

    The returned probe is the deterministic bridge between the
    adapters' state vocabularies and the monitor's external-state
    vocabulary: the monitor agent no longer needs a hand-written
    mapping (issue #151). Only an exact mapped value equal to the
    completion signal can trigger completion -- the AC-02 safe default
    is unchanged, and the mapping can never fabricate completion.

    Args:
        raw_probe: the adapter-state source (e.g. a closure calling
            the adapter's ``status`` operation and returning the raw
            ``JobState`` / ``DispatchState`` value).

    Returns:
        The mapped external-status probe.

    Raises:
        TypeError: ``raw_probe`` is not callable.
    """
    if not callable(raw_probe):
        raise TypeError(
            f"raw_probe must be callable, got {type(raw_probe).__name__}"
        )

    def probe(external: RunExternal) -> str:
        return map_adapter_state(raw_probe(external))

    return probe

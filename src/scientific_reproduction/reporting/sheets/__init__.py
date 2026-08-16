"""Operator-facing execution sheets (issue #106).

Deterministic, print-ready A4 HTML sheets rendered from the real
registered state -- one **experiment execution sheet** per dispatched
lab package (the ``FilesystemLabAdapter`` outgoing handoff,
``10-EXPERIMENT-SUBSYSTEM.md`` SS2) and one **computation execution
sheet** per compute job (the durable job record of the compute
subsystem, ``15-ADAPTER-SPEC.md`` SS3 / ``11-COMPUTATION-SUBSYSTEM.md``
SS2). Both renderers share the A4 print design system of
``reporting.sheets.html`` (reuse it in any future plan renderer rather
than duplicating it) and are pure functions of the registered state:
no wall clock (an optional ``generated_at`` stamp is caller-injected),
no randomness, no network. Language is an explicit renderer input
(issue #122): the template strings come from the injected
``TemplatePack`` resolved from the ``language`` key (default ``"en"``),
never from locale detection -- the experiment sheet also gets a
first-class deterministic PDF rendering through
``reporting.sheet_pdf``.

Public API
----------
* ``build_experiment_sheet`` / ``render_experiment_sheet`` --
  experiment sheets from ``lab/outgoing/<RUN_ID>/`` + goal context.
* ``build_computation_sheet`` / ``render_computation_sheet`` --
  computation sheets from ``<state_dir>/jobs/<job_id>.json`` + the
  run/goal/acceptance/statistical-design context chain (07 SS7/SS9).
* ``SheetError`` / ``SheetNotInitializedError`` /
  ``SheetNotFoundError`` / ``SheetCorruptError`` -- the stable error
  surface (``TypeError`` at the public boundaries otherwise).
"""

from __future__ import annotations

from scientific_reproduction.reporting.sheets.computation import (
    COMPUTATION_SHEET_VERSION,
    ComputationSheet,
    build_computation_sheet,
    render_computation_sheet,
)
from scientific_reproduction.reporting.sheets.experiment import (
    EXPERIMENT_SHEET_VERSION,
    ExperimentSheet,
    SheetCorruptError,
    SheetError,
    SheetNotFoundError,
    SheetNotInitializedError,
    build_experiment_sheet,
    render_experiment_sheet,
)
from scientific_reproduction.reporting.sheets.html import SHEET_CSS

__all__ = [
    "COMPUTATION_SHEET_VERSION",
    "EXPERIMENT_SHEET_VERSION",
    "SHEET_CSS",
    "ComputationSheet",
    "ExperimentSheet",
    "SheetCorruptError",
    "SheetError",
    "SheetNotInitializedError",
    "SheetNotFoundError",
    "build_computation_sheet",
    "build_experiment_sheet",
    "render_computation_sheet",
    "render_experiment_sheet",
]

"""Computation execution sheet renderer (issue #106).

One print-ready A4 sheet per compute job, rendered from the **durable
job record** of the compute subsystem (``<state_dir>/jobs/<job_id>.json``
-- the session-independent external-job identity of
``15-ADAPTER-SPEC.md`` SS3 / ``11-COMPUTATION-SUBSYSTEM.md`` SS2, AC-02)
plus the goal context of the planning registries. The record is
re-hydrated through the **real record contracts** dispatched by its
``backend`` key: :class:`~scientific_reproduction.adapters.compute.local.JobRecord`
(``local``), :class:`~scientific_reproduction.adapters.compute.ssh.SSHJobRecord`
(``ssh``) and :class:`~scientific_reproduction.adapters.compute.slurm_ssh.SlurmJobRecord`
(``slurm_ssh``) -- the same ``from_dict`` validation the adapters
themselves use; an unknown backend is a loud ``SheetCorruptError``.

Goal context chain (07-STATISTICS-AND-ACCEPTANCE.md)
----------------------------------------------------
``job.run_id`` -> Run record (``workers.run_helpers.read_run``) ->
``goal_id`` -> GoalContract (``planning.plan.read_goal``) -> acceptance
criteria (``goal.acceptance.criteria_ref``) -> statistical design
(``acceptance.statistical_design_ref``, SS9). Every optional link that
is not registered renders "not registered" markers -- never guessed,
never silently matched; corrupt stored records raise loudly.

Sheet layout (operator-facing, print-ready)
-------------------------------------------
1. Header banner -- kind, job id, run id.
2. Identity and job state -- backend, state, lifecycle timestamps,
   scheduler/external ids, failure class, error/recovery notes.
3. Inputs -- the registered goal context inputs (``GoalContract.inputs``).
4. Command -- the verbatim argv (shell-joined, stdlib ``shlex.join``)
   and the working directory.
5. Resource requests -- the durable Slurm/SSH parameters: modules and
   environment overrides (``slurm_ssh``), scheduler state / external id;
   the ``#SBATCH`` directives live inside the verbatim command block.
6. Required outputs and artifact naming -- every declared output with
   the deterministic artifact id rule
   (``generate_id("artifact", job_id, output_name)``) and the recorded
   collected artifact ids.
7. Convergence and validation criteria -- acceptance criteria plus the
   frozen statistical design (07 SS7/SS9) with "not registered"
   markers.
8. Fixed print footer.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the given
inputs: no wall clock (an optional ``generated_at`` stamp is caller-
injected), no randomness, no network. Every collection is sorted by
stable keys, so identical state always yields byte-identical HTML and
canonical JSON. ``TypeError`` at the public boundaries; stored-record
errors are re-raised as ``SheetCorruptError`` with the same message; a
workspace without a project state raises ``SheetNotInitializedError``.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from scientific_reproduction.adapters.compute.local import (
    BACKEND_NAME,
    JOBS_STATE_DIR,
    JobRecord,
)
from scientific_reproduction.adapters.compute.slurm_ssh import (
    SLURM_BACKEND_NAME,
    SlurmJobRecord,
)
from scientific_reproduction.adapters.compute.ssh import (
    SSH_BACKEND_NAME,
    SSHJobRecord,
)
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    GoalContract,
    Project,
    Run,
    StatisticalDesign,
)
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.plan import (
    AcceptanceNotFoundError,
    GoalNotFoundError,
    StatisticalDesignNotFoundError,
    read_acceptance,
    read_goal,
    read_statistical_design,
)
from scientific_reproduction.reporting.sheets.experiment import (
    SheetCorruptError,
    SheetNotFoundError,
    SheetNotInitializedError,
)
from scientific_reproduction.reporting.sheets.html import (
    SHEET_CSS,
    html_document,
    html_escape,
    value_html,
)
from scientific_reproduction.workers.run_helpers import RunNotFoundError, read_run

__all__ = [
    "COMPUTATION_SHEET_VERSION",
    "ComputationSheet",
    "build_computation_sheet",
    "render_computation_sheet",
]

#: Version of the computation sheet serialization (``sheet_version`` key
#: of :class:`ComputationSheet`).
COMPUTATION_SHEET_VERSION: str = "1.0"

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2


# ---------------------------------------------------------------------------
# The sheet surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputationSheet:
    """One rendered computation execution sheet.

    ``record`` is the durable job record re-hydrated through the real
    record contract of its ``backend`` key; the goal-context records are
    ``None`` when not registered (the sheet then renders "not
    registered" markers instead of guessing).
    """

    job_id: str
    record: JobRecord | SSHJobRecord | SlurmJobRecord
    run: Run | None
    goal: GoalContract | None
    acceptance: AcceptanceCriteria | None
    statistical_design: StatisticalDesign | None
    html: str

    def to_html(self) -> str:
        """The full self-contained A4 HTML document of the sheet."""
        return self.html

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the sheet surface (canonical field order)."""
        data: dict[str, Any] = {
            "sheet_version": COMPUTATION_SHEET_VERSION,
            "kind": "computation-execution-sheet",
            "job_id": self.job_id,
            "run_id": self.record.run_id,
            "backend": _backend_name(self.record),
            "state": self.record.state.value,
            "command": list(self.record.command),
            "working_directory": self.record.working_directory,
            "outputs": list(self.record.outputs),
        }
        for key in (
            "external_id",
            "scheduler_state",
            "failure_class",
            "artifact_ids",
        ):
            if hasattr(self.record, key):
                data[key] = getattr(self.record, key)
        if self.goal is not None:
            data["goal_id"] = self.goal.goal_id
            data["goal_title"] = self.goal.title
            data["goal_track"] = self.goal.track.value
        if self.acceptance is not None:
            data["acceptance_id"] = self.acceptance.acceptance_id
        if self.statistical_design is not None:
            data["design_id"] = self.statistical_design.design_id
        return data

    def to_canonical_json(self) -> str:
        """Canonical JSON of :meth:`to_dict` (sorted keys, 2-space indent,
        trailing newline) -- byte-identical for identical state."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_computation_sheet(
    root: str | Path,
    job_id: str,
    *,
    state_dir: str | Path | None = None,
    generated_at: str | None = None,
) -> ComputationSheet:
    """Build the computation execution sheet of one compute job.

    The sheet is rendered from the durable job record at
    ``<state_dir>/jobs/<job_id>.json`` (``state_dir`` defaults to the
    project root; the ``JOBS_STATE_DIR`` constant is the adapter's own)
    and the registered run/goal/acceptance/statistical-design context.
    It is a pure function of that state plus ``generated_at`` (an
    optional caller-injected timestamp shown in the footer -- never read
    from a wall clock).

    Args:
        root: the project workspace root (run/goal registries live
            there).
        job_id: the job id of the compute job (``<state_dir>/jobs/
            <job_id>.json``).
        state_dir: the compute state directory holding the durable job
            records; defaults to ``root``.
        generated_at: optional caller-injected timestamp string rendered
            in the sheet footer.

    Returns:
        The rendered :class:`ComputationSheet`.

    Raises:
        TypeError: ``root`` / ``job_id`` are not ``str``/``Path`` /
            ``str``, ``state_dir`` is not ``str``/``Path``/``None``, or
            ``generated_at`` is set but not a non-empty string.
        SheetNotInitializedError: the workspace has no project state
            record.
        SheetNotFoundError: no durable job record exists for
            ``job_id``.
        SheetCorruptError: the job record file is corrupt, violates its
            record contract, or carries an unsupported ``backend`` key;
            or a stored run/goal/acceptance/design record is corrupt.
    """
    root_path, state_path, job_id = _validate_inputs(
        root, job_id, state_dir, generated_at
    )
    _read_project(root_path)
    record = _read_job_record(state_path, job_id)
    run = _read_run_optional(root_path, record.run_id)
    goal = _read_goal_optional(root_path, run)
    acceptance = _read_acceptance_optional(root_path, goal)
    design = _read_design_optional(root_path, acceptance)
    body = _render_sheet(record, goal, acceptance, design, generated_at)
    return ComputationSheet(
        job_id=job_id,
        record=record,
        run=run,
        goal=goal,
        acceptance=acceptance,
        statistical_design=design,
        html=html_document(
            f"Computation execution sheet — {job_id}", body, stylesheet=SHEET_CSS
        ),
    )


def render_computation_sheet(
    root: str | Path,
    job_id: str,
    *,
    state_dir: str | Path | None = None,
    generated_at: str | None = None,
) -> str:
    """Render the computation execution sheet as a full HTML document.

    Convenience wrapper over :func:`build_computation_sheet` returning
    the self-contained A4 HTML document (PDF-convertible through the
    browser print path).
    """
    return build_computation_sheet(
        root, job_id, state_dir=state_dir, generated_at=generated_at
    ).to_html()


# ---------------------------------------------------------------------------
# Reading the registered state (real APIs, real record contracts)
# ---------------------------------------------------------------------------


def _read_project(root: Path) -> Project:
    """Read the project state record (required context of every sheet)."""
    try:
        return read_project_state(root)
    except ProjectNotInitializedError as exc:
        raise SheetNotInitializedError(str(exc)) from exc


def _read_job_record(
    state_dir: Path, job_id: str
) -> JobRecord | SSHJobRecord | SlurmJobRecord:
    """Read and re-hydrate the durable job record of one job.

    The record is dispatched by its ``backend`` key through the real
    record contracts the compute adapters themselves use; an unknown
    backend is corrupt state.

    Raises:
        SheetNotFoundError: no durable job record exists for the job.
        SheetCorruptError: the record file is corrupt (unparseable, not
            a JSON object, violates its contract, or carries an unknown
            ``backend``).
    """
    path = state_dir / JOBS_STATE_DIR / f"{job_id}.json"
    if not path.is_file():
        raise SheetNotFoundError(
            f"no durable job record for job {job_id!r} at {path};"
            " prepare the run context first"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SheetCorruptError(f"corrupt job record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SheetCorruptError(
            f"corrupt job record at {path}: expected a JSON object"
        )
    backend = raw.get("backend")
    try:
        if backend == BACKEND_NAME:
            return JobRecord.from_dict(raw)
        if backend == SSH_BACKEND_NAME:
            return SSHJobRecord.from_dict(raw)
        if backend == SLURM_BACKEND_NAME:
            return SlurmJobRecord.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc
    raise SheetCorruptError(
        f"job record at {path} carries backend {backend!r}, which is not"
        " a supported compute backend (expected 'local', 'ssh' or"
        " 'slurm_ssh')"
    )


def _read_run_optional(root: Path, run_id: str) -> Run | None:
    """Read the run record of the job, or ``None`` when not registered.

    A run id without a registered run record renders "not registered"
    markers on the sheet -- never guessed, never silently matched. A
    corrupt stored run is a loud ``SheetCorruptError``.
    """
    try:
        return read_run(root, run_id)
    except RunNotFoundError:
        return None
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc


def _read_goal_optional(root: Path, run: Run | None) -> GoalContract | None:
    """Read the goal contract of the run, or ``None`` when not
    registered."""
    if run is None:
        return None
    try:
        return read_goal(root, run.goal_id)
    except GoalNotFoundError:
        return None
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc


def _read_acceptance_optional(
    root: Path, goal: GoalContract | None
) -> AcceptanceCriteria | None:
    """Read the acceptance criteria of the goal, or ``None`` when not
    registered (``goal.acceptance.criteria_ref``)."""
    if goal is None:
        return None
    criteria_ref = goal.acceptance.criteria_ref
    try:
        return read_acceptance(root, criteria_ref)
    except AcceptanceNotFoundError:
        return None
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc


def _read_design_optional(
    root: Path, acceptance: AcceptanceCriteria | None
) -> StatisticalDesign | None:
    """Read the frozen statistical design of the acceptance criteria, or
    ``None`` when not registered (``statistical_design_ref``, 07-SS9)."""
    if acceptance is None or acceptance.statistical_design_ref is None:
        return None
    design_ref = acceptance.statistical_design_ref
    try:
        return read_statistical_design(root, design_ref)
    except StatisticalDesignNotFoundError:
        return None
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_sheet(
    record: JobRecord | SSHJobRecord | SlurmJobRecord,
    goal: GoalContract | None,
    acceptance: AcceptanceCriteria | None,
    design: StatisticalDesign | None,
    generated_at: str | None,
) -> str:
    """Render the sheet body (deterministic: sorted, stable structure)."""
    sections = [
        _render_banner(record.job_id, record.run_id),
        _render_identity_state(record, goal),
        _render_inputs(goal),
        _render_command(record),
        _render_resource_requests(record),
        _render_outputs(record),
        _render_validation(acceptance, design),
        _render_footer(generated_at),
    ]
    return "\n".join(section for section in sections if section)


def _render_banner(job_id: str, run_id: str) -> str:
    """The header banner: kind, job id, run id."""
    return (
        '<div class="sheet-banner">'
        '<div class="sheet-kind">Operator execution sheet</div>'
        "<h1>Computation Execution Sheet</h1>"
        '<div class="banner-ids">job '
        f"{html_escape(job_id)} &middot; run {html_escape(run_id)}"
        "</div></div>"
    )


def _render_identity_state(
    record: JobRecord | SSHJobRecord | SlurmJobRecord,
    goal: GoalContract | None,
) -> str:
    """Identity and job-state table: backend, state, lifecycle
    timestamps, scheduler/external ids, failure class, errors."""
    rows: list[tuple[str, str]] = [
        ("Job", record.job_id),
        ("Run", record.run_id),
        ("Backend", _backend_name(record)),
        ("State", record.state.value),
        ("Created at", record.created_at),
    ]
    for key in (
        "submitted_at",
        "completed_at",
        "cancelled_at",
        "collected_at",
    ):
        if getattr(record, key) is not None:
            rows.append((key.replace("_", " "), str(getattr(record, key))))
    if hasattr(record, "external_id") and getattr(record, "external_id") is not None:
        rows.append(("External id", str(getattr(record, "external_id"))))
    if hasattr(record, "remote_pid") and getattr(record, "remote_pid") is not None:
        rows.append(("Remote pid", str(getattr(record, "remote_pid"))))
    if hasattr(record, "scheduler_state") and getattr(
        record, "scheduler_state"
    ) is not None:
        rows.append(("Scheduler state", str(getattr(record, "scheduler_state"))))
    if hasattr(record, "pid") and getattr(record, "pid") is not None:
        rows.append(("Pid", str(getattr(record, "pid"))))
    if getattr(record, "exit_code", None) is not None:
        rows.append(("Exit code", str(record.exit_code)))
    if hasattr(record, "failure_class") and getattr(
        record, "failure_class"
    ) is not None:
        rows.append(("Failure class", str(getattr(record, "failure_class"))))
    for key in ("error", "recovery_note"):
        value = getattr(record, key, None)
        if value is not None:
            rows.append((key.replace("_", " "), value))
    goal_row = (
        f"{goal.goal_id} — {goal.title}"
        if goal is not None
        else "not registered in the project registry"
    )
    rows.append(("Goal", goal_row))
    cells = "\n".join(
        f'<tr><td class="label">{html_escape(label)}</td>'
        f'<td class="value">{value_html(value)}</td></tr>'
        for label, value in rows
    )
    return (
        '<h2 class="sheet-section"><span class="section-index">1</span>'
        "Identity and job state</h2>\n"
        f'<table class="meta">{cells}</table>'
    )


def _render_inputs(goal: GoalContract | None) -> str:
    """Inputs of the computation: the registered goal context inputs
    (``GoalContract.inputs``); "not registered" when the goal is."""
    if goal is None:
        return (
            '<h2 class="sheet-section"><span class="section-index">2</span>'
            "Inputs</h2>\n"
            '<p><span class="missing">goal not registered — inputs not'
            " recorded</span></p>"
        )
    items = [entry for entry in goal.inputs if isinstance(entry, Mapping)]
    if not items:
        return (
            '<h2 class="sheet-section"><span class="section-index">2</span>'
            "Inputs</h2>\n"
            '<p><span class="missing">no inputs recorded</span></p>'
        )
    columns = sorted({key for item in items for key in item})
    header = "\n".join(f"<th>{html_escape(c)}</th>" for c in columns)
    body = "\n".join(
        "<tr>"
        + "".join(f"<td>{value_html(item.get(c))}</td>" for c in columns)
        + "</tr>"
        for item in items
    )
    return (
        '<h2 class="sheet-section"><span class="section-index">2</span>'
        "Inputs</h2>\n"
        f'<table class="data"><tr>{header}</tr>{body}</table>'
    )


def _render_command(record: JobRecord | SSHJobRecord | SlurmJobRecord) -> str:
    """The verbatim command (shell-joined argv via stdlib ``shlex.join``)
    and the working directory."""
    command = shlex.join(record.command)
    return (
        '<h2 class="sheet-section"><span class="section-index">3</span>'
        "Command</h2>\n"
        f'<pre class="command">{html_escape(command)}</pre>\n'
        '<div class="step-detail"><span class="step-label">Working'
        f" directory</span>: {value_html(record.working_directory)}</div>"
    )


def _render_resource_requests(
    record: JobRecord | SSHJobRecord | SlurmJobRecord,
) -> str:
    """Resource requests: the durable Slurm/SSH parameters.

    The ``slurm_ssh`` record carries the module list and environment
    overrides as first-class fields (and the ``#SBATCH`` directives live
    inside the verbatim command block above); the ``local`` and ``ssh``
    backends persist no resource parameters -- the sheet says so rather
    than guessing.
    """
    blocks: list[str] = []
    if isinstance(record, SlurmJobRecord):
        if record.modules:
            rows = "".join(
                f"<tr><td>{html_escape(module)}</td></tr>"
                for module in record.modules
            )
            blocks.append(
                '<div class="step-detail"><span class="step-label">'
                "Modules</span></div>\n"
                f'<table class="data"><tr><th>module load</th></tr>{rows}</table>'
            )
        if record.environment:
            rows = "".join(
                f"<tr><td>{html_escape(name)}</td><td>{html_escape(value)}</td></tr>"
                for name, value in record.environment
            )
            blocks.append(
                '<div class="step-detail"><span class="step-label">'
                "Environment overrides</span></div>\n"
                '<table class="data"><tr><th>variable</th><th>value</th>'
                f"</tr>{rows}</table>"
            )
        if not blocks:
            blocks.append(
                '<p><span class="missing">no modules or environment'
                " overrides recorded</span></p>"
            )
    elif isinstance(record, SSHJobRecord):
        blocks.append(
            "<p>No resource requests are persisted by the ssh backend;"
            " the remote command is executed as recorded above.</p>"
        )
    else:
        blocks.append(
            "<p>No resource requests are persisted by the local backend;"
            " the command runs on the local host as recorded above.</p>"
        )
    return (
        '<h2 class="sheet-section"><span class="section-index">4</span>'
        "Resource requests (Slurm/SSH parameters)</h2>\n" + "\n".join(blocks)
    )


def _render_outputs(record: JobRecord | SSHJobRecord | SlurmJobRecord) -> str:
    """Required outputs and artifact naming.

    Every declared output renders with the deterministic artifact id it
    will be collected under (``generate_id("artifact", job_id,
    output_name)`` -- the compute adapter's own id rule, AC-03) and the
    recorded collected artifact ids.
    """
    rows = ""
    for name in sorted(record.outputs):
        artifact_id = generate_id("artifact", record.job_id, name)
        rows += (
            f'<tr><td>{html_escape(name)}</td>'
            f"<td>{html_escape(artifact_id)}</td></tr>"
        )
    recorded = ""
    if hasattr(record, "artifact_ids") and getattr(record, "artifact_ids"):
        recorded = (
            '<div class="step-detail"><span class="step-label">Collected'
            f" artifacts</span>: {value_html(list(record.artifact_ids))}</div>"
        )
    elif hasattr(record, "artifact_ids"):
        recorded = (
            '<div class="step-detail"><span class="step-label">Collected'
            " artifacts</span>: <span class=\"missing\">not collected"
            " yet</span></div>"
        )
    return (
        '<h2 class="sheet-section"><span class="section-index">5</span>'
        "Required outputs and artifact naming</h2>\n"
        '<div class="step-detail"><span class="step-label">Artifact id'
        " rule</span>: deterministic"
        " <span class=\"step-id\">generate_id(\"artifact\", job_id,"
        " output_name)</span> — the compute adapter's own naming"
        " (15-ADAPTER-SPEC.md SS3, AC-03)</div>\n"
        '<table class="data"><tr><th>output</th><th>artifact id</th>'
        f"</tr>{rows}</table>"
        f"{recorded}"
    )


def _render_validation(
    acceptance: AcceptanceCriteria | None, design: StatisticalDesign | None
) -> str:
    """Convergence and validation criteria (07 SS7/SS9).

    Renders the acceptance criteria (decision mode + per-metric
    criteria) and the frozen statistical design when registered;
    every absent link renders a "not registered" marker -- never
    guessed, never silently matched.
    """
    blocks: list[str] = []
    if acceptance is None:
        blocks.append(
            '<div class="step-detail"><span class="step-label">Acceptance'
            " criteria</span>: <span class=\"missing\">not registered"
            "</span></div>"
        )
    else:
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{value_html(entry.get(column))}</td>"
                for column in sorted(entry)
            )
            + "</tr>"
            for entry in acceptance.criteria
            if isinstance(entry, Mapping)
        )
        columns = sorted(
            {
                column
                for entry in acceptance.criteria
                if isinstance(entry, Mapping)
                for column in entry
            }
        )
        header = "".join(f"<th>{html_escape(c)}</th>" for c in columns)
        blocks.append(
            '<div class="step-detail"><span class="step-label">Acceptance'
            f" criteria</span>: {html_escape(acceptance.acceptance_id)}"
            f" — decision mode {html_escape(acceptance.decision_mode.value)}"
            f" (frozen: {html_escape('yes' if acceptance.frozen else 'no')})"
            "</div>\n"
            f'<table class="data"><tr>{header}</tr>{rows}</table>'
        )
    if design is None:
        blocks.append(
            '<div class="step-detail"><span class="step-label">Statistical'
            " design</span>: <span class=\"missing\">not registered"
            " (07-SS9)</span></div>"
        )
    else:
        design_rows: list[tuple[str, str]] = [
            ("Design", design.design_id),
            ("Primary method", design.primary_method),
            ("Metrics", "; ".join(design.metrics)),
            ("Margin", value_html(design.margin)),
            (
                "Margin basis",
                design.margin_basis.value
                if design.margin_basis
                else "not recorded",
            ),
            (
                "Alpha",
                str(design.alpha) if design.alpha is not None else "not recorded",
            ),
            (
                "Confidence level",
                str(design.confidence_level)
                if design.confidence_level is not None
                else "not recorded",
            ),
        ]
        if design.preprocessing_exclusion_rules:
            design_rows.append(
                (
                    "Preprocessing/exclusion rules",
                    "; ".join(design.preprocessing_exclusion_rules),
                )
            )
        if design.outlier_rules:
            design_rows.append(("Outlier rules", "; ".join(design.outlier_rules)))
        if design.failed_run_handling:
            design_rows.append(("Failed-run handling", design.failed_run_handling))
        cells = "\n".join(
            f'<tr><td class="label">{html_escape(label)}</td>'
            f'<td class="value">{value}</td></tr>'
            for label, value in design_rows
        )
        blocks.append(
            '<div class="step-detail"><span class="step-label">Statistical'
            " design (07-SS9)</span></div>\n"
            f'<table class="meta">{cells}</table>'
        )
    return (
        '<h2 class="sheet-section"><span class="section-index">6</span>'
        "Convergence and validation criteria</h2>\n" + "\n".join(blocks)
    )


def _render_footer(generated_at: str | None) -> str:
    """The fixed print footer (repeats on every printed page)."""
    stamp = generated_at if generated_at is not None else "deterministic render"
    return (
        f'<div class="footer">scientific-reproduction &middot;'
        f" computation execution sheet v{COMPUTATION_SHEET_VERSION}"
        f" &middot; {html_escape(stamp)}</div>"
    )


def _backend_name(record: JobRecord | SSHJobRecord | SlurmJobRecord) -> str:
    """The durable ``backend`` key of the record.

    The record models do not store their backend key as a field -- it is
    the dispatch key of ``from_dict`` (the adapter constant of the
    record's own module).
    """
    if isinstance(record, SlurmJobRecord):
        return SLURM_BACKEND_NAME
    if isinstance(record, SSHJobRecord):
        return SSH_BACKEND_NAME
    return BACKEND_NAME


def _validate_inputs(
    root: str | Path,
    job_id: str,
    state_dir: str | Path | None,
    generated_at: str | None,
) -> tuple[Path, Path, str]:
    """Validate the public-boundary types (TypeError with stable
    messages), returning the normalized inputs."""
    if not isinstance(root, (str, Path)):
        raise TypeError(
            f"root must be a str or Path, got {type(root).__name__}"
        )
    if not isinstance(job_id, str) or not job_id.strip():
        raise TypeError(
            f"job_id must be a non-empty string, got {job_id!r}"
        )
    if state_dir is not None and not isinstance(state_dir, (str, Path)):
        raise TypeError(
            f"state_dir must be a str, Path or None, got"
            f" {type(state_dir).__name__}"
        )
    if generated_at is not None and (
        not isinstance(generated_at, str) or not generated_at.strip()
    ):
        raise TypeError(
            "generated_at must be a non-empty string when set"
        )
    return Path(root), Path(state_dir) if state_dir is not None else Path(root), job_id

"""AC-01 tests: Experiment Execution Package dispatch (DEV-M7-G01).

Every dispatch test runs against an injected ``tmp_path`` handoff root;
no test touches any path outside it, and the tests prove that dispatch

* writes the package (execution manifest + optional artifacts + the
  dispatch record) into the configured outgoing path,
* validates the package through the **real** schema gate on the way out
  (a malformed package is refused loudly and nothing is written),
* is exactly-once (a second dispatch of the same package is refused and
  the original handoff is never overwritten),
* refuses unsafe Run ids and artifact names before any write,
* is deterministic (identical inputs -> identical outputs),
* records the dispatch (returns the frozen DispatchRecord), and
* with the workspace root injected, renders the human-readable
  experiment sheet (PDF + SHA-256 sidecar + HTML) into the dispatch
  directory itself (issue #157) as a deterministic pure step.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.adapters.lab.base import (
    DispatchRecord,
    DuplicateDispatchError,
    LabAdapterDataError,
)
from scientific_reproduction.adapters.lab.filesystem import (
    DISPATCH_RECORD_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    FilesystemLabAdapter,
)
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    LabExecutionPackage,
)
from scientific_reproduction.core.schema_validation import SchemaValidationError
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.plan import register_goal
from scientific_reproduction.reporting.sheet_pdf import (
    HTML_FILENAME_TPL,
    JSON_FILENAME_TPL,
    PDF_FILENAME_TPL,
)
from scientific_reproduction.reporting.sheets import SheetNotInitializedError
from tests.adapters.lab.lab_helpers import (
    GOAL_ID,
    PACKAGE_ID,
    RUN_ID,
    make_package,
    read_json,
)


def test_dispatch_ac01_writes_package_to_outgoing_path(handoff):
    # AC-01: the Experiment Execution Package is dispatched to the
    # configured outgoing path (base/outgoing/<RUN_ID>/ per
    # 10-EXPERIMENT-SUBSYSTEM.md SS2).
    base, adapter = handoff
    record = adapter.dispatch(make_package())
    outgoing_dir = base / "outgoing" / RUN_ID
    manifest_path = outgoing_dir / EXECUTION_MANIFEST_FILENAME
    assert manifest_path.is_file()
    stored = read_json(manifest_path)
    assert stored == make_package()
    assert (outgoing_dir / DISPATCH_RECORD_FILENAME).is_file()
    assert record.outgoing_path == str(outgoing_dir)
    assert record.run_id == RUN_ID
    assert record.package_id == PACKAGE_ID


def test_dispatch_ac01_writes_artifacts_alongside_manifest(handoff):
    # AC-01: the package is written as manifest + artifacts into the
    # outgoing directory.
    base, adapter = handoff
    artifacts = {"sop.pdf": b"%PDF-1.4 fake", "note.txt": "wear gloves"}
    record = adapter.dispatch(make_package(), artifacts=artifacts)
    outgoing_dir = base / "outgoing" / RUN_ID
    assert (outgoing_dir / "sop.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert (outgoing_dir / "note.txt").read_text(encoding="utf-8") == "wear gloves"
    assert record.dispatch_id


def test_dispatch_ac01_returns_dispatch_record(handoff):
    # AC-01: the dispatch is recorded -- the returned DispatchRecord names
    # the deterministic dispatch_id, the package identity and the
    # outgoing path; no timestamp is fabricated.
    base, adapter = handoff
    record = adapter.dispatch(make_package())
    assert isinstance(record, DispatchRecord)
    assert record.dispatch_id == generate_id("dispatch", PACKAGE_ID, RUN_ID)
    assert record.project_id == "sr_project_reference_flow_0001"
    assert record.goal_id == "sr_goal_reference_flow_0001"
    assert record.dispatched_at is None
    stored = read_json(base / "outgoing" / RUN_ID / DISPATCH_RECORD_FILENAME)
    assert stored["dispatch_id"] == record.dispatch_id
    assert stored["run_id"] == RUN_ID


def test_dispatch_ac01_injected_timestamp_recorded(handoff):
    # The timestamp is caller-injected: the adapter never reads a wall
    # clock.
    base, adapter = handoff
    record = adapter.dispatch(make_package(), dispatched_at="2026-08-14T00:00:00Z")
    assert record.dispatched_at == "2026-08-14T00:00:00Z"
    stored = read_json(base / "outgoing" / RUN_ID / DISPATCH_RECORD_FILENAME)
    assert stored["dispatched_at"] == "2026-08-14T00:00:00Z"


def test_dispatch_ac01_malformed_package_refused_loudly_nothing_written(handoff):
    # AC-01 + constraint 12: a malformed package fails the REAL schema
    # gate (validate_and_reject) and nothing is written -- not even the
    # handoff directory.
    base, adapter = handoff
    malformed = make_package()
    del malformed["run_id"]
    with pytest.raises(SchemaValidationError) as exc:
        adapter.dispatch(malformed)
    assert "lab-execution-package" in str(exc.value)
    assert any("run_id" in error for error in exc.value.errors)
    assert not (base / "outgoing" / RUN_ID).exists()


def test_dispatch_ac01_schema_gate_rejects_every_required_field(tmp_path):
    # The real schema gate covers all required schema fields, not a
    # hand-rolled subset.
    adapter = FilesystemLabAdapter(tmp_path / "lab")
    for field in ("package_id", "project_id", "goal_id", "run_id", "objective",
                  "procedure", "required_return"):
        malformed = make_package()
        del malformed[field]
        with pytest.raises(SchemaValidationError) as exc:
            adapter.dispatch(malformed)
        assert any(field in error for error in exc.value.errors)
    assert not (tmp_path / "lab" / "outgoing").exists()


def test_dispatch_ac01_duplicate_dispatch_refused_and_original_untouched(handoff):
    # Dispatches are exactly-once: re-dispatching the same package (the
    # same deterministic dispatch_id) is refused and the original handoff
    # bytes are never overwritten.
    base, adapter = handoff
    first = adapter.dispatch(make_package())
    manifest_path = base / "outgoing" / RUN_ID / EXECUTION_MANIFEST_FILENAME
    original_bytes = manifest_path.read_bytes()
    with pytest.raises(DuplicateDispatchError) as exc:
        adapter.dispatch(make_package())
    assert "already dispatched" in str(exc.value)
    assert manifest_path.read_bytes() == original_bytes
    # A re-dispatch with different artifacts is refused too -- nothing is
    # added and the original handoff stays untouched.
    with pytest.raises(DuplicateDispatchError):
        adapter.dispatch(make_package(), artifacts={"extra.txt": "x"})
    assert not (base / "outgoing" / RUN_ID / "extra.txt").exists()
    assert manifest_path.read_bytes() == original_bytes
    assert first.dispatch_id == generate_id("dispatch", PACKAGE_ID, RUN_ID)


def test_dispatch_ac01_unsafe_run_id_refused(handoff):
    # The Run id maps to the handoff directory name; traversal or glob
    # shapes are refused before anything is written.
    base, adapter = handoff
    for unsafe in ("../escape", "run/../../etc", "..", "run*"):
        package = make_package(run_id=unsafe)
        with pytest.raises(LabAdapterDataError) as exc:
            adapter.dispatch(package)
        assert "not a safe handoff path segment" in str(exc.value)
    assert not (base / "outgoing").exists()


def test_dispatch_ac01_unsafe_artifact_name_refused(handoff):
    # An artifact name that would escape the dispatch directory is
    # refused before any write (nothing is written).
    base, adapter = handoff
    with pytest.raises(LabAdapterDataError) as exc:
        adapter.dispatch(make_package(), artifacts={"../escape.txt": b"x"})
    assert "not a safe path segment" in str(exc.value)
    assert not (base / "outgoing").exists()
    with pytest.raises(TypeError):
        adapter.dispatch(make_package(), artifacts={"note.txt": 7})  # type: ignore[arg-type]


def test_dispatch_ac01_deterministic_bytes_identical_outputs(tmp_path):
    # Identical inputs -> identical outputs: two independent adapters on
    # two independent roots write byte-identical handoff state (the
    # dispatch record differs only in its injected outgoing path).
    first = FilesystemLabAdapter(tmp_path / "a")
    second = FilesystemLabAdapter(tmp_path / "b")
    first.dispatch(make_package(), artifacts={"note.txt": "hello"})
    second.dispatch(make_package(), artifacts={"note.txt": "hello"})
    for name in (EXECUTION_MANIFEST_FILENAME, "note.txt"):
        assert (
            (tmp_path / "a" / "outgoing" / RUN_ID / name).read_bytes()
            == (tmp_path / "b" / "outgoing" / RUN_ID / name).read_bytes()
        )


def test_dispatch_ac01_accepts_typed_lab_execution_package(handoff):
    # The typed frozen LabExecutionPackage model is accepted through the
    # same schema-gated path.
    base, adapter = handoff
    model = LabExecutionPackage.from_dict(make_package())
    record = adapter.dispatch(model)
    assert record.package_id == PACKAGE_ID
    stored = read_json(base / "outgoing" / RUN_ID / EXECUTION_MANIFEST_FILENAME)
    # The typed model round-trips through the same schema-gated path and
    # is persisted byte-for-byte as the model's canonical dict.
    assert stored == model.to_dict()
    assert stored["run_id"] == RUN_ID
    assert stored["required_return"] == ["raw-data.csv"]


def test_dispatch_ac01_surfaces_goal_version_in_manifest(handoff):
    # AC-01 + 10-EXPERIMENT-SUBSYSTEM SS3: the frozen Goal version the
    # package carries is surfaced in the written execution manifest
    # (both the typed model and a raw mapping path).
    base, adapter = handoff
    package = make_package(goal_version="v1")
    adapter.dispatch(package)
    stored = read_json(base / "outgoing" / RUN_ID / EXECUTION_MANIFEST_FILENAME)
    assert stored["goal_version"] == "v1"
    model = LabExecutionPackage.from_dict(
        make_package(goal_version="v1", run_id="sr_run_goal_version_0001")
    )
    adapter.dispatch(model)
    stored_model = read_json(
        base / "outgoing" / "sr_run_goal_version_0001" / EXECUTION_MANIFEST_FILENAME
    )
    assert stored_model["goal_version"] == "v1"
    assert stored_model == model.to_dict()


def test_dispatch_ac01_manifest_without_goal_version_still_dispatchable(handoff):
    # Backwards compatibility: a package written before the goal_version
    # field existed (no goal_version key) passes the schema gate and is
    # written unchanged -- the field stays optional.
    base, adapter = handoff
    package = make_package()
    assert "goal_version" not in package
    adapter.dispatch(package)
    stored = read_json(base / "outgoing" / RUN_ID / EXECUTION_MANIFEST_FILENAME)
    assert stored == package
    assert "goal_version" not in stored


def test_dispatch_ac01_writes_only_within_injected_paths(tmp_path):
    # The adapter derives every path from the injected base_dir; nothing
    # is created next to it.
    base = tmp_path / "lab"
    adapter = FilesystemLabAdapter(base, outgoing="outbox", incoming="inbox")
    record = adapter.dispatch(make_package(), artifacts={"note.txt": "x"})
    assert (base / "outbox" / RUN_ID / EXECUTION_MANIFEST_FILENAME).is_file()
    assert (base / "outbox" / RUN_ID / "note.txt").is_file()
    assert record.outgoing_path == str(base / "outbox" / RUN_ID)
    assert not (tmp_path / "outgoing").exists()
    assert list(tmp_path.iterdir()) == [base]


def test_dispatch_ac01_rejects_malformed_constructor_path_names(tmp_path):
    with pytest.raises(LabAdapterDataError):
        FilesystemLabAdapter(tmp_path / "lab", outgoing="../out")
    with pytest.raises(LabAdapterDataError):
        FilesystemLabAdapter(tmp_path / "lab", incoming="a/b")
    with pytest.raises(TypeError):
        FilesystemLabAdapter(tmp_path / "lab", outgoing=7)  # type: ignore[arg-type]


def test_dispatch_ac01_manifest_is_canonical_deterministic_json(handoff):
    # The persisted execution manifest is canonical JSON: sorted keys,
    # 2-space indent, trailing newline -- identical bytes every time.
    base, adapter = handoff
    adapter.dispatch(make_package())
    text = (base / "outgoing" / RUN_ID / EXECUTION_MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    )
    canonical = json.dumps(make_package(), indent=2, sort_keys=True) + "\n"
    assert text == canonical


# ---------------------------------------------------------------------------
# Issue #157: the human-readable experiment sheet at dispatch time
# ---------------------------------------------------------------------------

#: The fixed caller-injected dispatch timestamp of the sheet tests (the
#: adapter never reads a wall clock; the injected value is the sheet's
#: generated_at).
DISPATCHED_AT = "2026-08-14T00:00:00Z"


def _init_workspace(root: Path) -> None:
    """Initialize a deterministic one-goal workspace at ``root`` through
    the real registration APIs (fixed identity/timestamp, no wall clock)."""
    initialize_project(
        root,
        "10.1039/D5TA00771B",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        identity=AuditIdentity(name="Adapter Bot", email="adapter@example.org"),
    )
    register_goal(
        root,
        GoalContract(
            goal_id=GOAL_ID,
            title="Reproduce the reference flow",
            unit_process_type="batch_adsorption",
            track=GoalTrack.STRICT_REPRODUCTION,
            objective="Reproduce the reference flow end to end",
            requirement_ids=["REQ-REF"],
            dependencies=[],
            acceptance=GoalAcceptance(criteria_ref="ACC-REF", frozen=True),
            analysis_protocol_ref="ANAL-REF",
            replication=GoalReplication(
                independent_required=True, planned_n_policy="n=1 per condition"
            ),
            version=INITIAL_PLAN_VERSION,
            frozen=False,
        ),
    )


def _dispatch_with_sheet(root: Path) -> Path:
    """Dispatch the reference package with the workspace root injected;
    return the dispatch directory."""
    base = root / "lab"
    FilesystemLabAdapter(base).dispatch(
        make_package(), dispatched_at=DISPATCHED_AT, workspace_root=root
    )
    return base / "outgoing" / RUN_ID


def test_dispatch_issue157_renders_sheet_files_into_dispatch_dir(tmp_path):
    # Issue #157: with the workspace root injected, the dispatch
    # directory carries the human-readable experiment sheet -- the
    # deterministic PDF, its canonical JSON sidecar (the PDF's SHA-256)
    # and the printable HTML sheet -- next to the machine handoff.
    root = tmp_path / "workspace"
    _init_workspace(root)
    outgoing_dir = _dispatch_with_sheet(root)

    pdf_path = outgoing_dir / PDF_FILENAME_TPL.format(run_id=RUN_ID)
    json_path = outgoing_dir / JSON_FILENAME_TPL.format(run_id=RUN_ID)
    html_path = outgoing_dir / HTML_FILENAME_TPL.format(run_id=RUN_ID)
    # The machine handoff is untouched; the sheet files sit next to it.
    assert (outgoing_dir / DISPATCH_RECORD_FILENAME).is_file()
    assert (outgoing_dir / EXECUTION_MANIFEST_FILENAME).is_file()
    assert pdf_path.is_file()
    assert json_path.is_file()
    assert html_path.is_file()

    # The sheet PDF is a real PDF 1.4 document.
    pdf_bytes = pdf_path.read_bytes()
    assert pdf_bytes.startswith(b"%PDF-1.4\n")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")

    # The sidecar is canonical JSON carrying the PDF's SHA-256 (the
    # audit-package registration pattern).
    sidecar = read_json(json_path)
    assert sidecar["kind"] == "experiment-execution-sheet-pdf"
    assert sidecar["run_id"] == RUN_ID
    assert sidecar["pdf_file"] == PDF_FILENAME_TPL.format(run_id=RUN_ID)
    assert sidecar["pdf_sha256"] == hashlib.sha256(pdf_bytes).hexdigest()
    assert sidecar["pdf_size_bytes"] == len(pdf_bytes)

    # The caller-injected dispatched_at is the sheet's generated_at (the
    # footer stamp); the PDF carries no wall-clock keys.
    assert DISPATCHED_AT.encode() in pdf_bytes
    assert b"/CreationDate" not in pdf_bytes
    assert b"/ModDate" not in pdf_bytes


def test_dispatch_issue157_html_sheet_is_operator_facing(tmp_path):
    # The printable HTML sheet carries the designed operator-facing
    # structure: the sheet title, the procedure steps, the required-return
    # checklist with the "returned as file" fill-in, the red/amber
    # callouts and the signature lines -- a lab operator with no CS
    # background can execute from the sheet alone.
    root = tmp_path / "workspace"
    _init_workspace(root)
    outgoing_dir = _dispatch_with_sheet(root)

    html = (outgoing_dir / HTML_FILENAME_TPL.format(run_id=RUN_ID)).read_text(
        encoding="utf-8"
    )
    assert html.startswith("<!DOCTYPE html>")
    assert "Experiment Execution Sheet" in html
    assert "weigh the precursor" in html  # the numbered procedure
    assert "raw-data.csv" in html  # the required-return token
    assert "returned as file" in html  # the checklist fill-in
    assert "Prohibited changes" in html  # the red callout
    assert "Safety notes" in html  # the amber callout
    assert "Operator signature" in html  # the signature lines (& escaped)
    assert "Supervisor signature" in html


def test_dispatch_issue157_sheet_rendering_is_deterministic(tmp_path):
    # Identical state -> byte-identical sheet files: two independent
    # adapters on two independent initialized workspaces render the same
    # PDF, sidecar and HTML bytes (the injected dispatched_at is the only
    # time source).
    first_root = tmp_path / "a"
    second_root = tmp_path / "b"
    for root in (first_root, second_root):
        _init_workspace(root)
        _dispatch_with_sheet(root)

    for name in (
        PDF_FILENAME_TPL.format(run_id=RUN_ID),
        JSON_FILENAME_TPL.format(run_id=RUN_ID),
        HTML_FILENAME_TPL.format(run_id=RUN_ID),
    ):
        assert (
            first_root / "lab" / "outgoing" / RUN_ID / name
        ).read_bytes() == (
            second_root / "lab" / "outgoing" / RUN_ID / name
        ).read_bytes(), name


def test_dispatch_issue157_sheet_rendering_boundaries(tmp_path):
    # workspace_root is an injected path: wrong types are refused at the
    # public boundary, a deviating handoff layout is refused before
    # anything is written, and an uninitialized workspace refuses the
    # sheet render loudly (the renderer's error surface).
    root = tmp_path / "workspace"
    _init_workspace(root)
    adapter = FilesystemLabAdapter(root / "lab")
    with pytest.raises(TypeError, match="workspace_root must be a str or Path"):
        adapter.dispatch(make_package(), workspace_root=7)  # type: ignore[arg-type]

    # The sheet renderer reads the canonical lab/outgoing handoff; a
    # deviating layout is refused before any write.
    with pytest.raises(LabAdapterDataError, match="canonical"):
        FilesystemLabAdapter(root / "lab", outgoing="outbox").dispatch(
            make_package(), workspace_root=root
        )
    assert not (root / "lab" / "outbox").exists()
    with pytest.raises(LabAdapterDataError, match="canonical"):
        FilesystemLabAdapter(tmp_path / "other").dispatch(
            make_package(), workspace_root=root
        )
    assert not (tmp_path / "other").exists()

    # A workspace root without a project state record refuses the sheet
    # render loudly (the handoff itself was written by then).
    bare = tmp_path / "bare"
    with pytest.raises(SheetNotInitializedError):
        FilesystemLabAdapter(bare / "lab").dispatch(
            make_package(), workspace_root=bare
        )

"""Experiment execution sheet PDF renderer tests (issue #122).

The sheet gets first-class PDF treatment next to its HTML form: one
deterministic PDF 1.4 document per dispatched lab package, rendered
from the same state and error surface as the HTML sheet (the real
outgoing handoff of the real ``FilesystemLabAdapter``), through the
shared ``rendering`` visual system. The tests pin:

* ``validity`` -- a real PDF 1.4 document, written with the PDF plus a
  canonical JSON sidecar (with the PDF's SHA-256) to the out dir;
* ``determinism`` -- byte-identical double renders, caller-injected
  ``generated_at``, no wall-clock keys;
* ``language`` -- the explicit ``language`` input (default ``"en"``
  byte-identical to the implicit default; the ``zh`` pack renders
  deterministically; unknown languages raise the stable boundary
  errors of ``resolve_pack``);
* ``errors`` -- the shared sheet error surface
  (``SheetNotInitializedError`` / ``SheetNotFoundError`` /
  ``SheetCorruptError``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from reporting_helpers import make_goal
from sheets_helpers import (
    dispatch_package,
    init_project,
    make_package,
)

from scientific_reproduction.core.state_backend import SCHEMA_TO_STATE_DIR
from scientific_reproduction.planning.plan import register_goal
from scientific_reproduction.reporting.sheet_pdf import (
    JSON_FILENAME_TPL,
    PDF_FILENAME_TPL,
    SHEET_PDF_VERSION,
    ExperimentSheetPdf,
    build_experiment_sheet_pdf,
    render_experiment_sheet_pdf,
)
from scientific_reproduction.reporting.sheets import (
    SheetCorruptError,
    SheetNotFoundError,
    SheetNotInitializedError,
    build_experiment_sheet,
)

GENERATED_AT = "2026-08-15T00:00:00Z"


def _install_goal(root: Path) -> None:
    register_goal(root, make_goal())


def _dispatch(root: Path, **overrides: object) -> str:
    manifest = make_package(**overrides)
    return dispatch_package(root, manifest)


def _handoff_dir(root: Path, run_id: str) -> Path:
    return (
        root
        / SCHEMA_TO_STATE_DIR["lab-execution-package"]
        / "outgoing"
        / run_id
    )


# ---------------------------------------------------------------------------
# validity: a real PDF 1.4 document plus the machine-auditable files
# ---------------------------------------------------------------------------


def test_sheet_pdf_renders_valid_pdf_and_writes_files(tmp_path: Path) -> None:
    """The sheet renders a PDF 1.4 document and writes the PDF plus a
    canonical JSON sidecar (with the PDF checksum) to the out dir."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    out_dir = tmp_path / "reports"
    result = build_experiment_sheet_pdf(
        root, run_id, generated_at=GENERATED_AT, out_dir=out_dir
    )

    assert result.pdf_bytes.startswith(b"%PDF-1.4\n")
    assert result.pdf_bytes.rstrip().endswith(b"%%EOF")
    assert result.pages >= 1
    assert result.sheet_pdf_version == SHEET_PDF_VERSION
    assert result.run_id == run_id
    assert result.language == "en"
    pdf_path = out_dir / PDF_FILENAME_TPL.format(run_id=run_id)
    json_path = out_dir / JSON_FILENAME_TPL.format(run_id=run_id)
    assert pdf_path.exists()
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["kind"] == "experiment-execution-sheet-pdf"
    assert data["run_id"] == run_id
    assert data["language"] == "en"
    assert data["pdf_file"] == PDF_FILENAME_TPL.format(run_id=run_id)
    assert data["pdf_sha256"] == result.pdf_sha256
    assert data["pdf_size_bytes"] == result.pdf_size_bytes
    # The sidecar's checksum matches the written file.
    assert result.pdf_sha256 == hashlib.sha256(
        pdf_path.read_bytes()
    ).hexdigest()
    # The HTML sheet is unchanged by the PDF build (same state).
    assert build_experiment_sheet(root, run_id).to_html()


def test_sheet_pdf_content_carries_the_sheet_surface(tmp_path: Path) -> None:
    """The rendered document carries the identity ids, the sheet title
    and the goal context -- ASCII text, cp1252-encodable (CJK glyphs of
    the zh pack fall back to the writer's deterministic ``?``)."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    result = build_experiment_sheet_pdf(root, run_id)

    data = result.pdf_bytes
    assert b"Experiment Execution Sheet" in data
    assert run_id.encode() in data
    assert b"Reproduce the FDM-201 batch-level uptake" in data
    assert b"precursor A" in data
    assert b"deterministic render" in data  # no wall clock in the footer


def test_sheet_pdf_structured_surface_roundtrip(tmp_path: Path) -> None:
    """The structured surface is stable and self-describing."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    result = build_experiment_sheet_pdf(root, run_id)
    data = json.loads(result.to_canonical_json())
    assert data["sheet_pdf_version"] == SHEET_PDF_VERSION
    assert data["kind"] == "experiment-execution-sheet-pdf"
    assert data["project_id"] == "PROJ-001"
    assert isinstance(result, ExperimentSheetPdf)
    assert render_experiment_sheet_pdf(root, run_id) == result.pdf_bytes


# ---------------------------------------------------------------------------
# determinism: pure function of the registered state
# ---------------------------------------------------------------------------


def test_sheet_pdf_determinism_byte_identical(tmp_path: Path) -> None:
    """Same state, same inputs -> byte-identical PDF and canonical JSON
    (the generated_at injection is the only time source)."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    first = build_experiment_sheet_pdf(root, run_id)
    second = build_experiment_sheet_pdf(root, run_id)
    assert first.pdf_bytes == second.pdf_bytes
    assert first.to_canonical_json() == second.to_canonical_json()


def test_sheet_pdf_no_wall_clock_keys_in_bytes(tmp_path: Path) -> None:
    """The rendered PDF carries no CreationDate/ModDate/ID keys."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    result = build_experiment_sheet_pdf(root, run_id)
    assert b"/CreationDate" not in result.pdf_bytes
    assert b"/ModDate" not in result.pdf_bytes
    assert b"/ID" not in result.pdf_bytes


def test_sheet_pdf_generated_at_injected_footer(tmp_path: Path) -> None:
    """``generated_at`` is caller-injected into the footer; the default
    footer says "deterministic render" (no wall clock)."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    stamped = build_experiment_sheet_pdf(root, run_id, generated_at=GENERATED_AT)
    assert GENERATED_AT.encode() in stamped.pdf_bytes
    assert b"deterministic render" not in stamped.pdf_bytes
    default = build_experiment_sheet_pdf(root, run_id)
    assert GENERATED_AT.encode() not in default.pdf_bytes
    assert b"deterministic render" in default.pdf_bytes


# ---------------------------------------------------------------------------
# language: explicit language input (issue #122)
# ---------------------------------------------------------------------------


def test_sheet_pdf_language_default_is_english_byte_identical(
    tmp_path: Path,
) -> None:
    """``language="en"`` is the explicit default and renders byte-identical
    to the implicit default."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    default = build_experiment_sheet_pdf(root, run_id)
    explicit = build_experiment_sheet_pdf(root, run_id, language="en")
    assert default.pdf_bytes == explicit.pdf_bytes
    assert default.to_canonical_json() == explicit.to_canonical_json()


def test_sheet_pdf_language_zh_renders_deterministically(tmp_path: Path) -> None:
    """The zh pack renders a deterministic PDF (the writer's documented
    cp1252 ``?`` fallback for CJK glyphs) with the Chinese pack key and
    titles on the structured surface."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    out_dir = tmp_path / "reports"
    result = build_experiment_sheet_pdf(
        root, run_id, language="zh", out_dir=out_dir
    )
    assert result.language == "zh"
    assert result.pdf_bytes.startswith(b"%PDF-1.4\n")
    data = json.loads(
        (out_dir / JSON_FILENAME_TPL.format(run_id=run_id)).read_text(
            encoding="utf-8"
        )
    )
    assert data["language"] == "zh"
    # Deterministic across builds; distinct from the en document.
    twice = build_experiment_sheet_pdf(root, run_id, language="zh")
    assert twice.pdf_bytes == result.pdf_bytes
    en = build_experiment_sheet_pdf(root, run_id)
    assert result.pdf_bytes != en.pdf_bytes


def test_sheet_pdf_language_unknown_raises_stable_error(tmp_path: Path) -> None:
    """Unknown languages and non-string inputs raise the stable boundary
    errors of ``resolve_pack`` (never silently fall back)."""
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    with pytest.raises(ValueError, match="available languages: en, zh"):
        build_experiment_sheet_pdf(root, run_id, language="fr")
    with pytest.raises(TypeError, match="language must be a non-empty string"):
        build_experiment_sheet_pdf(root, run_id, language=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# errors: the shared sheet error surface
# ---------------------------------------------------------------------------


def test_sheet_pdf_errors_not_initialized(tmp_path: Path) -> None:
    # No project state record -> SheetNotInitializedError.
    with pytest.raises(SheetNotInitializedError):
        build_experiment_sheet_pdf(tmp_path, "RUN-001")


def test_sheet_pdf_errors_no_dispatch(tmp_path: Path) -> None:
    # A run id without an outgoing handoff -> SheetNotFoundError.
    root = init_project(tmp_path)
    with pytest.raises(SheetNotFoundError):
        build_experiment_sheet_pdf(root, "RUN-MISSING")


def test_sheet_pdf_errors_corrupt_manifest(tmp_path: Path) -> None:
    # A corrupt manifest file -> SheetCorruptError (the shared surface
    # of the HTML sheet renderer).
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    manifest_path = _handoff_dir(root, run_id) / "manifest.json"
    manifest_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SheetCorruptError):
        build_experiment_sheet_pdf(root, run_id)


def test_sheet_pdf_errors_type_error_boundaries(tmp_path: Path) -> None:
    # TypeError at the public boundaries, stable messages.
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        build_experiment_sheet_pdf(123, "RUN-001")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_experiment_sheet_pdf(root, "")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_experiment_sheet_pdf(  # type: ignore[arg-type]
            root, "RUN-001", generated_at=""
        )

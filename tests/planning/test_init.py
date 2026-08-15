"""Tests for deterministic project initialization primitives (DEV-M4-G01).

Covers:
  * AC-01 -- exactly ONE Primary Target Paper per project: the initializer
    registers a single primary target and a second registration is rejected
    with a stable error, while the representation stays structural (the
    frozen ``Project`` model carries a single required ``primary_target``);
  * AC-02 -- project initialization creates the Git repository (M3-G01
    helpers), the workspace tree, ``project.yaml`` and the init event with
    zero lab/HPC inventory, fully offline, deterministically (fixed
    timestamps, identical bytes and identical commit SHA for identical
    inputs);
  * AC-03 -- PDF (local path), DOI and URL target forms are parsed and
    round-trip into the project state without loss; malformed forms (bad
    DOI syntax, unparsable URL, non-``.pdf`` path) are rejected with stable
    messages;
  * the init guardrails -- a non-empty root is refused with
    ``NonEmptyRootError`` unless ``allow_non_empty_root=True`` (API) or
    ``--allow-non-empty-root`` (CLI) is passed, and the starter
    ``.gitignore``/``.gitattributes`` are written and recorded in the
    initial audit commit;
  * the /reproduce CLI ``init`` subcommand (exit codes, stable output).

Every test name contains "init" so the goal verification command
``python -m pytest -q tests/planning -k init`` selects the full suite.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from init_helpers import IDENTITY, TIMESTAMP, TIMESTAMP_ISO

from scientific_reproduction.audit.git import count_commits, current_head, read_file_at
from scientific_reproduction.cli.reproduce import main as cli_main
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    PrimaryTarget,
    ProjectPhase,
    ReproductionOutcome,
    TargetSourceType,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import (
    DEFAULT_DOMAIN_PACK,
    GITATTRIBUTES_FILENAME,
    GITIGNORE_FILENAME,
    INIT_DIRECTORIES,
    INIT_EVENT_ACTOR,
    INIT_EVENT_TYPE,
    INITIAL_PLAN_VERSION,
    PROJECT_STATE_FILENAME,
    STARTER_GITATTRIBUTES,
    STARTER_GITIGNORE,
    NonEmptyRootError,
    PlanningError,
    ProjectAlreadyInitializedError,
    ProjectInitResult,
    ProjectNotInitializedError,
    TargetAlreadyRegisteredError,
    TargetValidationError,
    detect_target_form,
    initialize_project,
    parse_target_form,
    read_project_state,
    register_primary_target,
)
from scientific_reproduction.planning.plan import GOALS_STATE_DIR, register_goal
from tests.core.fixtures import VALID_DOCS

DOI = "10.1039/D5TA00771B"
URL = "https://doi.org/10.1039/D5TA00771B"
PDF_PATH = "papers/main.pdf"


def _init(
    root: Path,
    target: str | PrimaryTarget = DOI,
    *,
    title: str | None = None,
    **kwargs: object,
) -> ProjectInitResult:
    """Initialize with pinned identity/timestamp; extra kwargs pass through."""
    return initialize_project(
        root,
        target,
        title=title,
        timestamp=TIMESTAMP,
        identity=IDENTITY,
        **kwargs,
    )


def _state_dict(root: Path) -> dict[str, object]:
    """Parse the project.yaml record of ``root``."""
    path = root / PROJECT_STATE_FILENAME
    assert path.is_file(), f"{path} missing"
    return json.loads(path.read_text(encoding="utf-8"))


def _event_dict(root: Path, project_id: str) -> dict[str, object]:
    """Parse the project.initialized event record of ``root``."""
    event_id = generate_id("event", INIT_EVENT_TYPE, project_id)
    path = root / "events" / f"{event_id}.json"
    assert path.is_file(), f"{path} missing"
    return json.loads(path.read_text(encoding="utf-8"))


def _show_commit(root: Path, fmt: str) -> str:
    """Return ``git show -s --format=<fmt> HEAD`` output (single line)."""
    process = subprocess.run(
        ["git", "-C", str(root), "show", "-s", f"--format={fmt}", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return process.stdout.strip()


# ---------------------------------------------------------------------------
# AC-01: exactly ONE Primary Target Paper per project
# ---------------------------------------------------------------------------


def test_init_writes_exactly_one_primary_target(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI, title="FDM-201")
    assert result.project.primary_target == PrimaryTarget(
        source_type=TargetSourceType.DOI,
        identifier=DOI,
        doi=DOI,
        title="FDM-201",
    )
    # The state record carries exactly one primary_target object (the
    # frozen Project model makes it a single required object, AC-01).
    state = _state_dict(result.project_root)
    assert state["primary_target"] == {
        "source_type": "doi",
        "identifier": DOI,
        "doi": DOI,
        "title": "FDM-201",
    }
    assert result.project.project_phase == ProjectPhase.INITIALIZING
    assert result.project.reproduction_outcome == ReproductionOutcome.UNDETERMINED


def test_init_rejects_second_primary_target_registration(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    with pytest.raises(TargetAlreadyRegisteredError) as excinfo:
        register_primary_target(
            result.project_root,
            URL,
            timestamp=TIMESTAMP,
        )
    message = str(excinfo.value)
    assert "already registered" in message
    assert result.project.project_id in message
    assert DOI in message
    # Stable error: same inputs, same message.
    again = _init(tmp_path / "project-2", DOI)
    with pytest.raises(TargetAlreadyRegisteredError) as excinfo2:
        register_primary_target(again.project_root, URL, timestamp=TIMESTAMP)
    assert str(excinfo2.value) == message
    # Rejection leaves the recorded state untouched.
    assert _state_dict(result.project_root)["primary_target"]["identifier"] == DOI


def test_init_rejects_second_initialization_of_same_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init(root, DOI)
    with pytest.raises(ProjectAlreadyInitializedError) as excinfo:
        _init(root, URL)
    assert "already initialized" in str(excinfo.value)
    assert _state_dict(root)["primary_target"]["identifier"] == DOI


def test_init_rejects_primary_registration_before_initialization(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectNotInitializedError) as excinfo:
        register_primary_target(tmp_path / "empty", DOI, timestamp=TIMESTAMP)
    assert "initialize the project first" in str(excinfo.value)


def test_init_refuses_non_empty_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "unrelated.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(NonEmptyRootError) as excinfo:
        _init(root, DOI)
    message = str(excinfo.value)
    assert "not empty" in message
    assert "allow_non_empty_root=True" in message
    # Hidden entries count too: a root holding only a stray file is
    # refused, never silently initialized around.
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / ".DS_Store").write_text("junk", encoding="utf-8")
    with pytest.raises(NonEmptyRootError, match="not empty"):
        _init(hidden, DOI)
    # Nothing was created in the refused root: no state, no git repo, no
    # workspace directories, and the unrelated content is untouched.
    assert not (root / PROJECT_STATE_FILENAME).is_file()
    assert not (root / ".git").is_dir()
    assert not (root / "sources").is_dir()
    assert (root / "unrelated.txt").read_text(encoding="utf-8") == "keep me"


def test_init_allows_non_empty_root_with_flag(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "unrelated.txt").write_text("keep me", encoding="utf-8")
    result = _init(root, DOI, allow_non_empty_root=True)
    # The unrelated content survives, and the initial audit commit
    # records only the initialization payload, never the stray file.
    assert (root / "unrelated.txt").read_text(encoding="utf-8") == "keep me"
    assert _state_dict(root)["primary_target"]["identifier"] == DOI
    assert count_commits(result.project_root) == 1
    process = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert "unrelated.txt" not in process.stdout


def test_init_accepts_existing_empty_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    result = _init(root, DOI)
    assert result.project_root == root.resolve()
    assert _state_dict(root)["primary_target"]["identifier"] == DOI
    assert count_commits(result.project_root) == 1


# ---------------------------------------------------------------------------
# AC-02: Git/state structures with zero lab/HPC inventory, offline
# ---------------------------------------------------------------------------


def test_init_creates_project_directories(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    for rel in INIT_DIRECTORIES:
        assert (result.project_root / rel).is_dir(), f"{rel} missing"
    # Nested lab handoff directories exist (PROJECT-TREE template).
    assert (result.project_root / "lab" / "outgoing").is_dir()
    assert (result.project_root / "lab" / "incoming").is_dir()


def test_init_writes_starter_gitignore_and_gitattributes(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    root = result.project_root
    # The starter files exist with exactly the shipped content...
    gitignore = root / GITIGNORE_FILENAME
    gitattributes = root / GITATTRIBUTES_FILENAME
    assert gitignore.read_text(encoding="utf-8") == STARTER_GITIGNORE
    assert gitattributes.read_text(encoding="utf-8") == STARTER_GITATTRIBUTES
    # ...and cover the hazards of the issue: the nested-repo gitlink risk
    # (documented; the directory itself must be ignored), large raw
    # artifacts per ADR 38, and LF normalization.
    assert "gitlink" in STARTER_GITIGNORE
    assert "*.h5" in STARTER_GITIGNORE
    assert "*.zip" in STARTER_GITIGNORE
    assert "* text=auto eol=lf" in STARTER_GITATTRIBUTES
    assert "*.pdf binary" in STARTER_GITATTRIBUTES
    # Both files are recorded in the initial audit commit (git show, not
    # working copy), so .gitattributes is effective immediately and the
    # initial tree is complete.
    assert read_file_at(root, GITIGNORE_FILENAME) == STARTER_GITIGNORE
    assert read_file_at(root, GITATTRIBUTES_FILENAME) == STARTER_GITATTRIBUTES


def test_init_initializes_git_repository(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    assert (result.project_root / ".git").is_dir()
    assert count_commits(result.project_root) == 1
    assert current_head(result.project_root) == result.commit.commit_sha


def test_init_commits_project_initialized_checkpoint(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    # The M3-G01 audit checkpoint: kind and deterministic message.
    assert result.commit.kind == INIT_EVENT_TYPE
    assert result.commit.message == "project initialized"
    assert result.commit.object_id is None
    assert result.commit.version is None
    # Deterministic identity and date, independent of any git config.
    assert _show_commit(result.project_root, "%an|%ae|%cn|%ce|%aI") == (
        "Audit Bot|audit@example.org|Audit Bot|audit@example.org|"
        "2026-01-01T00:00:00Z"
    )
    # The initial state is in the commit tree (git show, not working copy).
    process = subprocess.run(
        [
            "git",
            "-C",
            str(result.project_root),
            "show",
            f"HEAD:{PROJECT_STATE_FILENAME}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert json.loads(process.stdout) == _state_dict(result.project_root)


def test_init_writes_project_state_file(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI, title="FDM-201")
    state = _state_dict(result.project_root)
    assert state == {
        "project_id": result.project.project_id,
        "title": "FDM-201",
        "primary_target": {
            "source_type": "doi",
            "identifier": DOI,
            "doi": DOI,
            "title": "FDM-201",
        },
        "project_phase": "INITIALIZING",
        "reproduction_outcome": "UNDETERMINED",
        "current_plan_version": INITIAL_PLAN_VERSION,
        "domain_pack": DEFAULT_DOMAIN_PACK,
        "state_backend": "filesystem",
        "created_at": TIMESTAMP_ISO,
        "updated_at": TIMESTAMP_ISO,
    }
    # The persisted record round-trips into the typed Project model.
    assert read_project_state(result.project_root) == result.project


def test_init_writes_project_initialized_event(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    event = _event_dict(result.project_root, result.project.project_id)
    assert event == {
        "event_id": result.event.event_id,
        "timestamp": TIMESTAMP_ISO,
        "actor": INIT_EVENT_ACTOR,
        "event_type": INIT_EVENT_TYPE,
        "object_id": result.project.project_id,
        "to": "INITIALIZING",
        "payload": {},
        # The init event is a first-class log record (sequence 1).
        "sequence": 1,
    }
    # The event is schema-valid (persistence gate).
    validate_and_reject("event", event)


def test_init_event_is_a_first_class_log_record(tmp_path: Path) -> None:
    """The init event lands in the canonical events/ tree directory and is
    readable as a ``ProjectEventLog`` record over the workspace root --
    the same directory the state backend resolves for obj_type "event".
    """
    result = _init(tmp_path / "project", DOI)
    log = ProjectEventLog(result.project_root)
    records = log.list_events()
    assert [r.event.event_id for r in records] == [result.event.event_id]
    assert [r.sequence for r in records] == [1]
    assert records[0].event == result.event
    # A state backend over the workspace root sees the same record.
    backend = FilesystemStateBackend(result.project_root)
    assert backend.list_ids("event") == [result.event.event_id]
    stored = backend.read("event", result.event.event_id)
    assert stored["event_id"] == result.event.event_id
    assert stored["sequence"] == 1
    # The log's sequence counter is initialized by the append.
    assert (
        result.project_root / "_event_log" / "sequence.json"
    ).is_file()


def test_init_works_without_lab_hpc_inventory(tmp_path: Path) -> None:
    # A bare temporary directory: no inventory input, no configuration,
    # nothing but the target itself.
    result = _init(tmp_path / "bare" / "nested", URL)
    # The inventory directory exists but contains no items: nothing was
    # discovered or registered from any lab/HPC inventory.
    inventory = result.project_root / "inventory"
    assert inventory.is_dir()
    assert list(inventory.iterdir()) == []
    # The state records carry no inventory references.
    assert "inventory" not in _state_dict(result.project_root)


def test_init_state_contents_are_deterministic(tmp_path: Path) -> None:
    first = _init(tmp_path / "a", DOI, title="FDM-201")
    second = _init(tmp_path / "b", DOI, title="FDM-201")
    # Identical state bytes, event bytes, project id and audit commit SHA.
    assert (first.project_root / PROJECT_STATE_FILENAME).read_bytes() == (
        second.project_root / PROJECT_STATE_FILENAME
    ).read_bytes()
    first_event = _event_dict(first.project_root, first.project.project_id)
    second_event = _event_dict(second.project_root, second.project.project_id)
    assert (first.project_root / "events" / f"{first_event['event_id']}.json").read_bytes() == (
        second.project_root / "events" / f"{second_event['event_id']}.json"
    ).read_bytes()
    # The starter guardrail files are byte-identical too.
    assert (first.project_root / GITIGNORE_FILENAME).read_bytes() == (
        second.project_root / GITIGNORE_FILENAME
    ).read_bytes()
    assert (first.project_root / GITATTRIBUTES_FILENAME).read_bytes() == (
        second.project_root / GITATTRIBUTES_FILENAME
    ).read_bytes()
    assert first.project.project_id == second.project.project_id
    assert first.commit.commit_sha == second.commit.commit_sha


def test_init_state_validates_against_frozen_schema(tmp_path: Path) -> None:
    result = _init(tmp_path / "project", DOI)
    validate_and_reject("project", _state_dict(result.project_root))


def test_init_project_id_is_deterministic(tmp_path: Path) -> None:
    expected = generate_id("project", TargetSourceType.DOI.value, DOI)
    result = _init(tmp_path / "project", DOI)
    assert result.project.project_id == expected
    assert result.project.project_id.startswith("sr_project_")
    # An explicit project id is honored.
    explicit = _init(
        tmp_path / "other", URL, project_id="RP-FDM201-001"
    )
    assert explicit.project.project_id == "RP-FDM201-001"


# ---------------------------------------------------------------------------
# AC-03: PDF / DOI / URL target forms
# ---------------------------------------------------------------------------


def test_init_target_form_pdf_represented() -> None:
    parsed = parse_target_form(PDF_PATH, title="paper")
    assert parsed == PrimaryTarget(
        source_type=TargetSourceType.PDF,
        identifier=PDF_PATH,
        title="paper",
    )
    assert detect_target_form(PDF_PATH) == TargetSourceType.PDF
    # Case-insensitive suffix detection.
    assert detect_target_form("paper.PDF") == TargetSourceType.PDF


def test_init_target_form_doi_represented() -> None:
    parsed = parse_target_form(DOI)
    assert parsed == PrimaryTarget(
        source_type=TargetSourceType.DOI,
        identifier=DOI,
        doi=DOI,
    )
    assert detect_target_form(DOI) == TargetSourceType.DOI


def test_init_target_form_url_represented() -> None:
    parsed = parse_target_form(URL)
    assert parsed == PrimaryTarget(
        source_type=TargetSourceType.URL,
        identifier=URL,
    )
    assert detect_target_form(URL) == TargetSourceType.URL


def test_init_target_forms_roundtrip_into_project_state(tmp_path: Path) -> None:
    targets = {
        "pdf": PDF_PATH,
        "doi": DOI,
        "url": URL,
    }
    for expected_form, target in targets.items():
        root = tmp_path / f"project-{expected_form}"
        result = _init(root, target)
        state_target = _state_dict(result.project_root)["primary_target"]
        assert isinstance(state_target, dict)
        # The form is represented losslessly: source_type + identifier.
        assert state_target["source_type"] == expected_form
        assert state_target["identifier"] == target
        # The typed record matches the parsed model.
        assert result.project.primary_target == parse_target_form(target)
        # The audit commit recorded the target state.
        assert read_project_state(root) == result.project


def test_init_rejects_malformed_doi(tmp_path: Path) -> None:
    malformed = [
        "10.1039/",           # empty suffix
        "10.1039",            # no slash
        "10.1/abc",           # too few registrant digits
        "doi:10.1039/abc",    # prefix not part of the DOI
        "10.1039/abc def",    # whitespace
    ]
    for value in malformed:
        with pytest.raises(TargetValidationError, match="malformed DOI"):
            parse_target_form(value, form=TargetSourceType.DOI)
        # Through the initializer the form is auto-detected; the value must
        # still be rejected deterministically.
        with pytest.raises(TargetValidationError):
            _init(tmp_path / "p", value)


def test_init_rejects_unparsable_url(tmp_path: Path) -> None:
    malformed = [
        "http://",              # empty host
        "https:///path",        # empty host
        "ftp://example.org/x",  # non-http(s) scheme
        "example.org/paper",    # no scheme
    ]
    for value in malformed:
        with pytest.raises(TargetValidationError, match="unparsable URL"):
            parse_target_form(value, form=TargetSourceType.URL)
        # Through the initializer the form is auto-detected; the value must
        # still be rejected deterministically.
        with pytest.raises(TargetValidationError):
            _init(tmp_path / "p", value)


def test_init_rejects_undeterminable_target_form(tmp_path: Path) -> None:
    for value in ("xyz", "paper.txt", "10.x/y"):
        with pytest.raises(TargetValidationError, match="cannot determine"):
            parse_target_form(value)
        with pytest.raises(TargetValidationError, match="cannot determine"):
            _init(tmp_path / "p", value)


def test_init_rejects_explicit_form_mismatch(tmp_path: Path) -> None:
    with pytest.raises(TargetValidationError, match="must be a local path"):
        parse_target_form("paper.txt", form=TargetSourceType.PDF)
    with pytest.raises(TargetValidationError, match="malformed DOI"):
        parse_target_form(URL, form=TargetSourceType.DOI)
    with pytest.raises(TargetValidationError, match="unparsable URL"):
        parse_target_form(DOI, form=TargetSourceType.URL)


# ---------------------------------------------------------------------------
# API robustness
# ---------------------------------------------------------------------------


def test_init_wrong_types_raise_type_error(tmp_path: Path) -> None:
    root = tmp_path / "project"
    with pytest.raises(TypeError):
        initialize_project(123, DOI)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(root, 42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(root, DOI, project_id=7)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(root, DOI, title=7)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(root, DOI, domain_pack=7)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(root, DOI, timestamp="2026-01-01")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(root, DOI, identity="Audit Bot")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        initialize_project(
            root, DOI, allow_non_empty_root=1  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        parse_target_form(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_primary_target(root, DOI, timestamp=7)  # type: ignore[arg-type]
    # Naive timestamps are rejected (same convention as the audit helpers).
    with pytest.raises(ValueError, match="timezone-aware"):
        initialize_project(root, DOI, timestamp=datetime(2026, 1, 1))


def test_init_accepts_preparsed_primary_target(tmp_path: Path) -> None:
    parsed = PrimaryTarget(
        source_type=TargetSourceType.DOI,
        identifier=DOI,
        doi=DOI,
        title="preparsed",
    )
    result = _init(tmp_path / "project", parsed)
    assert result.project.primary_target == parsed
    # A hand-built target with a malformed identifier is rejected even when
    # the model itself is constructible (form validity is enforced).
    broken = PrimaryTarget(
        source_type=TargetSourceType.DOI, identifier="not-a-doi"
    )
    with pytest.raises(TargetValidationError, match="malformed DOI"):
        _init(tmp_path / "broken", broken)


# ---------------------------------------------------------------------------
# CLI: reproduce init
# ---------------------------------------------------------------------------


def test_init_cli_initializes_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    code = cli_main(
        [
            "init",
            DOI,
            "--root",
            str(root),
            "--title",
            "FDM-201",
            "--timestamp",
            TIMESTAMP_ISO,
            "--author-name",
            "Audit Bot",
            "--author-email",
            "audit@example.org",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "error" not in captured.err
    # Stable summary on stdout.
    assert "initialized project" in captured.out
    assert "primary target: doi '10.1039/D5TA00771B'" in captured.out
    assert "git checkpoint:" in captured.out
    # The initialized state matches the deterministic API path.
    state = _state_dict(root)
    assert state["primary_target"]["source_type"] == "doi"
    assert state["created_at"] == TIMESTAMP_ISO
    assert count_commits(root) == 1


def test_init_cli_target_forms(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    targets = {"pdf": PDF_PATH, "doi": DOI, "url": URL}
    for name, target in targets.items():
        root = tmp_path / f"cli-{name}"
        code = cli_main(
            ["init", target, "--root", str(root), "--timestamp", TIMESTAMP_ISO]
        )
        assert code == 0
        capsys.readouterr()
        assert _state_dict(root)["primary_target"]["identifier"] == target


def test_init_cli_rejects_malformed_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(["init", "not-a-target", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: ")
    assert "cannot determine target form" in captured.err


def test_init_cli_rejects_second_initialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    assert cli_main(["init", DOI, "--root", str(root)]) == 0
    capsys.readouterr()
    code = cli_main(["init", URL, "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 1
    assert "already initialized" in captured.err


def test_init_cli_refuses_non_empty_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "unrelated.txt").write_text("keep me", encoding="utf-8")
    code = cli_main(["init", DOI, "--root", str(root)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: ")
    assert "not empty" in captured.err
    assert not (root / PROJECT_STATE_FILENAME).is_file()
    assert (root / "unrelated.txt").read_text(encoding="utf-8") == "keep me"


def test_init_cli_allow_non_empty_root_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "unrelated.txt").write_text("keep me", encoding="utf-8")
    code = cli_main(
        ["init", DOI, "--root", str(root), "--allow-non-empty-root"]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "error" not in captured.err
    assert "initialized project" in captured.out
    assert _state_dict(root)["primary_target"]["identifier"] == DOI
    # The starter guardrail files are part of the initialized workspace.
    assert (root / GITIGNORE_FILENAME).is_file()
    assert (root / GITATTRIBUTES_FILENAME).is_file()


def test_init_cli_rejects_naive_timestamp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(
        ["init", DOI, "--root", str(tmp_path), "--timestamp", "2026-01-01T00:00:00"]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "timezone-aware" in captured.err


def test_init_cli_missing_target_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["init", "--root", str(tmp_path)])
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_init_cli_ships_only_the_init_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # SKILL.md: "/goals views ... in v0.2 they are agent-authored from that
    # layer -- the runtime ships no /goals subcommand". Lock the claim: any
    # non-"init" subcommand is an argument-parsing error (exit code 2).
    for args in (["goals"], ["goals", "show", "G-1"]):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(args)
        assert excinfo.value.code == 2
        capsys.readouterr()


# ---------------------------------------------------------------------------
# Error hierarchy sanity
# ---------------------------------------------------------------------------


def test_init_errors_are_planning_value_errors() -> None:
    for exc_type in (
        TargetValidationError,
        ProjectAlreadyInitializedError,
        ProjectNotInitializedError,
        TargetAlreadyRegisteredError,
        NonEmptyRootError,
    ):
        assert issubclass(exc_type, PlanningError)
        assert issubclass(exc_type, ValueError)


def test_init_registry_records_are_visible_through_the_state_backend(
    tmp_path: Path,
) -> None:
    """The AC-02 truth-source contract in the issue scenario.

    A goal the planning registry writes to ``goals/<goal_id>.json`` must
    be found and read by a ``FilesystemStateBackend`` over the workspace
    root: a worker that reads Core state exclusively through the backend
    sees exactly the records the registries write (one canonical layout,
    ``SCHEMA_TO_STATE_DIR``).
    """
    result = _init(tmp_path / "project", DOI)
    goal = register_goal(result.project_root, VALID_DOCS["goal"])
    assert goal.goal_id == VALID_DOCS["goal"]["goal_id"]

    # The registry wrote the canonical tree directory ...
    path = result.project_root / GOALS_STATE_DIR / f"{goal.goal_id}.json"
    assert path.is_file()
    # ... and the backend over the workspace root reads that same record.
    backend = FilesystemStateBackend(result.project_root)
    assert backend.list_ids("goal") == [goal.goal_id]
    stored = backend.read("goal", goal.goal_id)
    assert stored == json.loads(path.read_text(encoding="utf-8"))

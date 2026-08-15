"""The /reproduce command-line interface (DEV-M4-G01).

Implements the ``/reproduce`` CLI primitive of ``01-PRODUCT-REQUIREMENTS.md``
SS9 ("``/reproduce <PDF|DOI|URL>`` -- initialize project"): the ``init``
subcommand parses the primary target paper (a local PDF path, a DOI, or an
http(s) URL), initializes the one-paper project workspace and registers the
primary target through ``planning.init.initialize_project``.

Determinism
-----------
``--timestamp`` pins the state/event/commit records to an explicit
ISO-8601 value (default: now-UTC, as the frozen model requires for real
runs); the Git author/committer identity is pinned via ``--author-name`` /
``--author-email`` (the M3-G01 audit helpers never consult git config).
The command performs no network access and no inventory discovery.

Invocation
----------
``python -m scientific_reproduction.cli.reproduce init <target> [options]``

Exit codes: 0 on success; 1 for deterministic errors (malformed target,
already-initialized or non-empty root, invalid timestamp); 2 for
argument-parsing errors.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.planning.init import (
    DEFAULT_AUDIT_IDENTITY,
    DEFAULT_DOMAIN_PACK,
    PlanningError,
    initialize_project,
)

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the /reproduce argument parser (``prog="reproduce"``)."""
    parser = argparse.ArgumentParser(
        prog="reproduce",
        description=(
            "Initialize one-paper scientific reproduction projects and"
            " register the primary target paper."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser(
        "init",
        help="initialize a project and register its primary target paper",
    )
    init.add_argument(
        "target",
        help=(
            "the primary target paper: a local PDF path (ending in '.pdf'),"
            " a DOI (10.<registrant>/<suffix>), or an http(s) URL"
        ),
    )
    init.add_argument(
        "--root",
        default=".",
        help="project root directory (default: current directory)",
    )
    init.add_argument(
        "--project-id",
        dest="project_id",
        default=None,
        help="explicit project id (default: deterministic id derived from the target)",
    )
    init.add_argument(
        "--title", default=None, help="project title (default: none)"
    )
    init.add_argument(
        "--domain-pack",
        default=DEFAULT_DOMAIN_PACK,
        help=f"domain pack identifier (default: {DEFAULT_DOMAIN_PACK})",
    )
    init.add_argument(
        "--author-name",
        default=DEFAULT_AUDIT_IDENTITY.name,
        help=f"git author/committer name (default: {DEFAULT_AUDIT_IDENTITY.name})",
    )
    init.add_argument(
        "--author-email",
        default=DEFAULT_AUDIT_IDENTITY.email,
        help=f"git author/committer email (default: {DEFAULT_AUDIT_IDENTITY.email})",
    )
    init.add_argument(
        "--timestamp",
        default=None,
        help=(
            "timezone-aware ISO-8601 timestamp for state/event/commit records"
            " (default: now-UTC)"
        ),
    )
    init.add_argument(
        "--allow-non-empty-root",
        dest="allow_non_empty_root",
        action="store_true",
        help=(
            "allow initializing into a non-empty root directory (default:"
            " refuse; unrelated content would be dragged into the"
            " scientific audit history)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the /reproduce CLI; return the process exit code (0, 1, or 2).

    Args:
        argv: command-line arguments; ``None`` means ``sys.argv[1:]``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return _run_init(args)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2  # pragma: no cover


def _run_init(args: argparse.Namespace) -> int:
    """Execute ``reproduce init``; return the exit code."""
    try:
        timestamp = _parse_timestamp(args.timestamp)
        identity = AuditIdentity(name=args.author_name, email=args.author_email)
        result = initialize_project(
            Path(args.root),
            args.target,
            project_id=args.project_id,
            title=args.title,
            domain_pack=args.domain_pack,
            timestamp=timestamp,
            identity=identity,
            allow_non_empty_root=args.allow_non_empty_root,
        )
    except PlanningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    primary = result.project.primary_target
    print(f"initialized project {result.project.project_id} at {result.project_root}")
    print(
        "primary target:"
        f" {primary.source_type.value} {primary.identifier!r}"
    )
    print(
        f"git checkpoint: {result.commit.commit_sha[:12]}"
        f" ({result.commit.message})"
    )
    return 0


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse the --timestamp option (None means default now-UTC)."""
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(
            f"--timestamp must be timezone-aware ISO-8601, got {raw!r}"
        )
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())

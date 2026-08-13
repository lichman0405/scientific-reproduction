"""Shared test helpers for the analysis protocol tests (DEV-M9-G01).

``IDENTITY`` / ``TIMESTAMP`` pin every deterministic input the backing
``initialize_project`` call takes, so each test exercises the
deterministic path; ``FROZEN_AT`` is the fixed freeze timestamp every
``freeze_primary_protocol`` call uses (no wall clock anywhere).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    PrimaryOrExploratory,
)
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)

#: Deterministic author/committer identity used by every init behind the
#: analysis protocol tests.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp: every freeze in this suite is deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects
#: (``17-FDM201-REFERENCE-CASE.md``).
DOI = "10.1039/D5TA00771B"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_protocol(
    analysis_id: str,
    *,
    primary_or_exploratory: PrimaryOrExploratory = PrimaryOrExploratory.PRIMARY,
    kind: AnalysisKind = AnalysisKind.PROTOCOL,
    frozen: bool = False,
    protocol_version: str = INITIAL_PLAN_VERSION,
    **kwargs: Any,
) -> AnalysisProtocolOrResult:
    """Build a schema-valid draft analysis protocol with compact defaults."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=kind,
        protocol_version=protocol_version,
        primary_or_exploratory=primary_or_exploratory,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=frozen,
        methods=[{"name": "isotherm_fit"}],
        **kwargs,
    )


def make_result(
    analysis_id: str,
    *,
    primary_or_exploratory: PrimaryOrExploratory = PrimaryOrExploratory.EXPLORATORY,
    protocol_version: str = INITIAL_PLAN_VERSION,
    **kwargs: Any,
) -> AnalysisProtocolOrResult:
    """Build a schema-valid analysis result record (kind=RESULT)."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=AnalysisKind.RESULT,
        protocol_version=protocol_version,
        primary_or_exploratory=primary_or_exploratory,
        frozen=False,
        outputs=[{"metric": "batch_level_uptake"}],
        **kwargs,
    )

"""Shared test helpers for the planning/init tests (DEV-M4-G01).

``IDENTITY`` / ``TIMESTAMP`` pin every deterministic input the
initialization API takes (audit identity, state/event/commit timestamps),
so the tests exercise the deterministic path: same inputs in, same state
and same audit commit out -- no wall clock anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scientific_reproduction.audit.git import AuditIdentity

#: Deterministic author/committer identity used by every init test.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp: state records, the init event, and the git commit all
#: use this value; tests never depend on the wall clock.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: The exact ISO text the fixed timestamp formats to.
TIMESTAMP_ISO = "2026-01-01T00:00:00Z"

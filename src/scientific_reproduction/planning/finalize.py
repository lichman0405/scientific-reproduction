"""Project finalization gate: the completion counterpart of ``freeze_plan``.

``freeze_plan`` guards the transition into execution; ``finalize_project``
guards the transition into ``COMPLETED``. It closes the loop that the
audit pipeline (``reporting/audit.py``) is designed to check:

* every requirement must be adjudicated (no ``OPEN`` outcome) and the
  project outcome must be adjudicated (not ``UNDETERMINED``);
* every evidence record must carry ``used_by`` links (the AC-01
  "evidence -> requirement" hop);
* the machine-auditable package must assemble **and validate PASS**
  (AC-01/AC-02/AC-03) for every claim the project's evidence declares;
* the formal report PDF must exist in ``reports/``;
* the human-readable summary must be written (``reporting.human_summary``).

The gate is a hard constraint in the same spirit as the freeze
preconditions: a project cannot be finalized with open requirements or a
broken trace chain -- the errors name exactly what is missing so the
Supervisor can repair and re-run. On success the audit package and the
human summary are written to ``reports/`` and the project phase advances
to ``COMPLETED`` (append-only event; no git commit is created here, the
checkpoint is owned by the Supervisor flow, ``14-STATE-GIT-ARTIFACTS.md``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from scientific_reproduction.core.rules.lifecycle import ProjectPhase
from scientific_reproduction.reporting.audit import (
    build_audit_package,
    validate_package,
)
from scientific_reproduction.reporting.human_summary import write_human_summary_both
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.research.state_helpers import list_evidence

REPORTS_DIR = "reports"
AUDIT_PACKAGE_FILENAME = "reproduction-audit-package.json"
REPORT_PDF_FILENAME = "reproduction-report.pdf"


class FinalizationError(ValueError):
    """Base class for finalization gate errors."""


class FinalizationProhibitedError(FinalizationError):
    """The project cannot be finalized: name the offending items."""


@dataclass
class FinalizationCheck:
    """Deterministic view of the finalization gate (like a freeze audit).

    ``passed`` is the AND of all checks; ``missing`` lists the exact
    items that must be repaired, mirroring ``FreezeProhibitedError``
    messages so the Supervisor gets the same information from the check
    and from the raised error.
    """

    open_requirements: list[str] = field(default_factory=list)
    evidence_without_used_by: list[str] = field(default_factory=list)
    audit_validation_errors: list[str] = field(default_factory=list)
    report_pdf_missing: bool = False
    summary_missing: bool = False
    outcome_undetermined: bool = False
    artifact_content_mismatches: list[str] = field(default_factory=list)
    unrecorded_adjudications: list[str] = field(default_factory=list)
    # human-readable delivery gate (v0.3.0, local): the zh summary must be
    # fresh (rendered after the last requirement adjudication) and every
    # closed requirement must carry an author-supplied statement_zh when
    # the deliverable language is zh -- the renderer falls back to English,
    # but a zh deliverable with untranslated claims is a defect, not a
    # display preference.
    summary_stale: bool = False
    zh_claims_missing: list[str] = field(default_factory=list)
    # evidence-interpretation gates awaiting the human's close-out
    # confirmation (v0.3.1 local): an OPEN gate means the run proceeded
    # under the recorded default_safe_action; the human confirms once
    # (approve/reject/cancel) before COMPLETED.
    open_human_gates: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not (
            self.open_requirements
            or self.evidence_without_used_by
            or self.audit_validation_errors
            or self.report_pdf_missing
            or self.summary_missing
            or self.outcome_undetermined
            or self.artifact_content_mismatches
            or self.unrecorded_adjudications
            or self.summary_stale
            or self.zh_claims_missing
            or self.open_human_gates
        )

    def missing_items(self) -> list[str]:
        items: list[str] = []
        if self.open_requirements:
            items.append(
                "requirement(s) still OPEN: " + ", ".join(self.open_requirements)
            )
        if self.evidence_without_used_by:
            items.append(
                "evidence record(s) without used_by links: "
                + ", ".join(self.evidence_without_used_by)
            )
        if self.audit_validation_errors:
            items.append(
                "audit package validation failed: "
                + "; ".join(self.audit_validation_errors[:5])
            )
        if self.report_pdf_missing:
            items.append(f"{REPORT_PDF_FILENAME} missing in reports/")
        if self.summary_missing:
            items.append("human-readable summary not generated")
        if self.outcome_undetermined:
            items.append(
                "reproduction_outcome still UNDETERMINED - the Supervisor must "
                "adjudicate the project outcome before COMPLETED"
            )
        if self.artifact_content_mismatches:
            items.append(
                "artifact content mismatch (manifest SHA vs file on disk): "
                + ", ".join(self.artifact_content_mismatches[:5])
            )
        if self.unrecorded_adjudications:
            items.append(
                "requirement(s) closed without a requirement.outcome.updated "
                "event (close via close_requirement): "
                + ", ".join(self.unrecorded_adjudications[:5])
            )
        if self.summary_stale:
            items.append(
                "human summary (复现结果摘要) predates the last requirement "
                "adjudication - re-render the summary before finalizing"
            )
        if self.zh_claims_missing:
            items.append(
                "zh deliverable: closed requirement(s) without author-supplied "
                "statement_zh (translate before finalizing): "
                + ", ".join(self.zh_claims_missing[:5])
            )
        if self.open_human_gates:
            items.append(
                "human gate(s) still OPEN (resolve or cancel before COMPLETED)"
                ": " + ", ".join(self.open_human_gates[:5])
            )
        return items


def _key_claims(root: Path) -> list[str]:
    """The project's declared claims: every evidence claim_id (deduped)."""
    return sorted({e.claim_id for e in list_evidence(root)})


def check_finalization(
    root: str | Path,
    evidence: EvidenceRegistry | None = None,
    *,
    language: str = "en",
) -> FinalizationCheck:
    """Evaluate the finalization gate without writing anything.

    Pure and deterministic: reads only registered state and returns the
    check view. ``passed`` False means ``finalize_project`` would raise.
    """
    root_path = Path(root)
    check = FinalizationCheck()

    # 0. project outcome adjudicated (UNDETERMINED blocks COMPLETED)
    project_path = root_path / "project.yaml"
    if project_path.is_file():
        try:
            project = json.loads(project_path.read_text(encoding="utf-8"))
            if project.get("reproduction_outcome") in (None, "UNDETERMINED"):
                check.outcome_undetermined = True
        except (OSError, json.JSONDecodeError):
            check.outcome_undetermined = True

    # 1. every requirement adjudicated (U6: and adjudicated through the
    #    sanctioned API -- close_requirement emits the audit event)
    import hashlib as _hashlib
    adjudication_events: set[str] = set()
    for ev in (root_path / "events").glob("sr_event_*.json"):
        try:
            rec = json.loads(ev.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("event_type") == "requirement.outcome.updated":
            oid = rec.get("object_id")
            if oid:
                adjudication_events.add(oid)
    for p in sorted((root_path / "requirements").glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rid = rec.get("requirement_id", p.stem)
        outcome = rec.get("outcome")
        if outcome in (None, "OPEN"):
            check.open_requirements.append(rid)
        elif rid not in adjudication_events:
            check.unrecorded_adjudications.append(rid)

    # 1b. (U2) artifact content integrity: every registered manifest must
    #     point at an existing file whose SHA-256 matches the record --
    #     a file overwritten after registration (re-runs, copies) silently
    #     drifts otherwise.
    for mf in sorted((root_path / "manifests").glob("*.json")):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        uri = m.get("uri")
        rec_sha = m.get("sha256")
        if not uri or not rec_sha:
            continue
        target = (root_path / str(uri).replace("/", os.sep).replace("\\", os.sep))
        if not target.is_file():
            check.artifact_content_mismatches.append(
                f"{m.get('artifact_id', mf.stem)} (file missing: {uri})"
            )
            continue
        h = _hashlib.sha256()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != rec_sha:
            check.artifact_content_mismatches.append(
                f"{m.get('artifact_id', mf.stem)} (SHA drift: {uri})"
            )

    # 2. every evidence carries used_by links that resolve to registered
    #    requirements / acceptances / goals (the AC-01 hop must be complete
    #    and non-dangling at completion time; registration stays permissive
    #    so research may precede planning)
    evs = list_evidence(root_path)
    reg = evidence or EvidenceRegistry.from_records(evs)
    for e in evs:
        used = getattr(e, "used_by", None) or []
        dangling = [
            ref
            for ref in used
            if not (
                (root_path / "requirements" / f"{ref}.json").is_file()
                or (root_path / "acceptance" / f"{ref}.json").is_file()
                or (root_path / "goals" / f"{ref}.json").is_file()
            )
        ]
        if not used:
            check.evidence_without_used_by.append(e.evidence_id)
        elif dangling:
            check.evidence_without_used_by.append(
                f"{e.evidence_id} (dangling used_by: {', '.join(dangling)})"
            )

    # 3. audit package assembles and validates PASS for all claims
    claims = _key_claims(root_path)
    if claims:
        try:
            result = validate_package(root_path, reg, claims)
            import re as _re
            m = _re.search(r"passed=(\w+)", repr(result))
            if not (m and m.group(1) == "True"):
                msgs = _re.findall(
                    r"ValidationError\(kind=<ValidationErrorKind\.(\w+):[^>]+>, "
                    r"claim_id='([^']+)'",
                    repr(result),
                )
                check.audit_validation_errors = [
                    f"{kind} on claim {claim}" for kind, claim in msgs[:8]
                ]
        except Exception as exc:  # noqa: BLE001 - the gate must never crash
            check.audit_validation_errors.append(f"validation raised: {exc}")

    # 4. formal report PDF exists
    last_adjudication = ""
    for ev in sorted((root_path / "events").glob("sr_event_*.json")):
        try:
            rec = json.loads(ev.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("event_type") == "requirement.outcome.updated":
            ts = rec.get("timestamp", "")
            if ts > last_adjudication:
                last_adjudication = ts
    if not (root_path / REPORTS_DIR / REPORT_PDF_FILENAME).is_file():
        check.report_pdf_missing = True
    elif (root_path / REPORTS_DIR / "reproduction-report.json").is_file():
        # 4b. freshness: the report must postdate the last requirement
        #     adjudication (a stale report renders OPEN/UNDETERMINED state)
        try:
            sidecar = json.loads(
                (root_path / REPORTS_DIR / "reproduction-report.json")
                .read_text(encoding="utf-8")
            )
            report_at = sidecar.get("generated_at", "")
            if last_adjudication and report_at < last_adjudication:
                check.report_pdf_missing = False  # file exists...
                check.audit_validation_errors.append(  # ...but is stale
                    f"report generated_at {report_at} predates the last "
                    f"requirement adjudication ({last_adjudication}) - "
                    "re-render the report before finalizing"
                )
        except (OSError, json.JSONDecodeError):
            check.audit_validation_errors.append("report sidecar unreadable")

    # 5. human summary present and fresh (v0.3.0 human-readable delivery
    #    gate: a summary rendered before the last adjudication is stale)
    from scientific_reproduction.reporting.human_summary import SUMMARY_FILENAME
    summary_md = root_path / REPORTS_DIR / SUMMARY_FILENAME
    if not summary_md.is_file():
        check.summary_missing = True
    else:
        import re as _sre
        head = ""
        try:
            head = summary_md.read_text(encoding="utf-8")[:400]
        except OSError:
            head = ""
        m = _sre.search(r"(?:generated|生成于)\s*[:：]?\s*([0-9TZ:.+-]+)", head)
        summary_at = m.group(1) if m else ""
        if last_adjudication and (
            not summary_at or summary_at < last_adjudication
        ):
            check.summary_stale = True

    # 5b. zh deliverable completeness: closed requirements must carry an
    #     author-supplied statement_zh (never machine-translated); the
    #     renderer would fall back to English silently otherwise.
    if language == "zh":
        for p_req in sorted((root_path / "requirements").glob("*.json")):
            try:
                rec = json.loads(p_req.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            outcome = rec.get("outcome")
            if outcome in (None, "OPEN"):
                continue
            if not str(rec.get("statement_zh") or "").strip():
                check.zh_claims_missing.append(
                    rec.get("requirement_id", p_req.stem)
                )

    # 6. human gates: an OPEN gate blocks COMPLETED -- the single close-out
    #    confirmation (the run continued under the recorded
    #    default_safe_action; confirm/reject/cancel the gate now).
    for p_gate in sorted((root_path / "human-gates").glob("*.json")):
        try:
            rec = json.loads(p_gate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("status") == "OPEN":
            check.open_human_gates.append(str(rec.get("gate_id") or p_gate.stem))

    return check


def finalize_project(
    root: str | Path,
    *,
    generated_at: str,
    language: str = "zh",
    actor: str,
    reason: str = "",
    evidence: EvidenceRegistry | None = None,
) -> Path:
    """Run the finalization gate; on PASS write artifacts and close the project.

    Args:
        root: initialized project workspace root.
        generated_at: injected timestamp (determinism).
        language: language key for the human-readable summary.
        actor: recording actor stamped on the phase event.
        reason: optional reason recorded in the phase event payload.
        evidence: optional evidence registry (defaults to the project's).

    Returns:
        The audit package path written to ``reports/``.

    Raises:
        FinalizationProhibitedError: the gate fails; the message names the
            exact missing/offending items (no record is written).
        ProjectNotInitializedError: no ``project.yaml`` at root.
    """
    root_path = Path(root)
    check = check_finalization(root_path, evidence=evidence, language=language)
    if not check.passed:
        raise FinalizationProhibitedError(
            "project finalization is prohibited: "
            + "; ".join(check.missing_items())
        )

    # 1. write the audit package (validated above)
    evs = list_evidence(root_path)
    reg = evidence or EvidenceRegistry.from_records(evs)
    claims = _key_claims(root_path)
    pkg = build_audit_package(root_path, reg, claims)
    reports = root_path / REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    audit_path = reports / AUDIT_PACKAGE_FILENAME
    audit_path.write_text(
        json.dumps(json.loads(json.dumps(pkg, default=str, ensure_ascii=False)),
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 2. human-readable summary (Markdown + PDF)
    write_human_summary_both(root_path, generated_at=generated_at, language=language)

    # 3. advance phase to COMPLETED (append-only event; no git commit here)
    from scientific_reproduction.core.events import ProjectEventLog
    from scientific_reproduction.core.models import ProjectEvent
    project_path = root_path / "project.yaml"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    previous = project.get("project_phase")
    project["project_phase"] = ProjectPhase.COMPLETED.value
    project["updated_at"] = generated_at
    project_path.write_text(
        json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log = ProjectEventLog(root_path)  # backend maps obj_type "event" -> events/
    import hashlib
    event_id = "sr_event_" + hashlib.sha256(
        f"project.finalized:{generated_at}".encode()
    ).hexdigest()[:32]
    event = ProjectEvent(
        event_id=event_id,
        timestamp=generated_at,
        actor=actor,
        event_type="project.finalized",
        object_id=project.get("project_id"),
        from_=previous,
        to=ProjectPhase.COMPLETED.value,
        reason=reason or None,
        payload={},
    )
    log.append(event, idempotency_key=f"project.finalized:{project.get('project_id')}")
    return audit_path

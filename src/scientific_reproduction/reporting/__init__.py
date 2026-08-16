"""reporting subsystem package.

Deterministic human-readable and machine-auditable projections of the
registered project state: the reproduction report (``report.py``,
DEV-M13-G02), the outcome summary (``summary.py``), the machine-auditable
audit package (``audit.py``), claim traceability (``traceability.py``),
the designed Plan document renderer (``plan_doc.py``, issue #105), the
operator-facing execution sheets (``sheets``, issue #106), the
experiment sheet's first-class deterministic PDF rendering
(``sheet_pdf``, issue #122) and the final reproduction report PDF
renderer (``pdf_report``, issue #107, built on the shared deterministic
PDF visual system in ``rendering``). Language is an explicit renderer
input (issue #122): the template strings come from the injected
``TemplatePack`` resolved from the ``language`` key (default ``"en"``,
``resolve_pack``) -- never from locale detection, so ``(state,
language)`` maps to byte-identical output. Every module is pure and
deterministic: reads go through the real registration APIs and the only
durable writes are the rendered document artifacts under ``reports/``
(``plan_doc.write_plan_document`` and its SHA-256 checksum sidecar, the
execution sheets, ``sheet_pdf.build_experiment_sheet_pdf``,
``pdf_report.build_pdf_report`` and their canonical JSON sidecars).
"""

from scientific_reproduction.reporting.audit import (
    AuditCorruptError,
    AuditNotInitializedError,
    AuditPackage,
    ReportFile,
    build_audit_package,
)
from scientific_reproduction.reporting.language import (
    AVAILABLE_LANGUAGES,
    EN_PACK,
    ZH_PACK,
    TemplatePack,
    resolve_pack,
)
from scientific_reproduction.reporting.pdf_report import (
    PdfReport,
    PdfReportCorruptError,
    PdfReportError,
    PdfReportNotInitializedError,
    PdfReportSection,
    build_pdf_report,
)
from scientific_reproduction.reporting.sheet_pdf import (
    ExperimentSheetPdf,
    build_experiment_sheet_pdf,
    render_experiment_sheet_pdf,
)
from scientific_reproduction.reporting.summary import (
    SummaryCorruptError,
    SummaryNotInitializedError,
    build_summary,
)

__all__ = [
    "AVAILABLE_LANGUAGES",
    "AuditCorruptError",
    "AuditNotInitializedError",
    "AuditPackage",
    "EN_PACK",
    "ExperimentSheetPdf",
    "PdfReport",
    "PdfReportCorruptError",
    "PdfReportError",
    "PdfReportNotInitializedError",
    "PdfReportSection",
    "ReportFile",
    "SummaryCorruptError",
    "SummaryNotInitializedError",
    "TemplatePack",
    "ZH_PACK",
    "build_audit_package",
    "build_experiment_sheet_pdf",
    "build_pdf_report",
    "build_summary",
    "render_experiment_sheet_pdf",
    "resolve_pack",
]

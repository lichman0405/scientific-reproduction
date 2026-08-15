"""reporting subsystem package.

Deterministic human-readable and machine-auditable projections of the
registered project state: the reproduction report (``report.py``,
DEV-M13-G02), the outcome summary (``summary.py``), the machine-auditable
audit package (``audit.py``), claim traceability (``traceability.py``),
the designed Plan document renderer (``plan_doc.py``, issue #105), the
operator-facing execution sheets (``sheets``, issue #106) and the final
reproduction report PDF renderer (``pdf_report``, issue #107, built on
the shared deterministic PDF visual system in ``rendering``). Every
module is pure and deterministic: reads go through the real registration
APIs and the only durable writes are the rendered document artifacts
under ``reports/`` (``plan_doc.write_plan_document`` and its SHA-256
checksum sidecar, the execution sheets, ``pdf_report.build_pdf_report``
and its canonical JSON sidecar).
"""

from scientific_reproduction.reporting.audit import (
    AuditCorruptError,
    AuditNotInitializedError,
    AuditPackage,
    ReportFile,
    build_audit_package,
)
from scientific_reproduction.reporting.pdf_report import (
    PdfReport,
    PdfReportCorruptError,
    PdfReportError,
    PdfReportNotInitializedError,
    PdfReportSection,
    build_pdf_report,
)
from scientific_reproduction.reporting.summary import (
    SummaryCorruptError,
    SummaryNotInitializedError,
    build_summary,
)

__all__ = [
    "AuditCorruptError",
    "AuditNotInitializedError",
    "AuditPackage",
    "PdfReport",
    "PdfReportCorruptError",
    "PdfReportError",
    "PdfReportNotInitializedError",
    "PdfReportSection",
    "ReportFile",
    "SummaryCorruptError",
    "SummaryNotInitializedError",
    "build_audit_package",
    "build_pdf_report",
    "build_summary",
]

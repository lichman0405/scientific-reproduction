"""reporting subsystem package.

Outcome aggregation (``summary``), the machine-auditable audit package
(``audit``) and the final reproduction report PDF renderer
(``pdf_report``, issue #107).
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

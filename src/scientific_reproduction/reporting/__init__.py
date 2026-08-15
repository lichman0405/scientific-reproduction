"""
reporting subsystem package.

Deterministic human-readable and machine-auditable projections of the
registered project state: the reproduction report (``report.py``,
DEV-M13-G02), the outcome summary (``summary.py``), the machine-auditable
audit package (``audit.py``), claim traceability (``traceability.py``)
and the designed Plan document renderer (``plan_doc.py``, issue #105).
Every module is pure and deterministic: reads go through the real
registration APIs and the only durable writes are the rendered document
artifacts under ``reports/`` (``plan_doc.write_plan_document`` and its
SHA-256 checksum sidecar).
"""

__all__: list[str] = []


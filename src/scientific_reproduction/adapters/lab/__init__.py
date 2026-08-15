"""adapters/lab subsystem: the LabAdapter contract and its v0.1 reference
implementation (DEV-M7-G01).

* ``base.py`` -- the **LabAdapter interface**: the four conceptual
  operations of 15-ADAPTER-SPEC.md SS2 (``dispatch`` / ``status`` /
  ``collect`` / ``request_missing_result``) and the frozen record types;
* ``manifest.py`` -- the **manifest validation** deliverable: the pure
  decision record that detects missing required result-manifest entries,
  missing declared data files and uncovered required returns, and
  refuses returned packages whose run reference is absent or mismatched
  (AC-02/AC-03);
* ``filesystem.py`` -- the **filesystem adapter** deliverable: the
  filesystem/manual handoff reference implementation
  (``lab/outgoing/<RUN_ID>/`` / ``lab/incoming/<RUN_ID>/`` per
  10-EXPERIMENT-SUBSYSTEM.md SS2) with the real schema gate on dispatch
  (AC-01).

All paths are injected; adapters are deterministic and pure given the
injected paths and inputs (no randomness, no wall clock, no network).
"""

from scientific_reproduction.adapters.lab.base import (
    LAB_ADAPTER_CONTRACT_VERSION,
    CollectionRejectedError,
    CollectionResult,
    DispatchNotFoundError,
    DispatchRecord,
    DispatchState,
    DispatchStatus,
    DuplicateDispatchError,
    LabAdapter,
    LabAdapterDataError,
    LabAdapterError,
    LabExecutionPackageInput,
    MissingResultRequest,
    ResultNotAvailableError,
    is_safe_path_segment,
)
from scientific_reproduction.adapters.lab.filesystem import (
    DISPATCH_RECORD_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    INCOMING_DIR_NAME,
    MISSING_RESULT_REQUEST_FILENAME,
    OUTGOING_DIR_NAME,
    RESULT_MANIFEST_FILENAME,
    FilesystemLabAdapter,
)
from scientific_reproduction.adapters.lab.manifest import (
    RESULT_MANIFEST_REQUIRED_FIELDS,
    RESULT_MANIFEST_RETURN_MAPPING_KEY,
    RESULT_MANIFEST_RULESET_VERSION,
    RESULT_MANIFEST_VERSION,
    LabResultManifest,
    ResultManifestAssessment,
    ResultManifestError,
    evaluate_result_manifest,
)

__all__ = [
    "LAB_ADAPTER_CONTRACT_VERSION",
    "DISPATCH_RECORD_FILENAME",
    "EXECUTION_MANIFEST_FILENAME",
    "INCOMING_DIR_NAME",
    "MISSING_RESULT_REQUEST_FILENAME",
    "OUTGOING_DIR_NAME",
    "RESULT_MANIFEST_FILENAME",
    "RESULT_MANIFEST_REQUIRED_FIELDS",
    "RESULT_MANIFEST_RETURN_MAPPING_KEY",
    "RESULT_MANIFEST_RULESET_VERSION",
    "RESULT_MANIFEST_VERSION",
    "CollectionRejectedError",
    "CollectionResult",
    "DispatchNotFoundError",
    "DispatchRecord",
    "DispatchState",
    "DispatchStatus",
    "DuplicateDispatchError",
    "FilesystemLabAdapter",
    "LabAdapter",
    "LabAdapterDataError",
    "LabAdapterError",
    "LabExecutionPackageInput",
    "LabResultManifest",
    "MissingResultRequest",
    "ResultManifestAssessment",
    "ResultManifestError",
    "ResultNotAvailableError",
    "evaluate_result_manifest",
    "is_safe_path_segment",
]

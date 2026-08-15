"""planning subsystem package (DEV-M4-G01).

Implements the deterministic one-paper project initialization primitives:
``init.initialize_project`` (workspace tree + ``project.yaml`` + Git audit
checkpoint, without any lab/HPC inventory), ``init.register_primary_target``
(the one-primary enforcement point), and the PDF/DOI/URL target-form parser
(``init.parse_target_form`` / ``init.detect_target_form``).
"""

from __future__ import annotations

from scientific_reproduction.planning.init import (
    DEFAULT_AUDIT_IDENTITY,
    DEFAULT_DOMAIN_PACK,
    DEFAULT_STATE_BACKEND,
    INIT_DIRECTORIES,
    INIT_EVENT_ACTOR,
    INIT_EVENT_TYPE,
    INITIAL_PLAN_VERSION,
    PROJECT_STATE_FILENAME,
    NonEmptyRootError,
    PlanningError,
    ProjectAlreadyInitializedError,
    ProjectInitResult,
    ProjectNotInitializedError,
    TargetAlreadyRegisteredError,
    TargetInput,
    TargetValidationError,
    detect_target_form,
    initialize_project,
    parse_target_form,
    read_project_state,
    register_primary_target,
)

__all__ = [
    "DEFAULT_AUDIT_IDENTITY",
    "DEFAULT_DOMAIN_PACK",
    "DEFAULT_STATE_BACKEND",
    "INIT_DIRECTORIES",
    "INIT_EVENT_ACTOR",
    "INIT_EVENT_TYPE",
    "INITIAL_PLAN_VERSION",
    "PROJECT_STATE_FILENAME",
    "NonEmptyRootError",
    "PlanningError",
    "ProjectAlreadyInitializedError",
    "ProjectInitResult",
    "ProjectNotInitializedError",
    "TargetAlreadyRegisteredError",
    "TargetValidationError",
    "TargetInput",
    "detect_target_form",
    "initialize_project",
    "parse_target_form",
    "read_project_state",
    "register_primary_target",
]

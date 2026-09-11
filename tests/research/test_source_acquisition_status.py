"""Tests for first-class acquisition status on source records (issue #134).

The frozen ``ResearchSource`` model and ``schemas/source.schema.json``
carry an ``acquisition_status`` (``REGISTERED`` / ``OBTAINED`` /
``PARTIAL`` / ``UNAVAILABLE``, harmonized with the FDM-201 benchmark
acquisition-log vocabulary) plus an optional stable
``unavailability_reason`` (``paywall`` / ``bot_block`` / ``not_found`` /
``access_gate_eligible``) and prose ``unavailability_detail``. Coverage:

* every acquisition outcome registers, persists and reads back
  deterministically through ``register_source`` / ``read_source``;
* defaults preserve existing registrations: records without the new
  fields load as ``REGISTERED`` (identity only) and re-serialize
  unchanged -- the default serializes as an absent key, so the
  persisted canonical JSON of a legacy registration gains no new keys;
* the ``source.recorded`` event payload carries ``acquisition_status``
  always and ``unavailability_reason`` / ``unavailability_detail`` when
  set (both the fresh registration and the crash-window replay path);
* the read API (``read_source`` / ``list_sources``) exposes the fields;
* unknown acquisition vocabulary is rejected before anything is written.

The deterministic path follows the house suites: every fixture pins the
identity/timestamp of ``initialize_project``, and all timestamps and
actors are injected (no wall clock anywhere).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.models import (
    AcquisitionStatus,
    ResearchSource,
    SourceType,
    UnavailabilityReason,
)
from scientific_reproduction.planning.init import initialize_project
from scientific_reproduction.research.state_helpers import (
    EVENTS_STATE_DIR,
    SOURCE_RECORDED_EVENT_TYPE,
    SOURCE_STATE_DIR,
    list_sources,
    read_source,
    register_source,
)

# ---------------------------------------------------------------------------
# Fixtures (deterministic: pinned identity/timestamp, injected stamps)
# ---------------------------------------------------------------------------

#: Deterministic author/committer identity for the initialized project.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects.
DOI = "10.1039/D5TA00771B"

#: Injected actor and recording stamps (no wall clock anywhere).
ACTOR = "research"
RECORDED_AT = "2026-01-02T00:00:00Z"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def event_log(root: Path) -> ProjectEventLog:
    """The canonical workspace event log (records at ``events/``)."""
    return ProjectEventLog(root)


def make_source(
    source_id: str = "SRC-1",
    *,
    source_type: SourceType = SourceType.TARGET_PAPER,
    doi: str | None = DOI,
    title: str | None = None,
    acquisition_status: AcquisitionStatus = AcquisitionStatus.REGISTERED,
    unavailability_reason: UnavailabilityReason | None = None,
    unavailability_detail: str | None = None,
    local_artifact_id: str | None = None,
) -> ResearchSource:
    """Build a schema-valid source record with compact defaults."""
    return ResearchSource(
        source_id=source_id,
        source_type=source_type,
        title=title or "The FDM-201 reference paper",
        provenance="acquisition:test",
        doi=doi,
        acquisition_status=acquisition_status,
        unavailability_reason=unavailability_reason,
        unavailability_detail=unavailability_detail,
        local_artifact_id=local_artifact_id,
    )


def legacy_mapping(source_id: str = "SRC-1") -> dict:
    """A schema-shaped source mapping without the acquisition fields,
    exactly the shape registrations had before issue #134."""
    return {
        "source_id": source_id,
        "source_type": "target_paper",
        "title": "The FDM-201 reference paper",
        "provenance": "acquisition:test",
        "doi": DOI,
        "access_class": "PUBLIC",
    }


def stored_record(root: Path, source_id: str) -> dict:
    """The persisted canonical JSON of one registered source."""
    return json.loads(
        (root / SOURCE_STATE_DIR / f"{source_id}.json").read_text(
            encoding="utf-8"
        )
    )


# ---------------------------------------------------------------------------
# Every acquisition outcome is representable, deterministically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kwargs", "serialized"),
    [
        (
            AcquisitionStatus.REGISTERED,
            {},
            # The REGISTERED default serializes as an absent key: legacy
            # registrations re-serialize unchanged (see the defaults test).
            {"present": False},
        ),
        (
            AcquisitionStatus.OBTAINED,
            {"local_artifact_id": "art-1"},
            {"present": True},
        ),
        (AcquisitionStatus.PARTIAL, {}, {"present": True}),
        (
            AcquisitionStatus.UNAVAILABLE,
            {
                "unavailability_reason": UnavailabilityReason.BOT_BLOCK,
                "unavailability_detail": (
                    "publisher returns HTTP 403 to automated fetch"
                ),
            },
            {"present": True},
        ),
    ],
    ids=["registered", "obtained", "partial", "unavailable"],
)
def test_each_acquisition_outcome_registers_persists_and_reads_back(
    tmp_path, status, kwargs, serialized
):
    root = init_project(tmp_path)
    registration = register_source(
        root,
        make_source(acquisition_status=status, **kwargs),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )
    assert registration.source.acquisition_status is status
    raw = stored_record(root, "SRC-1")
    if serialized["present"]:
        assert raw["acquisition_status"] == status.value
    else:
        assert "acquisition_status" not in raw
    if "unavailability_reason" in kwargs:
        assert (
            raw["unavailability_reason"] == kwargs["unavailability_reason"].value
        )
    if "unavailability_detail" in kwargs:
        assert raw["unavailability_detail"] == kwargs["unavailability_detail"]
    if "local_artifact_id" in kwargs:
        assert raw["local_artifact_id"] == kwargs["local_artifact_id"]
    # The typed read re-resolves the same frozen record.
    assert read_source(root, "SRC-1") == registration.source


# ---------------------------------------------------------------------------
# Defaults preserve existing registrations (issue #134 requirement 3)
# ---------------------------------------------------------------------------


def test_legacy_record_loads_as_registered_and_reserializes_unchanged(
    tmp_path,
):
    root = init_project(tmp_path)
    legacy = legacy_mapping()
    registration = register_source(
        root, legacy, actor=ACTOR, recorded_at=RECORDED_AT
    )
    # Records without the fields load as REGISTERED (identity only).
    assert registration.source.acquisition_status is AcquisitionStatus.REGISTERED
    assert registration.source.unavailability_reason is None
    assert registration.source.unavailability_detail is None
    # ... and re-serialize unchanged: the persisted canonical JSON gains
    # no new keys, so a legacy registration round-trips byte-identical
    # field sets.
    assert stored_record(root, "SRC-1") == legacy
    # The re-loaded record still resolves to REGISTERED.
    assert read_source(root, "SRC-1") == registration.source
    assert read_source(root, "SRC-1").acquisition_status is AcquisitionStatus.REGISTERED


# ---------------------------------------------------------------------------
# The source.recorded event carries the acquisition fields
# ---------------------------------------------------------------------------


def test_recorded_event_payload_carries_the_acquisition_fields(tmp_path):
    root = init_project(tmp_path)
    registration = register_source(
        root,
        make_source(
            acquisition_status=AcquisitionStatus.UNAVAILABLE,
            unavailability_reason=UnavailabilityReason.NOT_FOUND,
            unavailability_detail=(
                "no CSD/CCDC deposition located at registration"
            ),
        ),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )
    record = registration.event_record
    assert record is not None and record.replayed is False
    event = record.event
    assert event.event_type == SOURCE_RECORDED_EVENT_TYPE
    assert event.object_id == "SRC-1"
    assert event.payload == {
        "acquisition_status": "UNAVAILABLE",
        "unavailability_reason": "not_found",
        "unavailability_detail": "no CSD/CCDC deposition located at registration",
    }
    # The persisted event record carries the same payload.
    raw_event = json.loads(
        (root / EVENTS_STATE_DIR / f"{event.event_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw_event["payload"] == event.payload
    assert event_log(root).get(event.event_id) is not None


def test_recorded_event_payload_of_a_legacy_record_is_registered(tmp_path):
    root = init_project(tmp_path)
    registration = register_source(
        root, legacy_mapping(), actor=ACTOR, recorded_at=RECORDED_AT
    )
    assert registration.event_record is not None
    # The audit trail always carries the explicit status, even when the
    # record itself serializes the REGISTERED default as absence.
    assert registration.event_record.event.payload == {
        "acquisition_status": "REGISTERED"
    }


def test_replay_event_payload_reflects_the_stored_record(tmp_path):
    # Crash window: the record write landed (an OBTAINED source), the
    # event append did not. The converging re-run appends the missing
    # deterministic event; its payload is a pure function of the stored
    # record, not of the re-run's input model.
    root = init_project(tmp_path)
    log = event_log(root)
    obtained = make_source(
        acquisition_status=AcquisitionStatus.OBTAINED,
        local_artifact_id="art-1",
    )
    (root / SOURCE_STATE_DIR / "SRC-1.json").parent.mkdir(
        parents=True, exist_ok=True
    )
    (root / SOURCE_STATE_DIR / "SRC-1.json").write_text(
        json.dumps(obtained.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    registration = register_source(
        root,
        make_source(),  # legacy-shaped re-run input
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=log,
    )
    assert registration.replayed is True
    assert registration.source.acquisition_status is AcquisitionStatus.OBTAINED
    assert registration.event_record is not None
    assert registration.event_record.event.payload == {
        "acquisition_status": "OBTAINED"
    }


# ---------------------------------------------------------------------------
# Read API and rejection of unknown vocabulary
# ---------------------------------------------------------------------------


def test_read_api_exposes_the_acquisition_fields(tmp_path):
    root = init_project(tmp_path)
    register_source(root, legacy_mapping("SRC-1"), actor=ACTOR, recorded_at=RECORDED_AT)
    register_source(
        root,
        {
            "source_id": "SRC-2",
            "source_type": "target_paper",
            "title": "A paywalled reference",
            "provenance": "acquisition:test",
            "acquisition_status": "UNAVAILABLE",
            "unavailability_reason": "paywall",
            "unavailability_detail": "publisher page requires subscription",
        },
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )
    listed = {source.source_id: source for source in list_sources(root)}
    assert listed["SRC-1"].acquisition_status is AcquisitionStatus.REGISTERED
    assert listed["SRC-1"].unavailability_reason is None
    assert listed["SRC-1"].unavailability_detail is None
    assert listed["SRC-2"].acquisition_status is AcquisitionStatus.UNAVAILABLE
    assert listed["SRC-2"].unavailability_reason is UnavailabilityReason.PAYWALL
    assert listed["SRC-2"].unavailability_detail == (
        "publisher page requires subscription"
    )


def test_unknown_acquisition_vocabulary_is_rejected_before_any_write(tmp_path):
    root = init_project(tmp_path)
    bad_status = legacy_mapping()
    bad_status["acquisition_status"] = "BOGUS_STATUS"
    with pytest.raises(ValueError):
        register_source(root, bad_status, actor=ACTOR, recorded_at=RECORDED_AT)
    bad_reason = legacy_mapping()
    bad_reason["acquisition_status"] = "UNAVAILABLE"
    bad_reason["unavailability_reason"] = "bogus_reason"
    with pytest.raises(ValueError):
        register_source(root, bad_reason, actor=ACTOR, recorded_at=RECORDED_AT)
    assert list_sources(root) == ()

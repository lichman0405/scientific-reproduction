"""Status and missing-result request tests (DEV-M7-G01).

``status`` reports the observable dispatch state (``RUNNING_EXTERNAL``
after the outgoing handoff, ``RESULT_AVAILABLE`` once a Result Package
is detected in the incoming handoff -- the 10-EXPERIMENT-SUBSYSTEM.md
SS1 flow). ``request_missing_result`` records a canonical missing-result
request into the dispatch directory. Both only touch the injected
handoff paths.
"""

from __future__ import annotations

import json

import pytest

from scientific_reproduction.adapters.lab.base import (
    DispatchNotFoundError,
    DispatchState,
    LabAdapterDataError,
    MissingResultRequest,
)
from scientific_reproduction.adapters.lab.filesystem import (
    MISSING_RESULT_REQUEST_FILENAME,
)
from tests.adapters.lab.lab_helpers import (
    RUN_ID,
    make_package,
    make_result_manifest,
    write_result_package,
)

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_running_external_after_dispatch(handoff):
    # After the outgoing handoff the Run is RUNNING_EXTERNAL
    # (10-EXPERIMENT-SUBSYSTEM.md SS1: "LabAdapter -> outgoing handoff;
    # Run -> RUNNING_EXTERNAL").
    base, adapter = handoff
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    status = adapter.status(dispatch_id)
    assert status.dispatch_id == dispatch_id
    assert status.run_id == RUN_ID
    assert status.state is DispatchState.RUNNING_EXTERNAL


def test_status_result_available_when_result_detected(handoff):
    # Once a Result Package is detected in the incoming handoff the Run
    # is RESULT_AVAILABLE -- the Execution Monitor signal.
    base, adapter = handoff
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    write_result_package(
        base, RUN_ID, make_result_manifest(), files={"raw-data.csv": "data\n"}
    )
    status = adapter.status(dispatch_id)
    assert status.state is DispatchState.RESULT_AVAILABLE


def test_status_unknown_dispatch_raises(handoff):
    base, adapter = handoff
    assert not (base / "outgoing").exists()
    with pytest.raises(DispatchNotFoundError) as exc:
        adapter.status("sr_dispatch_unknown_00000000000000000000000000000000")
    assert "no dispatch with id" in str(exc.value)


def test_status_ignores_unrelated_handoff_directories(handoff):
    # The dispatch scan only reads dispatch-record files the adapter
    # itself wrote; unrelated directories and files are ignored.
    base, adapter = handoff
    (base / "outgoing").mkdir(parents=True)
    (base / "outgoing" / "other-run").mkdir()
    (base / "outgoing" / "other-run" / "manifest.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(DispatchNotFoundError):
        adapter.status("sr_dispatch_unknown_00000000000000000000000000000000")
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    assert adapter.status(dispatch_id).state is DispatchState.RUNNING_EXTERNAL


# ---------------------------------------------------------------------------
# request_missing_result
# ---------------------------------------------------------------------------


def test_request_missing_result_writes_request_file(handoff):
    # The request is recorded into the dispatch directory with canonical
    # sorted requirements and returned as a frozen record.
    base, adapter = handoff
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    request = adapter.request_missing_result(
        dispatch_id, ["log.csv", "raw-data.csv"], requested_at="2026-08-14T01:00:00Z"
    )
    assert isinstance(request, MissingResultRequest)
    assert request.dispatch_id == dispatch_id
    assert request.manifest_requirements == ("log.csv", "raw-data.csv")
    assert request.requested_at == "2026-08-14T01:00:00Z"
    path = base / "outgoing" / RUN_ID / MISSING_RESULT_REQUEST_FILENAME
    assert request.request_path == str(path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["dispatch_id"] == dispatch_id
    assert stored["manifest_requirements"] == ["log.csv", "raw-data.csv"]


def test_request_missing_result_canonicalizes_requirements(handoff):
    # Requirements are canonicalized to sorted order -- identical
    # request sets produce identical request files.
    base, adapter = handoff
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    first = adapter.request_missing_result(dispatch_id, ["b.csv", "a.csv"])
    second = adapter.request_missing_result(dispatch_id, ["a.csv", "b.csv"])
    assert first.manifest_requirements == ("a.csv", "b.csv")
    assert second.manifest_requirements == first.manifest_requirements
    path = base / "outgoing" / RUN_ID / MISSING_RESULT_REQUEST_FILENAME
    assert json.loads(path.read_text(encoding="utf-8"))["manifest_requirements"] == [
        "a.csv",
        "b.csv",
    ]


def test_request_missing_result_rejects_malformed_requirements(handoff):
    base, adapter = handoff
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    with pytest.raises(LabAdapterDataError) as exc:
        adapter.request_missing_result(dispatch_id, ["raw-data.csv", ""])
    assert "non-empty" in str(exc.value)
    with pytest.raises(LabAdapterDataError) as exc:
        adapter.request_missing_result(dispatch_id, ["raw-data.csv", "raw-data.csv"])
    assert "duplicate" in str(exc.value)
    with pytest.raises(TypeError):
        adapter.request_missing_result(dispatch_id, [7])  # type: ignore[list-item]
    with pytest.raises(LabAdapterDataError):
        adapter.request_missing_result(dispatch_id, [], requested_at="  ")
    path = base / "outgoing" / RUN_ID / MISSING_RESULT_REQUEST_FILENAME
    assert not path.exists()


def test_request_missing_result_unknown_dispatch_raises(handoff):
    base, adapter = handoff
    assert not (base / "outgoing").exists()
    with pytest.raises(DispatchNotFoundError) as exc:
        adapter.request_missing_result(
            "sr_dispatch_unknown_00000000000000000000000000000000", ["raw-data.csv"]
        )
    assert "no dispatch with id" in str(exc.value)


def test_request_missing_result_only_touches_the_dispatch_directory(handoff):
    # The request is written next to the dispatch it belongs to; nothing
    # appears in the incoming handoff.
    base, adapter = handoff
    dispatch_id = adapter.dispatch(make_package()).dispatch_id
    adapter.request_missing_result(dispatch_id, ["log.csv"])
    assert (
        base / "outgoing" / RUN_ID / MISSING_RESULT_REQUEST_FILENAME
    ).is_file()
    assert not (base / "incoming").exists()

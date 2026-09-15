"""Content-free operations fault contract and cursor gates."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from bluelab.modules.operations.faults import FaultCursor, fault_view
from bluelab.modules.operations.schemas import ResolveFaultRequest
from bluelab.platform.errors.denial import ProblemError

pytestmark = [pytest.mark.l1_unit]

FAULT_ID = UUID("01900000-0000-7000-8000-000000000421")
ORG_ID = UUID("01900000-0000-7000-8000-000000000422")
ATTEMPT_ID = UUID("01900000-0000-7000-8000-000000000423")


@pytest.mark.verifies("FR-SCR-009", "FR-SCR-013", "SEC-040")
def test_fault_projection_allowlists_content_free_detail() -> None:
    view = fault_view(
        {
            "id": FAULT_ID,
            "org_id": ORG_ID,
            "kind": "grading_failure",
            "attempt_id": ATTEMPT_ID,
            "status": "open",
            "detail": {
                "error_class": "grading_retry_exhausted",
                "retry_count": 5,
                "transcript": "must never render",
                "provider_response": "must never render",
            },
            "opened_at": datetime(2026, 9, 14, tzinfo=UTC),
            "resolved_at": None,
            "resolution_note": None,
        }
    )

    assert view.detail == {
        "error_class": "grading_retry_exhausted",
        "retry_count": 5,
    }
    assert "transcript" not in repr(view.model_dump())


@pytest.mark.verifies("FR-SCR-009", "SEC-040")
def test_fault_cursor_is_bound_to_status_filter_and_rejects_empty_input() -> None:
    cursor = FaultCursor(
        status="open",
        opened_at=datetime(2026, 9, 14, tzinfo=UTC),
        fault_id=FAULT_ID,
    ).encode()

    assert FaultCursor.decode(cursor, status="open").fault_id == FAULT_ID
    with pytest.raises(ProblemError):
        FaultCursor.decode(cursor, status="resolved")
    with pytest.raises(ProblemError):
        FaultCursor.decode("", status="open")


@pytest.mark.verifies("SEC-040")
def test_fault_resolution_body_is_closed_and_reason_is_required() -> None:
    assert ResolveFaultRequest(reason="operator approved re-drive").reason
    with pytest.raises(ValueError):
        ResolveFaultRequest(reason="ok", attempt_id=ATTEMPT_ID)
    with pytest.raises(ValueError):
        ResolveFaultRequest(reason="")

"""Deterministic internal-seam vectors shared independently with the agent."""

from typing import Any

import pytest
from pydantic import ValidationError

from bluelab.platform.security.agent_signature import sign, verify
from bluelab_runtime_bundle import Participant, PersonaSections, RuntimeBundle, Scenario


def test_canonical_signature_vector() -> None:
    signature = sign(
        "shared-agent-secret",
        method="POST",
        path="/internal/calls/call-1/completion",
        timestamp=1_800_000_000,
        raw_body=b"abc",
    )
    assert signature == "448db71d802237718798ac0a44f367de6d40c649b445488ee37156a5c98b158e"
    assert verify(
        "shared-agent-secret",
        method="POST",
        path="/internal/calls/call-1/completion",
        raw_body=b"abc",
        presented=signature,
        timestamp_header="1800000000",
        now_epoch=1_800_000_001,
    )
    assert not verify(
        "shared-agent-secret",
        method="POST",
        path="/internal/calls/other/completion",
        raw_body=b"abc",
        presented=signature,
        timestamp_header="1800000000",
        now_epoch=1_800_000_001,
    )


def test_test_call_bundle_has_no_attempt_id() -> None:
    values: dict[str, Any] = {
        "call_id": "call-1",
        "mode": "test",
        "attempt_id": None,
        "drill_id": "drill-1",
        "org_id": "org-1",
        "team_id": "team-1",
        "participant": Participant(kind="author", identity="acct-1", display_name="Author"),
        "scenario": Scenario(
            buyer_name="Mona",
            buyer_role="Buyer",
            buyer_company="Example Co",
            situation_context="Test call",
            participant_product_summary="Participant-safe summary",
        ),
        "persona": PersonaSections(
            who_you_are="A buyer",
            your_world="A Cairo company",
            where_you_are_right_now="Ready",
        ),
        "language": "ar-EG",
        "call_type": "discovery",
        "lead_type": "cold_outreach",
        "voice_identity": "egyptian-female-1",
    }
    assert RuntimeBundle(**values).attempt_id is None
    values["attempt_id"] = "must-not-exist"
    with pytest.raises(ValidationError):
        RuntimeBundle(**values)

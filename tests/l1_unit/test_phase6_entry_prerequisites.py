"""Focused invariant tests for Phase 6's unblocked entry prerequisites."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from bluelab.modules.assessment.schemas import AssessmentView, StageBriefView
from bluelab.modules.hiring.service import parse_candidate_csv
from bluelab.notifications.templates import render_e2
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.http.idempotency import (
    StoredResponse,
    check_replay,
    fingerprint,
    require_key,
)
from bluelab.platform.security.tokens import (
    StoredCandidateToken,
    TokenInvalid,
    TokenOutcome,
    hash_token,
    mint_token,
    verify_candidate_token,
)


@pytest.mark.l1_unit
@pytest.mark.verifies("FR-HIR-006", "FR-HIR-008")
def test_idempotency_key_is_scoped_and_canonical() -> None:
    """The same JSON content has one fingerprint within one manager endpoint."""
    org_id = uuid4()
    manager_id = uuid4()
    key = require_key(
        " replay-key ",
        org_id=org_id,
        principal_id=manager_id,
        endpoint="POST /positions/{position_id}/candidates",
    )
    assert key.storage_key == (
        f"{org_id}:{manager_id}:POST /positions/{{position_id}}/candidates:replay-key"
    )
    assert fingerprint({"candidates": [{"email": "a@example.test", "name": "A"}]}) == fingerprint(
        {"candidates": [{"name": "A", "email": "a@example.test"}]}
    )


@pytest.mark.l1_unit
@pytest.mark.verifies("FR-HIR-006", "FR-HIR-008")
def test_replay_rejects_a_changed_batch() -> None:
    """A repeated key cannot silently perform a different candidate batch."""
    stored = StoredResponse(status=201, body={"created": []}, fingerprint=fingerprint({"a": 1}))
    with pytest.raises(ProblemError) as error:
        check_replay(stored, body={"a": 2})
    assert error.value.problem is catalog.IDEMPOTENCY_KEY_REUSE


@pytest.mark.l1_unit
@pytest.mark.verifies("FR-IDA-011", "FR-IDA-012", "FR-IDA-013")
def test_candidate_token_is_bound_and_expiry_is_distinct() -> None:
    """A live token resolves to one candidate binding and expiry is explicit."""
    token = mint_token()
    at = datetime.now(UTC)
    stored = StoredCandidateToken(
        token_hash=hash_token(token),
        org_id=uuid4(),
        team_id=uuid4(),
        position_id=uuid4(),
        candidate_id=uuid4(),
        expires_at=at + timedelta(minutes=1),
    )
    binding = verify_candidate_token(token, stored, at=at)
    assert binding.candidate_id == stored.candidate_id
    with pytest.raises(TokenInvalid) as expired:
        verify_candidate_token(token, stored, at=stored.expires_at)
    assert expired.value.outcome is TokenOutcome.EXPIRED


@pytest.mark.l1_unit
@pytest.mark.verifies("FR-HIR-007", "SEC-010")
def test_candidate_csv_stays_inert_until_the_reviewed_json_batch() -> None:
    """Spreadsheet formula syntax stays literal review text, never an action."""
    rows = parse_candidate_csv(
        b'name,email,phone,linkedin,source\n"=HYPERLINK(""https://bad"")",a@example.test,,,Referral\n',
        content_type="text/csv",
    )
    assert rows[0].name == '=HYPERLINK("https://bad")'
    assert rows[0].email == "a@example.test"
    assert rows[0].problems == []


@pytest.mark.l1_unit
@pytest.mark.verifies("FR-HIR-008", "FR-IDA-011", "SEC-023")
def test_e2_uses_a_fragment_capability_and_escapes_the_template() -> None:
    message = render_e2(
        email_send_id=uuid4(),
        recipient="candidate@example.test",
        recipient_name="Candidate",
        org_name="BlueLab",
        position_title="AE",
        expires_at="2026-10-01T00:00:00+00:00",
        token="opaque-token",
        template="Welcome <candidate>\nBring headphones.",
        sender="hello@example.test",
        public_app_url="https://app.example.test",
    )
    assert "#token=opaque-token" in message.text_body
    assert "?token=" not in message.text_body
    assert "&lt;candidate&gt;" in message.html_body
    assert "<br>Bring headphones." in message.html_body


@pytest.mark.l1_unit
@pytest.mark.verifies("FR-CND-004", "AC-CND-003")
def test_candidate_schemas_have_stage_order_but_no_evaluation_fields() -> None:
    assert "stages" in AssessmentView.model_fields
    assert "score" not in AssessmentView.model_fields
    assert "review" not in AssessmentView.model_fields
    assert "author_recap" not in StageBriefView.model_fields

"""Confirmed service terms cross the ops API, independent head, and DB projection."""

from datetime import UTC, datetime, timedelta

import pytest
from tests.l3_integration.operations.conftest import OPS_PASSWORD, OPS_SEED

from bluelab.adapters.lifecycle_history import (
    HistoryConflict,
    HistoryUnverified,
    LifecycleHistory,
)
from bluelab.platform.ids import new_id
from bluelab.platform.security.totp import STEP_SECONDS, code_for_step
from bluelab.work.org_terms import transition_due

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[bytes, str]] = {}
        self.version = 0

    async def read(self, key: str) -> tuple[bytes, str] | None:
        return self.items.get(key)

    async def put(self, key: str, body: bytes, *, expected_etag: str | None) -> None:
        current = self.items.get(key)
        if (current is None and expected_etag is not None) or (
            current is not None and current[1] != expected_etag
        ):
            raise HistoryConflict("conditional write rejected")
        self.version += 1
        self.items[key] = (body, str(self.version))


async def test_operator_confirms_and_extends_term_with_stable_replay(
    ops_client, ops_world, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    created = await ops_client.post(
        "/ops/v1/orgs",
        json={"name": "Service Org", "registered_domain": "service.example.com",
              "timezone": "UTC", "reason": "approved contract"},
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    shell = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert shell.status_code == 200, shell.text
    assert shell.json()["access_status"] == "unconfigured"
    assert shell.json()["service_term_enforced"] is True
    default_policy = await ops_client.get(f"/ops/v1/orgs/{org_id}/retention-policy")
    assert default_policy.status_code == 200, default_policy.text
    assert default_policy.json()["source"] == "default"
    assert default_policy.json()["period_value"] == 90

    start = datetime.now(UTC).date() + timedelta(days=1)
    last = start + timedelta(days=30)
    preview = await ops_client.post(
        "/ops/v1/service-term-preview",
        json={"timezone": "UTC", "service_start_on": start.isoformat(),
              "service_last_access_on": last.isoformat(), "period_value": 90,
              "period_unit": "elapsed_days"},
    )
    assert preview.status_code == 200, preview.text
    operation_id = str(new_id())
    command = {
        "expected_sequence": 0, "service_start_on": start.isoformat(),
        "service_last_access_on": last.isoformat(),
        "contract_reference": "contract-1", "reason": "approved dates",
    }
    path = f"/ops/v1/orgs/{org_id}/service-terms"
    confirmed = await ops_client.post(
        path, json=command, headers={"Lifecycle-Operation-Id": operation_id}
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["lifecycle_sequence"] == 1
    assert body["access_status"] == "scheduled"
    assert body["service_starts_at"] == preview.json()["service_starts_at"]
    assert body["projected_purge_eligible_at"] == preview.json()["projected_purge_eligible_at"]
    replayed = await ops_client.post(
        path, json=command, headers={"Lifecycle-Operation-Id": operation_id}
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["service_term_id"] == body["service_term_id"]
    reused = await ops_client.post(
        path, json={**command, "contract_reference": "different"},
        headers={"Lifecycle-Operation-Id": operation_id},
    )
    assert reused.status_code == 409
    assert reused.json()["type"].endswith("/operation-id-reuse")

    renewal = await ops_client.post(
        path,
        json={**command, "expected_sequence": 1,
              "service_last_access_on": (last + timedelta(days=30)).isoformat(),
              "reason": "approved extension"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert renewal.status_code == 200, renewal.text
    assert renewal.json()["lifecycle_sequence"] == 2
    assert renewal.json()["service_start_on"] == start.isoformat()
    assert renewal.json()["service_ends_at"] > body["service_ends_at"]


async def test_create_with_term_is_one_replayable_operator_decision(
    ops_client, ops_world, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    start = datetime.now(UTC).date() + timedelta(days=1)
    payload = {
        "name": "Bundled Org", "registered_domain": "bundled.example.com",
        "timezone": "UTC", "reason": "signed agreement",
        "service_start_on": start.isoformat(),
        "service_last_access_on": (start + timedelta(days=30)).isoformat(),
        "contract_reference": "agreement-1",
    }
    operation_id = str(new_id())
    headers = {"Lifecycle-Operation-Id": operation_id}
    created = await ops_client.post("/ops/v1/orgs", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json()["service_starts_at"] is not None
    replay = await ops_client.post("/ops/v1/orgs", json=payload, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json()["org_id"] == created.json()["org_id"]
    changed = await ops_client.post(
        "/ops/v1/orgs", json={**payload, "contract_reference": "other"},
        headers=headers,
    )
    assert changed.status_code == 409, changed.text
    without_id = await ops_client.post("/ops/v1/orgs", json=payload)
    assert without_id.status_code == 422, without_id.text


async def test_expired_term_transitions_from_persisted_cutoff_and_renews_during_hold(
    ops_client, ops_world, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    last = datetime.now(UTC).date() - timedelta(days=2)
    start = last - timedelta(days=30)
    created = await ops_client.post(
        "/ops/v1/orgs",
        json={
            "name": "Due Org", "registered_domain": "due.example.com",
            "timezone": "UTC", "reason": "signed term",
            "service_start_on": start.isoformat(),
            "service_last_access_on": last.isoformat(),
            "contract_reference": "term-1",
        },
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    sweep = await transition_due(history)
    assert sweep["transitioned"] >= 1
    held = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert held.status_code == 200, held.text
    hold = held.json()
    assert hold["lifecycle_status"] == "offboarding"
    assert hold["offboarding_started_at"] == created.json()["service_ends_at"]
    assert hold["purge_eligible_at"] == created.json()["projected_purge_eligible_at"]
    assert hold["lifecycle_sequence"] == 2
    second = await transition_due(history)
    assert second["transitioned"] == 0

    next_start = datetime.now(UTC).date() + timedelta(days=1)
    renewed = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/service-terms",
        json={
            "expected_sequence": hold["lifecycle_sequence"],
            "current_offboarding_id": hold["offboarding_id"],
            "service_start_on": next_start.isoformat(),
            "service_last_access_on": (next_start + timedelta(days=30)).isoformat(),
            "contract_reference": "term-2", "reason": "signed renewal",
        },
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["lifecycle_status"] == "active"
    assert renewed.json()["access_status"] == "scheduled"
    assert renewed.json()["offboarding_id"] is None
    assert renewed.json()["lifecycle_sequence"] == 3


async def test_pending_operator_term_recovers_after_uncertain_history_write(
    ops_client, ops_world, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    created = await ops_client.post(
        "/ops/v1/orgs", json={
            "name": "Recover Org", "registered_domain": "recover.example.com",
            "timezone": "UTC", "reason": "signed agreement",
        },
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    start = datetime.now(UTC).date() + timedelta(days=1)
    original_append = history.append

    async def unavailable(**_kwargs):
        raise HistoryUnverified("store outage")

    monkeypatch.setattr(history, "append", unavailable)
    requested = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/service-terms",
        json={
            "expected_sequence": 0, "service_start_on": start.isoformat(),
            "service_last_access_on": (start + timedelta(days=30)).isoformat(),
            "contract_reference": "agreement", "reason": "approved term",
        },
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert requested.status_code == 503, requested.text
    monkeypatch.setattr(history, "append", original_append)
    recovered = await transition_due(history)
    assert recovered["recovered"] >= 1
    view = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert view.status_code == 200, view.text
    assert view.json()["lifecycle_sequence"] == 1
    assert view.json()["access_status"] == "scheduled"

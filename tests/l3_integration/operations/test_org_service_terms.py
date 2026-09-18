"""Confirmed service terms cross the ops API, independent head, and DB projection."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.l3_integration.operations.conftest import APP_URL, OPS_PASSWORD, OPS_SEED

from bluelab.adapters.lifecycle_history import (
    HistoryConflict,
    HistoryUnverified,
    LifecycleHistory,
)
from bluelab.platform.db.privileged import ops_scope
from bluelab.platform.db.scope import ScopeContext, apply_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.ids import new_id
from bluelab.platform.security.totp import STEP_SECONDS, code_for_step
from bluelab.work import org_terms
from bluelab.work.org_terms import transition_due

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]


async def _no_calls(_org_id: UUID, _since: datetime) -> None:
    """The fixture has no LiveKit rooms; production passes provider quiescence."""


@pytest_asyncio.fixture
async def maintenance_scope(monkeypatch) -> AsyncIterator[None]:
    """Exercise scheduler discovery with its dedicated non-bypass role."""
    engine = create_async_engine(make_url(APP_URL).set(username="bluelab_maintenance"))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(scope: ScopeContext) -> AsyncIterator[AsyncSession]:
        async with maker() as session, session.begin():
            await apply_scope(session, scope)
            yield session

    monkeypatch.setattr(org_terms, "scoped_transaction", transaction)
    yield
    await engine.dispose()


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[bytes, str]] = {}
        self.version = 0

    async def read(self, key: str) -> tuple[bytes, str] | None:
        return self.items.get(key)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.items if key.startswith(prefix))

    async def put(self, key: str, body: bytes, *, expected_etag: str | None) -> None:
        current = self.items.get(key)
        if (current is None and expected_etag is not None) or (
            current is not None and current[1] != expected_etag
        ):
            raise HistoryConflict("conditional write rejected")
        self.version += 1
        self.items[key] = (body, str(self.version))


@pytest.mark.verifies("FR-IDA-015")
async def test_operator_confirms_and_extends_term_with_stable_replay(
    ops_client, ops_world, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    unauthorized = await ops_client.post(
        "/ops/v1/orgs", json={"name": "Denied Org", "registered_domain": "denied.example.com",
                              "timezone": "UTC", "reason": "no operator session"},
    )
    assert unauthorized.status_code == 401
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
    missing_reason = await ops_client.post(
        path, json={key: value for key, value in command.items() if key != "reason"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert missing_reason.status_code == 422
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


@pytest.mark.verifies("FR-IDA-015", "FR-IDA-016")
async def test_expired_term_transitions_from_persisted_cutoff_and_renews_during_hold(
    ops_client, ops_world, monkeypatch, maintenance_scope
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
    sweep = await transition_due(history, quiesce=_no_calls)
    assert sweep["transitioned"] >= 1
    held = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert held.status_code == 200, held.text
    hold = held.json()
    assert hold["lifecycle_status"] == "offboarding"
    assert hold["offboarding_started_at"] == created.json()["service_ends_at"]
    assert hold["purge_eligible_at"] == created.json()["projected_purge_eligible_at"]
    assert hold["lifecycle_sequence"] == 2
    second = await transition_due(history, quiesce=_no_calls)
    assert second["transitioned"] == 0

    extended_deadline = (
        datetime.fromisoformat(hold["purge_eligible_at"]) + timedelta(days=30)
    ).isoformat()
    extension_id = str(new_id())
    extension_path = (
        f"/ops/v1/orgs/{org_id}/offboarding/{hold['offboarding_id']}/deadline"
    )
    extension_command = {
        "expected_sequence": hold["lifecycle_sequence"],
        "purge_eligible_at": extended_deadline,
        "reason": "approved longer hold",
    }
    extended = await ops_client.post(
        extension_path, json=extension_command,
        headers={"Lifecycle-Operation-Id": extension_id},
    )
    assert extended.status_code == 200, extended.text
    assert extended.json()["purge_eligible_at"] == extended_deadline.replace("+00:00", "Z")
    extension_replay = await ops_client.post(
        extension_path,
        json={
            **extension_command,
            "purge_eligible_at": datetime.fromisoformat(extended_deadline)
            .astimezone(timezone(timedelta(hours=2))).isoformat(),
        },
        headers={"Lifecycle-Operation-Id": extension_id},
    )
    assert extension_replay.status_code == 200, extension_replay.text
    assert extension_replay.json()["lifecycle_sequence"] == 3
    held = extended.json()

    next_start = datetime.now(UTC).date() + timedelta(days=1)
    renewed = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/service-terms",
        json={
            "expected_sequence": held["lifecycle_sequence"],
            "current_offboarding_id": held["offboarding_id"],
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
    assert renewed.json()["lifecycle_sequence"] == 4
    stale = await ops_client.post(
        extension_path,
        json={**extension_command, "expected_sequence": 4},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert stale.status_code == 409, stale.text


async def test_operator_restriction_serializes_with_hold_and_requires_evidence(
    ops_client, ops_world, monkeypatch, maintenance_scope, operations_engine
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
    created = await ops_client.post(
        "/ops/v1/orgs",
        json={
            "name": "Restricted Org", "registered_domain": "restricted.example.com",
            "timezone": "UTC", "reason": "signed term",
            "service_start_on": (last - timedelta(days=30)).isoformat(),
            "service_last_access_on": last.isoformat(),
            "contract_reference": "term-1",
        },
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    await transition_due(history, quiesce=_no_calls)
    held = (await ops_client.get(f"/ops/v1/orgs/{org_id}")).json()
    path = f"/ops/v1/orgs/{org_id}/offboarding/{held['offboarding_id']}/restrictions"
    command = {
        "expected_sequence": held["lifecycle_sequence"], "scope": "legal_hold",
        "reason": "documented legal hold", "authority_ref": "case-42",
        "release_condition": "case_closed",
    }
    operation_id = str(new_id())
    created_restriction = await ops_client.post(
        path, json=command, headers={"Lifecycle-Operation-Id": operation_id},
    )
    assert created_restriction.status_code == 200, created_restriction.text
    restriction = created_restriction.json()
    assert restriction["status"] == "active"
    assert restriction["offboarding_id"] == held["offboarding_id"]
    replay = await ops_client.post(
        path, json=command, headers={"Lifecycle-Operation-Id": operation_id},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == restriction["id"]
    current = (await ops_client.get(f"/ops/v1/orgs/{org_id}")).json()
    assert current["lifecycle_sequence"] == held["lifecycle_sequence"] + 1
    assert any(item["id"] == restriction["id"] for item in current["restrictions"])
    release_path = f"/ops/v1/orgs/{org_id}/restrictions/{restriction['id']}/release"
    missing_evidence = await ops_client.post(
        release_path,
        json={"expected_sequence": current["lifecycle_sequence"], "reason": "case closed"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert missing_evidence.status_code == 422
    released = await ops_client.post(
        release_path,
        json={"expected_sequence": current["lifecycle_sequence"],
              "reason": "case closed", "evidence_reference": "closure-42"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "released"
    assert released.json()["released_at"] is not None
    run_id, step_id = new_id(), new_id()
    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await db.execute(text("""
            insert into org_purge_run
                (id,org_id,service_term_id,offboarding_id,initiating_operator_id,
                 offboarding_started_at,purge_eligible_at,retention_policy_reference,
                 status,execution_epoch,purge_started_at,verification_summary)
            values (:id,:org,:term,:episode,:actor,:started,:deadline,:policy,
                    'running',1,now(),cast(:summary as jsonb))
        """), {
            "id": run_id, "org": UUID(org_id),
            "term": UUID(held["service_term_id"]),
            "episode": UUID(held["offboarding_id"]),
            "actor": ops_world.ops_account_id,
            "started": datetime.fromisoformat(held["offboarding_started_at"]),
            "deadline": datetime.fromisoformat(held["purge_eligible_at"]),
            "policy": held["retention_policy_reference"],
            "summary": '{"objects_absent":true}',
        })
        await db.execute(text("""
            insert into org_purge_step
                (id,purge_run_id,step_key,batch_key,batch_manifest_key,
                 batch_manifest_digest,status,execution_epoch,authorized_at,
                 completed_at,deleted_count)
            values (:id,:run,'objects','batch-1','manifest-1','digest-1',
                    'completed',1,now(),now(),2)
        """), {"id": step_id, "run": run_id})
    purge = await ops_client.get(
        f"/ops/v1/orgs/{org_id}/offboarding/{held['offboarding_id']}/purge"
    )
    assert purge.status_code == 200, purge.text
    assert purge.json()["purge_run_id"] == str(run_id)
    assert purge.json()["destructive_started"] is True
    assert purge.json()["steps"][0]["deleted_count"] == 2


@pytest.mark.verifies("FR-IDA-016", "FR-LIV-017")
async def test_early_termination_fences_before_call_drain_and_cancellation_restores_term(
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
    start = datetime.now(UTC).date() - timedelta(days=1)
    last = start + timedelta(days=30)
    created = await ops_client.post(
        "/ops/v1/orgs",
        json={
            "name": "Early Offboard Org", "registered_domain": "early.example.com",
            "timezone": "UTC", "reason": "signed term",
            "service_start_on": start.isoformat(),
            "service_last_access_on": last.isoformat(),
            "contract_reference": "term-early",
        },
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    deactivated_manager = new_id()
    async with scoped_transaction(ops_scope(ops_account_id=ops_world.ops_account_id)) as db:
        await db.execute(text(
            "insert into account(id,org_id,team_id,email,display_name,role,password_hash,status) "
            "values(:id,:org,:id,:email,'Inactive manager','manager','x','deactivated')"
        ), {"id": deactivated_manager, "org": UUID(org_id),
            "email": f"inactive-{deactivated_manager}@early.example.com"})
    operation_id = str(new_id())
    observations: list[str] = []

    async def quiesce(target: UUID, *, since: datetime, valkey, settings) -> None:
        assert str(target) == org_id
        assert since.tzinfo is not None
        pending = await ops_client.get(f"/ops/v1/orgs/{org_id}")
        assert pending.status_code == 200, pending.text
        assert pending.json()["pending_operation_id"] == operation_id
        observations.append("fenced")

    monkeypatch.setattr(ops_v1, "quiesce_org_calls", quiesce)
    started = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/offboarding",
        json={"expected_sequence": 1, "reason": "customer requested early end"},
        headers={"Lifecycle-Operation-Id": operation_id},
    )
    assert started.status_code == 200, started.text
    assert observations == ["fenced"]
    hold = started.json()
    assert hold["lifecycle_status"] == "offboarding"
    assert hold["service_ends_at"] == hold["offboarding_started_at"]
    assert hold["lifecycle_sequence"] == 2
    assert hold["purge_eligible_at"] is not None
    cancelled = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/offboarding/{hold['offboarding_id']}/cancel",
        json={"expected_sequence": 2, "reason": "contract reinstated"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["lifecycle_status"] == "active"
    assert cancelled.json()["offboarding_id"] is None
    assert cancelled.json()["service_ends_at"] == created.json()["service_ends_at"]
    assert cancelled.json()["access_status"] == "available"
    async with scoped_transaction(ops_scope(ops_account_id=ops_world.ops_account_id)) as db:
        state = (await db.execute(text(
            "select status,team_id from account where id=:id"
        ), {"id": deactivated_manager})).one()
    assert tuple(state) == ("deactivated", deactivated_manager)

    # The confirmed term can end while the provider drains established rooms.
    # That unaccepted early-end command must retire so ordinary expiry can win.
    from bluelab.lifecycle import service as lifecycle_service

    actual_now = lifecycle_service.now
    term_end = datetime.fromisoformat(created.json()["service_ends_at"])

    async def drain_past_term_end(target: UUID, *, since: datetime, valkey, settings) -> None:
        assert str(target) == org_id
        monkeypatch.setattr(lifecycle_service, "now", lambda: term_end)

    monkeypatch.setattr(ops_v1, "quiesce_org_calls", drain_past_term_end)
    expired_operation = str(new_id())
    expired_start = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/offboarding",
        json={"expected_sequence": 3, "reason": "end service before term expiry"},
        headers={"Lifecycle-Operation-Id": expired_operation},
    )
    monkeypatch.setattr(lifecycle_service, "now", actual_now)
    assert expired_start.status_code == 422, expired_start.text
    async with scoped_transaction(ops_scope(ops_account_id=ops_world.ops_account_id)) as db:
        status = await db.scalar(
            text("select status from org_lifecycle_operation where id=:id"),
            {"id": UUID(expired_operation)},
        )
    assert status == "rejected"


async def test_pending_operator_term_recovers_after_uncertain_history_write(
    ops_client, ops_world, monkeypatch, maintenance_scope
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
    recovered = await transition_due(history, quiesce=_no_calls)
    assert recovered["recovered"] >= 1
    view = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert view.status_code == 200, view.text
    assert view.json()["lifecycle_sequence"] == 1
    assert view.json()["access_status"] == "scheduled"


async def test_term_conflict_after_accepted_append_returns_recorded_outcome(
    ops_client, ops_world, monkeypatch
) -> None:
    """A lost append response cannot reject an independently accepted term."""
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    assert (await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )).status_code == 200
    created = await ops_client.post(
        "/ops/v1/orgs", json={
            "name": "Accepted Retry Org", "registered_domain": "accepted-retry.example.com",
            "timezone": "UTC", "reason": "approved contract",
        },
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    original_append = history.append

    async def accepted_then_conflict(**kwargs):
        await original_append(**kwargs)
        raise HistoryConflict("response lost after accepted append")

    monkeypatch.setattr(history, "append", accepted_then_conflict)
    start = datetime.now(UTC).date() + timedelta(days=1)
    operation_id = str(new_id())
    command = {
        "expected_sequence": 0, "service_start_on": start.isoformat(),
        "service_last_access_on": (start + timedelta(days=30)).isoformat(),
        "contract_reference": "agreement", "reason": "approved term",
    }
    response = await ops_client.post(
        f"/ops/v1/orgs/{org_id}/service-terms", json=command,
        headers={"Lifecycle-Operation-Id": operation_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["lifecycle_sequence"] == 1
    assert await history.accepted(UUID(org_id), UUID(operation_id)) is not None


async def test_policy_revision_keeps_confirmed_term_deadline(
    ops_client, ops_world, monkeypatch, maintenance_scope
) -> None:
    """A later policy cannot silently rewrite a term's applied retention."""
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    assert (await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )).status_code == 200
    start = datetime.now(UTC).date() - timedelta(days=31)
    created = await ops_client.post(
        "/ops/v1/orgs", json={
            "name": "Policy Revision Org", "registered_domain": "policy-revision.example.com",
            "timezone": "UTC", "reason": "signed first contract",
            "service_start_on": start.isoformat(),
            "service_last_access_on": (start + timedelta(days=30)).isoformat(),
            "contract_reference": "contract-1",
        }, headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["org_id"]
    before = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert before.status_code == 200, before.text
    deadline = before.json()["projected_purge_eligible_at"]
    revised = await ops_client.put(
        f"/ops/v1/orgs/{org_id}/retention-policy", json={
            "expected_sequence": 1, "policy_reference": "contract-2:180d",
            "contract_reference": "contract-2", "period_value": 180,
            "period_unit": "elapsed_days", "reason": "approved amendment",
        }, headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["period_value"] == 180
    assert revised.json()["lifecycle_sequence"] == 2
    after = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert after.status_code == 200, after.text
    assert after.json()["projected_purge_eligible_at"] == deadline
    assert (await transition_due(history, quiesce=_no_calls))["transitioned"] >= 1
    held = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert held.status_code == 200, held.text
    assert held.json()["purge_eligible_at"] == deadline

"""Replay rollback-lost lifecycle decisions in their real dependency order."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bluelab.adapters.lifecycle_history import HistoryConflict, LifecycleHistory
from bluelab.lifecycle import restrictions, service, subject_requests
from bluelab.lifecycle.restore import RestoreUnverified, replay_organization
from bluelab.modules.operations.schemas import (
    OrgDeadlineCommand,
    OrgLifecycleCommand,
    OrgRestrictionCommand,
    OrgRestrictionReleaseCommand,
    OrgRetentionPolicyCommand,
    OrgServiceTermCommand,
)
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.ids import new_id


class _HistoryStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[bytes, str]] = {}
        self.version = 0

    async def read(self, key: str):
        return self.items.get(key)

    async def list_keys(self, prefix: str):
        return sorted(key for key in self.items if key.startswith(prefix))

    async def put(self, key: str, body: bytes, *, expected_etag: str | None):
        current = self.items.get(key)
        if (current is None and expected_etag is not None) or (
            current is not None and current[1] != expected_etag
        ):
            raise HistoryConflict("conditional write rejected")
        self.version += 1
        self.items[key] = body, str(self.version)


class _Rollback(Exception):
    """Roll the database back after recording a complete accepted command."""


@pytest.mark.asyncio
@pytest.mark.verifies("SEC-043")
async def test_restore_replays_each_accepted_lifecycle_decision() -> None:
    migration_url = os.getenv("TEST_MIGRATION_URL")
    if not migration_url or "bluelab_maintenance" not in os.getenv("DATABASE_URL", ""):
        pytest.skip("isolated migration and maintenance database roles required")
    engine = create_async_engine(migration_url)
    org_id, actor_id, manager_id, subject_id = (
        new_id(), new_id(), new_id(), new_id()
    )
    request_id: UUID | None = None
    history = LifecycleHistory(_HistoryStore(), _HistoryStore())

    async def accept_after_snapshot(prepare, *, finalize=None):
        head = await history.verified_head(org_id)
        prepared = None
        try:
            async with scoped_transaction(system_scope(org_id=org_id)) as db:
                operation = await prepare(db, head)
                if finalize is not None:
                    operation = await finalize(db, operation.id)
                prepared = (
                    operation.id, operation.action, operation.reason,
                    operation.actor_ops_account_id, dict(operation.command_payload),
                )
                raise _Rollback
        except _Rollback:
            pass
        assert prepared is not None
        operation_id, action, reason, actor, payload = prepared
        await history.append(
            org_id=org_id, operation_id=operation_id,
            expected_sequence=head.sequence, action=action,
            accepted_at=payload["cutoff_at"] if action == "start"
            else datetime.now(UTC).isoformat(), actor_id=actor,
            reason=reason, data=payload,
        )
        await replay_organization(
            org_id, await history.verified_events(org_id), history=history,
            markers=[], ledger=None, object_store=None, sessions=None,
        )
        async with engine.connect() as db:
            assert await db.scalar(text(
                "select lifecycle_sequence from org where id=:id"
            ), {"id": org_id}) == head.sequence + 1
            assert await db.scalar(text(
                "select status from org_lifecycle_operation where id=:id"
            ), {"id": operation_id}) == "applied"
        return payload

    try:
        await history.register(org_id)
        async with engine.begin() as db:
            await db.execute(text(
                "insert into org(id,name,registered_domain,timezone,lifecycle_sequence) "
                "values(:id,'Restore matrix',:domain,'UTC',0)"
            ), {"id": org_id, "domain": f"restore-{org_id}.example.test"})
            await db.execute(text(
                "insert into ops_account(id,email,display_name,password_hash,"
                "totp_secret_ciphertext) values(:id,:email,'Restore operator','x',:secret)"
            ), {
                "id": actor_id, "email": f"restore-{actor_id}@example.test",
                "secret": b"x",
            })
            await db.execute(text(
                "insert into account(id,org_id,team_id,email,display_name,role,"
                "password_hash) values(:id,:org,:id,:email,'Restore manager','manager','x')"
            ), {
                "id": manager_id, "org": org_id,
                "email": f"restore-{manager_id}@example.test",
            })
            await db.execute(text(
                "insert into account(id,org_id,team_id,email,display_name,role,"
                "password_hash) values(:id,:org,:team,:email,'Restore subject','rep','x')"
            ), {
                "id": subject_id, "org": org_id, "team": manager_id,
                "email": f"restore-{subject_id}@example.test",
            })

        initial = await accept_after_snapshot(
            lambda db, head: service.prepare_term(
                db, org_id=org_id, operation_id=new_id(), actor_id=actor_id,
                command=OrgServiceTermCommand(
                    expected_sequence=head.sequence,
                    service_start_on=date(2026, 7, 1),
                    service_last_access_on=date(2026, 8, 1),
                    contract_reference="restore-term-1", reason="signed term",
                ), head=head,
            ),
        )
        assert initial["service_term_id"]

        expired = await accept_after_snapshot(
            lambda db, head: service.prepare_expiry(
                db, org_id=org_id, head=head, at=datetime.now(UTC),
            ),
        )
        episode_id = UUID(expired["offboarding_id"])
        assert expired["hold_started_at"] == initial["ends_at"]

        extended = await accept_after_snapshot(
            lambda db, head: service.prepare_deadline_extension(
                db, org_id=org_id, offboarding_id=episode_id,
                operation_id=new_id(), actor_id=actor_id,
                command=OrgDeadlineCommand(
                    expected_sequence=head.sequence, reason="approved extension",
                    purge_eligible_at=datetime.fromisoformat(
                        expired["purge_eligible_at"]
                    ) + timedelta(days=30),
                ), head=head,
            ),
        )
        assert datetime.fromisoformat(extended["new_deadline"]) > datetime.fromisoformat(
            expired["purge_eligible_at"]
        )

        renewed = await accept_after_snapshot(
            lambda db, head: service.prepare_term(
                db, org_id=org_id, operation_id=new_id(), actor_id=actor_id,
                command=OrgServiceTermCommand(
                    expected_sequence=head.sequence,
                    current_offboarding_id=episode_id,
                    service_start_on=datetime.now(UTC).date(),
                    service_last_access_on=datetime.now(UTC).date() + timedelta(days=30),
                    contract_reference="restore-term-2", reason="signed renewal",
                ), head=head,
            ),
        )
        assert renewed["cancelled_offboarding_id"] == str(episode_id)

        started = await accept_after_snapshot(
            lambda db, head: service.prepare_start_offboarding(
                db, org_id=org_id, operation_id=new_id(), actor_id=actor_id,
                command=OrgLifecycleCommand(
                    expected_sequence=head.sequence, reason="early termination",
                ), head=head,
            ),
            finalize=lambda db, operation_id: service.finalize_start_offboarding(
                db, operation_id=operation_id,
            ),
        )
        early_episode = UUID(started["offboarding_id"])
        assert started["cutoff_at"]

        created = await accept_after_snapshot(
            lambda db, head: restrictions.prepare_create(
                db, org_id=org_id, offboarding_id=early_episode,
                operation_id=new_id(), actor_id=actor_id,
                command=OrgRestrictionCommand(
                    expected_sequence=head.sequence, reason="legal hold",
                    scope="legal_hold", authority_ref="case-42",
                    release_condition="case_closed",
                ), head=head,
            ),
        )
        restriction_id = UUID(created["restriction_id"])
        async with engine.connect() as db:
            assert await db.scalar(text(
                "select status from org_deletion_restriction where id=:id"
            ), {"id": restriction_id}) == "active"

        released = await accept_after_snapshot(
            lambda db, head: restrictions.prepare_release(
                db, org_id=org_id, restriction_id=restriction_id,
                operation_id=new_id(), actor_id=actor_id,
                command=OrgRestrictionReleaseCommand(
                    expected_sequence=head.sequence, reason="case closed",
                    evidence_reference="closure-42",
                ), head=head,
            ),
        )
        assert released["restriction_id"] == str(restriction_id)

        cancelled = await accept_after_snapshot(
            lambda db, head: service.prepare_cancel_offboarding(
                db, org_id=org_id, offboarding_id=early_episode,
                operation_id=new_id(), actor_id=actor_id,
                command=OrgLifecycleCommand(
                    expected_sequence=head.sequence, reason="termination cancelled",
                ), head=head,
            ),
        )
        assert cancelled["offboarding_id"] == str(early_episode)
        async with engine.connect() as db:
            row = (await db.execute(text(
                "select lifecycle_status,offboarding_id from org where id=:id"
            ), {"id": org_id})).one()
            assert tuple(row) == ("active", None)

        revised = await accept_after_snapshot(
            lambda db, head: service.prepare_policy_revision(
                db, org_id=org_id, operation_id=new_id(), actor_id=actor_id,
                command=OrgRetentionPolicyCommand(
                    expected_sequence=head.sequence,
                    policy_reference="restore-policy-120d:v1",
                    period_value=120, period_unit="elapsed_days",
                    contract_reference="amendment-120", reason="signed amendment",
                ), head=head,
            ),
        )
        assert revised["policy_reference"] == "restore-policy-120d:v1"
        async with engine.connect() as db:
            assert await db.scalar(text(
                "select policy_reference from org_retention_policy where org_id=:id"
            ), {"id": org_id}) == "restore-policy-120d:v1"
        async with engine.begin() as db:
            await db.execute(text(
                "update org_retention_policy set policy_reference='stale' where org_id=:id"
            ), {"id": org_id})
        with pytest.raises(RestoreUnverified, match="retention policy projection"):
            await replay_organization(
                org_id, await history.verified_events(org_id), history=history,
                markers=[], ledger=None, object_store=None, sessions=None,
            )
        async with engine.begin() as db:
            await db.execute(text(
                "update org_retention_policy set policy_reference=:ref where org_id=:id"
            ), {"id": org_id, "ref": revised["policy_reference"]})

        accepted = await accept_after_snapshot(
            lambda db, head: subject_requests.prepare(
                db, operation_id=new_id(), org_id=org_id, kind="export",
                subject_kind="account", subject_id=subject_id,
                actor_id=actor_id, reason="subject export request", head=head,
            ),
        )
        request_id = UUID(accepted["request_id"])
        async with engine.connect() as db:
            assert await db.scalar(text(
                "select status from export_request where id=:id"
            ), {"id": request_id}) == "pending"
            assert await db.scalar(text(
                "select status from org_deletion_restriction where id=:id"
            ), {"id": UUID(accepted["restriction_id"])}) == "active"
            assert await db.scalar(text(
                "select count(*) from procrastinate_jobs where "
                "args->'args'->>'request_id'=:id"
            ), {"id": str(request_id)}) == 1

        # A mixed rollback can lose request, restriction, audit, and queued work
        # while retaining the independently accepted lifecycle sequence.
        async with engine.begin() as db:
            await db.execute(text("delete from export_request where id=:id"), {
                "id": request_id,
            })
            await db.execute(text("delete from org_deletion_restriction where id=:id"), {
                "id": UUID(accepted["restriction_id"]),
            })
            await db.execute(text("delete from ops_audit where id=:id"), {
                "id": UUID(accepted["audit_id"]),
            })
            await db.execute(text(
                "delete from procrastinate_jobs where args->'args'->>'request_id'=:id"
            ), {"id": str(request_id)})
        for _ in range(2):
            await replay_organization(
                org_id, await history.verified_events(org_id), history=history,
                markers=[], ledger=None, object_store=None, sessions=None,
            )
        async with engine.connect() as db:
            assert await db.scalar(text(
                "select status from export_request where id=:id"
            ), {"id": request_id}) == "pending"
            assert await db.scalar(text(
                "select status from org_deletion_restriction where id=:id"
            ), {"id": UUID(accepted["restriction_id"])}) == "active"
            assert await db.scalar(text(
                "select count(*) from ops_audit where id=:id"
            ), {"id": UUID(accepted["audit_id"])}) == 1
            assert await db.scalar(text(
                "select count(*) from procrastinate_jobs where "
                "args->'args'->>'request_id'=:id"
            ), {"id": str(request_id)}) == 1
    finally:
        async with engine.begin() as db:
            if request_id is not None:
                await db.execute(text(
                    "delete from procrastinate_jobs where "
                    "args->'args'->>'request_id'=:id"
                ), {"id": str(request_id)})
            await db.execute(text("delete from export_request where org_id=:id"), {
                "id": org_id,
            })
            await db.execute(text(
                "delete from ops_audit where target_org_id=:id"
            ), {"id": org_id})
            await db.execute(text(
                "delete from org_deletion_restriction where org_id=:id"
            ), {"id": org_id})
            await db.execute(text(
                "update org set current_service_term_id=null,service_starts_at=null,"
                "service_ends_at=null where id=:id"
            ), {"id": org_id})
            await db.execute(text(
                "delete from org_lifecycle_operation where org_id=:id"
            ), {"id": org_id})
            await db.execute(text(
                "delete from org_service_term where org_id=:id"
            ), {"id": org_id})
            await db.execute(text(
                "delete from org_retention_policy where org_id=:id"
            ), {"id": org_id})
            await db.execute(text("delete from account where id=:id"), {
                "id": subject_id,
            })
            await db.execute(text("delete from account where id=:id"), {
                "id": manager_id,
            })
            await db.execute(text("delete from org where id=:id"), {"id": org_id})
            await db.execute(text("delete from ops_account where id=:id"), {
                "id": actor_id,
            })
        await engine.dispose()

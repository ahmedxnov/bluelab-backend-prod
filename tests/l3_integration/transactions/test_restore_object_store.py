"""Restore reconciliation against PostgreSQL and a disposable S3 bucket."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from bluelab.adapters.erasure_ledger import ErasureMarker
from bluelab.adapters.object_store import (
    AuthorizedObjectRead,
    DeletionReason,
    ObjectRef,
    create_object_store,
)
from bluelab.entrypoints.restore_reconcile import _replay_remaining_markers
from bluelab.modules.operations.reconciliation import reconcile_objects
from bluelab.platform.config import Settings
from bluelab.platform.ids import new_id


@pytest.mark.asyncio
async def test_pre_erasure_database_restore_replays_marker_before_release(
    session, base_org,
) -> None:
    """An independent marker re-erases a subject absent from the DB snapshot."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url or make_url(database_url).username != "bluelab":
        pytest.skip("restore replay requires the isolated migration database role")
    org_id = base_org["org"]
    subject_id, operator_id, request_id = new_id(), new_id(), new_id()
    instant = datetime.now(UTC)
    marker = ErasureMarker(
        request_id=request_id, org_id=org_id, subject_kind="account",
        subject_id=subject_id, requested_at=instant, armed_at=instant,
        executed_by=operator_id,
    )

    class Ledger:
        async def arm(self, received):
            assert received.request_id == request_id

    class Objects:
        async def delete(self, _ref, *, reason):
            assert reason is DeletionReason.ERASURE

        async def exists(self, _ref):
            return False

    try:
        async with session.begin():
            await session.execute(text(
                "insert into ops_account(id,email,display_name,password_hash,"
                "totp_secret_ciphertext) values(:id,:email,'Restore operator','x',:secret)"
            ), {
                "id": operator_id, "email": f"restore-{operator_id}@example.test",
                "secret": b"x",
            })
            await session.execute(text(
                "insert into account(id,org_id,team_id,email,display_name,role,"
                "password_hash) values(:id,:org,:team,:email,'Restored subject','rep','x')"
            ), {
                "id": subject_id, "org": org_id, "team": base_org["manager"],
                "email": f"restore-{subject_id}@example.test",
            })
        await _replay_remaining_markers(
            [marker], ledger=Ledger(), object_store=Objects(), purged=set(),
        )
        async with session.begin():
            row = (await session.execute(text(
                "select email,display_name,password_hash from account where id=:id"
            ), {"id": subject_id})).one()
            assert tuple(row) == (
                f"erased+{subject_id}@erased.invalid", "Erased person", None,
            )
            assert await session.scalar(text(
                "select status from erasure_request where id=:id"
            ), {"id": request_id}) == "executed"
            remaining = (await session.execute(text(
                "select app_verify_erasure(:id)"
            ), {"id": request_id})).scalar_one()
            assert all(int(count) == 0 for count in remaining.values())
        await _replay_remaining_markers(
            [marker], ledger=Ledger(), object_store=Objects(), purged=set(),
        )
    finally:
        await session.rollback()
        async with session.begin():
            await session.execute(text(
                "delete from erasure_request where id=:id"
            ), {"id": request_id})
            await session.execute(text("delete from account where id=:id"), {
                "id": subject_id,
            })
            await session.execute(text("delete from ops_account where id=:id"), {
                "id": operator_id,
            })


@pytest.mark.asyncio
@pytest.mark.verifies("SEC-043")
async def test_object_only_restore_and_missing_recording(
    session, base_org, make_drill,
) -> None:
    endpoint = os.getenv("RESTORE_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("RESTORE_TEST_S3_ENDPOINT selects disposable S3 storage")
    bucket = f"bluelab-restore-{uuid4().hex}"
    client = boto3.client(
        "s3", endpoint_url=endpoint, region_name="us-east-1",
        aws_access_key_id=os.getenv("RESTORE_TEST_S3_ACCESS_KEY", "bluelab"),
        aws_secret_access_key=os.getenv(
            "RESTORE_TEST_S3_SECRET_KEY", "bluelabbluelab",
        ),
        config=Config(signature_version="s3v4"),
    )
    client.create_bucket(Bucket=bucket)
    settings = Settings(
        BLUELAB_ENV="local", DATABASE_URL=os.environ["TEST_DATABASE_URL"],
        VALKEY_URL="redis://127.0.0.1:6379/0",
        AGENT_HMAC_SECRET="test-secret-not-real",  # pragma: allowlist secret
        OBJECT_STORE_ENDPOINT=endpoint, OBJECT_STORE_BUCKET=bucket,
        OBJECT_STORE_REGION="us-east-1",
        OBJECT_STORE_ACCESS_KEY=os.getenv("RESTORE_TEST_S3_ACCESS_KEY", "bluelab"),
        OBJECT_STORE_SECRET_KEY=os.getenv(
            "RESTORE_TEST_S3_SECRET_KEY", "bluelabbluelab",  # pragma: allowlist secret
        ),
    )
    objects = create_object_store(settings)
    org_id = base_org["org"]
    orphan = ObjectRef.recording(org_id=org_id, attempt_id=new_id())
    unknown_export = ObjectRef.export(request_id=new_id())
    attempt_id = new_id()
    drill_id = None
    missing = ObjectRef.recording(org_id=org_id, attempt_id=attempt_id)
    try:
        await objects.put(orphan, b"restored object", content_type="audio/ogg")
        evidence = await reconcile_objects(
            session, object_store=objects, catalog_orgs={org_id},
            purged_orgs=set(), purge_export_owners={},
        )
        assert evidence["orphan_objects_deleted"] == 1
        assert not await objects.exists(orphan)

        # A URL issued before file-only recovery cannot read a purged object.
        await objects.put(orphan, b"pre-recovery object", content_type="audio/ogg")
        grant = await objects.presign_get(AuthorizedObjectRead(
            object_ref=orphan, principal_id=base_org["manager"],
            authorized_until=datetime.now(UTC) + timedelta(minutes=5),
        ))
        async with httpx.AsyncClient() as browser:
            assert (await browser.get(grant.url)).status_code == 200
            await reconcile_objects(
                session, object_store=objects, catalog_orgs={org_id},
                purged_orgs={org_id}, purge_export_owners={},
            )
            assert (await browser.get(grant.url)).status_code == 404

        await objects.put(unknown_export, b"unknown", content_type="application/zip")
        with pytest.raises(RuntimeError, match="unverifiable ownership"):
            await reconcile_objects(
                session, object_store=objects, catalog_orgs={org_id},
                purged_orgs=set(), purge_export_owners={},
            )
        assert await objects.exists(unknown_export)
        await objects.delete(unknown_export, reason=DeletionReason.SWEEP)
        await session.commit()

        drill_id = await make_drill(status="published", self_authored=True)
        async with session.begin():
            await session.execute(text(
                "insert into attempt(id,org_id,team_id,drill_id,rep_account_id,"
                "self_authored,status,recording_object_key,recording_status) "
                "values(:id,:org,:team,:drill,:rep,true,'completed',:key,'available')"
            ), {
                "id": attempt_id, "org": org_id, "team": base_org["manager"],
                "drill": drill_id, "rep": base_org["manager"], "key": missing.key,
            })
        evidence = await reconcile_objects(
            session, object_store=objects, catalog_orgs={org_id},
            purged_orgs=set(), purge_export_owners={},
        )
        assert evidence["missing_references_repaired"] == 1
        await session.commit()
        async with session.begin():
            state = (await session.execute(text(
                "select recording_status,recording_object_key from attempt where id=:id"
            ), {"id": attempt_id})).one()
            assert tuple(state) == ("unavailable", None)
            assert await session.scalar(text(
                "select count(*) from ops_fault where attempt_id=:id "
                "and kind='playback_asset' and status='open'"
            ), {"id": attempt_id}) == 1
    finally:
        await session.rollback()
        async with session.begin():
            await session.execute(text(
                "delete from ops_fault where attempt_id=:id"
            ), {"id": attempt_id})
            await session.execute(text(
                "delete from attempt where id=:id"
            ), {"id": attempt_id})
            if drill_id is not None:
                await session.execute(text(
                    "delete from drill where id=:id"
                ), {"id": drill_id})
        for item in client.list_objects_v2(Bucket=bucket).get("Contents", []):
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)

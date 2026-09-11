"""Phase 2 legal-document and password-reset journeys.

These tests keep the two non-disclosure boundaries observable: account existence
does not change the reset response, and plaintext reset material never enters a
database row or queue payload.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.legal_fixtures import seed_admitted_accounts

from bluelab.adapters.secrets import DeliverySecretContext
from bluelab.platform.security.passwords import verify_password

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

SESSION_COOKIE = "__Host-bluelab_session"


@pytest_asyncio.fixture(autouse=True)
async def admitted_baseline(auth_engine, world):
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await seed_admitted_accounts(db, world.org)


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_legal_documents_are_public_current_and_cacheable(client):
    response = await client.get("/api/v1/legal-documents")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert set(body) == {
        "recording_consent_notice",
        "terms_of_use",
        "privacy_notice",
    }
    assert all(item["version"] == "test-legal-baseline" for item in body.values())
    assert all(item["url"].startswith("https://legal.example.com/") for item in body.values())


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_missing_legal_document_fails_closed(client, auth_engine):
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text("delete from legal_document_version where kind = 'privacy_notice'")
        )

    response = await client.get("/api/v1/legal-documents")

    assert response.status_code == 503
    assert response.json()["type"].endswith("/service-unavailable")


async def _phase2_counts(auth_engine, account_id):
    async with async_sessionmaker(auth_engine)() as db:
        return tuple(
            int(value)
            for value in (
                await db.execute(
                    text(
                        "select"
                        " (select count(*) from password_reset_token where account_id = :account),"
                        " (select count(*) from email_send where account_id = :account"
                        "   and kind = 'E1_credentials'),"
                        " (select count(*) from email_delivery_secret s"
                        "   join email_send e on e.id = s.email_send_id"
                        "  where e.account_id = :account),"
                        " (select count(*) from procrastinate_jobs"
                        "  where task_name = 'dispatch_email'"
                        "    and args #>> '{args,email_send_id}' in"
                        "        (select id::text from email_send where account_id = :account))"
                    ),
                    {"account": account_id},
                )
            ).one()
        )


@pytest.mark.verifies("FR-IDA-005", "FR-IDA-006")
async def test_unknown_reset_request_is_indistinguishable_and_persists_nothing(
    client, world, auth_engine
):
    before = await _phase2_counts(auth_engine, world.rep)

    unknown = await client.post(
        "/api/v1/auth/password-reset-request",
        json={"email": "not-provisioned@example.com"},
    )
    known = await client.post(
        "/api/v1/auth/password-reset-request",
        json={"email": world.rep_email},
    )

    assert unknown.status_code == known.status_code == 202
    assert unknown.content == known.content == b""
    after = await _phase2_counts(auth_engine, world.rep)
    assert after == tuple(value + 1 for value in before)


@pytest.mark.verifies("FR-IDA-006", "ADR-0028")
async def test_reset_issuance_keeps_plaintext_out_of_storage_and_queue(
    client, world, auth_engine, delivery_cipher
):
    response = await client.post(
        "/api/v1/auth/password-reset-request", json={"email": world.rep_email}
    )
    assert response.status_code == 202

    async with async_sessionmaker(auth_engine)() as db:
        row = (
            await db.execute(
                text(
                    "select t.token_hash, t.expires_at, e.id as send_id, e.org_id,"
                    " s.purpose, s.ciphertext, j.args"
                    " from password_reset_token t"
                    " join email_send e on e.dedupe_key = t.id::text"
                    " join email_delivery_secret s on s.email_send_id = e.id"
                    " join procrastinate_jobs j"
                    "   on j.args #>> '{args,email_send_id}' = e.id::text"
                    " where t.account_id = :account"
                    " order by t.created_at desc limit 1"
                ),
                {"account": world.rep},
            )
        ).mappings().one()

    context = DeliverySecretContext(
        email_send_id=row["send_id"],
        org_id=row["org_id"],
        purpose="password_reset_token",
    )
    plaintext = await delivery_cipher.unseal(row["ciphertext"], context=context)

    assert plaintext
    assert plaintext not in row["token_hash"]
    assert row["purpose"] == "password_reset_token"
    assert row["expires_at"] > datetime.now(UTC)
    assert row["args"] == {
        "v": 1,
        "org_id": str(world.org),
        "args": {"email_send_id": str(row["send_id"])},
    }
    assert plaintext not in str(row["args"])
    assert plaintext.encode() not in row["ciphertext"]


@pytest.mark.verifies("FR-IDA-006", "FR-IDA-010")
async def test_reset_is_single_use_changes_password_and_revokes_live_sessions(
    client, world, credentials, auth_engine, delivery_cipher
):
    signed_in = await client.post(
        "/api/v1/auth/session", json=credentials(world.rep_email)
    )
    old_cookie = signed_in.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

    await client.post(
        "/api/v1/auth/password-reset-request", json={"email": world.rep_email}
    )
    async with async_sessionmaker(auth_engine)() as db:
        row = (
            await db.execute(
                text(
                    "select e.id as send_id, e.org_id, s.ciphertext"
                    " from email_send e"
                    " join email_delivery_secret s on s.email_send_id = e.id"
                    " where e.account_id = :account and s.purpose = 'password_reset_token'"
                    " order by e.created_at desc limit 1"
                ),
                {"account": world.rep},
            )
        ).mappings().one()
    token = await delivery_cipher.unseal(
        row["ciphertext"],
        context=DeliverySecretContext(
            email_send_id=row["send_id"],
            org_id=row["org_id"],
            purpose="password_reset_token",
        ),
    )
    replacement = "replacement-horse-battery-staple"

    completed = await client.post(
        "/api/v1/auth/password-reset",
        json={"token": token, "new_password": replacement},
    )
    assert completed.status_code == 204

    replay = await client.post(
        "/api/v1/auth/password-reset",
        json={"token": token, "new_password": replacement},
    )
    assert replay.status_code == 401
    assert replay.json()["type"].endswith("/reset-token-invalid")

    stale = await client.get(
        "/api/v1/auth/session", cookies={SESSION_COOKIE: old_cookie}
    )
    assert stale.status_code == 401

    old_password = await client.post(
        "/api/v1/auth/session", json=credentials(world.rep_email)
    )
    assert old_password.status_code == 401
    new_password = await client.post(
        "/api/v1/auth/session", json=credentials(world.rep_email, replacement)
    )
    assert new_password.status_code == 200

    async with async_sessionmaker(auth_engine)() as db:
        stored = (
            await db.execute(
                text("select password_hash from account where id = :id"),
                {"id": world.rep},
            )
        ).scalar_one()
    assert verify_password(replacement, stored)

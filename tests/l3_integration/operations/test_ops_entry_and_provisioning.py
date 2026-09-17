"""Phase 2 operations authentication, provisioning, and audit journeys."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.l3_integration.operations.conftest import OPS_PASSWORD, OPS_SEED

from bluelab.adapters.secrets import DeliverySecretContext
from bluelab.platform.db.privileged import ops_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.security.passwords import verify_password
from bluelab.platform.security.totp import STEP_SECONDS, code_for_step

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

OPS_COOKIE = "__Host-bluelab_ops_session"
CUSTOMER_COOKIE = "__Host-bluelab_session"


def _code() -> str:
    step = int(datetime.now(UTC).timestamp()) // STEP_SECONDS
    return code_for_step(OPS_SEED, step)


def _wrong_code() -> str:
    valid = _code()
    return valid[:-1] + str((int(valid[-1]) + 1) % 10)


async def _sign_in(client, world, code: str | None = None):
    return await client.post(
        "/ops/v1/session",
        json={
            "email": world.email,
            "password": OPS_PASSWORD,
            "totp_code": code or _code(),
        },
    )


@pytest.mark.verifies("SEC-002", "SEC-006", "ADR-0028")
async def test_ops_requires_password_and_non_replayed_totp_in_separate_cookie(
    ops_client, ops_world
):
    wrong_password = await ops_client.post(
        "/ops/v1/session",
        json={
            "email": ops_world.email,
            "password": "not-the-password",  # pragma: allowlist secret -- negative-control value
            "totp_code": _code(),
        },
    )
    wrong_totp = await ops_client.post(
        "/ops/v1/session",
        json={
            "email": ops_world.email,
            "password": OPS_PASSWORD,
            "totp_code": _wrong_code(),
        },
    )
    assert wrong_password.status_code == wrong_totp.status_code == 401
    for response in (wrong_password, wrong_totp):
        assert response.json()["type"].endswith("/invalid-credentials")

    replayed_code = _code()
    accepted = await _sign_in(ops_client, ops_world, replayed_code)
    assert accepted.status_code == 200
    assert accepted.json()["ops_account_id"] == str(ops_world.ops_account_id)
    cookie = accepted.headers["set-cookie"]
    assert cookie.startswith(f"{OPS_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Domain=" not in cookie
    assert CUSTOMER_COOKIE not in cookie

    replay = await _sign_in(ops_client, ops_world, replayed_code)
    assert replay.status_code == 401
    assert replay.json()["type"].endswith("/invalid-credentials")

    # A customer cookie cannot become an operations credential, and the ops
    # cookie established above cannot become a customer session.
    customer_surface = await ops_client.get("/api/v1/auth/session")
    assert customer_surface.status_code == 401
    assert customer_surface.json()["type"].endswith("/session-invalid")
    ops_client.cookies.clear()
    ops_client.cookies.set(CUSTOMER_COOKIE, "customer-only")
    ops_surface = await ops_client.post(
        "/ops/v1/orgs",
        json={
            "name": "Must not exist",
            "registered_domain": "example.com",
            "reason": "boundary probe",
        },
    )
    assert ops_surface.status_code == 401
    assert ops_surface.json()["type"].endswith("/ops-session-invalid")


@pytest.mark.verifies("FR-IDA-001", "FR-IDA-003", "SEC-040")
async def test_domain_bound_provisioning_queues_id_only_e1_and_audits(
    ops_client, ops_world, operations_engine
):
    assert (await _sign_in(ops_client, ops_world)).status_code == 200
    org_response = await ops_client.post(
        "/ops/v1/orgs",
        json={
            "name": "Phase Two Org",
            "registered_domain": "EXAMPLE.COM.",
            "timezone": "Africa/Cairo",
            "reason": "signed customer agreement",
        },
    )
    assert org_response.status_code == 201
    org = org_response.json()
    assert org["registered_domain"] == "example.com"

    manager_response = await ops_client.post(
        "/ops/v1/accounts",
        json={
            "org_id": org["org_id"],
            "email": "manager@example.com",
            "display_name": "Manager",
            "role": "manager",
            "reason": "approved roster",
        },
    )
    assert manager_response.status_code == 201
    manager = manager_response.json()

    rep_response = await ops_client.post(
        "/ops/v1/accounts",
        json={
            "org_id": org["org_id"],
            "email": "rep@example.com",
            "display_name": "Rep",
            "role": "rep",
            "manager_account_id": manager["account_id"],
            "reason": "approved roster",
        },
    )
    assert rep_response.status_code == 201
    rep = rep_response.json()
    assert set(rep) == {"account_id", "email", "role"}

    audit_response = await ops_client.get(
        "/ops/v1/audit", params={"org_id": org["org_id"]}
    )
    assert audit_response.status_code == 200
    audit = audit_response.json()["data"]
    assert [entry["verb"] for entry in audit] == [
        "provision_account",
        "provision_account",
        "provision_org",
    ]
    assert {entry["ops_account_id"] for entry in audit} == {
        str(ops_world.ops_account_id)
    }
    assert all(entry["reason"] for entry in audit)

    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db:
        rows = (
            await db.execute(
                text(
                    "select a.id, a.team_id, a.role, a.password_hash, a.credential_state,"
                    " e.id as email_send_id, e.dedupe_key, s.purpose, s.ciphertext,"
                    " s.expires_at, j.args"
                    " from account a"
                    " join email_send e on e.account_id = a.id"
                    " join email_delivery_secret s on s.email_send_id = e.id"
                    " join procrastinate_jobs j"
                    "   on j.args #>> '{args,email_send_id}' = e.id::text"
                    " where a.org_id = :org order by a.role"
                ),
                {"org": UUID(org["org_id"])},
            )
        ).mappings().all()
    assert len(rows) == 2
    by_role = {row["role"]: row for row in rows}
    assert by_role["manager"]["team_id"] == UUID(manager["account_id"])
    assert by_role["rep"]["team_id"] == UUID(manager["account_id"])
    assert all(row["credential_state"] == "initial" for row in rows)
    for row in rows:
        initial = await ops_world.delivery_cipher.unseal(
            row["ciphertext"],
            context=DeliverySecretContext(
                email_send_id=row["email_send_id"],
                org_id=UUID(org["org_id"]),
                purpose="initial_credential",
            ),
        )
        assert verify_password(initial, row["password_hash"])
        assert row["dedupe_key"] == str(row["id"])
        assert row["purpose"] == "initial_credential"
        assert row["expires_at"] > datetime.now(UTC)
        assert row["args"] == {
            "v": 1,
            "org_id": org["org_id"],
            "args": {"email_send_id": str(row["email_send_id"])},
        }
        assert initial not in str(row["args"])


@pytest.mark.verifies("AC-IDA-008", "SEC-040")
async def test_domain_mismatch_is_refused_without_override_and_is_audited(
    ops_client, ops_world, operations_engine
):
    assert (await _sign_in(ops_client, ops_world)).status_code == 200
    org = (
        await ops_client.post(
            "/ops/v1/orgs",
            json={
                "name": "Bound Org",
                "registered_domain": "example.com",
                "reason": "new customer",
            },
        )
    ).json()
    rejected = await ops_client.post(
        "/ops/v1/accounts",
        json={
            "org_id": org["org_id"],
            "email": "rep@other.example",
            "display_name": "Rejected Rep",
            "role": "rep",
            "manager_account_id": "01936d54-7ad5-7000-8000-000000000001",
            "reason": "roster import",
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["type"].endswith("/validation-error")

    audit = await ops_client.get(
        "/ops/v1/audit", params={"org_id": org["org_id"]}
    )
    entry = audit.json()["data"][0]
    assert entry["target_ref"]["email_domain"] == "other.example"
    assert entry["target_ref"]["outcome"] == "domain_mismatch"
    assert entry["reason"] == "roster import"

    no_override = await ops_client.post(
        "/ops/v1/accounts",
        json={
            "org_id": org["org_id"],
            "email": "rep@other.example",
            "display_name": "Rejected Rep",
            "role": "rep",
            "manager_account_id": "01936d54-7ad5-7000-8000-000000000001",
            "reason": "roster import",
            "override": True,
        },
    )
    assert no_override.status_code == 422

    async with async_sessionmaker(operations_engine)() as db:
        counts = (
            await db.execute(
                text(
                    "select"
                    " (select count(*) from account where email = 'rep@other.example'),"
                    " (select count(*) from email_send where org_id = :org)"
                ),
                {"org": UUID(org["org_id"])},
            )
        ).one()
    assert tuple(counts) == (0, 0)


@pytest.mark.verifies("SEC-040")
async def test_ops_audit_cannot_be_updated_or_deleted(
    ops_client, ops_world, operations_engine
):
    assert (await _sign_in(ops_client, ops_world)).status_code == 200
    created = await ops_client.post(
        "/ops/v1/orgs",
        json={
            "name": "Immutable Audit Org",
            "registered_domain": "example.com",
            "reason": "original immutable reason",
        },
    )
    assert created.status_code == 201

    async with scoped_transaction(
        ops_scope(ops_account_id=ops_world.ops_account_id)
    ) as db:
        updated = await db.execute(
            text("update ops_audit set reason = 'rewritten'")
        )
        assert updated.rowcount == 0  # type: ignore[attr-defined]
    async with scoped_transaction(
        ops_scope(ops_account_id=ops_world.ops_account_id)
    ) as db:
        deleted = await db.execute(text("delete from ops_audit"))
        assert deleted.rowcount == 0  # type: ignore[attr-defined]

    async with async_sessionmaker(operations_engine)() as db:
        reason = (
            await db.execute(
                text(
                    "select reason from ops_audit"
                    " where ops_account_id = :actor order by occurred_at desc limit 1"
                ),
                {"actor": ops_world.ops_account_id},
            )
        ).scalar_one()
    assert reason == "original immutable reason"


@pytest.mark.verifies("SEC-001", "ADR-0010")
async def test_deactivated_operator_session_fails_on_next_request(
    ops_client, ops_world, operations_engine
):
    assert (await _sign_in(ops_client, ops_world)).status_code == 200
    async with async_sessionmaker(operations_engine)() as db, db.begin():
        await db.execute(
            text("update ops_account set status = 'deactivated' where id = :id"),
            {"id": ops_world.ops_account_id},
        )

    response = await ops_client.get("/ops/v1/audit")
    assert response.status_code == 401
    assert response.json()["type"].endswith("/ops-session-invalid")


@pytest.mark.verifies("SEC-001")
async def test_ops_sign_out_revokes_the_server_side_session(ops_client, ops_world):
    signed_in = await _sign_in(ops_client, ops_world)
    raw = signed_in.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

    signed_out = await ops_client.delete("/ops/v1/session")
    replayed = await ops_client.get(
        "/ops/v1/audit", headers={"cookie": f"{OPS_COOKIE}={raw}"}
    )

    assert signed_out.status_code == 204
    assert replayed.status_code == 401
    assert replayed.json()["type"].endswith("/ops-session-invalid")


@pytest.mark.verifies("ADR-0028")
async def test_authenticated_ops_requests_do_not_consume_the_sign_in_throttle(
    ops_client, ops_world
):
    assert (await _sign_in(ops_client, ops_world)).status_code == 200

    responses = [await ops_client.get("/ops/v1/audit") for _ in range(6)]

    assert [response.status_code for response in responses] == [200] * 6

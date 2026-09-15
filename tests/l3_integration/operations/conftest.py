"""Real-PostgreSQL operations-surface fixtures with isolated Valkey state."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import fakeredis.aioredis
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.support import required_url

from bluelab.adapters.secrets import LocalAeadCipher, OpsTotpContext
from bluelab.platform.config import Settings
from bluelab.platform.ids import new_id
from bluelab.platform.security.passwords import hash_password

MIGRATION_URL = required_url("TEST_MIGRATION_URL")
APP_URL = required_url("TEST_DATABASE_URL")
os.environ.setdefault("DATABASE_URL", APP_URL)
os.environ.setdefault("VALKEY_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("AGENT_HMAC_SECRET", "test-only-not-a-real-secret")

OPS_PASSWORD = "ops-correct-horse-battery-staple"  # pragma: allowlist secret
OPS_SEED = "JBSWY3DPEHPK3PXP"  # pragma: allowlist secret
OPS_KEY = "IiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiI="  # pragma: allowlist secret
DELIVERY_KEY = "MzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzMzM="  # pragma: allowlist secret


def ops_settings() -> Settings:
    return Settings(
        BLUELAB_ENV="staging",
        DATABASE_URL=APP_URL,
        VALKEY_URL="redis://127.0.0.1:6379/0",
        AGENT_HMAC_SECRET="test-only-not-a-real-secret",  # pragma: allowlist secret
        OPS_THROTTLE_ATTEMPTS=5,
    )


@pytest_asyncio.fixture(scope="session")
async def operations_engine():
    engine = create_async_engine(MIGRATION_URL)
    yield engine
    await engine.dispose()


@dataclass(frozen=True, slots=True)
class OpsWorld:
    ops_account_id: UUID
    email: str
    ops_cipher: LocalAeadCipher
    delivery_cipher: LocalAeadCipher


@pytest_asyncio.fixture
async def ops_world(operations_engine) -> AsyncIterator[OpsWorld]:
    ops_account_id = new_id()
    suffix = str(ops_account_id).replace("-", "")[-12:]
    email = f"operator-{suffix}@example.com"
    ops_cipher = LocalAeadCipher.from_base64(OPS_KEY)
    delivery_cipher = LocalAeadCipher.from_base64(DELIVERY_KEY)
    ciphertext = await ops_cipher.seal(
        OPS_SEED,
        context=OpsTotpContext(ops_account_id=ops_account_id),
    )
    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await db.execute(
            text(
                "insert into ops_account ("
                "id, email, display_name, password_hash, totp_secret_ciphertext, status)"
                " values (:id, :email, 'Phase 2 Operator', :password_hash, :totp, 'active')"
            ),
            {
                "id": ops_account_id,
                "email": email,
                "password_hash": hash_password(OPS_PASSWORD),
                "totp": ciphertext,
            },
        )

    yield OpsWorld(
        ops_account_id=ops_account_id,
        email=email,
        ops_cipher=ops_cipher,
        delivery_cipher=delivery_cipher,
    )

    async with maker() as db, db.begin():
        org_ids = list(
            (
                await db.execute(
                    text(
                        "select distinct target_org_id from ops_audit"
                        " where ops_account_id = :actor and target_org_id is not null"
                    ),
                    {"actor": ops_account_id},
                )
            ).scalars()
        )
        if org_ids:
            await db.execute(
                text(
                    "delete from procrastinate_jobs"
                    " where args ->> 'org_id' = any(cast(:org_ids as text[]))"
                ),
                {"org_ids": [str(value) for value in org_ids]},
            )
            await db.execute(
                text("delete from email_delivery_secret where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from email_send where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from ops_fault where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from attempt where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from drill_concealed where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from rubric_dimension where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from drill where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
            await db.execute(
                text("delete from account where org_id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
        await db.execute(
            text("delete from ops_audit where ops_account_id = :actor"),
            {"actor": ops_account_id},
        )
        if org_ids:
            await db.execute(
                text("delete from org where id = any(:org_ids)"),
                {"org_ids": org_ids},
            )
        await db.execute(
            text("delete from ops_account where id = :actor"),
            {"actor": ops_account_id},
        )


@pytest_asyncio.fixture
async def ops_client(ops_world) -> AsyncIterator[AsyncClient]:
    from bluelab.entrypoints.api import create_app
    from bluelab.platform.config import get_settings

    settings = ops_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.ops_totp_unsealer = ops_world.ops_cipher
    app.state.delivery_secret_sealer = ops_world.delivery_cipher
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://ops.test") as client:
        yield client
    await app.state.valkey.aclose()

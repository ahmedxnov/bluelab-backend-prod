"""Worker-only SQL privileges cannot be selected by an API database URL."""

from __future__ import annotations

from uuid import uuid4

import pytest

from bluelab.entrypoints import restore_reconcile, worker
from bluelab.lifecycle.restore import RestoreUnverified
from bluelab.platform.config import Settings
from bluelab.platform.db.engine import (
    require_erasure_database_role,
    require_maintenance_database_role,
)


def _settings(username: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        DATABASE_URL=f"postgresql+asyncpg://{username}:test@db/bluelab",  # pragma: allowlist secret
        VALKEY_URL="redis://cache:6379/0",
        AGENT_HMAC_SECRET="unit-test-only",  # pragma: allowlist secret
    )


def test_api_credential_cannot_start_privileged_work() -> None:
    settings = _settings("bluelab_app")
    with pytest.raises(RuntimeError, match="bluelab_erasure"):
        require_erasure_database_role(settings)
    with pytest.raises(RuntimeError, match="bluelab_maintenance"):
        require_maintenance_database_role(settings)


def test_dedicated_credentials_admit_only_their_work() -> None:
    require_erasure_database_role(_settings("bluelab_erasure"))
    require_maintenance_database_role(_settings("bluelab_maintenance"))


async def test_restore_rejects_database_org_missing_from_catalog(monkeypatch) -> None:
    org_id = uuid4()

    class History:
        async def catalog_orgs(self):
            return []

    class Objects:
        async def list_prefix(self, _prefix):
            return []

    async def database_org_ids():
        return {org_id}

    monkeypatch.setattr(restore_reconcile, "_database_org_ids", database_org_ids)
    with pytest.raises(RestoreUnverified, match="absent from lifecycle catalog"):
        await restore_reconcile._verify_lifecycle_restore(
            history=History(), markers=[], ledger=object(),
            object_store=Objects(), sessions=object(),
        )


def test_export_worker_does_not_build_erasure_or_maintenance_adapters(monkeypatch) -> None:
    settings = Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://bluelab_app:test@db/bluelab",  # pragma: allowlist secret
        VALKEY_URL="redis://cache:6379/0",
        AGENT_HMAC_SECRET="unit-test-only",  # pragma: allowlist secret
        WORKER_QUEUES="execute_export",
    )
    selected = object()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unselected adapter was built")

    monkeypatch.setattr(worker, "erasure_registration", forbidden)
    monkeypatch.setattr(worker, "register_maintenance", forbidden)
    monkeypatch.setattr(worker, "create_lifecycle_history", forbidden)
    monkeypatch.setattr(worker, "export_registration", lambda _settings: selected)
    monkeypatch.setattr(
        worker, "build_worker_app",
        lambda _url, registrations, **_kwargs: list(registrations),
    )
    assert worker.create_worker(settings) == [selected]

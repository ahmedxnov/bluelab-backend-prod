"""Explicit synthetic legal documents and evidence for already-onboarded accounts."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.platform.ids import new_id

KINDS = ("recording_consent_notice", "terms_of_use", "privacy_notice")
BASELINE = "test-legal-baseline"


@asynccontextmanager
async def isolated_legal_catalog(engine):
    """Restore global reference data, even when a test fails or changes versions."""
    maker = async_sessionmaker(engine)
    async with maker() as db, db.begin():
        original = (
            (await db.execute(text("select * from legal_document_version")))
            .mappings()
            .all()
        )
        await db.execute(text("delete from legal_document_version"))
    try:
        yield
    finally:
        async with maker() as db, db.begin():
            await db.execute(text("delete from legal_document_version"))
            for row in original:
                await db.execute(
                    text(
                        "insert into legal_document_version (id, kind, version, effective_at, created_at)"
                        " values (:id, :kind, :version, :effective_at, :created_at)"
                    ),
                    dict(row),
                )


async def seed_admitted_accounts(db, org):
    """Model existing onboarding honestly; never bypass the request dependency."""
    for kind in KINDS:
        await db.execute(
            text(
                "insert into legal_document_version (id, kind, version, effective_at)"
                " values (:id, :kind, :version, '1900-01-01T00:00:00Z')"
                " on conflict (kind, version) do nothing"
            ),
            {"id": new_id(), "kind": kind, "version": BASELINE},
        )
    accounts = (
        (
            await db.execute(
                text(
                    "select id from account where org_id = :org and credential_state = 'set'"
                ),
                {"org": org},
            )
        )
        .scalars()
        .all()
    )
    for account in accounts:
        await db.execute(
            text(
                "insert into consent_record (id, org_id, account_id, notice_version)"
                " values (:id, :org, :account, :version)"
            ),
            {"id": new_id(), "org": org, "account": account, "version": BASELINE},
        )
        await db.execute(
            text(
                "insert into terms_acceptance (id, org_id, account_id, terms_version, privacy_version)"
                " values (:id, :org, :account, :version, :version)"
            ),
            {"id": new_id(), "org": org, "account": account, "version": BASELINE},
        )

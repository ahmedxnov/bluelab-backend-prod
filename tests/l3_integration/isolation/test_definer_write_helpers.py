"""The three definer WRITE helpers, attacked through the app role.

`app_record_consent`, `app_record_terms_acceptance` and
`app_set_initial_credential` exist because the sign-in gates have to write three
things the product surface cannot reach: `consent_record` and `terms_acceptance`
are P9_OPS/`system_write_only`, and P2_ACCOUNT grants `self_read` with no
self-UPDATE. Each runs `SECURITY DEFINER`, so each is a hole cut deliberately
through RLS — and a hole is only as good as the constraints on its shape.

The auth suite proves they work. This proves what they refuse, which is the half
that fails silently:

* the subject comes from the transaction GUC, so no caller can name another
  person — there is no account parameter to name one WITH;
* `org_id` comes off the account being written about, not off `app.org_id`, so a
  mismatched scope cannot file a compliance record under another customer;
* a deactivated account cannot set a credential;
* the helper is the ONLY path — a direct write still fails, so `system_write_only`
  is doing its job and these are not decoration.

Every one of these was a real defect caught in review, reproduced, and fixed. They
are here so the fixes cannot quietly come undone.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.platform.ids import new_id

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

NOTICE = "isolation-notice-1.0"
TERMS = "isolation-terms-1.0"


@pytest_asyncio.fixture
async def published(migration_engine):
    """Publish the two legal documents these helpers require, then remove them.

    Torn down without fail. `legal_document_version` is platform-wide P0 reference
    data that `_reset` truncates only once per session, so a leftover row would
    open a consent gate for every later test in this database — including the auth
    suite, which asserts on `pending_gates` being empty.
    """
    maker = async_sessionmaker(migration_engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        for kind, version in (("privacy_notice", NOTICE), ("terms_of_use", TERMS)):
            await s.execute(
                text(
                    "insert into legal_document_version (id, kind, version, effective_at)"
                    " values (:id, :kind, :version, now())"
                ),
                {"id": new_id(), "kind": kind, "version": version},
            )
    yield
    async with maker() as s, s.begin():
        await s.execute(
            text("delete from legal_document_version where version in (:n, :t)"),
            {"n": NOTICE, "t": TERMS},
        )


async def consent_rows(migration_engine, account: UUID) -> list[tuple[UUID, str]]:
    """(org_id, notice_version) for one account, read with BYPASSRLS."""
    maker = async_sessionmaker(migration_engine, expire_on_commit=False)
    async with maker() as s:
        rows = await s.execute(
            text("select org_id, notice_version from consent_record where account_id = :a"),
            {"a": account},
        )
        return [(r[0], r[1]) for r in rows]


# ── the subject cannot be named ───────────────────────────────────────────────


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
async def test_no_helper_accepts_an_account_id(as_principal, manager, world, published):
    """The single most important property of the write helpers.

    A definer function runs with BYPASSRLS. One that took `p_account_id` would let
    any authenticated user record consent — or set a password — for anybody in the
    database, and RLS would not be there to stop it. The defence is that no such
    parameter exists, so this asserts the overload is absent rather than that some
    check rejects it.
    """
    async with as_principal(manager(world.m1, world.org_a)) as db:
        with pytest.raises(DBAPIError, match="does not exist"):
            await db.execute(
                text("select app_record_consent(:id, :victim, :version)"),
                {"id": new_id(), "victim": world.r1a, "version": NOTICE},
            )


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
async def test_recording_consent_touches_only_the_caller(
    as_principal, manager, world, published, migration_engine
):
    """M1 records consent; nobody else gains a row."""
    async with as_principal(manager(world.m1, world.org_a)) as db:
        await db.execute(
            text("select app_record_consent(:id, :version)"), {"id": new_id(), "version": NOTICE}
        )

    assert len(await consent_rows(migration_engine, world.m1)) == 1
    for other in (world.r1a, world.r1b, world.m2, world.m3):
        assert await consent_rows(migration_engine, other) == [], "a bystander gained consent"


@pytest.mark.verifies("CMP-004")
async def test_a_candidate_scope_cannot_record_account_consent(
    as_principal, candidate, world, published
):
    """A candidate is not an account and carries no `app.account_id`, so the helper
    has no subject and refuses rather than writing an orphan row."""
    async with as_principal(candidate()) as db:
        with pytest.raises(DBAPIError, match="no account in scope"):
            await db.execute(
                text("select app_record_consent(:id, :version)"),
                {"id": new_id(), "version": NOTICE},
            )


# ── the evidence row is attributed to the right customer ─────────────────────


@pytest.mark.verifies("CMP-004", "CMP-002")
async def test_the_org_comes_off_the_account_not_the_scope(
    as_principal, manager, world, published, migration_engine
):
    """A scope naming the wrong org must not misfile a compliance record.

    Nothing in the schema ties `consent_record.org_id` to its `account_id` — the
    two foreign keys are independent — so taking the org from `app.org_id` let a
    mismatched tuple write M1's consent under org B, with no constraint to catch
    it. Reproduced in review; the helper now reads the org off the account.
    """
    async with as_principal(manager(world.m1, world.org_b)) as db:
        await db.execute(
            text("select app_record_consent(:id, :version)"), {"id": new_id(), "version": NOTICE}
        )

    rows = await consent_rows(migration_engine, world.m1)

    assert rows == [(world.org_a, NOTICE)], "the row was filed under the org the scope claimed"


# ── the helper is the only path ──────────────────────────────────────────────


@pytest.mark.verifies("CMP-002", "AC-IDA-006")
@pytest.mark.parametrize(
    ("table", "columns", "values"),
    [
        (
            "consent_record",
            "id, org_id, account_id, notice_version",
            ":id, :org, :account, 'direct'",
        ),
        (
            "terms_acceptance",
            "id, org_id, account_id, terms_version, privacy_version",
            ":id, :org, :account, 'direct', 'direct'",
        ),
    ],
    ids=["consent_record", "terms_acceptance"],
)
async def test_the_app_role_cannot_write_the_evidence_tables_directly(
    as_principal, manager, world, table, columns, values
):
    """`system_write_only` still holds, which is what makes the helper necessary
    rather than decorative. If this ever passes, the definer functions are an
    elaborate way of doing something the app role could already do.

    Either refusal is accepted. Today it is RLS — the table grants INSERT at the
    SQL level and no policy admits an `account` principal, so the row is rejected
    on the way in. A future revoke would refuse it earlier, with `permission
    denied`, and that is equally correct. What must never change is that it is
    refused at all.
    """
    async with as_principal(manager(world.m1, world.org_a)) as db:
        with pytest.raises(DBAPIError, match="row-level security policy|permission denied"):
            await db.execute(
                text(f"insert into {table} ({columns}) values ({values})"),
                {"id": new_id(), "org": world.org_a, "account": world.m1},
            )


@pytest.mark.verifies("FR-IDA-004")
async def test_the_app_role_cannot_update_its_own_account_directly(
    as_principal, manager, world
):
    """P2_ACCOUNT grants `self_read` and no self-UPDATE, deliberately: an account
    that could update its own row could change its own `role`, `team_id` or
    `status`. `app_set_initial_credential` is the one narrow exception, and this
    is what makes it one."""
    async with as_principal(manager(world.m1, world.org_a)) as db:
        result = await db.execute(
            text("update account set role = 'manager' where id = :id"), {"id": world.m1}
        )

    assert result.rowcount == 0, "an account updated its own row"


# ── the credential helper's own guards ───────────────────────────────────────


@pytest.mark.verifies("FR-IDA-010")
async def test_a_deactivated_account_cannot_set_its_credential(
    as_principal, manager, world, migration_engine
):
    """Deactivation revokes sessions (FR-IDA-010), so the window is small — but a
    small window is not a control, and this function is the boundary. Without the
    `status = 'active'` guard a disabled account holding a live session set its own
    password and flipped its credential to 'set'. Reproduced in review.

    Uses a throwaway account rather than mutating a seeded one: the isolation world
    is session-scoped and shared, and order-independence is mandatory.
    """
    maker = async_sessionmaker(migration_engine, expire_on_commit=False)
    account = new_id()
    async with maker() as s, s.begin():
        await s.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role,"
                " password_hash, credential_state, status)"
                " values (:id, :org, :id, :email, 'Gone', 'manager', '$argon2-seed',"
                " 'initial', 'deactivated')"
            ),
            {"id": account, "org": world.org_a, "email": f"gone-{account}@example.com"},
        )
    try:
        async with as_principal(manager(account, world.org_a)) as db:
            changed = (
                await db.execute(
                    text("select app_set_initial_credential(:hash)"), {"hash": "$argon2-new"}
                )
            ).scalar_one()

        async with maker() as s:
            state, pw = (
                await s.execute(
                    text("select credential_state, password_hash from account where id = :id"),
                    {"id": account},
                )
            ).one()

        assert changed is False, "a deactivated account set its own credential"
        assert (state, pw) == ("initial", "$argon2-seed"), "the row changed anyway"
    finally:
        async with maker() as s, s.begin():
            await s.execute(text("delete from account where id = :id"), {"id": account})


# ── the version must be one the platform published ───────────────────────────


@pytest.mark.verifies("CMP-002")
async def test_an_unpublished_version_is_refused(as_principal, manager, world, published):
    """Integrity, not a gate control — a version nobody published never opened a
    gate, because `pending_gates` reads the current version from the same table.
    What it stops is the evidence trail holding acceptances of documents that do
    not exist."""
    async with as_principal(manager(world.m1, world.org_a)) as db:
        with pytest.raises(DBAPIError, match="no published privacy_notice"):
            await db.execute(
                text("select app_record_consent(:id, :version)"),
                {"id": new_id(), "version": "never-published-9.9"},
            )


@pytest.mark.verifies("CMP-005")
async def test_terms_acceptance_requires_both_documents_published(
    as_principal, manager, world, published
):
    """The acceptance covers Terms AND the Privacy Notice, so both versions are
    checked — and against their own kinds. Checking only the terms would leave the
    privacy half free-form, which is the half a reader is most likely to trust."""
    async with as_principal(manager(world.m1, world.org_a)) as db:
        with pytest.raises(DBAPIError, match="no published privacy_notice"):
            await db.execute(
                text("select app_record_terms_acceptance(:id, :terms, :privacy)"),
                {"id": new_id(), "terms": TERMS, "privacy": "never-published-9.9"},
            )

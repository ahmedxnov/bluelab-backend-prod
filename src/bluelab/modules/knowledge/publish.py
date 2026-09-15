"""T-4 — knowledge publish under the stale-review guard (data/02 §1).

The request carries `based_on_version`; a losing writer gets `409 stale-review`
and refetches the review (FR-KNW-011). Publish also mints the frozen snapshot a
drill freezes at its own publish (ADR-0007).

## Why a version CAS and not a lock

A manager reviews a diff, considers it, and publishes minutes later. Holding a
row lock across that is not an option, and last-write-wins would mean the second
manager publishes facts they never reviewed — approving a diff they did not see.

So the request carries the `live_version` the diff was built against, and the
update is conditional on it. Zero rows means the facts moved underneath the
review: reject, and make them look again. **The refusal is the feature.**

## The swap is pointer-free

Publishing does not repoint anything. The old live set is deleted and the draft
is relabelled `live`, and `uq_fact_set_live` — a partial unique index on
`document_id where kind = 'live'` — seals "exactly one" at the database. Delete
before relabel, or the index rejects the moment both rows are live.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found

_DROP_OLD_LIVE = text(
    "delete from fact_set where document_id = :document and kind = 'live'"
)

_HAS_DRAFT = text(
    "select exists (select 1 from fact_set where document_id = :document and kind = 'draft')"
)


async def publish_facts(
    session: AsyncSession,
    *,
    document_id: UUID,
    based_on_version: int,
    org_id: UUID | None = None,
    team_id: UUID | None = None,
    publisher_account_id: UUID | None = None,
) -> int:
    """Run T-4 inside the caller's transaction.

    Returns:
        The new `live_version`.

    Raises:
        ProblemError: `409 no-draft-to-review` if there is nothing staged, or
            `409 stale-review` if the facts moved since the diff was built. The
            client refetches `/review` and re-confirms (AC-KNW-005) — it does not
            retry blindly, because the point is that a human sees the new diff.
    """
    if org_id is None or team_id is None:
        scope = (
            await session.execute(
                text("select org_id,team_id from product_document where id=:document"),
                {"document": document_id},
            )
        ).first()
        if scope is None:
            raise not_found()
        org_id, team_id = scope.org_id, scope.team_id
    # T-4 is serialized with T-5a/T-5d on the same team advisory lock. That is
    # what makes a drill snapshot observe one coherent set of live versions.
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(cast(:team as text), 0))"),
        {"team": str(team_id)},
    )
    document = (
        await session.execute(
            text(
                "select live_version from product_document where id=:document "
                "and org_id=:org and team_id=:team for update"
            ),
            {"document": document_id, "org": org_id, "team": team_id},
        )
    ).first()
    if document is None:
        raise not_found()
    has_draft = (
        await session.execute(_HAS_DRAFT, {"document": document_id})
    ).scalar_one()
    if not has_draft:
        raise ProblemError(catalog.NO_DRAFT_TO_REVIEW)
    if publisher_account_id is None:
        publisher_account_id = (
            await session.execute(
                text(
                    "select created_by from fact_set where document_id=:document "
                    "and kind='draft'"
                ),
                {"document": document_id},
            )
        ).scalar_one()

    if int(document.live_version) != based_on_version:
        raise ProblemError(catalog.STALE_REVIEW)
    bumped = await session.execute(
        text(
            "update product_document set live_version=live_version+1,updated_at=now() "
            "where id=:document and org_id=:org and team_id=:team "
            "and live_version=:based_on_version"
        ),
        {
            "document": document_id,
            "org": org_id,
            "team": team_id,
            "based_on_version": based_on_version,
        },
    )
    if bumped.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        # Someone else published between the review being built and confirmed.
        # Zero rows is the entire detection mechanism — no read-then-write, so
        # two managers cannot both pass the check.
        raise ProblemError(catalog.STALE_REVIEW)

    # Order matters: the old live set must leave before the draft becomes live,
    # or the partial unique index sees two live sets and rejects.
    await session.execute(_DROP_OLD_LIVE, {"document": document_id})
    promoted = await session.execute(
        text(
            "update fact_set set kind='live', based_on_version=null, "
            "published_by_account_id=:publisher "
            "where document_id=:document and org_id=:org and team_id=:team and kind='draft'"
        ),
        {
            "document": document_id,
            "org": org_id,
            "team": team_id,
            "publisher": publisher_account_id,
        },
    )
    if promoted.rowcount != 1:  # type: ignore[attr-defined]
        raise ProblemError(catalog.NO_DRAFT_TO_REVIEW)

    return based_on_version + 1

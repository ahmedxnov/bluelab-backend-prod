"""T-5 — drill publish and freeze (data/02 §1).

Status transition `draft -> published`, the sum-to-100 gate (`409 weights-not-100`,
carrying `delta`), the generation-complete check, and the content freeze. Frozen
rows are guarded by triggers in `sql/triggers/`; erasure is the only sanctioned
writer that crosses them (ADR-0032, ADR-0033).

## Publish is the moment the answer key is copied, not referenced

The team's live facts are snapshotted **into** `drill.answer_key` (FR-DRL-014,
FR-KNW-007). A reference would mean a fact edited next month silently changes what
an attempt was graded against last month — and with it, a rep's recorded score and
a candidate's hiring decision. The copy is what makes a score mean the same thing
a year later (ADR-0007).

## Why sum-to-100 is here and not a constraint

A row constraint cannot see its siblings. The gate lives in this transaction, and
`meta.delta` carries the shortfall so the UI can say "+3 to balance" rather than
"invalid" (AC-DRL-003).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found

REQUIRED_WEIGHT_TOTAL = 100


def canonical_content_hash(payload: dict[str, Any]) -> str:
    """Return the lowercase SHA-256 over the v1 JCS-compatible value domain."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def publish_drill(
    session: AsyncSession,
    *,
    drill_id: UUID,
    team_id: UUID | None = None,
    org_id: UUID | None = None,
    viewer_id: UUID | None = None,
    is_manager: bool = True,
    # Release-N compatibility arguments are ignored: Phase 3 always hashes and
    # publishes the server-held generated basis.
    scenario: dict[str, Any] | None = None,
    label: str | None = None,
    content_hash: str | None = None,
) -> None:
    """Run T-5 inside the caller's transaction.

    Raises:
        ProblemError: `409 weights-not-100` carrying `delta`,
            `409 generation-incomplete` if the rubric was never generated, or
            `409 drill-not-draft` on a replay against an already-published drill.
    """
    del scenario, label, content_hash
    if team_id is None or org_id is None:
        scope = (
            await session.execute(
                text(
                    "select org_id,team_id,author_account_id from drill where id=:drill"
                ),
                {"drill": drill_id},
            )
        ).first()
        if scope is None:
            raise not_found()
        org_id, team_id = scope.org_id, scope.team_id
        viewer_id = viewer_id or scope.author_account_id
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(cast(:team as text),0))"),
        {"team": str(team_id)},
    )
    drill = (
        (
            await session.execute(
                text(
                    "select d.*,c.challenges,c.hidden_motives from drill d "
                    "join drill_concealed c on c.drill_id=d.id "
                    "where d.id=:drill and d.org_id=:org and d.team_id=:team "
                    "and (cast(:viewer as uuid) is null or d.author_account_id=:viewer or (:manager and not d.self_authored)) "
                    "for update of d"
                ),
                {
                    "drill": drill_id,
                    "org": org_id,
                    "team": team_id,
                    "viewer": viewer_id,
                    "manager": is_manager,
                },
            )
        )
        .mappings()
        .first()
    )
    if drill is None:
        raise not_found()
    if drill["status"] != "draft":
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
    dimensions = list(
        (
            await session.execute(
                text(
                    "select id,ord,name,weight,rationale from rubric_dimension where drill_id=:drill order by ord,id for update"
                ),
                {"drill": drill_id},
            )
        ).mappings()
    )
    total, dimension_count = (
        sum(int(row["weight"]) for row in dimensions),
        len(dimensions),
    )

    if dimension_count == 0:
        # No rubric means generation never completed. Publishing would create a
        # drill that can be taken and cannot be graded (FR-DRL-006).
        raise ProblemError(catalog.GENERATION_INCOMPLETE)

    if total != REQUIRED_WEIGHT_TOTAL:
        # `delta` is the contract, not a nicety: the UI renders "{±delta} to
        # balance" from it (AC-DRL-003, ux/01 §5.5).
        raise ProblemError(
            catalog.WEIGHTS_NOT_100, meta={"delta": REQUIRED_WEIGHT_TOTAL - int(total)}
        )

    if (
        drill["scenario_generation_status"] != "succeeded"
        or drill["rubric_generation_status"] != "succeeded"
        or drill["scenario"] is None
        or drill["draft_grounding"] is None
    ):
        raise ProblemError(catalog.GENERATION_INCOMPLETE)

    current_vector = [
        {"document_id": str(row.document_id), "live_version": row.live_version}
        for row in (
            await session.execute(
                text(
                    "select d.id document_id,d.live_version from product_document d join fact_set s on s.document_id=d.id and s.kind='live' where d.org_id=:org and d.team_id=:team order by d.id for share of d"
                ),
                {"org": org_id, "team": team_id},
            )
        ).all()
    ]
    captured_vector = [
        {
            "document_id": str(document["document_id"]),
            "live_version": document["live_version"],
        }
        for document in drill["draft_grounding"].get("documents", [])
    ]
    if current_vector != captured_vector:
        raise ProblemError(catalog.GROUNDING_STALE)

    scenario_value = dict(drill["scenario"])
    hash_payload = {
        "v": 1,
        "call_type": drill["call_type"],
        "lead_type": drill["lead_type"],
        "language": drill["language"],
        "label": drill["label"],
        "inputs": {
            "challenges": drill["challenges"],
            "hidden_motives": drill["hidden_motives"],
        },
        "scenario": scenario_value,
        "rubric": [
            {
                "id": str(row["id"]),
                "ord": row["ord"],
                "name": row["name"],
                "weight": row["weight"],
                "rationale": row["rationale"],
            }
            for row in dimensions
        ],
        "answer_key": drill["draft_grounding"],
    }
    digest = canonical_content_hash(hash_payload)
    published = await session.execute(
        text(
            "update drill set status='published',published_at=now(),updated_at=now(),answer_key=draft_grounding,draft_grounding=null,content_hash=:hash where id=:drill and status='draft'"
        ),
        {"drill": drill_id, "hash": digest},
    )
    if published.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        # Conditional on `status = 'draft'`, so two racing publishes cannot both
        # win and a replay against a published drill is refused rather than
        # rewriting frozen content.
        raise ProblemError(catalog.DRILL_NOT_DRAFT)

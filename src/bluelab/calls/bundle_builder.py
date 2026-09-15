"""Builds the runtime bundle from the frozen drill (api/02 §1.1).

**Security-relevant component.** This is the one place a concealment leak could
originate, so it sits in the invariant-path quality band (quality/01 §4,
ADR-0071 negative consequence 3): branch coverage plus mutation testing plus an
explicit adversarial case.

The bundle model declares no field for product facts, the rubric, the answer key,
or any element of `drill_concealed`, and forbids unknown keys — so a smuggled key
fails to parse rather than reaching prompt assembly. The model itself lives in
`bluelab_runtime_bundle`, version-locked with the call plane.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.calls.registry import CallSession
from bluelab.platform.errors.denial import not_found
from bluelab_runtime_bundle import (
    Participant,
    PersonaSections,
    RuntimeBundle,
    Scenario,
    assert_runtime_safe,
)


async def build_bundle(session: AsyncSession, call: CallSession) -> RuntimeBundle:
    row = (
        (
            await session.execute(
                text("""select call_type,lead_type,language,scenario,answer_key
                      from drill where id=:drill and org_id=:org and team_id=:team
                       and status in ('published','archived')"""),
                {"drill": call.drill_id, "org": call.org_id, "team": call.team_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise not_found()
    frozen = dict(row["scenario"] or {})
    persona = dict(frozen.get("persona") or {})
    facts = [
        str(fact.get("value", ""))
        for document in (row["answer_key"] or {}).get("documents", [])
        for fact in document.get("facts", [])
        if fact.get("value")
    ]
    meta = persona.get("meta_facts") or []
    buyer_meta = {f"fact_{index}": str(value) for index, value in enumerate(meta, 1)}
    buyer_name = str(persona.get("name") or "Buyer")
    buyer_role = str(persona.get("role") or "Buyer")
    buyer_company = str(persona.get("company") or "Company")
    context = str(frozen.get("context") or "A sales conversation")
    product_summary = (
        " ".join(facts[:3]) or "Use only participant-safe product information."
    )
    bundle = RuntimeBundle(
        call_id=str(call.call_id),
        mode=call.mode,
        attempt_id=str(call.attempt_id) if call.attempt_id else None,
        drill_id=str(call.drill_id),
        org_id=str(call.org_id),
        team_id=str(call.team_id),
        participant=Participant(
            kind=call.participant_kind,
            identity=call.participant_identity,
            display_name=call.participant_display_name,
        ),
        scenario=Scenario(
            buyer_name=buyer_name,
            buyer_role=buyer_role,
            buyer_company=buyer_company,
            buyer_meta=buyer_meta,
            situation_context=context,
            participant_product_summary=product_summary,
        ),
        persona=PersonaSections(
            who_you_are=f"You are {buyer_name}, {buyer_role} at {buyer_company}.",
            your_world=" ".join(str(value) for value in meta) or buyer_company,
            where_you_are_right_now=context,
            call_context=context,
        ),
        language=str(row["language"]),
        call_type=row["call_type"],
        lead_type=row["lead_type"],
        voice_identity="egyptian-female-1",
        prompt_versions={"buyer": "v1"},
        model_versions={"runtime": "v1"},
    )
    return assert_runtime_safe(bundle)

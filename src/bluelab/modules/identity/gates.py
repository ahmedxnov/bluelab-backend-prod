"""The sign-in state gates (api/00 §3).

An account with an initial credential authenticates into a **gate-limited
session**: every endpoint except `GET /auth/session` and `POST /auth/first-sign-in`
answers `409 first-sign-in-required` until the gate completes (FR-IDA-004). A
material notice or terms change puts an established session into the same limited
shape until `POST /auth/acceptances` clears it (CMP-002 / CMP-005).

## Why these are computed, not stored

There is no `pending_gates` column, and there should not be. A gate opens when a
*document version* changes — `legal_document_version` gains a row — which is an
event about the platform, not about any one account. Storing the answer would mean
finding and rewriting every affected account at publish time, and the first one
missed is a person who silently keeps access they should have been re-asked for.

Derived on read, the same publish is a single insert and every session picks it up
the next time this function runs. This is the derived-on-read stance the V-1…V-11
views take (ADR-0034), applied to consent.

## What is derived on read, and what is enforced — they are not the same set

`pending_gates` is called on sign-in and on `GET /auth/session`. Its answer is
reported in the body *and*, at sign-in, written to `SessionRecord.gate`, which is
what `api.deps.current_principal` refuses on. So a gate is **enforced from the next
sign-in onward**, not from the next request.

The difference matters for a publish landing mid-session: an account already
signed in keeps a record whose gate predates it, and goes on reaching the product
surface until the session ends. `GET /auth/session` will tell that client it is
behind a gate; nothing yet stops it from ignoring the answer.

Closing that needs a decision this module cannot make alone — re-derive per request
(a database round trip on every call), or invalidate the affected sessions at
publish time. **OPEN, and tracked as such.** Until then the enforcement boundary is
sign-in, and this docstring says so rather than implying the stronger guarantee.

## Precedence, and why it is a total order

`first_sign_in` subsumes the other two: completing it records consent and terms in
the same transaction, so an account behind it is never *also* meaningfully behind
the others. `SessionRecord.gate` holds a single value, so the order here decides
which gate a limited session carries.
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.identity.models import (
    CREDENTIAL_INITIAL,
    Account,
    LegalDocumentVersion,
)
from bluelab.modules.identity.schemas import Gate

KIND_NOTICE = "privacy_notice"
KIND_TERMS = "terms_of_use"


async def current_versions(session: AsyncSession, *kinds: str) -> dict[str, str]:
    """The newest effective version of each named document, keyed by kind.

    One round trip for all kinds rather than one apiece. `pending_gates` runs on
    every sign-in *and* every session read — the two most frequent calls on this
    surface — so the difference is on the hottest path there is.

    A kind absent from the result has no published document. That is not a pending
    gate: an account cannot be behind a version that does not exist, and treating
    it as one would lock every user out of a fresh environment.

    `DISTINCT ON (kind)` with `effective_at DESC` is the newest row per kind in a
    single indexed scan — the set-shaped form of the `LIMIT 1` this replaced, with
    the same tie-breaking (none: two rows sharing an `effective_at` are equally
    newest and either may win, exactly as before).
    """
    rows = await session.execute(
        select(LegalDocumentVersion.kind, LegalDocumentVersion.version)
        .where(LegalDocumentVersion.kind.in_(kinds))
        .distinct(LegalDocumentVersion.kind)
        .order_by(LegalDocumentVersion.kind, LegalDocumentVersion.effective_at.desc())
    )
    return {kind: version for kind, version in rows}


_HAS_ACCEPTED = text("select app_account_has_accepted(:account, :kind, :version)")
"""The enumerated definer helper that answers the acceptance question.

`consent_record` and `terms_acceptance` are `P9_OPS` with `system_write_only`, so
their only read policies are `ops_read` and the system ones: an account cannot
read its own acceptance rows, and under its scope a count comes back zero whether
or not consent exists. Widening those policies would have made the entire evidence
trail account-readable to answer a yes/no question; the helper returns the boolean
and nothing else. See its comment in `tools/generate_rls_policies.py`.
"""


async def pending_gates(session: AsyncSession, account: Account) -> tuple[Gate, ...]:
    """Which gates currently block full access for this account.

    Args:
        session: A session scoped to this account.
        account: The authenticated account.

    Returns:
        Gates in precedence order — see the module docstring. Empty when the
        account is fully admitted.
    """
    if account.credential_state == CREDENTIAL_INITIAL:
        # Subsumes the rest: completing first sign-in records both instruments.
        return ("first_sign_in",)

    versions = await current_versions(session, KIND_NOTICE, KIND_TERMS)

    # Sequential on purpose, not gathered. These share one `AsyncSession`, which is
    # not safe for concurrent use — `asyncio.gather` over two queries on the same
    # session interleaves them on one connection and corrupts its state. Batching
    # the version lookup above is the round trip that could be removed safely.
    gates: list[Gate] = []

    notice = versions.get(KIND_NOTICE)
    if notice is not None and not await _has_accepted(session, account, KIND_NOTICE, notice):
        gates.append("consent")

    terms = versions.get(KIND_TERMS)

    # The terms gate needs BOTH documents published, not just the terms.
    #
    # `terms_acceptance` records a Terms version AND a Privacy Notice version —
    # one instrument covering both (CMP-005), and `privacy_version` is NOT NULL.
    # So terms cannot be accepted while no notice is published.
    #
    # Keyed on `terms` alone, this opened a gate nothing could close: terms
    # published without a notice left first sign-in completing with
    # `pending_gates: ["terms"]`, and `POST /auth/acceptances` answering 200 while
    # recording nothing, forever. Verified — the account was locked out of every
    # guarded route with a 200 telling it everything was fine.
    #
    # Requiring both restores the rule the notice branch above already follows:
    # an account cannot be behind a version that does not exist.
    if (
        terms is not None
        and notice is not None
        and not await _has_accepted(session, account, KIND_TERMS, terms)
    ):
        gates.append("terms")

    return tuple(gates)


async def _has_accepted(
    session: AsyncSession, account: Account, kind: str, version: str
) -> bool:
    """Has this account accepted this version of this document?

    The account id comes from the loaded `Account`, which came from the session
    record — never from anything the request carried. A caller cannot ask about
    somebody else's acceptance because it has no way to name them.
    """
    return bool(
        (
            await session.execute(
                _HAS_ACCEPTED,
                {"account": account.id, "kind": kind, "version": version},
            )
        ).scalar_one()
    )

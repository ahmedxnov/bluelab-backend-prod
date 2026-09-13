"""Assignment full-replace and allowance-refresh behaviour (FR-TRM-011..013).

The tests use the real ASGI application, PostgreSQL, RLS, and the T-1 allowance
rows. They deliberately exercise the reads a rep and manager make after a write,
rather than asserting only that the mutation returned 200.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.platform.ids import new_id

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]

MANAGER_PASSWORD = "correct-horse-battery-staple"  # pragma: allowlist secret


def _payload(*, recipients: list[str], due_date: date, attempts_allowed: int) -> dict[str, object]:
    return {
        "recipient_account_ids": recipients,
        "due_date": due_date.isoformat(),
        "attempts_allowed": attempts_allowed,
    }


async def _library_card(client: AsyncClient, drill_id: str) -> dict[str, object]:
    response = await client.get("/api/v1/me/library")
    assert response.status_code == 200, response.text
    return next(card for card in response.json()["data"] if card["drill_id"] == drill_id)


async def _record_current_consent(training_engine, *, org_id, account_ids) -> None:
    """Clear the unrelated legal gate for principals used by this test module."""
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        notice_version = (
            await session.execute(
                text(
                    "select version from legal_document_version "
                    "where kind = 'recording_consent_notice' and effective_at <= now() "
                    "order by effective_at desc, version desc limit 1"
                )
            )
        ).scalar_one()
        terms_version, privacy_version = (
            await session.execute(
                text(
                    "select "
                    "(select version from legal_document_version "
                    " where kind = 'terms_of_use' and effective_at <= now() "
                    " order by effective_at desc, version desc limit 1), "
                    "(select version from legal_document_version "
                    " where kind = 'privacy_notice' and effective_at <= now() "
                    " order by effective_at desc, version desc limit 1)"
                )
            )
        ).one()
        for account_id in account_ids:
            await session.execute(
                text(
                    "insert into consent_record (id, org_id, account_id, notice_version) "
                    "values (:id, :org, :account, :version) "
                    "on conflict (account_id, notice_version) do nothing"
                ),
                {
                    "id": new_id(),
                    "org": org_id,
                    "account": account_id,
                    "version": notice_version,
                },
            )
            await session.execute(
                text(
                    "insert into terms_acceptance "
                    "(id, org_id, account_id, terms_version, privacy_version) "
                    "values (:id, :org, :account, :terms, :privacy) "
                    "on conflict (account_id, terms_version, privacy_version) do nothing"
                ),
                {
                    "id": new_id(),
                    "org": org_id,
                    "account": account_id,
                    "terms": terms_version,
                    "privacy": privacy_version,
                },
            )


@pytest_asyncio.fixture(autouse=True)
async def _clear_test_acceptances(training_engine, world):
    """Remove test-only gate records before the shared world's account teardown."""
    yield
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("delete from consent_record where org_id = :org"), {"org": world.org})
        await session.execute(text("delete from terms_acceptance where org_id = :org"), {"org": world.org})


@pytest.mark.verifies("FR-TRM-011", "FR-TRM-012", "FR-TRM-013", "AC-TRM-005")
async def test_assignment_create_then_replace_refreshes_the_recipients_library(
    client, sign_in, world, training_engine
) -> None:
    """A replacement changes recipients, date and allowance without a duplicate.

    The initial delete gives this test an actual create path. The second PUT is a
    full replacement: the old recipient retains the published team-library card
    but no assignment block, while the new recipient gets the new date and a
    zeroed counter.
    """
    drill_id = str(world.drill_renewal)
    assignment_path = f"/api/v1/drills/{drill_id}/assignment"
    first_due = date(2030, 1, 10)
    replacement_due = date(2030, 2, 20)

    # This shared drill is deliberately assigned. Removing only the assignment
    # row gives the endpoint a real create path; its FK cascade clears recipients.
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text("delete from assignment where drill_id = :drill"),
            {"drill": world.drill_renewal},
        )

    await _record_current_consent(
        training_engine,
        org_id=world.org,
        account_ids=(world.manager, world.rep, world.other_rep),
    )
    await sign_in(world.manager_email)
    before_manager_stats = await client.get(f"/api/v1/team/drills/{drill_id}/stats")
    assert before_manager_stats.status_code == 200, before_manager_stats.text
    initial = await client.put(
        assignment_path,
        json=_payload(
            recipients=[str(world.rep)], due_date=first_due, attempts_allowed=4
        ),
    )
    assert initial.status_code == 200, initial.text
    assert initial.json()["due_date"] == first_due.isoformat()
    assert initial.json()["attempts_allowed"] == 4
    assert initial.json()["recipients"] == [
        {
            "account_id": str(world.rep),
            "attempts_used": 0,
            "granted_at": initial.json()["recipients"][0]["granted_at"],
        }
    ]

    replacement = await client.put(
        assignment_path,
        json=_payload(
            recipients=[str(world.other_rep)],
            due_date=replacement_due,
            attempts_allowed=2,
        ),
    )
    assert replacement.status_code == 200, replacement.text
    body = replacement.json()
    assert body["due_date"] == replacement_due.isoformat()
    assert body["attempts_allowed"] == 2
    assert [recipient["account_id"] for recipient in body["recipients"]] == [
        str(world.other_rep)
    ]
    assert body["recipients"][0]["attempts_used"] == 0

    # Assignment is separate mutable state, not frozen drill content or scored
    # work. The manager's drill detail therefore stays coherent after the write.
    after_manager_stats = await client.get(f"/api/v1/team/drills/{drill_id}/stats")
    assert after_manager_stats.status_code == 200, after_manager_stats.text
    assert after_manager_stats.json() == before_manager_stats.json()

    await sign_in(world.rep_email)
    old_recipient_card = await _library_card(client, drill_id)
    assert old_recipient_card["source"] == "library"
    assert old_recipient_card["assignment"] is None

    await sign_in(world.other_rep_email)
    new_recipient_card = await _library_card(client, drill_id)
    assert new_recipient_card["source"] == "assigned"
    assert new_recipient_card["assignment"] == {
        "due_date": replacement_due.isoformat(),
        "attempts_used": 0,
        "attempts_allowed": 2,
        "locked": False,
    }


@pytest.mark.verifies("FR-TRM-012", "FR-TRP-013", "AC-TRP-005")
async def test_replaying_assignment_resets_a_retained_recipients_allowance(
    client, sign_in, world, training_engine
) -> None:
    """A same-body replay is a re-assignment, not a duplicate or a no-op."""
    drill_id = str(world.drill_discovery)
    path = f"/api/v1/drills/{drill_id}/assignment"
    payload = _payload(
        recipients=[str(world.rep)], due_date=date(2030, 3, 15), attempts_allowed=3
    )

    await _record_current_consent(
        training_engine, org_id=world.org, account_ids=(world.manager, world.rep)
    )
    await sign_in(world.manager_email)
    first = await client.put(path, json=payload)
    assert first.status_code == 200, first.text

    # Model T-1 having consumed the fresh grant. The next identical PUT must
    # replace it with a fresh counter; it must not merely echo this row back.
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "update assignment_recipient set attempts_used = 3 "
                "where assignment_id = (select id from assignment where drill_id = :drill) "
                "and rep_account_id = :rep"
            ),
            {"drill": world.drill_discovery, "rep": world.rep},
        )

    replay = await client.put(path, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["recipients"][0]["attempts_used"] == 0

    async with maker() as session:
        assignment_count, recipient_count, attempts_used = (
            await session.execute(
                text(
                    "select (select count(*) from assignment where drill_id = :drill), "
                    "       (select count(*) from assignment_recipient ar "
                    "          join assignment a on a.id = ar.assignment_id "
                    "         where a.drill_id = :drill), "
                    "       (select ar.attempts_used from assignment_recipient ar "
                    "          join assignment a on a.id = ar.assignment_id "
                    "         where a.drill_id = :drill and ar.rep_account_id = :rep)"
                ),
                {"drill": world.drill_discovery, "rep": world.rep},
            )
        ).one()
    assert (assignment_count, recipient_count, attempts_used) == (1, 1, 0)

    await sign_in(world.rep_email)
    card = await _library_card(client, drill_id)
    assert card["assignment"]["locked"] is False
    assert card["assignment"]["due_date"] == payload["due_date"]


@pytest.mark.verifies("FR-TRM-012", "FR-TRM-013")
async def test_simultaneous_assignment_updates_end_in_one_complete_last_write(
    app, world, training_engine
) -> None:
    """Concurrent managers sessions cannot duplicate or interleave assignments."""
    drill_id = str(world.drill_renewal)
    path = f"/api/v1/drills/{drill_id}/assignment"
    left = _payload(
        recipients=[str(world.rep)], due_date=date(2030, 4, 1), attempts_allowed=2
    )
    right = _payload(
        recipients=[str(world.other_rep)], due_date=date(2030, 5, 2), attempts_allowed=5
    )

    await _record_current_consent(
        training_engine, org_id=world.org, account_ids=(world.manager,)
    )

    async def session_with_manager_cookie() -> AsyncClient:
        http = AsyncClient(transport=ASGITransport(app=app), base_url="https://api.test")
        response = await http.post(
            "/api/v1/auth/session",
            json={"email": world.manager_email, "password": MANAGER_PASSWORD},
        )
        assert response.status_code == 200, response.text
        return http

    one, two = await asyncio.gather(session_with_manager_cookie(), session_with_manager_cookie())
    try:
        first, second = await asyncio.gather(
            one.put(path, json=left), two.put(path, json=right)
        )
        assert first.status_code == second.status_code == 200
    finally:
        await one.aclose()
        await two.aclose()

    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session:
        count, due_date, attempts_allowed, recipient_ids = (
            await session.execute(
                text(
                    "select count(*), max(a.due_date), max(a.attempts_allowed), "
                    "       array_agg(ar.rep_account_id order by ar.rep_account_id) "
                    "  from assignment a "
                    "  left join assignment_recipient ar on ar.assignment_id = a.id "
                    " where a.drill_id = :drill"
                ),
                {"drill": world.drill_renewal},
            )
        ).one()

    assert count == 1
    persisted = (
        due_date.isoformat(),
        attempts_allowed,
        tuple(str(value) for value in recipient_ids),
    )
    expected = {
        (left["due_date"], left["attempts_allowed"], (str(world.rep),)),
        (right["due_date"], right["attempts_allowed"], (str(world.other_rep),)),
    }
    assert persisted in expected


@pytest.mark.verifies("FR-TRM-011", "FR-TRM-013", "AC-TRM-006")
async def test_assignment_denies_nonpublished_and_out_of_team_drills(
    client, sign_in, world, team_world, training_engine
) -> None:
    payload = _payload(
        recipients=[str(world.rep)], due_date=date(2030, 6, 1), attempts_allowed=1
    )

    await _record_current_consent(
        training_engine, org_id=world.org, account_ids=(world.manager,)
    )
    await sign_in(world.manager_email)
    for drill_id in (world.manager_draft, world.archived_drill):
        response = await client.put(f"/api/v1/drills/{drill_id}/assignment", json=payload)
        assert response.status_code == 409
        assert response.json()["type"] == "/problems/drill-not-startable"

    cross_team = await client.put(
        f"/api/v1/drills/{team_world.other_drill}/assignment", json=payload
    )
    assert cross_team.status_code == 404
    assert cross_team.json()["type"] == "/problems/not-found"

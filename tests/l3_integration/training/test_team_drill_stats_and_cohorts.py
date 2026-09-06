"""Manager drill statistics and deterministic assignment quick picks."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]


@pytest.mark.verifies("FR-SCR-016", "FR-TRM-009", "AC-TRM-004")
async def test_published_drill_stats_use_v7_and_v8(client, sign_in, world) -> None:
    await sign_in(world.manager_email)

    response = await client.get(f"/api/v1/team/drills/{world.drill_discovery}/stats")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["drill_id"] == str(world.drill_discovery)
    assert body["rollup"] == {
        "team_average": {"score": 8.6, "band": "green"},
        "reps_practiced": 2,
        "eligible_reps": 2,
        "total_attempts": 3,
    }
    assert [row["account_id"] for row in body["leaderboard"]] == [
        str(world.other_rep),
        str(world.rep),
    ]
    assert [row["best"] for row in body["leaderboard"]] == [
        {"score": 9.9, "band": "green"},
        {"score": 8.4, "band": "green"},
    ]
    assert all(row["latest_attempt_id"] for row in body["leaderboard"])


@pytest.mark.verifies("AC-TRM-006", "FR-TRP-009")
async def test_stats_deny_other_team_and_private_drills(client, sign_in, world, team_world) -> None:
    await sign_in(team_world.manager_email)

    for drill_id in (world.drill_discovery, world.self_drill):
        response = await client.get(f"/api/v1/team/drills/{drill_id}/stats")
        assert response.status_code == 404
        assert response.json()["type"] == "/problems/not-found"


@pytest.mark.verifies("FR-TRM-014")
async def test_cohorts_follow_the_authoritative_membership_rules(
    client, sign_in, team_world, org_month
) -> None:
    await sign_in(team_world.manager_email)

    response = await client.get("/api/v1/team/cohorts")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == await org_month()
    assert body["bottom_half"] == [str(team_world.reps[4]), str(team_world.reps[3])]
    assert body["rating_below_6"] == []
    assert set(body["fewer_than_5_attempts"]) == {
        *{str(rep) for rep in team_world.reps},
        str(team_world.unrated_rep),
    }
    assert str(team_world.deactivated_rep) not in body["fewer_than_5_attempts"]
    assert body["newest_joiners"] == sorted(str(rep) for rep in team_world.reps)


@pytest.mark.verifies("FR-TRM-014")
async def test_bottom_half_and_below_six_are_empty_without_ratings(
    client, sign_in, world, org_month
) -> None:
    await sign_in(world.manager_email)
    prior_month = await org_month(months_back=2)

    response = await client.get("/api/v1/team/cohorts", params={"month": prior_month})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bottom_half"] == []
    assert body["rating_below_6"] == []
    assert set(body["fewer_than_5_attempts"]) == {str(world.rep), str(world.other_rep)}


async def test_a_rep_cannot_reach_stats_or_cohorts(client, sign_in, world) -> None:
    await sign_in(world.rep_email)

    stats = await client.get(f"/api/v1/team/drills/{world.drill_discovery}/stats")
    cohorts = await client.get("/api/v1/team/cohorts")

    assert stats.status_code == cohorts.status_code == 404

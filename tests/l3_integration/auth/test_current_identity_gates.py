"""Current legal evidence and authorization, exercised through the real API/RLS."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

KINDS = ("recording_consent_notice", "terms_of_use", "privacy_notice")
SIGN_IN = "/api/v1/auth/session"
ACCEPTANCES = "/api/v1/auth/acceptances"
FIRST = "/api/v1/auth/first-sign-in"
BODY = {"consent": True, "terms_accepted": True}


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_the_two_records_retain_their_distinct_document_versions(
    guarded, world, credentials, legal_version, auth_engine
):
    for kind, version in zip(KINDS, ("consent-1", "terms-2", "privacy-3"), strict=True):
        await legal_version(kind, version)
    await guarded.http.post(SIGN_IN, json=credentials(world.initial_email))
    completed = await guarded.http.post(
        FIRST, json={**BODY, "new_password": "replacement-test-passphrase"}
    )
    assert completed.status_code == 200, completed.text
    async with async_sessionmaker(auth_engine)() as db:
        consent = (
            await db.execute(
                text(
                    "select notice_version from consent_record where account_id = :id"
                ),
                {"id": world.initial},
            )
        ).scalar_one()
        terms = (
            await db.execute(
                text(
                    "select terms_version, privacy_version from terms_acceptance where account_id = :id"
                ),
                {"id": world.initial},
            )
        ).one()
    assert consent == "consent-1"
    assert tuple(terms) == ("terms-2", "privacy-3")


@pytest.mark.verifies("CMP-005")
async def test_terms_gate_requires_both_versions_in_one_row(
    guarded, world, credentials, legal_version, auth_engine, record_consent
):
    from bluelab.platform.ids import new_id

    await publish_all(legal_version)
    await record_consent(world.rep, "v1")
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        for terms, privacy in (("v1", "old-privacy"), ("old-terms", "v1")):
            await db.execute(
                text(
                    "insert into terms_acceptance (id, org_id, account_id, terms_version, privacy_version)"
                    " values (:id, :org, :account, :terms, :privacy)"
                ),
                {
                    "id": new_id(),
                    "org": world.org,
                    "account": world.rep,
                    "terms": terms,
                    "privacy": privacy,
                },
            )
    response = await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    assert response.json()["pending_gates"] == ["terms"]
    assert (await guarded.http.get(guarded.PROBE)).status_code == 409


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_acceptance_on_another_device_does_not_upgrade_a_limited_cookie(
    guarded, world, credentials, legal_version
):
    await publish_all(legal_version)
    first = await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    older_cookie = first.cookies["__Host-bluelab_session"]
    await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    assert (await guarded.http.post(ACCEPTANCES, json=BODY)).status_code == 200
    guarded.http.cookies.set(
        "__Host-bluelab_session", older_cookie, domain="api.test", path="/"
    )
    assert (await guarded.http.get(guarded.PROBE)).status_code == 401


async def publish_all(publish):
    for kind in KINDS:
        await publish(kind, "v1", effective_offset=-10)


@pytest.mark.verifies("FR-IDA-004", "CMP-002", "CMP-005")
@pytest.mark.parametrize(
    "published",
    [
        (),
        *[(k,) for k in KINDS],
        *[tuple(k for k in KINDS if k != missing) for missing in KINDS],
    ],
)
async def test_missing_legal_documents_cannot_complete_onboarding(
    guarded, world, credentials, auth_engine, legal_version, published
):
    for kind in published:
        await legal_version(kind, "v1", effective_offset=-10)
    await guarded.http.post(SIGN_IN, json=credentials(world.initial_email))
    response = await guarded.http.post(
        FIRST, json={**BODY, "new_password": "replacement-test-passphrase"}
    )
    assert response.status_code == 409, response.text
    assert "set-cookie" not in response.headers
    assert (await guarded.http.get(guarded.PROBE)).json()[
        "type"
    ] == "/problems/first-sign-in-required"
    async with async_sessionmaker(auth_engine)() as db:
        row = (
            await db.execute(
                text(
                    "select credential_state, "
                    "(select count(*) from consent_record where account_id = :id), "
                    "(select count(*) from terms_acceptance where account_id = :id) "
                    "from account where id = :id"
                ),
                {"id": world.initial},
            )
        ).one()
        assert tuple(row) == ("initial", 0, 0)
    assert (
        await guarded.http.post(SIGN_IN, json=credentials(world.initial_email))
    ).status_code == 200


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_missing_documents_keep_an_existing_account_gated(
    guarded, world, credentials
):
    response = await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    assert response.json()["pending_gates"] == ["consent", "terms"]
    assert (await guarded.http.post(ACCEPTANCES, json=BODY)).status_code == 409
    assert (await guarded.http.get(guarded.PROBE)).status_code == 409


@pytest.mark.verifies("CMP-002", "CMP-005")
@pytest.mark.parametrize(
    "kind,gate",
    [
        (KINDS[0], "consent-required"),
        (KINDS[1], "terms-acceptance-required"),
        (KINDS[2], "terms-acceptance-required"),
    ],
)
async def test_publication_blocks_next_product_request_and_acceptance_restores_access(
    guarded, world, credentials, legal_version, kind, gate
):
    await publish_all(legal_version)
    await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    assert (await guarded.http.post(ACCEPTANCES, json=BODY)).json()[
        "pending_gates"
    ] == []
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200
    await legal_version(kind, "v2", effective_offset=-1)
    # No GET /auth/session, re-login, or frontend action before the protected call.
    blocked = await guarded.http.get(guarded.PROBE)
    assert blocked.status_code == 409
    assert blocked.json()["type"] == f"/problems/{gate}"
    if kind == "privacy_notice":
        consent_only = await guarded.http.post(ACCEPTANCES, json={"consent": True})
        assert consent_only.json()["pending_gates"] == ["terms"]
        assert (await guarded.http.get(guarded.PROBE)).status_code == 409
    assert (await guarded.http.post(ACCEPTANCES, json=BODY)).json()[
        "pending_gates"
    ] == []
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_future_versions_do_not_open_gates(
    guarded, world, credentials, legal_version
):
    await publish_all(legal_version)
    await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    await guarded.http.post(ACCEPTANCES, json=BODY)
    for kind in KINDS:
        await legal_version(kind, "scheduled", effective_offset=60)
    assert (await guarded.http.get(SIGN_IN)).json()["pending_gates"] == []
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200


@pytest.mark.verifies("FR-IDA-010")
async def test_deactivation_blocks_next_request_even_if_session_remains_in_valkey(
    guarded, world, credentials, legal_version, auth_engine
):
    await publish_all(legal_version)
    await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    await guarded.http.post(ACCEPTANCES, json=BODY)
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text("update account set status = 'deactivated' where id = :id"),
            {"id": world.rep},
        )
    assert (await guarded.http.get(guarded.PROBE)).status_code == 401
    assert (await guarded.http.get(SIGN_IN)).status_code == 401


@pytest.mark.verifies("FR-IDA-009")
async def test_changed_role_revokes_the_old_cookie(
    guarded, world, credentials, legal_version, auth_engine
):
    await publish_all(legal_version)
    await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    await guarded.http.post(ACCEPTANCES, json=BODY)
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text("update account set role = 'manager', team_id = id where id = :id"),
            {"id": world.rep},
        )
    assert (await guarded.http.get(guarded.PROBE)).status_code == 401
    signed_in = await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    assert signed_in.json()["role"] == "manager"
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200


@pytest.mark.verifies("FR-IDA-009")
async def test_team_mapping_is_current_even_without_reading_auth_session(
    guarded, world, credentials, legal_version, auth_engine
):
    from bluelab.platform.ids import new_id

    await publish_all(legal_version)
    await guarded.http.post(SIGN_IN, json=credentials(world.rep_email))
    await guarded.http.post(ACCEPTANCES, json=BODY)
    assert (await guarded.http.get("/api/v1/me/progress")).status_code == 200
    new_manager = new_id()
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " select :new, org_id, :new, :email, 'New manager', 'manager', password_hash"
                " from account where id = :old"
            ),
            {
                "new": new_manager,
                "email": f"{new_manager}@example.com",
                "old": world.manager,
            },
        )
        await db.execute(
            text("update account set team_id = :team where id = :id"),
            {"team": new_manager, "id": world.rep},
        )
    # The probe echoes the resolved scope, before any session-screen refresh.
    response = await guarded.http.get(guarded.SCOPE)
    assert response.json()["team_id"] == str(new_manager)


@pytest.mark.verifies("FR-IDA-004")
async def test_other_initial_credential_cookie_is_not_silently_upgraded(
    guarded, world, credentials, legal_version
):
    await publish_all(legal_version)
    first = await guarded.http.post(SIGN_IN, json=credentials(world.initial_email))
    older_cookie = first.cookies["__Host-bluelab_session"]
    await guarded.http.post(SIGN_IN, json=credentials(world.initial_email))
    completed = await guarded.http.post(
        FIRST, json={**BODY, "new_password": "replacement-test-passphrase"}
    )
    assert completed.status_code == 200
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200
    guarded.http.cookies.set(
        "__Host-bluelab_session", older_cookie, domain="api.test", path="/"
    )
    assert (await guarded.http.get(guarded.PROBE)).status_code == 401

"""The two gate exits — `POST /auth/first-sign-in` and `POST /auth/acceptances`.

Until these existed, both gates were one-way doors: an account with a provisioned
credential signed in, was refused everything with `409 first-sign-in-required`,
and had no endpoint to clear it. So the battery here is about what completing a
gate must do *and* what it must refuse to do half-way.

Three properties carry most of the weight:

**Atomicity.** Password, consent and terms commit together or not at all
(FR-IDA-004: "completing any subset alone does not grant access"). The 422 tests
assert the evidence tables are still EMPTY afterwards — a 422 that had already
written a consent row would satisfy a status-code assertion and violate
AC-IDA-002.

**Partial acceptance.** An account behind both consent and terms that supplies
only consent still owes terms. The session's gate must become `terms`, not None.
Clearing it would hand over the product surface with an instrument outstanding.

**Rotation.** Completing first sign-in is an authentication-level change, so the
session id must change (ASVS 3.2.1). Asserted by replaying the OLD cookie.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from tests.l3_integration.auth.conftest import PASSWORD

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

SESSION_COOKIE = "__Host-bluelab_session"
NEW_PASSWORD = "a-genuinely-new-passphrase"  # pragma: allowlist secret
FIRST_SIGN_IN = "/api/v1/auth/first-sign-in"
ACCEPTANCES = "/api/v1/auth/acceptances"


def cookie_of(response) -> str:
    return response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]


async def counts(engine, account) -> tuple[int, int]:
    """(consent rows, terms rows) for one account, read as the migration role."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        consent = await s.execute(
            text("select count(*) from consent_record where account_id = :a"), {"a": account}
        )
        terms = await s.execute(
            text("select count(*) from terms_acceptance where account_id = :a"), {"a": account}
        )
        return consent.scalar_one(), terms.scalar_one()


# ── first sign-in: the happy path, so the refusals below mean something ───────


@pytest.mark.verifies("FR-IDA-004")
async def test_completing_first_sign_in_clears_the_gate(client, world, credentials, legal_version):
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    response = await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["pending_gates"] == []
    assert response.json()["account_id"] == str(world.initial)


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_it_records_both_instruments_at_their_current_versions(
    client, world, credentials, legal_version, auth_engine
):
    """Two rows in two tables — the separation CMP-002/CMP-005 require, asserted
    against the versions actually published rather than whatever was submitted."""
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )

    assert await counts(auth_engine, world.initial) == (1, 1)


@pytest.mark.verifies("FR-IDA-004")
async def test_the_new_password_is_what_signs_in_afterwards(
    client, world, credentials, legal_version
):
    """The credential really changed — not just `credential_state`."""
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))
    await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )
    await client.delete("/api/v1/auth/session")

    old = await client.post(
        "/api/v1/auth/session", json=credentials(world.initial_email, PASSWORD)
    )
    new = await client.post(
        "/api/v1/auth/session", json=credentials(world.initial_email, NEW_PASSWORD)
    )

    assert old.status_code == 401, "the provisioned credential still works"
    assert new.status_code == 200
    assert new.json()["pending_gates"] == []


@pytest.mark.verifies("SEC-002")
async def test_the_session_id_rotates_on_completion(client, world, credentials, legal_version):
    """ASVS 3.2.1 — setting the real password is an authentication-level change.

    Asserted by replaying the OLD cookie: a value captured before the change (a
    shoulder-surfed cookie, a proxy log from the provisioning email) must not
    still open the now fully-privileged session.
    """
    await legal_version("privacy_notice", "2026.1")
    signed_in = await client.post("/api/v1/auth/session", json=credentials(world.initial_email))
    before = cookie_of(signed_in)

    completed = await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )
    after = cookie_of(completed)

    assert before != after, "the session id did not change"
    replayed = await client.get("/api/v1/auth/session", cookies={SESSION_COOKIE: before})
    assert replayed.status_code == 401, "the pre-completion cookie still resolves"


# ── first sign-in: what it must refuse, and refuse WITHOUT writing ───────────


@pytest.mark.verifies("AC-IDA-002")
@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"consent": False, "terms_accepted": True}, "consent declined"),
        ({"consent": True, "terms_accepted": False}, "terms declined"),
        ({"consent": False, "terms_accepted": False}, "both declined"),
    ],
)
async def test_a_declined_instrument_is_422_and_records_nothing(
    client, world, credentials, legal_version, auth_engine, payload, why
):
    """AC-IDA-002 has two halves and the second is the one that breaks silently.

    `422` alone would pass against an implementation that recorded the consent row
    first and validated afterwards — leaving an evidence trail saying someone
    consented when they had explicitly declined. So the tables are counted.
    """
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    response = await client.post(FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, **payload})

    assert response.status_code == 422, why
    assert await counts(auth_engine, world.initial) == (0, 0), f"{why}: something was recorded"


@pytest.mark.verifies("FR-IDA-004")
async def test_a_password_below_policy_is_refused_and_records_nothing(
    client, world, credentials, legal_version, auth_engine
):
    await legal_version("privacy_notice", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    response = await client.post(
        FIRST_SIGN_IN,
        json={
            "new_password": "short",  # pragma: allowlist secret
            "consent": True,
            "terms_accepted": True,
        },
    )

    assert response.status_code == 422
    assert await counts(auth_engine, world.initial) == (0, 0)


@pytest.mark.verifies("FR-IDA-004")
async def test_replaying_after_success_is_refused_as_not_pending(
    client, world, credentials, legal_version
):
    """`409 first-sign-in-not-pending` (gate F-6).

    The credential is already set, so the gate this endpoint exists to clear is
    not pending. §6's "replay is a no-op success" governs the consent rows — which
    ARE safe to write twice — not the credential flip.
    """
    await legal_version("privacy_notice", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))
    body = {"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    assert (await client.post(FIRST_SIGN_IN, json=body)).status_code == 200

    replay = await client.post(FIRST_SIGN_IN, json=body)

    assert replay.status_code == 409
    assert replay.json()["type"].endswith("/first-sign-in-not-pending")


@pytest.mark.verifies("FR-IDA-004")
async def test_an_account_that_never_had_the_gate_is_refused(
    client, world, credentials, legal_version
):
    """A fully-admitted account calling this gets `409`, not a password reset.

    This endpoint is a gate exit, not a change-password route — routing it to one
    would be a self-service password change with no re-authentication.
    """
    await legal_version("privacy_notice", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.manager_email))

    response = await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )

    assert response.status_code == 409
    assert response.json()["type"].endswith("/first-sign-in-not-pending")


@pytest.mark.verifies("SEC-002")
async def test_first_sign_in_without_a_session_is_refused(client):
    response = await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )

    assert response.status_code == 401
    assert response.json()["type"].endswith("/session-invalid")


@pytest.mark.verifies("FR-IDA-004")
async def test_it_completes_in_an_environment_with_no_published_documents(
    client, world, credentials, auth_engine
):
    """No notice, no terms — a fresh environment, and the gate must still open.

    `first_sign_in` is keyed on `credential_state`, not on any document, so an
    account cannot be behind a version that does not exist. There is simply
    nothing to record: the definer helpers reject unpublished versions, so the
    caller must skip the instrument rather than invent one.
    """
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    response = await client.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )

    assert response.status_code == 200, response.text
    assert response.json()["pending_gates"] == []
    assert await counts(auth_engine, world.initial) == (0, 0)


# ── acceptances: the re-consent path (CMP-002 / CMP-005) ─────────────────────


@pytest.mark.verifies("CMP-002")
async def test_recording_consent_clears_the_consent_gate(
    client, world, credentials, legal_version, auth_engine
):
    await legal_version("privacy_notice", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    response = await client.post(ACCEPTANCES, json={"consent": True})

    assert response.status_code == 200, response.text
    assert response.json()["pending_gates"] == []
    assert (await counts(auth_engine, world.rep))[0] == 1


@pytest.mark.verifies("CMP-002", "CMP-005")
async def test_supplying_only_one_instrument_leaves_the_other_pending(
    world, credentials, legal_version, guarded
):
    """The partial-acceptance case, and the one that would silently grant access.

    Both gates are open. The client accepts consent only. `terms` must remain —
    and, crucially, must still REFUSE a guarded route. A session whose gate was
    cleared to None here would report `pending_gates: [terms]` in the body while
    admitting the holder to everything.
    """
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")
    await guarded.http.post("/api/v1/auth/session", json=credentials(world.rep_email))

    response = await guarded.http.post(ACCEPTANCES, json={"consent": True})

    assert response.status_code == 200, response.text
    assert response.json()["pending_gates"] == ["terms"]

    blocked = await guarded.http.get(guarded.PROBE)
    assert blocked.status_code == 409, "terms outstanding, yet the surface was open"
    assert blocked.json()["type"].endswith("/terms-acceptance-required")


@pytest.mark.verifies("CMP-005")
async def test_supplying_both_clears_both(
    client, world, credentials, legal_version, auth_engine
):
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    response = await client.post(ACCEPTANCES, json={"consent": True, "terms_accepted": True})

    assert response.status_code == 200, response.text
    assert response.json()["pending_gates"] == []
    assert await counts(auth_engine, world.rep) == (1, 1)


@pytest.mark.verifies("CMP-002")
async def test_replaying_acceptances_is_a_no_op_success(
    client, world, credentials, legal_version, auth_engine
):
    """Unlike first sign-in, this one really is idempotent: the guard is the
    unique `(person, version)` row, and writing it twice changes nothing
    (00-contract-overview §6)."""
    await legal_version("privacy_notice", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    first = await client.post(ACCEPTANCES, json={"consent": True})
    second = await client.post(ACCEPTANCES, json={"consent": True})

    assert first.status_code == second.status_code == 200
    assert await counts(auth_engine, world.rep) == (1, 0), "the replay wrote a second row"


@pytest.mark.verifies("AC-IDA-002")
async def test_declining_through_acceptances_is_refused_and_records_nothing(
    client, world, credentials, legal_version, auth_engine
):
    """`false` is not a way to clear a gate. Ignoring it would answer 200 to a
    client that had explicitly declined, and leave it wondering why it is still
    gated."""
    await legal_version("privacy_notice", "2026.1")
    await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    response = await client.post(ACCEPTANCES, json={"consent": False})

    assert response.status_code == 422
    assert await counts(auth_engine, world.rep) == (0, 0)


@pytest.mark.verifies("CMP-005")
async def test_terms_published_without_a_notice_does_not_strand_the_account(
    world, credentials, legal_version, guarded
):
    """The gate a user could never close, and the reason it existed.

    `terms_acceptance` carries a Privacy Notice version too, `NOT NULL`, so terms
    cannot be accepted while no notice is published. But the gate was keyed on the
    terms version ALONE — so publishing terms first opened a gate that
    `POST /auth/acceptances` answered `200` to while recording nothing. The account
    completed first sign-in, was refused every guarded route, and was told
    everything had worked. Forever.

    A gate must be closeable by the endpoint that exists to close it. Here that
    means not opening at all until both documents exist — the same rule the
    consent branch already followed.
    """
    await legal_version("terms_of_use", "2026.1")
    await guarded.http.post("/api/v1/auth/session", json=credentials(world.initial_email))

    completed = await guarded.http.post(
        FIRST_SIGN_IN, json={"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}
    )

    assert completed.status_code == 200, completed.text
    assert completed.json()["pending_gates"] == [], "stranded behind an uncloseable gate"
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200


@pytest.mark.verifies("SEC-004")
async def test_gate_attempts_are_throttled_before_the_hash(
    world, credentials, legal_version, throttled, monkeypatch
):
    """`POST /auth/first-sign-in` hashes with argon2id — 19 MiB — and cannot know
    the gate is still pending until after it does, because the helper is one
    atomic check-and-set. So every call costs a full hash even when the answer is
    `409`, and an authenticated caller could spend that without limit.

    The placement is asserted the same way sign-in's is: make hashing fatal, and
    check the throttled request never reaches it.
    """
    from bluelab.platform.security import passwords

    await legal_version("privacy_notice", "2026.1")
    await throttled.post("/api/v1/auth/session", json=credentials(world.initial_email))
    body = {"new_password": NEW_PASSWORD, "consent": True, "terms_accepted": True}

    assert (await throttled.post(FIRST_SIGN_IN, json=body)).status_code == 200
    assert (await throttled.post(FIRST_SIGN_IN, json=body)).status_code == 409

    def explode(*_a: object, **_k: object) -> str:
        raise AssertionError("argon2 ran on a request the throttle should have refused")

    monkeypatch.setattr(passwords, "hash_password", explode)
    refused = await throttled.post(FIRST_SIGN_IN, json=body)

    assert refused.status_code == 429
    assert refused.json()["type"].endswith("/rate-limited")
    assert int(refused.headers["retry-after"]) > 0


@pytest.mark.verifies("SEC-002")
async def test_acceptances_without_a_session_is_refused(client):
    response = await client.post(ACCEPTANCES, json={"consent": True})

    assert response.status_code == 401
    assert response.json()["type"].endswith("/session-invalid")

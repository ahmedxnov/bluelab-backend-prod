"""The Auth surface, attacked (quality/01 §4, quality/04 §2).

Sign-in is the one endpoint every other one depends on, and its failure modes are
disclosure rather than error: the wrong `401` tells an attacker an email is
provisioned, the wrong ordering tells them an account exists before they hold its
password. So the battery here is mostly about what the surface *refuses to say*.

Each test drives the real ASGI app against the real database. Nothing is mocked
except the coordination store — see the conftest for why that substitution is the
safe one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from tests.l3_integration.auth.conftest import app_settings

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

SESSION_COOKIE = "__Host-bluelab_session"


def set_cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


# ── the happy path, so the refusals below mean something ──────────────────────


@pytest.mark.verifies("FR-IDA-005")
async def test_valid_credentials_establish_a_session(client, world, credentials):
    """The permitted case, asserted first. Without it every refusal test would
    pass against an endpoint that refused everything."""
    response = await client.post("/api/v1/auth/session", json=credentials(world.manager_email))

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == str(world.manager)
    assert body["email"] == world.manager_email
    assert body["role"] == "manager"
    assert body["org"]["timezone"] == "Africa/Cairo"
    assert body["pending_gates"] == []


@pytest.mark.verifies("SEC-002")
async def test_the_session_cookie_carries_every_required_attribute(client, world, credentials):
    """SEC-002 on the wire, not on the source.

    `tools/audit_cookie_attributes.py` proves the *code* only emits cookies
    through the audited helper. This proves what the helper actually put on the
    response — the two catch different mistakes, which is why both exist.
    """
    response = await client.post("/api/v1/auth/session", json=credentials(world.manager_email))
    header = set_cookie_header(response)

    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=strict" in header.replace("samesite", "SameSite")
    assert "Path=/" in header
    assert "Domain=" not in header, "the __Host- prefix forbids Domain"


@pytest.mark.verifies("FR-IDA-005", "SEC-005")
async def test_the_response_body_never_carries_the_session_id(client, world, credentials):
    """The cookie is `HttpOnly` for a reason, and echoing the id into the body
    would hand it straight back to any script on the page."""
    response = await client.post("/api/v1/auth/session", json=credentials(world.manager_email))

    raw = response.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    serialized = response.text

    assert raw not in serialized
    for forbidden in ("password", "hash", "argon2", "session_id", "token"):
        assert forbidden not in serialized.lower()


# ── non-disclosure: the whole point of the surface ────────────────────────────


@pytest.mark.verifies("FR-IDA-005")
async def test_an_unknown_email_and_a_wrong_password_are_indistinguishable(
    client, world, credentials
):
    """Byte-for-byte identical, not merely both `401`.

    Provisioning is operator-mediated (FR-IDA-001), so whether an address has an
    account is a real fact about a customer org. If the two responses differed in
    any member — a different slug, a different detail string — the account list
    would be enumerable at the rate an attacker can send requests.

    `request_id` is excluded because it is per-request by design and carries no
    information about the account.
    """
    unknown = await client.post(
        "/api/v1/auth/session", json={"email": "nobody@example.com", "password": "whatever-long"}  # pragma: allowlist secret
    )
    wrong = await client.post(
        "/api/v1/auth/session", json=credentials(world.manager_email, "wrong-but-long-enough")
    )

    assert unknown.status_code == wrong.status_code == 401

    def comparable(response) -> dict[str, object]:
        body = dict(response.json())
        body.pop("request_id", None)
        return body

    assert comparable(unknown) == comparable(wrong)
    assert comparable(unknown)["type"].endswith("/invalid-credentials")


@pytest.mark.verifies("AC-IDA-003")
async def test_a_deactivated_account_is_disclosed_only_after_a_correct_password(
    client, world, credentials
):
    """The ordering IS the control (AC-IDA-003).

    `403 account-deactivated` is the one state sign-in may disclose, and only to
    someone who has proved they hold the password. Reaching it with a *wrong*
    password would make it a free oracle: an attacker would learn which addresses
    are provisioned-but-disabled without ever authenticating.

    So both halves are asserted here. The `401` is the one that would break
    silently if someone moved the status check above the verification, and it
    would look like a harmless refactor.
    """
    correct = await client.post(
        "/api/v1/auth/session", json=credentials(world.deactivated_email)
    )
    assert correct.status_code == 403
    assert correct.json()["type"].endswith("/account-deactivated")

    incorrect = await client.post(
        "/api/v1/auth/session", json=credentials(world.deactivated_email, "wrong-but-long-enough")
    )
    assert incorrect.status_code == 401, "status leaked before the credential was proven"
    assert incorrect.json()["type"].endswith("/invalid-credentials")


@pytest.mark.verifies("FR-IDA-005")
async def test_the_email_lookup_is_case_insensitive(client, world, credentials):
    response = await client.post(
        "/api/v1/auth/session", json=credentials(world.manager_email.upper())
    )

    assert response.status_code == 200


# ── the first-sign-in gate ────────────────────────────────────────────────────


@pytest.mark.verifies("FR-IDA-004")
async def test_an_initial_credential_authenticates_into_a_gated_session(
    client, world, credentials
):
    """`200`, not an error. The user authenticated correctly; they are simply not
    finished. `pending_gates` is how the client knows where to send them."""
    response = await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    assert response.status_code == 200
    assert response.json()["pending_gates"] == ["first_sign_in"]
    assert SESSION_COOKIE in set_cookie_header(response), "a gated session is still a session"


@pytest.mark.verifies("FR-IDA-004")
async def test_a_gated_session_can_still_read_its_own_session(client, world, credentials):
    """`GET /auth/session` takes `GatedPrincipal` deliberately. Refusing it with
    the very `409` the client is trying to understand would deadlock them."""
    await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    response = await client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.json()["pending_gates"] == ["first_sign_in"]


# ── the consent and terms gates (CMP-002 / CMP-005) ───────────────────────────
#
# These read through `app_account_has_accepted`, because the acceptance rows are
# P9_OPS/system_write_only and an account cannot see its own. The gate is derived
# on read, so publishing a version opens it for every live session at once — which
# is the property these tests pin down.


@pytest.mark.verifies("CMP-002")
async def test_publishing_a_notice_version_opens_the_consent_gate(
    client, world, credentials, legal_version
):
    """A version nobody has accepted is a pending gate, immediately.

    No backfill, no per-account write: the publish is one insert and every session
    picks it up on its next request. A stored `pending_gates` column would need
    every affected account rewritten at publish time, and the first one missed
    keeps access it should have been re-asked for.
    """
    await legal_version("privacy_notice", "2026.1")

    response = await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    assert response.status_code == 200
    assert response.json()["pending_gates"] == ["consent"]


@pytest.mark.verifies("CMP-002")
async def test_recording_consent_clears_the_gate(
    client, world, credentials, legal_version, record_consent
):
    await legal_version("privacy_notice", "2026.1")
    await record_consent(world.rep, "2026.1")

    response = await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    assert response.json()["pending_gates"] == []


@pytest.mark.verifies("CMP-002")
async def test_consent_to_an_older_version_does_not_satisfy_a_new_one(
    client, world, credentials, legal_version, record_consent
):
    """The re-request on material change is the whole point of CMP-002.

    Accepting `2026.1` must not carry forward to `2026.2` — otherwise a material
    change to the notice would ship with everyone silently still consented to the
    old text, which is the failure the requirement exists to prevent.
    """
    await legal_version("privacy_notice", "2026.1")
    await record_consent(world.rep, "2026.1")
    await legal_version("privacy_notice", "2026.2", effective_offset=1)

    response = await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    assert response.json()["pending_gates"] == ["consent"]


@pytest.mark.verifies("CMP-005")
async def test_terms_are_a_separate_instrument_from_consent(
    client, world, credentials, legal_version, record_consent
):
    """Consent and terms are distinct instruments (CMP-002 vs CMP-005), and one
    does not imply the other. Both pending is both listed, in precedence order."""
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")

    both = await client.post("/api/v1/auth/session", json=credentials(world.rep_email))
    assert both.json()["pending_gates"] == ["consent", "terms"]

    await record_consent(world.rep, "2026.1")
    only_terms = await client.post("/api/v1/auth/session", json=credentials(world.rep_email))
    assert only_terms.json()["pending_gates"] == ["terms"]


@pytest.mark.verifies("CMP-002", "AC-IDA-006")
async def test_one_accounts_consent_does_not_satisfy_another(
    client, world, credentials, legal_version, record_consent
):
    """The helper runs with BYPASSRLS, so its `account_id` filter is the only thing
    keeping one person's acceptance from answering for everyone."""
    await legal_version("privacy_notice", "2026.1")
    await record_consent(world.manager, "2026.1")

    response = await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    assert response.json()["pending_gates"] == ["consent"], (
        "the rep was admitted on the manager's consent"
    )


@pytest.mark.verifies("FR-IDA-004")
async def test_first_sign_in_subsumes_the_other_gates(
    client, world, credentials, legal_version
):
    """Precedence, asserted rather than assumed. `SessionRecord.gate` is scalar, so
    an account behind first-sign-in must report exactly that even when a notice is
    also outstanding — completing it records both instruments anyway."""
    await legal_version("privacy_notice", "2026.1")
    await legal_version("terms_of_use", "2026.1")

    response = await client.post("/api/v1/auth/session", json=credentials(world.initial_email))

    assert response.json()["pending_gates"] == ["first_sign_in"]


# ── session lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-IDA-009")
async def test_the_session_read_reflects_the_database_not_the_cookie(
    client, world, credentials, auth_engine
):
    """The session record stores no display name on purpose.

    A rep renamed or moved between teams by ops must see it on their next request,
    not their next sign-in (FR-IDA-009). That only holds if the read goes back to
    the database every time — which is exactly what a cached copy in the session
    record would quietly break.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    maker = async_sessionmaker(auth_engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        await s.execute(
            text("update account set display_name = 'Renamed By Ops' where id = :id"),
            {"id": world.rep},
        )

    response = await client.get("/api/v1/auth/session")

    assert response.json()["display_name"] == "Renamed By Ops"


@pytest.mark.verifies("SEC-002")
async def test_no_cookie_is_refused(client):
    response = await client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["type"].endswith("/session-invalid")


@pytest.mark.verifies("SEC-002")
async def test_a_forged_cookie_is_refused(client):
    """A 256-bit opaque id is not guessable, but the refusal path still has to
    exist and still has to look like every other invalid session."""
    response = await client.get(
        "/api/v1/auth/session", cookies={SESSION_COOKIE: "not-a-real-session-id"}
    )

    assert response.status_code == 401
    assert response.json()["type"].endswith("/session-invalid")


@pytest.mark.verifies("FR-IDA-010")
async def test_sign_out_ends_the_session_server_side(client, world, credentials):
    """Clearing the cookie is courtesy; deleting the record is the control.

    Asserted by reusing the same cookie after sign-out. A test that only checked
    the `Set-Cookie` header would pass against an implementation that cleared the
    browser's copy and left the session live — which is precisely the bug worth
    catching, because the stolen copy is the one that matters.
    """
    signed_in = await client.post("/api/v1/auth/session", json=credentials(world.manager_email))
    raw = signed_in.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

    signed_out = await client.delete("/api/v1/auth/session")
    assert signed_out.status_code == 204

    replayed = await client.get("/api/v1/auth/session", cookies={SESSION_COOKIE: raw})
    assert replayed.status_code == 401, "the session survived sign-out"


@pytest.mark.verifies("FR-IDA-010")
async def test_signing_out_of_an_expired_session_still_succeeds(client):
    """`204`, not `401`. A user whose session lapsed in another tab should not be
    met with a sign-out button that refuses to work."""
    response = await client.delete(
        "/api/v1/auth/session", cookies={SESSION_COOKIE: "already-gone"}
    )

    assert response.status_code == 204


@pytest.mark.verifies("SEC-002")
async def test_a_session_past_its_absolute_window_is_refused(
    guarded, world, credentials, monkeypatch
):
    """The absolute clock, which no amount of activity may extend.

    The idle clock slides on every resolve — that is its job. The absolute one is
    the ceiling that makes a stolen cookie eventually worthless no matter how
    diligently it is used, so it has to be checked against the record rather than
    left to the store's TTL.

    Time is moved rather than waited for: `sessions.now` is the single clock the
    store reads, so patching it is the whole simulation.
    """
    from datetime import timedelta

    from bluelab.platform import clock
    from bluelab.platform.security import sessions

    await guarded.http.post("/api/v1/auth/session", json=credentials(world.manager_email))
    assert (await guarded.http.get(guarded.PROBE)).status_code == 200, "live before the jump"

    monkeypatch.setattr(sessions, "now", lambda: clock.now() + timedelta(days=365))

    assert (await guarded.http.get(guarded.PROBE)).status_code == 401


@pytest.mark.verifies("SEC-002")
async def test_a_resolved_session_never_outlives_the_nearer_clock(guarded, world, credentials):
    """The stored key's TTL is the idle window, not the absolute one.

    Both clocks are enforced, but by different mechanisms — the absolute one by the
    check above, the idle one by the key simply expiring. If the TTL were written
    from the absolute window, an abandoned session would sit resolvable in Valkey
    for its whole absolute life and the idle timeout would be decorative.
    """
    signed_in = await guarded.http.post(
        "/api/v1/auth/session", json=credentials(world.manager_email)
    )
    raw = signed_in.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]

    from bluelab.platform.security.tokens import hash_token

    ttl = await guarded.store._client.ttl("session:" + hash_token(raw))
    settings = app_settings()

    assert 0 < ttl <= settings.session_idle_seconds
    assert ttl < settings.session_absolute_seconds, "the idle clock is the nearer one"


# ── sign-in throttling (SEC-004/005, OWASP A07) ───────────────────────────────
#
# Two things are being defended, and they are not the same thing. One is credential
# stuffing. The other is that argon2id allocates 19 MiB per verification — and
# `dummy_verify` makes an unknown email pay it too, so an unauthenticated caller
# needed no valid address to spend our memory at will. `MAX_LENGTH` already bounds
# what one attempt can cost; these bound how many attempts there are.


@pytest.mark.verifies("SEC-004")
async def test_repeated_failures_are_refused_with_a_retry_after(throttled, world, credentials):
    """`429` once the identifier window is spent, carrying `Retry-After`."""
    wrong = credentials(world.manager_email, "wrong-but-long-enough")
    for _ in range(2):
        assert (await throttled.post("/api/v1/auth/session", json=wrong)).status_code == 401

    refused = await throttled.post("/api/v1/auth/session", json=wrong)

    assert refused.status_code == 429
    assert refused.json()["type"].endswith("/rate-limited")
    assert int(refused.headers["retry-after"]) > 0


@pytest.mark.verifies("SEC-004")
async def test_the_throttle_refuses_before_the_password_is_verified(
    throttled, world, credentials, monkeypatch
):
    """The placement, not just the presence.

    Refusing *after* the hash would stop credential stuffing and do nothing about
    the memory, which is half the reason this exists. Asserted by making a
    verification fatal: if the limit is checked first, the throttled request never
    reaches it.
    """
    from bluelab.platform.security import passwords

    wrong = credentials(world.manager_email, "wrong-but-long-enough")
    for _ in range(2):
        await throttled.post("/api/v1/auth/session", json=wrong)

    def explode(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("argon2 ran on a request the throttle should have refused")

    monkeypatch.setattr(passwords, "verify_password", explode)
    monkeypatch.setattr(passwords, "dummy_verify", explode)

    assert (await throttled.post("/api/v1/auth/session", json=wrong)).status_code == 429


@pytest.mark.verifies("SEC-004", "FR-IDA-005")
async def test_throttling_discloses_nothing_about_whether_an_account_exists(
    throttled, world, credentials
):
    """A `429` for an unknown address must be byte-identical to one for a real one.

    The counters key on what was *submitted*, before any lookup, so they cannot
    vary with account existence. If they ever did, the throttle would reintroduce
    exactly the enumeration oracle `dummy_verify` was written to close — and it
    would do it more cheaply, since the attacker would only need to reach the
    limit rather than measure microseconds.
    """
    unknown = {
        "email": "nobody-at-all@example.com",
        "password": "wrong-but-long-enough",  # pragma: allowlist secret
    }
    real = credentials(world.manager_email, "wrong-but-long-enough")

    for payload in (unknown, real):
        for _ in range(2):
            await throttled.post("/api/v1/auth/session", json=payload)

    a = await throttled.post("/api/v1/auth/session", json=unknown)
    b = await throttled.post("/api/v1/auth/session", json=real)

    assert a.status_code == b.status_code == 429

    def comparable(response) -> dict[str, object]:
        body = dict(response.json())
        body.pop("request_id", None)
        return body

    assert comparable(a) == comparable(b)


@pytest.mark.verifies("SEC-004")
async def test_spraying_across_addresses_trips_the_source_limit(throttled, world):
    """The attack a per-identifier counter cannot see.

    One guess against each of many addresses never reaches an identifier limit —
    and it is also the shape the memory-exhaustion attack takes, because every one
    of those attempts is a full argon2 verification against an account that does
    not exist.
    """
    codes = [
        (
            await throttled.post(
                "/api/v1/auth/session",
                json={
                    "email": f"spray-{i}@example.com",
                    "password": "wrong-but-long-enough",  # pragma: allowlist secret
                },
            )
        ).status_code
        for i in range(5)
    ]

    assert codes[:3] == [401, 401, 401], "under the source limit"
    assert codes[3:] == [429, 429], "the source limit caught what no identifier limit would"


@pytest.mark.verifies("SEC-004")
async def test_a_successful_sign_in_clears_the_counters(throttled, world, credentials):
    """The counters measure consecutive failures, not lifetime traffic.

    Without this, anyone who fumbles a password and then gets in stays one mistake
    from a lockout for the rest of the window — and a whole office behind one
    address would trip the source limit on an ordinary morning.
    """
    wrong = credentials(world.manager_email, "wrong-but-long-enough")
    await throttled.post("/api/v1/auth/session", json=wrong)

    assert (
        await throttled.post("/api/v1/auth/session", json=credentials(world.manager_email))
    ).status_code == 200

    # Would be the third failure, and refused, had the success not reset it.
    for _ in range(2):
        assert (await throttled.post("/api/v1/auth/session", json=wrong)).status_code == 401


# ── the gate REFUSAL, which is what makes the gate a gate ─────────────────────
#
# Everything above proves a limited session is admitted where it should be. These
# prove it is turned away everywhere else — the deny-by-default half, and the one
# `api.deps` spends its module docstring on. It had no test until now because the
# three Auth routes all take `GatedPrincipal` or no principal, so nothing in the
# suite ever reached the refusing branch.


@pytest.mark.verifies("FR-IDA-004")
async def test_a_fully_admitted_session_reaches_a_guarded_route(guarded, world, credentials):
    """Asserted first, so the refusal below is not a route that refuses everyone."""
    await guarded.http.post("/api/v1/auth/session", json=credentials(world.manager_email))

    response = await guarded.http.get(guarded.PROBE)

    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == str(world.manager)


@pytest.mark.verifies("FR-IDA-004")
async def test_a_gate_limited_session_is_refused_by_a_guarded_route(
    guarded, world, credentials
):
    """`409`, naming the gate.

    A gate-limited session is a *valid* session — it resolves, it carries a real
    scope, and it would read and write perfectly well. Nothing about it fails on
    its own; only this check stands between it and the whole product surface.
    """
    await guarded.http.post("/api/v1/auth/session", json=credentials(world.initial_email))

    response = await guarded.http.get(guarded.PROBE)

    assert response.status_code == 409
    assert response.json()["type"].endswith("/first-sign-in-required")


@pytest.mark.verifies("CMP-002")
async def test_a_consent_gate_refuses_a_guarded_route_too(
    guarded, world, credentials, legal_version
):
    """The gate that opens on an *established* session, not just at first sign-in.
    Publishing a notice must close the product surface to everyone who has not
    accepted it, on their next request."""
    await legal_version("privacy_notice", "2026.1")
    await guarded.http.post("/api/v1/auth/session", json=credentials(world.rep_email))

    response = await guarded.http.get(guarded.PROBE)

    assert response.status_code == 409
    assert response.json()["type"].endswith("/consent-required")


@pytest.mark.verifies("FR-IDA-004")
async def test_a_gate_this_release_does_not_know_is_refused_not_a_500(guarded, world):
    """The rolling-deploy case, and the reason `GATE_PROBLEMS` is read with `.get`.

    `_decode` drops unknown *keys* so a release N+1 record stays readable by
    release N — but it passes any *value* through for a key it knows, and
    `SessionRecord.gate` is a bare `str` because `Gate` lives in `modules` and
    cannot be imported into `platform`. So N+1 minting a new gate kind arrives here
    as an ordinary string. Subscripted, that was a `KeyError` and a 500 on every
    request from that user — the exact outage `_decode` exists to prevent.

    401, not 409: this process cannot evaluate the limitation, so it must not admit
    the session, and the client is sent somewhere it can recover.
    """
    raw = await guarded.store.create(
        account_id=world.manager,
        org_id=world.org,
        team_id=world.manager,
        role="manager",
        gate="a_gate_from_the_future",
    )

    response = await guarded.http.get(guarded.PROBE, cookies={SESSION_COOKIE: raw})

    assert response.status_code == 401, "an unrecognised gate must not admit the session"
    assert response.json()["type"].endswith("/session-invalid")


# ── isolation, through the real policy set ────────────────────────────────────


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
async def test_the_session_read_runs_under_the_accounts_own_scope(client, world, credentials):
    """The response body comes back through `account_self_read`, not through the
    definer helper.

    `app_account_for_sign_in` runs with BYPASSRLS and could return anything. It is
    used once, for the credential check, and nothing it returns is rendered — the
    body is re-read under the principal's own scope. If that ever stopped being
    true, this passes anyway; what it pins down is that the *scoped* read succeeds,
    so the policy path is real rather than incidental.
    """
    await client.post("/api/v1/auth/session", json=credentials(world.rep_email))

    response = await client.get("/api/v1/auth/session")

    assert response.status_code == 200
    assert response.json()["account_id"] == str(world.rep)
    assert response.json()["team_id"] == str(world.manager)

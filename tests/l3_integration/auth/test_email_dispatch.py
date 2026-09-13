"""E-1 dispatch deduplication and terminal failure persistence."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from procrastinate import JobContext
from procrastinate.jobs import Job
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.l3_integration.auth.conftest import app_settings

from bluelab.adapters.email import OutboundEmail
from bluelab.adapters.email_events import SesSnsAuthenticator
from bluelab.modules.identity import service as identity_service
from bluelab.notifications import dispatcher
from bluelab.notifications.delivery_events import (
    ReconciliationOutcome,
    reconcile_delivery_event,
)
from bluelab.platform.queue.catalog import Lane, spec_for
from bluelab.platform.queue.context import job_transaction
from bluelab.platform.queue.runtime import JobExecutor, TerminalJobFailure
from bluelab.work.dispatch_email import registration

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]


class _CapturingTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboundEmail] = []
        self.fail = fail

    async def send(self, message: OutboundEmail) -> str:
        self.messages.append(message)
        await asyncio.sleep(0.01)
        if self.fail:
            raise OSError("transport unavailable")
        return "provider-message-1"


async def _authenticated_delivery_event():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SNS test")])
    current = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(current - timedelta(minutes=1))
        .not_valid_after(current + timedelta(minutes=10))
        .sign(key, hashes.SHA256())
    )
    topic = "arn:aws:sns:eu-south-1:123456789012:bluelab-email-events"
    certificate_url = (
        "https://sns.eu-south-1.amazonaws.com/"
        "SimpleNotificationService-testcertificate.pem"
    )
    message = json.dumps(
        {
            "notificationType": "Delivery",
            "mail": {
                "messageId": "provider-message-1",
                "timestamp": current.isoformat(),
            },
        },
        separators=(",", ":"),
    )
    outer = {
        "Type": "Notification",
        "MessageId": "provider-event-1",
        "TopicArn": topic,
        "Message": message,
        "Timestamp": current.isoformat(),
        "SignatureVersion": "2",
        "SigningCertURL": certificate_url,
    }
    canonical = "".join(
        f"{field}\n{outer[field]}\n"
        for field in ("Message", "MessageId", "Timestamp", "TopicArn", "Type")
    ).encode()
    outer["Signature"] = base64.b64encode(
        key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    ).decode()

    async def load(_url: str) -> bytes:
        return certificate.public_bytes(serialization.Encoding.PEM)

    return await SesSnsAuthenticator(
        topic_arn=topic,
        region="eu-south-1",
        certificate_loader=load,
    ).authenticate(json.dumps(outer).encode())


async def _queued_send(client, world, auth_engine) -> tuple[UUID, dict[str, object]]:
    response = await client.post(
        "/api/v1/auth/password-reset-request", json={"email": world.rep_email}
    )
    assert response.status_code == 202
    async with async_sessionmaker(auth_engine)() as db:
        row = (
            await db.execute(
                text(
                    "select e.id, j.args from email_send e"
                    " join procrastinate_jobs j"
                    " on j.args #>> '{args,email_send_id}' = e.id::text"
                    " where e.account_id = :account and e.status = 'queued'"
                    " order by e.created_at desc limit 1"
                ),
                {"account": world.rep},
            )
        ).one()
    return UUID(str(row.id)), cast(dict[str, object], row.args)


@pytest.mark.verifies("FR-IDA-006", "ADR-0023")
async def test_duplicate_e1_jobs_make_one_transport_send(
    client, world, auth_engine, delivery_cipher
):
    email_send_id, body = await _queued_send(client, world, auth_engine)
    transport = _CapturingTransport()

    async def run(job_id: str) -> dispatcher.DispatchOutcome:
        async with job_transaction(
            Lane.DISPATCH_EMAIL, body, job_id=job_id
        ) as (session, _job):
            return await dispatcher.dispatch_e1(
                session,
                email_send_id=email_send_id,
                unsealer=delivery_cipher,
                transport=transport,
                recipient_resolver=identity_service.resolve_e1_recipient,
                sender="sender@example.com",
                public_app_url="https://app.example.com",
            )

    outcomes = await asyncio.gather(run("duplicate-1"), run("duplicate-2"))

    assert sorted(outcomes) == [
        dispatcher.DispatchOutcome.ALREADY_FINAL,
        dispatcher.DispatchOutcome.SENT,
    ]
    assert len(transport.messages) == 1
    assert transport.messages[0].recipient == world.rep_email
    assert "reset-password#token=" in transport.messages[0].text_body

    event = await _authenticated_delivery_event()
    async with job_transaction(
        Lane.DISPATCH_EMAIL, body, job_id="delivery-event-1"
    ) as (session, _job):
        first_event = await reconcile_delivery_event(session, event)
    async with job_transaction(
        Lane.DISPATCH_EMAIL, body, job_id="delivery-event-replay"
    ) as (session, _job):
        replay = await reconcile_delivery_event(session, event)
    assert first_event is ReconciliationOutcome.UPDATED
    assert replay is ReconciliationOutcome.IDEMPOTENT

    async with async_sessionmaker(auth_engine)() as db:
        state = (
            await db.execute(
                text(
                    "select e.status, e.provider_message_id,"
                    " exists(select 1 from email_delivery_secret s where s.email_send_id=e.id)"
                    " from email_send e where e.id=:id"
                ),
                {"id": email_send_id},
            )
        ).one()
    assert tuple(state) == ("delivered", "provider-message-1", False)


@pytest.mark.verifies("FR-IDA-006", "ADR-0023")
async def test_retry_exhaustion_records_failure_and_destroys_secret(
    client, world, auth_engine, delivery_cipher
):
    email_send_id, body = await _queued_send(client, world, auth_engine)
    transport = _CapturingTransport(fail=True)
    executor = JobExecutor(
        registration(
            app_settings(),
            transport=transport,
            unsealer=delivery_cipher,
        )
    )
    context = JobContext(
        app=cast(Any, None),
        job=Job(
            id=99,
            queue=Lane.DISPATCH_EMAIL.value,
            lock=None,
            queueing_lock=None,
            task_name=Lane.DISPATCH_EMAIL.value,
            task_kwargs=body,
            attempts=spec_for(Lane.DISPATCH_EMAIL).max_attempts,
        ),
        start_timestamp=0,
        abort_reason=lambda: None,
    )

    with pytest.raises(TerminalJobFailure):
        await executor(context, body)

    async with async_sessionmaker(auth_engine)() as db:
        state = (
            await db.execute(
                text(
                    "select e.status,"
                    " exists(select 1 from email_delivery_secret s where s.email_send_id=e.id)"
                    " from email_send e where e.id=:id"
                ),
                {"id": email_send_id},
            )
        ).one()
    assert tuple(state) == ("failed", False)

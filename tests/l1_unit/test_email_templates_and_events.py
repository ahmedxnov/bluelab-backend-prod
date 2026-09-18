"""Safe E-1 rendering and authenticated SES/SNS event parsing."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from bluelab.adapters.email import validate_address
from bluelab.adapters.email_events import (
    DeliveryState,
    InvalidDeliveryEvent,
    SesSnsAuthenticator,
    VerifiedDeliveryEvent,
)
from bluelab.notifications.templates import E1Purpose, EmailKind, render_e1

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security]

SEND_ID = UUID("01936d54-7ad5-7000-8000-000000000001")
TOPIC = "arn:aws:sns:eu-south-1:123456789012:bluelab-email-events"
CERT_URL = (
    "https://sns.eu-south-1.amazonaws.com/"
    "SimpleNotificationService-testcertificate.pem"
)


def test_validate_address_accepts_one_bare_mailbox_and_rejects_ambiguous_forms():
    """The mail transport accepts no display name, list, or malformed mailbox."""
    assert validate_address("person@example.com") == "person@example.com"

    for value in (
        "Person <person@example.com>",
        "person@example.com,other@example.com",
        "person@@example.com",
        "person @example.com",
        "person@example.com (comment)",
        "person@example.com\nBcc: other@example.com",
    ):
        with pytest.raises(ValueError):
            validate_address(value)


def _certificate():
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
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    return key, pem


def _signed_event(key, *, notification_type: str = "Delivery") -> bytes:
    message = json.dumps(
        {
            "notificationType": notification_type,
            "mail": {
                "messageId": "provider-message-1",
                "timestamp": "2026-09-11T12:00:00Z",
            },
        },
        separators=(",", ":"),
    )
    outer = {
        "Type": "Notification",
        "MessageId": "provider-event-1",
        "TopicArn": TOPIC,
        "Message": message,
        "Timestamp": "2026-09-11T12:00:01Z",
        "SignatureVersion": "2",
        "SigningCertURL": CERT_URL,
    }
    canonical = "".join(
        f"{field}\n{outer[field]}\n"
        for field in ("Message", "MessageId", "Timestamp", "TopicArn", "Type")
    ).encode()
    outer["Signature"] = base64.b64encode(
        key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    return json.dumps(outer, separators=(",", ":")).encode()


@pytest.mark.verifies("FR-IDA-003", "FR-IDA-006", "ADR-0026")
def test_e1_renderer_escapes_html_strips_bidi_controls_and_uses_fragment_token():
    initial = render_e1(
        email_send_id=SEND_ID,
        recipient="person@example.com",
        recipient_name="<b>Alice</b>\u202e",
        sender="sender@example.com",
        purpose=E1Purpose.INITIAL_CREDENTIAL,
        secret="initial-secret",  # pragma: allowlist secret -- synthetic test credential
        public_app_url="https://app.example.com",
    )
    reset = render_e1(
        email_send_id=SEND_ID,
        recipient="person@example.com",
        recipient_name="Alice",
        sender="sender@example.com",
        purpose=E1Purpose.PASSWORD_RESET_TOKEN,
        secret="reset-token",  # pragma: allowlist secret -- synthetic test token
        public_app_url="https://app.example.com",
    )

    assert "<b>Alice</b>" in initial.text_body
    assert "&lt;b&gt;Alice&lt;/b&gt;" in initial.html_body
    assert "\u202e" not in initial.text_body + initial.html_body
    assert "Initial credential: initial-secret" in initial.text_body
    assert "reset-password#token=reset-token" in reset.text_body
    assert "reset-password?" not in reset.text_body
    with pytest.raises(ValueError):
        EmailKind("E6_marketing")


@pytest.mark.verifies("ADR-0026")
def test_e1_renderer_rejects_address_header_injection():
    with pytest.raises(ValueError):
        render_e1(
            email_send_id=SEND_ID,
            recipient="person@example.com\nBcc: attacker@example.com",
            recipient_name="Alice",
            sender="sender@example.com",
            purpose=E1Purpose.INITIAL_CREDENTIAL,
            secret="secret",  # pragma: allowlist secret -- synthetic injection-test value
            public_app_url="https://app.example.com",
        )


@pytest.mark.verifies("FR-HIR-010")
async def test_sns_authenticator_verifies_signature_topic_and_certificate_source():
    key, certificate = _certificate()
    loads: list[str] = []

    async def load(url: str) -> bytes:
        loads.append(url)
        return certificate

    authenticator = SesSnsAuthenticator(
        topic_arn=TOPIC,
        region="eu-south-1",
        certificate_loader=load,
    )
    event = await authenticator.authenticate(_signed_event(key))

    assert event.provider_message_id == "provider-message-1"
    assert event.provider_event_id == "provider-event-1"
    assert event.state is DeliveryState.DELIVERED
    assert loads == [CERT_URL]

    tampered = json.loads(_signed_event(key))
    tampered["Message"] = tampered["Message"].replace("Delivery", "Bounce")
    with pytest.raises(InvalidDeliveryEvent, match="signature is invalid"):
        await authenticator.authenticate(json.dumps(tampered).encode())

    wrong_url = json.loads(_signed_event(key))
    wrong_url["SigningCertURL"] = "https://attacker.invalid/certificate.pem"
    with pytest.raises(InvalidDeliveryEvent, match="URL is not allowed"):
        await authenticator.authenticate(json.dumps(wrong_url).encode())
    assert loads == [CERT_URL]


@pytest.mark.verifies("FR-HIR-010")
def test_verified_delivery_event_cannot_be_forged_as_a_plain_dictionary():
    with pytest.raises(TypeError, match="provider authentication"):
        VerifiedDeliveryEvent(
            provider_message_id="provider-message-1",
            state=DeliveryState.DELIVERED,
            occurred_at=datetime.now(UTC),
            provider_event_id="provider-event-1",
            _proof=object(),
        )

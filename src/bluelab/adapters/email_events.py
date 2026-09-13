"""Amazon SNS authentication for SES delivery-state notifications.

The verifier accepts only the configured topic and region, validates SNS's RSA
signature over its canonical field order, and emits a proof-carrying event.
Notification reconciliation never accepts an unauthenticated dictionary.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bluelab.platform.resilience import (
    CircuitBreaker,
    DependencyName,
    DependencyPolicy,
    call_dependency,
)

_MAX_EVENT_BYTES = 256 * 1024
_MAX_CERTIFICATE_BYTES = 64 * 1024
_VERIFIED = object()


class InvalidDeliveryEvent(ValueError):
    """An event is malformed, unauthenticated, or outside the configured source."""


class DeliveryState(StrEnum):
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    DELAYED = "delayed"


@dataclass(frozen=True, slots=True, init=False)
class VerifiedDeliveryEvent:
    """A SES event that can only be initialized with this verifier's proof."""

    provider_message_id: str
    state: DeliveryState
    occurred_at: datetime
    provider_event_id: str

    def __init__(
        self,
        *,
        provider_message_id: str,
        state: DeliveryState,
        occurred_at: datetime,
        provider_event_id: str,
        _proof: object,
    ) -> None:
        if _proof is not _VERIFIED:
            raise TypeError("delivery events require provider authentication")
        object.__setattr__(self, "provider_message_id", provider_message_id)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "provider_event_id", provider_event_id)


class CertificateLoader(Protocol):
    async def __call__(self, url: str) -> bytes: ...


class HttpCertificateLoader:
    """Bounded HTTPS fetcher for an already allowlisted SNS certificate URL."""

    def __init__(
        self,
        *,
        policy: DependencyPolicy,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._policy = policy
        self._circuit = circuit or CircuitBreaker(DependencyName.EMAIL)

    async def __call__(self, url: str) -> bytes:
        async def operation() -> bytes:
            async with httpx.AsyncClient(
                timeout=self._policy.timeout_seconds,
                follow_redirects=False,
            ) as client, client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_CERTIFICATE_BYTES:
                        raise InvalidDeliveryEvent("SNS certificate is too large")
                    chunks.append(chunk)
            return b"".join(chunks)

        return await call_dependency(
            DependencyName.EMAIL,
            operation,
            policy=self._policy,
            circuit=self._circuit,
            retryable=lambda error: not isinstance(error, InvalidDeliveryEvent),
        )


class SesSnsAuthenticator:
    """Authenticate one SNS Notification and parse its inner SES event."""

    def __init__(
        self,
        *,
        topic_arn: str,
        region: str,
        certificate_loader: CertificateLoader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not topic_arn.startswith(f"arn:aws:sns:{region}:"):
            raise ValueError("SNS topic ARN does not belong to the configured region")
        self._topic_arn = topic_arn
        self._region = region
        self._certificate_loader = certificate_loader
        self._clock = clock
        self._certificates: dict[str, x509.Certificate] = {}

    async def authenticate(self, payload: bytes) -> VerifiedDeliveryEvent:
        if not payload or len(payload) > _MAX_EVENT_BYTES:
            raise InvalidDeliveryEvent("SNS event size is invalid")
        outer = _json_object(payload, label="SNS event")
        if _field(outer, "Type") != "Notification":
            raise InvalidDeliveryEvent("SNS event type is not Notification")
        if _field(outer, "TopicArn") != self._topic_arn:
            raise InvalidDeliveryEvent("SNS event topic is not allowed")

        cert_url = _field(outer, "SigningCertURL")
        if not _certificate_url_allowed(cert_url, region=self._region):
            raise InvalidDeliveryEvent("SNS certificate URL is not allowed")
        certificate = await self._certificate(cert_url)
        self._verify_signature(outer, certificate)

        message = _json_object(_field(outer, "Message").encode(), label="SES event")
        mail = message.get("mail")
        if not isinstance(mail, dict):
            raise InvalidDeliveryEvent("SES event has no mail object")
        provider_message_id = _field(mail, "messageId")
        if len(provider_message_id) > 512:
            raise InvalidDeliveryEvent("SES message identity is too long")
        occurred_at = _instant(_field(mail, "timestamp"))
        raw_state = message.get("notificationType", message.get("eventType"))
        state = (
            {
                "Delivery": DeliveryState.DELIVERED,
                "Bounce": DeliveryState.BOUNCED,
                "Complaint": DeliveryState.BOUNCED,
                "DeliveryDelay": DeliveryState.DELAYED,
            }.get(raw_state)
            if isinstance(raw_state, str)
            else None
        )
        if state is None:
            raise InvalidDeliveryEvent("SES delivery state is unsupported")

        return VerifiedDeliveryEvent(
            provider_message_id=provider_message_id,
            state=state,
            occurred_at=occurred_at,
            provider_event_id=_field(outer, "MessageId"),
            _proof=_VERIFIED,
        )

    async def _certificate(self, url: str) -> x509.Certificate:
        cached = self._certificates.get(url)
        if cached is not None and _certificate_current(cached, at=self._clock()):
            return cached
        raw = await self._certificate_loader(url)
        try:
            certificate = x509.load_pem_x509_certificate(raw)
        except ValueError as exc:
            raise InvalidDeliveryEvent("SNS signing certificate is malformed") from exc
        if not _certificate_current(certificate, at=self._clock()):
            raise InvalidDeliveryEvent("SNS signing certificate is not current")
        self._certificates[url] = certificate
        return certificate

    @staticmethod
    def _verify_signature(outer: dict[str, Any], certificate: x509.Certificate) -> None:
        version = _field(outer, "SignatureVersion")
        algorithm: hashes.HashAlgorithm
        if version == "1":
            algorithm = hashes.SHA1()  # SNS protocol v1; v2 is accepted below.
        elif version == "2":
            algorithm = hashes.SHA256()
        else:
            raise InvalidDeliveryEvent("SNS signature version is unsupported")
        try:
            signature = base64.b64decode(_field(outer, "Signature"), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise InvalidDeliveryEvent("SNS signature is malformed") from exc
        public_key = certificate.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise InvalidDeliveryEvent("SNS signing key is not RSA")
        try:
            public_key.verify(
                signature,
                _canonical_notification(outer),
                padding.PKCS1v15(),
                algorithm,
            )
        except InvalidSignature as exc:
            raise InvalidDeliveryEvent("SNS signature is invalid") from exc


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidDeliveryEvent(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidDeliveryEvent(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _field(value: dict[str, Any], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field:
        raise InvalidDeliveryEvent(f"delivery event field {key} is invalid")
    return field


def _canonical_notification(outer: dict[str, Any]) -> bytes:
    fields = ["Message", "MessageId"]
    if "Subject" in outer:
        fields.append("Subject")
    fields.extend(["Timestamp", "TopicArn", "Type"])
    return "".join(f"{field}\n{_field(outer, field)}\n" for field in fields).encode()


def _certificate_url_allowed(url: str, *, region: str) -> bool:
    pattern = re.compile(
        rf"https://sns\.{re.escape(region)}\.amazonaws\.com/"
        r"SimpleNotificationService-[A-Za-z0-9_-]+\.pem"
    )
    return pattern.fullmatch(url) is not None


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidDeliveryEvent("delivery event timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise InvalidDeliveryEvent("delivery event timestamp has no timezone")
    return parsed.astimezone(UTC)


def _certificate_current(certificate: x509.Certificate, *, at: datetime) -> bool:
    return certificate.not_valid_before_utc <= at <= certificate.not_valid_after_utc

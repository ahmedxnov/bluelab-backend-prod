"""Bounded C-14 transports: local SMTP and in-region Amazon SES.

Callers pass an already rendered member of the closed five-template inventory.
SDK retries are disabled because the shared dependency policy owns the attempt
budget; exception details and rendered content never reach telemetry.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlsplit

from bluelab.platform.config import EmailTransport as EmailTransportKind
from bluelab.platform.config import Settings
from bluelab.platform.resilience import (
    CircuitBreaker,
    DependencyName,
    DependencyPolicy,
    call_dependency,
)


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    """One safe, rendered message. Values exist in dispatch memory only."""

    recipient: str
    sender: str
    subject: str
    text_body: str
    html_body: str
    message_id: str


class EmailTransport(Protocol):
    async def send(self, message: OutboundEmail) -> str:
        """Return the provider message identity after transport acceptance."""


def validate_address(value: str) -> str:
    """Reject header injection and anything other than one bare mailbox."""
    if "\r" in value or "\n" in value:
        raise ValueError("email address contains a header boundary")
    display, address = parseaddr(value, strict=True)
    if display or address != value or "@" not in address:
        raise ValueError("email address must be one bare mailbox")
    return address


class SmtpEmailTransport:
    """Mailpit/local SMTP transport with explicit socket and retry limits."""

    def __init__(
        self,
        smtp_url: str,
        *,
        policy: DependencyPolicy,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        parsed = urlsplit(smtp_url)
        if parsed.scheme not in {"smtp", "smtps"} or parsed.hostname is None:
            raise ValueError("SMTP URL must use smtp or smtps and include a host")
        self._host = parsed.hostname
        self._port = parsed.port or (465 if parsed.scheme == "smtps" else 25)
        self._tls = parsed.scheme == "smtps"
        self._username = unquote(parsed.username) if parsed.username else None
        self._password = unquote(parsed.password) if parsed.password else None
        self._policy = policy
        self._circuit = circuit or CircuitBreaker(DependencyName.EMAIL)

    async def send(self, message: OutboundEmail) -> str:
        recipient = validate_address(message.recipient)
        sender = validate_address(message.sender)

        def blocking_send() -> str:
            rendered = EmailMessage()
            rendered["From"] = sender
            rendered["To"] = recipient
            rendered["Subject"] = message.subject
            rendered["Message-ID"] = message.message_id
            rendered.set_content(message.text_body)
            rendered.add_alternative(message.html_body, subtype="html")

            if self._tls:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self._host,
                    self._port,
                    timeout=self._policy.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(
                    self._host,
                    self._port,
                    timeout=self._policy.timeout_seconds,
                )
            with client:
                if self._username is not None:
                    client.login(self._username, self._password or "")
                refused = client.send_message(rendered)
            if refused:
                raise OSError("email transport refused a recipient")
            return message.message_id

        async def operation() -> str:
            return await asyncio.to_thread(blocking_send)

        return await call_dependency(
            DependencyName.EMAIL,
            operation,
            policy=self._policy,
            circuit=self._circuit,
        )


class SesEmailTransport:
    """Amazon SES v2 simple-message transport for E-1."""

    def __init__(
        self,
        client: Any,
        *,
        policy: DependencyPolicy,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._circuit = circuit or CircuitBreaker(DependencyName.EMAIL)

    async def send(self, message: OutboundEmail) -> str:
        recipient = validate_address(message.recipient)
        sender = validate_address(message.sender)

        async def operation() -> Any:
            return await asyncio.to_thread(
                self._client.send_email,
                FromEmailAddress=sender,
                Destination={"ToAddresses": [recipient]},
                Content={
                    "Simple": {
                        "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                        "Body": {
                            "Text": {"Data": message.text_body, "Charset": "UTF-8"},
                            "Html": {"Data": message.html_body, "Charset": "UTF-8"},
                        },
                    }
                },
            )

        response = await call_dependency(
            DependencyName.EMAIL,
            operation,
            policy=self._policy,
            circuit=self._circuit,
        )
        provider_id = cast(str | None, response.get("MessageId"))
        if not provider_id:
            raise RuntimeError("email transport returned no message identity")
        return provider_id


def create_email_transport(settings: Settings) -> EmailTransport:
    """Build the selected transport without a tier or residency branch."""
    policy = DependencyPolicy(
        timeout_seconds=settings.dependency_timeout_seconds,
        max_attempts=settings.dependency_max_attempts,
        backoff_base_seconds=settings.dependency_backoff_base_seconds,
        backoff_max_seconds=settings.dependency_backoff_max_seconds,
    )
    circuit = CircuitBreaker(
        DependencyName.EMAIL,
        failure_threshold=settings.dependency_circuit_failure_threshold,
        recovery_seconds=settings.dependency_circuit_recovery_seconds,
    )
    if settings.email_transport is EmailTransportKind.SMTP:
        if settings.smtp_url is None:
            raise RuntimeError("SMTP_URL is required for the SMTP transport")
        return SmtpEmailTransport(
            settings.smtp_url.get_secret_value(), policy=policy, circuit=circuit
        )

    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    client = boto3.client(
        "sesv2",
        region_name=settings.email_region,
        config=Config(
            connect_timeout=settings.dependency_timeout_seconds,
            read_timeout=settings.dependency_timeout_seconds,
            retries={"max_attempts": 0},
        ),
    )
    return SesEmailTransport(client, policy=policy, circuit=circuit)

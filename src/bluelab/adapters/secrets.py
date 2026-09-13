"""Versioned authenticated-encryption adapters for short-lived E-1 material.

The API receives a sealer capability and the worker receives an unsealer
capability. AWS IAM separates those operations in deployed environments; the
local adapter implements both so Docker development and tests exercise the same
envelope/AAD contract without a cloud dependency.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bluelab.platform.config import Settings
from bluelab.platform.resilience import (
    CircuitBreaker,
    DependencyName,
    DependencyPolicy,
    call_dependency,
)

DeliverySecretPurpose = Literal["initial_credential", "password_reset_token"]

_LOCAL_PREFIX = b"BL1L"
_KMS_PREFIX = b"BL1K"
_NONCE_BYTES = 12


class SecretEnvelopeError(ValueError):
    """The encrypted value is malformed, unauthentic, or uses an unknown version."""


class SecretContext(Protocol):
    """Authenticated metadata accepted by an envelope adapter."""

    def aad(self) -> bytes: ...

    def encryption_context(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class DeliverySecretContext:
    """Authenticated metadata that prevents moving a secret between sends."""

    email_send_id: UUID
    org_id: UUID
    purpose: DeliverySecretPurpose

    def aad(self) -> bytes:
        return json.dumps(
            {
                "email_send_id": str(self.email_send_id),
                "org_id": str(self.org_id),
                "purpose": self.purpose,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def encryption_context(self) -> dict[str, str]:
        return {
            "email_send_id": str(self.email_send_id),
            "org_id": str(self.org_id),
            "purpose": self.purpose,
        }


@dataclass(frozen=True, slots=True)
class OpsTotpContext:
    """Bind an encrypted TOTP seed to exactly one operations account."""

    ops_account_id: UUID

    def aad(self) -> bytes:
        return json.dumps(
            {
                "ops_account_id": str(self.ops_account_id),
                "purpose": "ops_totp_seed",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def encryption_context(self) -> dict[str, str]:
        return {
            "ops_account_id": str(self.ops_account_id),
            "purpose": "ops_totp_seed",
        }


class SecretSealer(Protocol):
    async def seal(self, plaintext: str, *, context: SecretContext) -> bytes:
        """Encrypt one credential with the context authenticated as AAD."""


class SecretUnsealer(Protocol):
    async def unseal(self, envelope: bytes, *, context: SecretContext) -> str:
        """Authenticate and decrypt one credential."""


class LocalAeadCipher:
    """AES-256-GCM envelope used by local Docker and deterministic tests."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("the local delivery key must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_base64(cls, encoded: str) -> LocalAeadCipher:
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("the local delivery key must be valid base64") from exc
        return cls(key)

    async def seal(self, plaintext: str, *, context: SecretContext) -> bytes:
        if not plaintext:
            raise ValueError("a delivery secret cannot be empty")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), context.aad())
        return _LOCAL_PREFIX + nonce + ciphertext

    async def unseal(self, envelope: bytes, *, context: SecretContext) -> str:
        minimum = len(_LOCAL_PREFIX) + _NONCE_BYTES
        if not envelope.startswith(_LOCAL_PREFIX) or len(envelope) <= minimum:
            raise SecretEnvelopeError("unsupported or truncated local secret envelope")
        nonce_start = len(_LOCAL_PREFIX)
        nonce_end = nonce_start + _NONCE_BYTES
        try:
            plaintext = self._cipher.decrypt(
                envelope[nonce_start:nonce_end], envelope[nonce_end:], context.aad()
            )
        except InvalidTag as exc:
            raise SecretEnvelopeError("delivery secret authentication failed") from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretEnvelopeError("delivery secret is not valid UTF-8") from exc


class KmsSealer:
    """AWS KMS encrypt-only capability for the application plane."""

    def __init__(
        self,
        client: Any,
        *,
        key_id: str,
        policy: DependencyPolicy | None = None,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._key_id = key_id
        self._policy = policy or DependencyPolicy()
        self._circuit = circuit or CircuitBreaker(DependencyName.KMS)

    async def seal(self, plaintext: str, *, context: SecretContext) -> bytes:
        if not plaintext:
            raise ValueError("a delivery secret cannot be empty")
        async def operation() -> Any:
            return await asyncio.to_thread(
                self._client.encrypt,
                KeyId=self._key_id,
                Plaintext=plaintext.encode("utf-8"),
                EncryptionContext=context.encryption_context(),
            )

        response = await call_dependency(
            DependencyName.KMS,
            operation,
            policy=self._policy,
            circuit=self._circuit,
        )
        return _KMS_PREFIX + cast(bytes, response["CiphertextBlob"])


class KmsUnsealer:
    """AWS KMS decrypt-only capability for the work plane."""

    def __init__(
        self,
        client: Any,
        *,
        policy: DependencyPolicy | None = None,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._policy = policy or DependencyPolicy()
        self._circuit = circuit or CircuitBreaker(DependencyName.KMS)

    async def unseal(self, envelope: bytes, *, context: SecretContext) -> str:
        if not envelope.startswith(_KMS_PREFIX) or len(envelope) == len(_KMS_PREFIX):
            raise SecretEnvelopeError("unsupported or truncated KMS secret envelope")
        async def operation() -> Any:
            return await asyncio.to_thread(
                self._client.decrypt,
                CiphertextBlob=envelope[len(_KMS_PREFIX) :],
                EncryptionContext=context.encryption_context(),
            )

        response = await call_dependency(
            DependencyName.KMS,
            operation,
            policy=self._policy,
            circuit=self._circuit,
        )
        plaintext = cast(bytes, response["Plaintext"])
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretEnvelopeError("delivery secret is not valid UTF-8") from exc


def kms_client(settings: Settings) -> Any:
    """Build KMS with SDK retries off and I/O bounded by the shared policy."""
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    return boto3.client(
        "kms",
        config=Config(
            connect_timeout=settings.dependency_timeout_seconds,
            read_timeout=settings.dependency_timeout_seconds,
            retries={"max_attempts": 0},
        ),
    )


def kms_policy(settings: Settings) -> DependencyPolicy:
    """Resolve the common dependency limits for KMS calls."""
    return DependencyPolicy(
        timeout_seconds=settings.dependency_timeout_seconds,
        max_attempts=settings.dependency_max_attempts,
        backoff_base_seconds=settings.dependency_backoff_base_seconds,
        backoff_max_seconds=settings.dependency_backoff_max_seconds,
    )


def kms_circuit(settings: Settings) -> CircuitBreaker:
    """Resolve the common dependency circuit for one KMS capability."""
    return CircuitBreaker(
        DependencyName.KMS,
        failure_threshold=settings.dependency_circuit_failure_threshold,
        recovery_seconds=settings.dependency_circuit_recovery_seconds,
    )


def create_delivery_unsealer(settings: Settings) -> SecretUnsealer:
    """Build the work plane's decrypt-only E-1 capability."""
    if settings.email_delivery_local_key is not None:
        return LocalAeadCipher.from_base64(
            settings.email_delivery_local_key.get_secret_value()
        )
    if settings.email_delivery_kms_key_id is not None:
        return KmsUnsealer(
            kms_client(settings),
            policy=kms_policy(settings),
            circuit=kms_circuit(settings),
        )
    raise RuntimeError(
        "EMAIL_DELIVERY_LOCAL_KEY or EMAIL_DELIVERY_KMS_KEY_ID is required "
        "for the E-1 worker"
    )

"""S3-API object storage with exact keys and authorization-bound presigning.

The database is the only object index, so this adapter deliberately exposes no
bucket-list operation. Object keys can only be constructed in the four layouts
from data/03 section 5, and deletion requires an erasure-or-sweep reason. A raw
key cannot be presigned: callers first present an authorization result bound to
the same single object and carrying a bounded lifetime (SEC-039).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from bluelab.platform.config import Settings
from bluelab.platform.resilience import (
    CircuitBreaker,
    DependencyName,
    DependencyPolicy,
    call_dependency,
)

_KEY_PATTERNS = (
    re.compile(
        r"^orgs/(?P<org>[0-9a-f-]{36})/recordings/(?P<subject>[0-9a-f-]{36})\.ogg$"
    ),
    re.compile(r"^orgs/(?P<org>[0-9a-f-]{36})/uploads/(?P<subject>[0-9a-f-]{36})$"),
    re.compile(
        r"^orgs/(?P<org>[0-9a-f-]{36})/reports/(?P<subject>[0-9a-f-]{36})\.pdf$"
    ),
    re.compile(r"^exports/(?P<subject>[0-9a-f-]{36})\.zip$"),
)


class InvalidObjectKey(ValueError):
    """A key is outside the closed object-store layout."""


class ExpiredObjectAuthorization(PermissionError):
    """The authorization decision no longer permits issuing a URL."""


class DeletionReason(StrEnum):
    """The only two paths permitted to delete stored objects."""

    ERASURE = "erasure"
    SWEEP = "sweep"


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """One key proven to belong to the authoritative object layout."""

    key: str

    def __post_init__(self) -> None:
        match: re.Match[str] | None = None
        for pattern in _KEY_PATTERNS:
            match = pattern.fullmatch(self.key)
            if match is not None:
                break
        if match is None:
            raise InvalidObjectKey("object key is outside the closed layout")
        for name in ("org", "subject"):
            raw = match.groupdict().get(name)
            if raw is not None:
                try:
                    parsed = UUID(raw)
                except ValueError as exc:
                    raise InvalidObjectKey(
                        "object key contains a malformed identifier"
                    ) from exc
                if str(parsed) != raw:
                    raise InvalidObjectKey(
                        "object key identifiers must be canonical UUIDs"
                    )

    @classmethod
    def recording(cls, *, org_id: UUID, attempt_id: UUID) -> ObjectRef:
        return cls(f"orgs/{org_id}/recordings/{attempt_id}.ogg")

    @classmethod
    def upload(cls, *, org_id: UUID, upload_id: UUID) -> ObjectRef:
        return cls(f"orgs/{org_id}/uploads/{upload_id}")

    @classmethod
    def report(cls, *, org_id: UUID, candidate_id: UUID) -> ObjectRef:
        return cls(f"orgs/{org_id}/reports/{candidate_id}.pdf")

    @classmethod
    def export(cls, *, request_id: UUID) -> ObjectRef:
        return cls(f"exports/{request_id}.zip")


@dataclass(frozen=True, slots=True)
class AuthorizedObjectRead:
    """Result of an access decision, bound to exactly one object.

    Domain services construct this only after their own authorization query.
    The presigner checks the decision's freshness and exact key again; it never
    accepts a bare ``ObjectRef``.
    """

    object_ref: ObjectRef
    principal_id: UUID
    authorized_until: datetime

    def __post_init__(self) -> None:
        if self.authorized_until.tzinfo is None:
            raise ValueError("object authorization expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PresignedObject:
    """A single-object bearer capability and its effective expiry."""

    url: str
    expires_at: datetime


class ObjectStore(Protocol):
    """Operations application/work code may perform against large objects."""

    async def put(self, ref: ObjectRef, data: bytes, *, content_type: str) -> None: ...

    async def get(self, ref: ObjectRef) -> bytes: ...

    async def exists(self, ref: ObjectRef) -> bool: ...

    async def delete(self, ref: ObjectRef, *, reason: DeletionReason) -> None: ...

    async def presign_get(
        self, authorization: AuthorizedObjectRead
    ) -> PresignedObject: ...


class RestoreObjectStore(ObjectStore, Protocol):
    """Maintenance-only enumeration capability used after a restore."""

    async def list_prefix(self, prefix: str) -> list[ObjectRef]: ...


class S3ObjectStore:
    """One S3 client for Supabase Storage, Amazon S3, or MinIO."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        presign_seconds: int,
        policy: DependencyPolicy,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        if not bucket or "/" in bucket:
            raise ValueError("object-store bucket is invalid")
        if not 1 <= presign_seconds <= 300:
            raise ValueError("presigned URL lifetime must be between 1 and 300 seconds")
        self._client = client
        self._bucket = bucket
        self._presign_seconds = presign_seconds
        self._policy = policy
        self._circuit = circuit or CircuitBreaker(DependencyName.OBJECT_STORE)

    async def put(self, ref: ObjectRef, data: bytes, *, content_type: str) -> None:
        if not content_type or len(content_type) > 128:
            raise ValueError("object content type is invalid")

        async def operation() -> None:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=ref.key,
                Body=data,
                ContentType=content_type,
            )

        await self._call(operation)

    async def get(self, ref: ObjectRef) -> bytes:
        def blocking_get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=ref.key)
            body = response["Body"]
            try:
                return cast(bytes, body.read())
            finally:
                body.close()

        async def operation() -> bytes:
            return await asyncio.to_thread(blocking_get)

        return await self._call(operation)

    async def exists(self, ref: ObjectRef) -> bool:
        """Check one exact key with HEAD; never grant ops access to object bytes."""
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        def blocking_head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=ref.key)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return False
                raise
            return True

        async def operation() -> bool:
            return await asyncio.to_thread(blocking_head)

        return await self._call(operation)

    async def delete(self, ref: ObjectRef, *, reason: DeletionReason) -> None:
        if not isinstance(reason, DeletionReason):
            raise TypeError("object deletion requires an erasure or sweep reason")

        async def operation() -> None:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=ref.key,
            )

        await self._call(operation)

    async def presign_get(self, authorization: AuthorizedObjectRead) -> PresignedObject:
        if not isinstance(authorization, AuthorizedObjectRead):
            raise TypeError("presigning requires a completed object-read authorization")
        now = datetime.now(UTC)
        authorized_until = authorization.authorized_until.astimezone(UTC)
        remaining = int((authorized_until - now).total_seconds())
        if remaining < 1:
            raise ExpiredObjectAuthorization("object-read authorization has expired")
        lifetime = min(self._presign_seconds, remaining)

        async def operation() -> str:
            return cast(
                str,
                await asyncio.to_thread(
                    self._client.generate_presigned_url,
                    "get_object",
                    Params={
                        "Bucket": self._bucket,
                        "Key": authorization.object_ref.key,
                    },
                    ExpiresIn=lifetime,
                ),
            )

        url = await self._call(operation)
        return PresignedObject(url=url, expires_at=now + timedelta(seconds=lifetime))

    async def list_prefix(self, prefix: str) -> list[ObjectRef]:
        """Enumerate a closed prefix for restore reconciliation only."""
        if prefix not in {"orgs/", "exports/"}:
            raise ValueError("restore enumeration requires a closed prefix")

        def blocking_list() -> list[ObjectRef]:
            token: str | None = None
            refs: list[ObjectRef] = []
            while True:
                arguments: dict[str, object] = {
                    "Bucket": self._bucket,
                    "Prefix": prefix,
                }
                if token is not None:
                    arguments["ContinuationToken"] = token
                page = self._client.list_objects_v2(**arguments)
                refs.extend(ObjectRef(str(item["Key"])) for item in page.get("Contents", []))
                if not page.get("IsTruncated"):
                    return refs
                token = str(page["NextContinuationToken"])

        async def operation() -> list[ObjectRef]:
            return await asyncio.to_thread(blocking_list)

        return await self._call(operation)

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        return await call_dependency(
            DependencyName.OBJECT_STORE,
            operation,
            policy=self._policy,
            circuit=self._circuit,
        )


def create_object_store(settings: Settings) -> S3ObjectStore:
    """Build the configured S3 client with SDK retries disabled and bounded I/O."""
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    policy = DependencyPolicy(
        timeout_seconds=settings.dependency_timeout_seconds,
        max_attempts=settings.dependency_max_attempts,
        backoff_base_seconds=settings.dependency_backoff_base_seconds,
        backoff_max_seconds=settings.dependency_backoff_max_seconds,
    )
    client = boto3.client(
        "s3",
        endpoint_url=settings.object_store_endpoint,
        region_name=settings.object_store_region,
        aws_access_key_id=(
            settings.object_store_access_key.get_secret_value()
            if settings.object_store_access_key is not None
            else None
        ),
        aws_secret_access_key=(
            settings.object_store_secret_key.get_secret_value()
            if settings.object_store_secret_key is not None
            else None
        ),
        config=Config(
            connect_timeout=settings.dependency_timeout_seconds,
            read_timeout=settings.dependency_timeout_seconds,
            retries={"max_attempts": 0},
            signature_version="s3v4",
        ),
    )
    return S3ObjectStore(
        client,
        bucket=settings.object_store_bucket,
        presign_seconds=settings.object_presign_seconds,
        policy=policy,
        circuit=CircuitBreaker(
            DependencyName.OBJECT_STORE,
            failure_threshold=settings.dependency_circuit_failure_threshold,
            recovery_seconds=settings.dependency_circuit_recovery_seconds,
        ),
    )

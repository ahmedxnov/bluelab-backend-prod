"""Independent, conditionally ordered organization lifecycle history."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from bluelab.platform.config import Settings

GENESIS_DIGEST = hashlib.sha256(b"").hexdigest()


class HistoryConflict(Exception):
    """The operation id or expected head cannot be appended as requested."""


class HistoryUnverified(Exception):
    """A head, link, or independent store cannot be verified."""


class HistoryStore(Protocol):
    async def read(self, key: str) -> tuple[bytes, str] | None: ...

    async def put(self, key: str, body: bytes, *, expected_etag: str | None) -> None: ...


def _bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    org_id: UUID
    operation_id: UUID
    sequence: int
    previous_operation_id: UUID | None
    previous_digest: str
    action: str
    accepted_at: str
    actor_id: UUID | None
    reason: str
    data: dict[str, Any]

    def document(self) -> dict[str, Any]:
        return {
            "org_id": str(self.org_id), "operation_id": str(self.operation_id),
            "sequence": self.sequence,
            "previous_operation_id": str(self.previous_operation_id)
            if self.previous_operation_id else None,
            "previous_digest": self.previous_digest, "action": self.action,
            "accepted_at": self.accepted_at,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "reason": self.reason, "data": self.data,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_bytes(self.document())).hexdigest()

    @classmethod
    def parse(cls, body: bytes) -> HistoryEvent:
        raw = json.loads(body)
        return cls(
            org_id=UUID(raw["org_id"]), operation_id=UUID(raw["operation_id"]),
            sequence=int(raw["sequence"]),
            previous_operation_id=UUID(raw["previous_operation_id"])
            if raw["previous_operation_id"] else None,
            previous_digest=str(raw["previous_digest"]), action=str(raw["action"]),
            accepted_at=str(raw["accepted_at"]),
            actor_id=UUID(raw["actor_id"]) if raw["actor_id"] else None,
            reason=str(raw["reason"]), data=dict(raw["data"]),
        )


@dataclass(frozen=True, slots=True)
class HistoryHead:
    sequence: int
    digest: str
    operation_id: UUID | None
    etag: str | None


class LifecycleHistory:
    """Verify the protected head and every immutable link before decisions."""

    def __init__(self, store: HistoryStore) -> None:
        self._store = store

    @staticmethod
    def _head_key(org_id: UUID) -> str:
        return f"org-lifecycle/{org_id}/head.json"

    @staticmethod
    def _event_key(org_id: UUID, operation_id: UUID) -> str:
        return f"org-lifecycle/{org_id}/events/{operation_id}.json"

    async def verified_head(self, org_id: UUID) -> HistoryHead:
        try:
            stored = await self._store.read(self._head_key(org_id))
            if stored is None:
                return HistoryHead(0, GENESIS_DIGEST, None, None)
            body, etag = stored
            raw = json.loads(body)
            head = HistoryHead(
                sequence=int(raw["sequence"]), digest=str(raw["digest"]),
                operation_id=UUID(raw["operation_id"]), etag=etag,
            )
            if head.sequence < 1:
                raise HistoryUnverified("invalid lifecycle head sequence")
            cursor_id = head.operation_id
            expected_digest = head.digest
            for sequence in range(head.sequence, 0, -1):
                if cursor_id is None:
                    raise HistoryUnverified("missing lifecycle predecessor")
                event_result = await self._store.read(self._event_key(org_id, cursor_id))
                if event_result is None:
                    raise HistoryUnverified("missing lifecycle event")
                event = HistoryEvent.parse(event_result[0])
                if (
                    event.org_id != org_id or event.operation_id != cursor_id
                    or event.sequence != sequence or event.digest != expected_digest
                ):
                    raise HistoryUnverified("lifecycle event chain mismatch")
                cursor_id = event.previous_operation_id
                expected_digest = event.previous_digest
            if cursor_id is not None or expected_digest != GENESIS_DIGEST:
                raise HistoryUnverified("lifecycle genesis mismatch")
            return head
        except (HistoryUnverified, HistoryConflict):
            raise
        except Exception as exc:
            raise HistoryUnverified("lifecycle history unavailable") from exc

    async def accepted(self, org_id: UUID, operation_id: UUID) -> HistoryEvent | None:
        """Return an event only if it is on the verified head chain."""
        head = await self.verified_head(org_id)
        cursor = head.operation_id
        for _ in range(head.sequence):
            if cursor is None:
                raise HistoryUnverified("lifecycle predecessor missing")
            result = await self._store.read(self._event_key(org_id, cursor))
            if result is None:
                raise HistoryUnverified("lifecycle event missing")
            event = HistoryEvent.parse(result[0])
            if cursor == operation_id:
                return event
            cursor = event.previous_operation_id
        return None

    async def append(
        self, *, org_id: UUID, operation_id: UUID, expected_sequence: int,
        action: str, accepted_at: str, actor_id: UUID | None, reason: str,
        data: dict[str, Any],
    ) -> HistoryEvent:
        head = await self.verified_head(org_id)
        accepted = await self.accepted(org_id, operation_id)
        if accepted is not None:
            if (accepted.action, accepted.actor_id, accepted.reason, accepted.data) != (
                action, actor_id, reason, data
            ):
                raise HistoryConflict("operation id reused with different command")
            return accepted
        if head.sequence != expected_sequence:
            raise HistoryConflict("lifecycle sequence changed")
        event = HistoryEvent(
            org_id=org_id, operation_id=operation_id, sequence=head.sequence + 1,
            previous_operation_id=head.operation_id, previous_digest=head.digest,
            action=action, accepted_at=accepted_at, actor_id=actor_id,
            reason=reason, data=data,
        )
        event_key = self._event_key(org_id, operation_id)
        try:
            prior = await self._store.read(event_key)
            if prior is None:
                await self._store.put(event_key, _bytes(event.document()), expected_etag=None)
            elif prior[0] != _bytes(event.document()):
                raise HistoryConflict("operation id reused with different event")
            await self._store.put(
                self._head_key(org_id),
                _bytes({"sequence": event.sequence, "digest": event.digest,
                        "operation_id": str(operation_id)}),
                expected_etag=head.etag,
            )
        except HistoryConflict:
            resolved = await self.accepted(org_id, operation_id)
            if resolved is not None:
                return resolved
            raise
        except Exception as exc:
            resolved = await self.accepted(org_id, operation_id)
            if resolved is not None:
                return resolved
            raise HistoryUnverified("lifecycle append outcome uncertain") from exc
        verified = await self.accepted(org_id, operation_id)
        if verified is None or verified.digest != event.digest:
            raise HistoryUnverified("lifecycle append not verified")
        return verified


class S3HistoryStore:
    def __init__(self, client: Any, bucket: str, kms_key_id: str | None = None) -> None:
        self._client = client
        self._bucket = bucket
        self._kms_key_id = kms_key_id

    async def read(self, key: str) -> tuple[bytes, str] | None:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        try:
            result = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        stream = result["Body"]
        try:
            return bytes(stream.read()), str(result["ETag"])
        finally:
            stream.close()

    async def put(self, key: str, body: bytes, *, expected_etag: str | None) -> None:
        from botocore.exceptions import ClientError

        encryption = (
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._kms_key_id}
            if self._kms_key_id else {}
        )
        try:
            await asyncio.to_thread(
                self._client.put_object, Bucket=self._bucket, Key=key, Body=body,
                ContentType="application/json",
                **({"IfMatch": expected_etag} if expected_etag else {"IfNoneMatch": "*"}),
                **encryption,
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}:
                raise HistoryConflict("lifecycle append lost conditional race") from exc
            raise


def create_lifecycle_history(settings: Settings) -> LifecycleHistory:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    client = boto3.client(
        "s3", endpoint_url=settings.object_store_endpoint,
        region_name=settings.object_store_region,
        aws_access_key_id=settings.object_store_access_key.get_secret_value()
        if settings.object_store_access_key else None,
        aws_secret_access_key=settings.object_store_secret_key.get_secret_value()
        if settings.object_store_secret_key else None,
        config=Config(connect_timeout=settings.dependency_timeout_seconds,
                      read_timeout=settings.dependency_timeout_seconds,
                      retries={"max_attempts": 0}, signature_version="s3v4"),
    )
    return LifecycleHistory(S3HistoryStore(
        client, settings.lifecycle_history_bucket, settings.lifecycle_history_kms_key_id
    ))

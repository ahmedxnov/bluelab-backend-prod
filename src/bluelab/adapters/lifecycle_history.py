"""Independent, conditionally ordered organization lifecycle history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
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

    async def list_keys(self, prefix: str) -> list[str]: ...


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

    def __init__(self, store: HistoryStore, backup_store: HistoryStore | None = None) -> None:
        self._store = store
        self._backup = backup_store or store

    @staticmethod
    def _validate_purge_key(key: str) -> None:
        if re.fullmatch(r"org-purge/[0-9a-f-]{36}/[0-9a-f-]{36}/[a-z0-9_/-]+\.json", key) is None:
            raise ValueError("invalid purge evidence key")

    async def put_purge_record(self, key: str, value: dict[str, Any]) -> str:
        """Write an immutable purge manifest or outcome to both independent copies."""
        self._validate_purge_key(key)
        body = _bytes(value)
        try:
            await self._put_immutable(key, body)
            digest = hashlib.sha256(body).hexdigest()
            await self.read_purge_record(key, digest)
            return digest
        except (HistoryConflict, HistoryUnverified):
            raise
        except Exception as exc:
            raise HistoryUnverified("purge evidence write unavailable") from exc

    async def read_purge_record(self, key: str, digest: str) -> dict[str, Any]:
        """Require matching copies and a caller-pinned digest before use."""
        self._validate_purge_key(key)
        try:
            primary = await self._store.read(key)
            backup = await self._backup.read(key)
            if (primary is None or backup is None or primary[0] != backup[0]
                    or hashlib.sha256(primary[0]).hexdigest() != digest):
                raise HistoryUnverified("purge evidence copy or digest mismatch")
            value = json.loads(primary[0])
            if not isinstance(value, dict):
                raise HistoryUnverified("purge evidence shape invalid")
            return value
        except HistoryUnverified:
            raise
        except Exception as exc:
            raise HistoryUnverified("purge evidence read unavailable") from exc

    async def verified_purge_record(self, key: str) -> tuple[str, dict[str, Any]]:
        """Recover a manifest digest when its database projection was rolled back."""
        self._validate_purge_key(key)
        try:
            primary = await self._store.read(key)
            backup = await self._backup.read(key)
            if primary is None or backup is None or primary[0] != backup[0]:
                raise HistoryUnverified("purge evidence copies differ")
            digest = hashlib.sha256(primary[0]).hexdigest()
            return digest, await self.read_purge_record(key, digest)
        except HistoryUnverified:
            raise
        except Exception as exc:
            raise HistoryUnverified("purge evidence read unavailable") from exc

    @staticmethod
    def _catalog_key(org_id: UUID) -> str:
        return f"org-lifecycle/catalog/orgs/{org_id}.json"

    @staticmethod
    def _receipt_prefix(org_id: UUID) -> str:
        return f"org-lifecycle/catalog/receipts/{org_id}/"

    @classmethod
    def _receipt_key(cls, org_id: UUID, sequence: int) -> str:
        return f"{cls._receipt_prefix(org_id)}{sequence}.json"

    async def _put_immutable(self, key: str, body: bytes) -> None:
        for store in (self._store, self._backup):
            current = await store.read(key)
            if current is None:
                try:
                    await store.put(key, body, expected_etag=None)
                except HistoryConflict:
                    raced = await store.read(key)
                    if raced is None or raced[0] != body:
                        raise
            elif current[0] != body:
                raise HistoryConflict("immutable lifecycle record differs")

    async def register(self, org_id: UUID) -> None:
        """Reserve an organization identifier before its shell is acknowledged."""
        try:
            await self._put_immutable(
                self._catalog_key(org_id), _bytes({"org_id": str(org_id)})
            )
            await self._put_immutable(self._receipt_key(org_id, 0), _bytes({
                "org_id": str(org_id), "sequence": 0,
                "operation_id": None, "digest": GENESIS_DIGEST,
            }))
        except HistoryConflict:
            raise
        except Exception as exc:
            raise HistoryUnverified("organization catalog registration unavailable") from exc

    async def catalog_orgs(self) -> list[UUID]:
        """Enumerate both protected copies, including purged organizations."""
        prefix = "org-lifecycle/catalog/orgs/"
        try:
            primary = set(await self._store.list_keys(prefix))
            backup = set(await self._backup.list_keys(prefix))
            if primary != backup:
                raise HistoryUnverified("organization catalog copies differ")
            if any(not key.endswith(".json") for key in primary):
                raise HistoryUnverified("malformed organization catalog entry")
            return sorted(UUID(key[len(prefix):-5]) for key in primary)
        except HistoryUnverified:
            raise
        except Exception as exc:
            raise HistoryUnverified("organization catalog unavailable") from exc

    @staticmethod
    def _head_key(org_id: UUID) -> str:
        return f"org-lifecycle/{org_id}/head.json"

    @staticmethod
    def _event_key(org_id: UUID, operation_id: UUID) -> str:
        return f"org-lifecycle/{org_id}/events/{operation_id}.json"

    async def _verify_catalog(self, org_id: UUID, head: HistoryHead) -> None:
        registration = _bytes({"org_id": str(org_id)})
        for store in (self._store, self._backup):
            entry = await store.read(self._catalog_key(org_id))
            if entry is None or entry[0] != registration:
                raise HistoryUnverified("organization catalog entry missing")
            keys = set(await store.list_keys(self._receipt_prefix(org_id)))
            expected = {
                self._receipt_key(org_id, sequence)
                for sequence in range(head.sequence + 1)
            }
            if keys != expected:
                raise HistoryUnverified("lifecycle receipt sequence mismatch")
            previous_digest = GENESIS_DIGEST
            previous_operation_id: UUID | None = None
            for sequence in range(head.sequence + 1):
                receipt = await store.read(self._receipt_key(org_id, sequence))
                if receipt is None:
                    raise HistoryUnverified("lifecycle receipt missing")
                data = json.loads(receipt[0])
                if data.get("org_id") != str(org_id) or data.get("sequence") != sequence:
                    raise HistoryUnverified("lifecycle receipt identity mismatch")
                if sequence == 0:
                    if data.get("digest") != GENESIS_DIGEST or data.get("operation_id") is not None:
                        raise HistoryUnverified("lifecycle genesis receipt mismatch")
                else:
                    event_result = await self._store.read(
                        self._event_key(org_id, UUID(data["operation_id"]))
                    )
                    if event_result is None:
                        raise HistoryUnverified("lifecycle receipt event missing")
                    event = HistoryEvent.parse(event_result[0])
                    if (
                        event.sequence != sequence or event.digest != data.get("digest")
                        or event.previous_digest != previous_digest
                        or event.previous_operation_id != previous_operation_id
                    ):
                        raise HistoryUnverified("lifecycle receipt digest mismatch")
                    backup_event = await self._backup.read(
                        self._event_key(org_id, event.operation_id))
                    if backup_event is None or backup_event[0] != event_result[0]:
                        raise HistoryUnverified("lifecycle event backup mismatch")
                    previous_digest = event.digest
                    previous_operation_id = event.operation_id
                if sequence == head.sequence and data.get("digest") != head.digest:
                    raise HistoryUnverified("lifecycle receipt head mismatch")
            if previous_operation_id != head.operation_id:
                raise HistoryUnverified("lifecycle receipt operation mismatch")

    async def verified_head(self, org_id: UUID) -> HistoryHead:
        try:
            stored = await self._store.read(self._head_key(org_id))
            if stored is None:
                head = HistoryHead(0, GENESIS_DIGEST, None, None)
                await self._verify_catalog(org_id, head)
                return head
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
            await self._verify_catalog(org_id, head)
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

    async def verified_events(self, org_id: UUID) -> list[HistoryEvent]:
        """Return the complete authoritative sequence for isolated replay."""
        head = await self.verified_head(org_id)
        events: list[HistoryEvent] = []
        cursor = head.operation_id
        for expected in range(head.sequence, 0, -1):
            if cursor is None:
                raise HistoryUnverified("lifecycle predecessor missing")
            result = await self._store.read(self._event_key(org_id, cursor))
            if result is None:
                raise HistoryUnverified("lifecycle event missing")
            event = HistoryEvent.parse(result[0])
            if event.sequence != expected or event.org_id != org_id:
                raise HistoryUnverified("lifecycle event sequence mismatch")
            events.append(event)
            cursor = event.previous_operation_id
        events.reverse()
        return events

    async def repair_interrupted_receipt(self, org_id: UUID) -> bool:
        """Repair only a head whose final event is durable in both authorities.

        This runs behind the restore barrier. It never infers an event from a
        database projection or advances the head to an unverified event.
        """
        try:
            stored = await self._store.read(self._head_key(org_id))
            if stored is None:
                return False
            raw = json.loads(stored[0])
            sequence = int(raw["sequence"])
            operation_id = UUID(raw["operation_id"])
            digest = str(raw["digest"])
            if sequence < 1:
                raise HistoryUnverified("invalid lifecycle head sequence")
            key = self._receipt_key(org_id, sequence)
            primary = await self._store.read(key)
            backup = await self._backup.read(key)
            if primary is not None and backup is not None:
                return False
            event_key = self._event_key(org_id, operation_id)
            event_pair = await self._store.read(event_key)
            backup_pair = await self._backup.read(event_key)
            if event_pair is None or backup_pair is None or event_pair[0] != backup_pair[0]:
                raise HistoryUnverified("unverified lifecycle tail event")
            event = HistoryEvent.parse(event_pair[0])
            if (event.org_id != org_id or event.sequence != sequence
                    or event.operation_id != operation_id or event.digest != digest):
                raise HistoryUnverified("lifecycle tail disagrees with head")
            predecessor = await self._store.read(self._receipt_key(org_id, sequence - 1))
            backup_predecessor = await self._backup.read(
                self._receipt_key(org_id, sequence - 1))
            if (predecessor is None or backup_predecessor is None
                    or predecessor[0] != backup_predecessor[0]):
                raise HistoryUnverified("lifecycle predecessor receipt missing")
            previous = json.loads(predecessor[0])
            if (previous.get("digest") != event.previous_digest
                    or previous.get("operation_id") != (
                        str(event.previous_operation_id)
                        if event.previous_operation_id else None)):
                raise HistoryUnverified("lifecycle predecessor receipt mismatch")
            receipt = _bytes({
                "org_id": str(org_id), "sequence": sequence,
                "operation_id": str(operation_id), "digest": digest,
            })
            for existing in (primary, backup):
                if existing is not None and existing[0] != receipt:
                    raise HistoryUnverified("lifecycle tail receipt conflict")
            await self._put_immutable(key, receipt)
            await self.verified_head(org_id)
            return True
        except (HistoryUnverified, HistoryConflict):
            raise
        except Exception as exc:
            raise HistoryUnverified("lifecycle receipt repair unavailable") from exc

    async def reconstruct_head_from_receipts(self, org_id: UUID) -> HistoryHead:
        """Advance a stale or absent head only through complete dual-copy receipts."""
        try:
            registration = _bytes({"org_id": str(org_id)})
            for store in (self._store, self._backup):
                item = await store.read(self._catalog_key(org_id))
                if item is None or item[0] != registration:
                    raise HistoryUnverified("organization catalog entry missing")
            primary = set(await self._store.list_keys(self._receipt_prefix(org_id)))
            backup = set(await self._backup.list_keys(self._receipt_prefix(org_id)))
            if primary != backup or self._receipt_key(org_id, 0) not in primary:
                raise HistoryUnverified("lifecycle receipts differ")
            highest = len(primary) - 1
            if primary != {self._receipt_key(org_id, index)
                           for index in range(highest + 1)}:
                raise HistoryUnverified("lifecycle receipt sequence mismatch")
            previous_digest = GENESIS_DIGEST
            previous_id: UUID | None = None
            for index in range(highest + 1):
                key = self._receipt_key(org_id, index)
                pair = await self._store.read(key)
                backup_pair = await self._backup.read(key)
                if pair is None or backup_pair is None or pair[0] != backup_pair[0]:
                    raise HistoryUnverified("lifecycle receipt copies differ")
                receipt = json.loads(pair[0])
                if receipt.get("org_id") != str(org_id) or receipt.get("sequence") != index:
                    raise HistoryUnverified("lifecycle receipt identity mismatch")
                if index == 0:
                    if (receipt.get("digest") != GENESIS_DIGEST
                            or receipt.get("operation_id") is not None):
                        raise HistoryUnverified("lifecycle genesis receipt mismatch")
                    continue
                operation_id = UUID(receipt["operation_id"])
                event_key = self._event_key(org_id, operation_id)
                event_pair = await self._store.read(event_key)
                backup_event = await self._backup.read(event_key)
                if (event_pair is None or backup_event is None
                        or event_pair[0] != backup_event[0]):
                    raise HistoryUnverified("lifecycle event copies differ")
                event = HistoryEvent.parse(event_pair[0])
                if (event.org_id != org_id or event.operation_id != operation_id
                        or event.sequence != index or event.digest != receipt.get("digest")
                        or event.previous_digest != previous_digest
                        or event.previous_operation_id != previous_id):
                    raise HistoryUnverified("lifecycle receipt chain mismatch")
                previous_digest = event.digest
                previous_id = operation_id
            current = await self._store.read(self._head_key(org_id))
            if current is not None:
                raw = json.loads(current[0])
                current_sequence = int(raw["sequence"])
                if current_sequence > highest:
                    raise HistoryUnverified("lifecycle head ahead of receipts")
                if current_sequence == highest:
                    return await self.verified_head(org_id)
                receipt_pair = await self._store.read(
                    self._receipt_key(org_id, current_sequence))
                if receipt_pair is None or json.loads(receipt_pair[0]).get("digest") != raw["digest"]:
                    raise HistoryUnverified("lifecycle head disagrees with receipts")
            if highest == 0:
                return await self.verified_head(org_id)
            await self._store.put(self._head_key(org_id), _bytes({
                "sequence": highest, "digest": previous_digest,
                "operation_id": str(previous_id),
            }), expected_etag=current[1] if current else None)
            return await self.verified_head(org_id)
        except (HistoryUnverified, HistoryConflict):
            raise
        except Exception as exc:
            raise HistoryUnverified("lifecycle head reconstruction unavailable") from exc

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
            await self._put_immutable(event_key, _bytes(event.document()))
            await self._store.put(
                self._head_key(org_id),
                _bytes({"sequence": event.sequence, "digest": event.digest,
                        "operation_id": str(operation_id)}),
                expected_etag=head.etag,
            )
            await self._put_immutable(self._receipt_key(org_id, event.sequence), _bytes({
                "org_id": str(org_id), "sequence": event.sequence,
                "operation_id": str(operation_id), "digest": event.digest,
            }))
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

    async def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if continuation is not None:
                request["ContinuationToken"] = continuation
            result = await asyncio.to_thread(self._client.list_objects_v2, **request)
            keys.extend(str(item["Key"]) for item in result.get("Contents", []))
            if not result.get("IsTruncated"):
                return keys
            continuation = str(result["NextContinuationToken"])

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

    config = Config(connect_timeout=settings.dependency_timeout_seconds,
                    read_timeout=settings.dependency_timeout_seconds,
                    retries={"max_attempts": 0}, signature_version="s3v4")
    client = boto3.client(
        "s3", endpoint_url=settings.object_store_endpoint,
        region_name=settings.object_store_region,
        aws_access_key_id=settings.object_store_access_key.get_secret_value()
        if settings.object_store_access_key else None,
        aws_secret_access_key=settings.object_store_secret_key.get_secret_value()
        if settings.object_store_secret_key else None,
        config=config,
    )
    backup = boto3.client(
        "s3", endpoint_url=settings.erasure_backup_endpoint or settings.object_store_endpoint,
        region_name=settings.erasure_backup_region or settings.object_store_region,
        aws_access_key_id=(settings.erasure_backup_access_key.get_secret_value()
                           if settings.erasure_backup_access_key else
                           settings.object_store_access_key.get_secret_value()
                           if settings.object_store_access_key else None),
        aws_secret_access_key=(settings.erasure_backup_secret_key.get_secret_value()
                               if settings.erasure_backup_secret_key else
                               settings.object_store_secret_key.get_secret_value()
                               if settings.object_store_secret_key else None),
        config=config,
    )
    return LifecycleHistory(
        S3HistoryStore(client, settings.lifecycle_history_bucket,
                       settings.lifecycle_history_kms_key_id),
        S3HistoryStore(backup, settings.lifecycle_history_backup_bucket,
                       settings.lifecycle_history_kms_key_id),
    )

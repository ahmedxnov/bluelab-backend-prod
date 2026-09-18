"""Immutable two-copy erasure replay ledger (SEC-018)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from bluelab.platform.config import Settings


@dataclass(frozen=True, slots=True)
class ErasureMarker:
    request_id: UUID
    org_id: UUID
    subject_kind: str
    subject_id: UUID
    requested_at: datetime
    armed_at: datetime
    executed_by: UUID

    def bytes(self) -> bytes:
        return json.dumps(
            {
                "request_id": str(self.request_id),
                "org_id": str(self.org_id),
                "subject_kind": self.subject_kind,
                "subject_id": str(self.subject_id),
                "requested_at": self.requested_at.isoformat(),
                "armed_at": self.armed_at.isoformat(),
                "executed_by": str(self.executed_by),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


class ErasureLedger(Protocol):
    async def arm(self, marker: ErasureMarker) -> None: ...

    async def markers(self) -> list[ErasureMarker]: ...

    async def is_armed(self, request_id: UUID) -> bool: ...


class S3ErasureLedger:
    """Write identical, immutable markers to live and off-provider buckets."""

    def __init__(
        self,
        client: Any,
        *,
        live_bucket: str,
        backup_bucket: str,
        backup_client: Any | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        self._client = client
        self._backup_client = backup_client or client
        self._live = live_bucket
        self._backup = backup_bucket
        self._kms_key_id = kms_key_id

    async def arm(self, marker: ErasureMarker) -> None:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        key = f"erasure-ledger/{marker.request_id}.json"
        candidate = marker.bytes()

        async def put(
            client: Any,
            bucket: str,
            body: bytes,
            *,
            allow_same_identity: bool,
        ) -> bytes:
            encryption = (
                {
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": self._kms_key_id,
                }
                if self._kms_key_id is not None
                else {}
            )
            try:
                await asyncio.to_thread(
                    client.put_object,
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    # S3 rejects an overwrite atomically. Bucket IAM additionally
                    # denies overwrite/delete in deployed environments.
                    IfNoneMatch="*",
                    **encryption,
                )
                return body
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code not in {"PreconditionFailed", "412"} and status != 412:
                    raise

                # A retry after the live write succeeded but the backup write
                # failed must be able to finish the second copy. An existing
                # marker is accepted only when it describes the same request.
                # The live copy supplies the canonical armed_at timestamp to a
                # backup write that is completing a partially successful retry.
                result = await asyncio.to_thread(
                    client.get_object, Bucket=bucket, Key=key
                )
                stream = result["Body"]
                try:
                    existing = bytes(stream.read())
                finally:
                    stream.close()
                same_identity = False
                if allow_same_identity:
                    try:
                        existing_data = json.loads(existing)
                        candidate_data = json.loads(body)
                        existing_data.pop("armed_at", None)
                        candidate_data.pop("armed_at", None)
                        same_identity = existing_data == candidate_data
                    except (TypeError, ValueError, UnicodeDecodeError):
                        same_identity = False
                if existing != body and not same_identity:
                    raise RuntimeError("erasure ledger marker conflict") from exc
                return existing

        canonical = await put(
            self._client,
            self._live,
            candidate,
            allow_same_identity=True,
        )
        await put(
            self._backup_client,
            self._backup,
            canonical,
            allow_same_identity=False,
        )

    async def markers(self) -> list[ErasureMarker]:
        markers: list[ErasureMarker] = []
        continuation_token: str | None = None
        while True:
            request = {
                "Bucket": self._backup,
                "Prefix": "erasure-ledger/",
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            response = await asyncio.to_thread(
                self._backup_client.list_objects_v2,
                **request,
            )
            for item in response.get("Contents", []):
                result = await asyncio.to_thread(
                    self._backup_client.get_object,
                    Bucket=self._backup,
                    Key=item["Key"],
                )
                body = result["Body"]
                try:
                    data = json.loads(body.read())
                finally:
                    body.close()
                markers.append(
                    ErasureMarker(
                        request_id=UUID(data["request_id"]),
                        org_id=UUID(data["org_id"]),
                        subject_kind=str(data["subject_kind"]),
                        subject_id=UUID(data["subject_id"]),
                        requested_at=datetime.fromisoformat(data["requested_at"]),
                        armed_at=datetime.fromisoformat(data["armed_at"]),
                        executed_by=UUID(data["executed_by"]),
                    )
                )
            if not response.get("IsTruncated"):
                break
            continuation_token = str(response["NextContinuationToken"])
        return markers

    async def is_armed(self, request_id: UUID) -> bool:
        """Treat a marker in either durability copy as irreversible."""
        from botocore.exceptions import ClientError

        key = f"erasure-ledger/{request_id}.json"
        found = False
        for client, bucket in (
            (self._client, self._live),
            (self._backup_client, self._backup),
        ):
            try:
                await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
                found = True
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"404", "NoSuchKey", "NotFound"}:
                    raise
        return found


def create_erasure_ledger(settings: Settings) -> S3ErasureLedger:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    client_options = {
        "config": Config(
            connect_timeout=settings.dependency_timeout_seconds,
            read_timeout=settings.dependency_timeout_seconds,
            retries={"max_attempts": 0},
            signature_version="s3v4",
        )
    }
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
        **client_options,
    )
    backup_client = boto3.client(
        "s3",
        endpoint_url=settings.erasure_backup_endpoint or settings.object_store_endpoint,
        region_name=settings.erasure_backup_region or settings.object_store_region,
        aws_access_key_id=(
            settings.erasure_backup_access_key.get_secret_value()
            if settings.erasure_backup_access_key is not None
            else (
                settings.object_store_access_key.get_secret_value()
                if settings.object_store_access_key is not None
                else None
            )
        ),
        aws_secret_access_key=(
            settings.erasure_backup_secret_key.get_secret_value()
            if settings.erasure_backup_secret_key is not None
            else (
                settings.object_store_secret_key.get_secret_value()
                if settings.object_store_secret_key is not None
                else None
            )
        ),
        **client_options,
    )
    return S3ErasureLedger(
        client,
        live_bucket=settings.erasure_ledger_bucket,
        backup_bucket=settings.erasure_backup_ledger_bucket,
        backup_client=backup_client,
        kms_key_id=settings.erasure_ledger_kms_key_id,
    )

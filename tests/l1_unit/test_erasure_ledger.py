from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from bluelab.adapters.erasure_ledger import ErasureMarker, S3ErasureLedger


class _S3:
    def __init__(self, *, fail_first_put: bool = False, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_first_put = fail_first_put
        self.page_size = page_size

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **_: object) -> None:
        if self.fail_first_put:
            self.fail_first_put = False
            raise ClientError(
                {"Error": {"Code": "InternalError"}, "ResponseMetadata": {}},
                "PutObject",
            )
        identity = (Bucket, Key)
        if identity in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        self.objects[identity] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def list_objects_v2(
        self,
        *,
        Bucket: str,
        Prefix: str,
        ContinuationToken: str | None = None,
    ) -> dict[str, object]:
        keys = sorted(
            key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)
        )
        offset = int(ContinuationToken or "0")
        page = keys[offset : offset + self.page_size]
        next_offset = offset + len(page)
        truncated = next_offset < len(keys)
        response: dict[str, object] = {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextContinuationToken"] = str(next_offset)
        return response


def _marker(*, request_id=None, armed_at=None) -> ErasureMarker:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    return ErasureMarker(
        request_id=request_id or uuid4(),
        org_id=uuid4(),
        subject_kind="account",
        subject_id=uuid4(),
        requested_at=now,
        armed_at=armed_at or now,
        executed_by=uuid4(),
    )


@pytest.mark.asyncio
async def test_retry_finishes_backup_with_canonical_live_marker() -> None:
    live = _S3()
    backup = _S3(fail_first_put=True)
    ledger = S3ErasureLedger(
        live,
        live_bucket="live",
        backup_bucket="backup",
        backup_client=backup,
    )
    original = _marker()

    with pytest.raises(ClientError):
        await ledger.arm(original)

    retried = ErasureMarker(
        request_id=original.request_id,
        org_id=original.org_id,
        subject_kind=original.subject_kind,
        subject_id=original.subject_id,
        requested_at=original.requested_at,
        armed_at=original.armed_at + timedelta(minutes=5),
        executed_by=original.executed_by,
    )
    await ledger.arm(retried)

    key = f"erasure-ledger/{original.request_id}.json"
    assert backup.objects[("backup", key)] == live.objects[("live", key)]
    assert backup.objects[("backup", key)] == original.bytes()


@pytest.mark.asyncio
async def test_restore_inventory_reads_every_ledger_page() -> None:
    live = _S3()
    backup = _S3(page_size=1)
    ledger = S3ErasureLedger(
        live,
        live_bucket="live",
        backup_bucket="backup",
        backup_client=backup,
    )
    first = _marker()
    second = _marker()
    await ledger.arm(first)
    await ledger.arm(second)

    markers = await ledger.markers()

    assert {marker.request_id for marker in markers} == {
        first.request_id,
        second.request_id,
    }

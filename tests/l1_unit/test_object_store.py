"""Exact object keys and narrow, expiring S3 presigning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID

import pytest

from bluelab.adapters.object_store import (
    AuthorizedObjectRead,
    DeletionReason,
    ExpiredObjectAuthorization,
    InvalidObjectKey,
    ObjectRef,
    S3ObjectStore,
)
from bluelab.platform.resilience import DependencyPolicy

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security]

ORG = UUID("01936d54-7ad5-7000-8000-000000000001")
SUBJECT = UUID("01936d54-7ad5-7000-8000-000000000002")
PRINCIPAL = UUID("01936d54-7ad5-7000-8000-000000000003")


class _Body(BytesIO):
    pass


class _S3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def put_object(self, **kwargs: object) -> None:
        self.calls.append(("put", kwargs))

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("get", kwargs))
        return {"Body": _Body(b"stored")}

    def delete_object(self, **kwargs: object) -> None:
        self.calls.append(("delete", kwargs))

    def generate_presigned_url(self, operation: str, **kwargs: object) -> str:
        self.calls.append((operation, kwargs))
        return "https://objects.invalid/single-use-capability"


def _store(client: _S3) -> S3ObjectStore:
    return S3ObjectStore(
        client,
        bucket="bluelab-test",
        presign_seconds=300,
        policy=DependencyPolicy(
            timeout_seconds=1,
            max_attempts=1,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
        ),
    )


@pytest.mark.verifies("CMP-001")
def test_only_authoritative_object_key_layouts_are_constructible():
    assert ObjectRef.recording(org_id=ORG, attempt_id=SUBJECT).key == (
        f"orgs/{ORG}/recordings/{SUBJECT}.ogg"
    )
    assert ObjectRef.upload(org_id=ORG, upload_id=SUBJECT).key == (
        f"orgs/{ORG}/uploads/{SUBJECT}"
    )
    assert ObjectRef.report(org_id=ORG, candidate_id=SUBJECT).key == (
        f"orgs/{ORG}/reports/{SUBJECT}.pdf"
    )
    assert ObjectRef.export(request_id=SUBJECT).key == f"exports/{SUBJECT}.zip"
    with pytest.raises(InvalidObjectKey):
        ObjectRef(f"orgs/{ORG}/anything/{SUBJECT}")
    with pytest.raises(InvalidObjectKey):
        ObjectRef("../../another-bucket/object")


@pytest.mark.verifies("ADR-0025")
async def test_presign_requires_fresh_authorization_and_exactly_one_key():
    client = _S3()
    store = _store(client)
    ref = ObjectRef.recording(org_id=ORG, attempt_id=SUBJECT)
    grant = AuthorizedObjectRead(
        object_ref=ref,
        principal_id=PRINCIPAL,
        authorized_until=datetime.now(UTC) + timedelta(seconds=30),
    )

    signed = await store.presign_get(grant)

    assert signed.expires_at <= grant.authorized_until
    assert client.calls == [
        (
            "get_object",
            {
                "Params": {"Bucket": "bluelab-test", "Key": ref.key},
                "ExpiresIn": pytest.approx(29, abs=1),
            },
        )
    ]
    with pytest.raises(TypeError):
        await store.presign_get(ref)  # type: ignore[arg-type]
    with pytest.raises(ExpiredObjectAuthorization):
        await store.presign_get(
            AuthorizedObjectRead(
                object_ref=ref,
                principal_id=PRINCIPAL,
                authorized_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )


@pytest.mark.verifies("CMP-001")
async def test_store_has_no_listing_and_delete_requires_erasure_or_sweep():
    client = _S3()
    store = _store(client)
    ref = ObjectRef.upload(org_id=ORG, upload_id=SUBJECT)

    assert not hasattr(store, "list")
    await store.put(ref, b"bytes", content_type="application/octet-stream")
    assert await store.get(ref) == b"stored"
    await store.delete(ref, reason=DeletionReason.ERASURE)
    with pytest.raises(TypeError):
        await store.delete(ref, reason="cleanup")  # type: ignore[arg-type]

    assert [name for name, _arguments in client.calls] == ["put", "get", "delete"]

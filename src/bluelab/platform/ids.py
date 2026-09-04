"""UUIDv7 identifiers, minted application-side (ADR-0030).

The database never mints identity — that is a portability rule as much as an
identifier one (data/04 §8).

Two properties the choice buys, both load-bearing:

* **Non-enumerable.** A client cannot walk the id space, which is what makes
  denial-indistinguishable-from-non-existence meaningful in the first place
  (AC-IDA-006). An integer key would leak existence through arithmetic no matter
  how careful the API layer was.
* **Time-ordered.** The high bits are a millisecond timestamp, so inserts keep
  index locality and the id itself is a stable newest-first cursor — which is
  exactly what `platform.http.pagination` uses instead of an offset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import uuid_utils


def new_id() -> UUID:
    """Mint a UUIDv7.

    Returns a stdlib `uuid.UUID` rather than the `uuid_utils` type so nothing
    downstream — SQLAlchemy, Pydantic, asyncpg — has to know which library minted
    it.
    """
    return UUID(str(uuid_utils.uuid7()))


def id_timestamp(value: UUID) -> datetime:
    """Recover the creation instant encoded in a UUIDv7's high 48 bits.

    Useful for cursor arithmetic and for debugging; **never** a substitute for a
    `created_at` column, because a row's identity and its business timestamp are
    not the same fact and only one of them is a requirement.

    Raises:
        ValueError: If `value` is not a version-7 UUID.
    """
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: version={value.version}")
    milliseconds = value.int >> 80
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def is_uuid7(value: UUID) -> bool:
    """True if `value` is a version-7 UUID."""
    return value.version == 7

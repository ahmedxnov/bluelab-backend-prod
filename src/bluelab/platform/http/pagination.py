"""Opaque `cursor` + `limit` (default 25, max 100), newest-first unless the owning
requirement fixes another order. Domain-bounded sets return whole and say so
(api/00 §2).

## Why cursors and not offsets

UUIDv7 keys are time-ordered, so a cursor is free: the last id on a page *is* the
position. An `OFFSET` would re-scan the skipped rows on every page and, worse,
would silently skip or repeat rows when something is inserted mid-pagination —
which on a list of attempts means a rep's history quietly losing an entry.

## Opaque, and why that is enforced rather than requested

The cursor is base64url over a small payload. It is not encrypted and is not
pretending to be: the id inside is already non-enumerable (ADR-0030), so there is
nothing to protect. What the encoding buys is that clients **cannot** parse it,
which keeps the internal ordering key free to change without a contract break
(api/00 §2: "clients treat them as opaque").

The `order` field is carried inside the cursor and checked on decode, so a cursor
minted for a newest-first list cannot be replayed against a differently ordered
one and silently return the wrong page.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError

DEFAULT_LIMIT: Final = 25
MAX_LIMIT: Final = 100
_CURSOR_VERSION: Final = 1


class Order(StrEnum):
    """Newest-first unless the owning FR fixes another order (api/00 §2)."""

    NEWEST_FIRST = "desc"
    OLDEST_FIRST = "asc"


@dataclass(frozen=True, slots=True)
class Cursor:
    """A decoded cursor: where the previous page stopped."""

    after_id: UUID
    order: Order

    def encode(self) -> str:
        payload = json.dumps(
            {"v": _CURSOR_VERSION, "id": str(self.after_id), "o": self.order.value},
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @classmethod
    def decode(cls, raw: str, *, expected_order: Order) -> Cursor:
        """Decode a cursor, or reject it.

        Raises:
            ProblemError: `422 validation-error` if the cursor is malformed or
                was minted for a different ordering. A bad cursor is a client
                bug, not a server error, and it must not silently degrade into
                "start from the beginning" — that would repeat a page.
        """
        try:
            padded = raw + "=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if payload["v"] != _CURSOR_VERSION:
                raise ValueError("version")
            order = Order(payload["o"])
            after_id = UUID(payload["id"])
        except (KeyError, ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
            raise _invalid_cursor() from exc

        if order is not expected_order:
            raise _invalid_cursor()
        return cls(after_id=after_id, order=order)


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A validated page request, ready to bound a query."""

    limit: int
    cursor: Cursor | None
    order: Order = Order.NEWEST_FIRST

    @classmethod
    def parse(
        cls,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        order: Order = Order.NEWEST_FIRST,
    ) -> PageRequest:
        """Validate the wire parameters.

        Raises:
            ProblemError: `422 validation-error` if `limit` is out of range. Not
                clamped silently: a client asking for 500 has a wrong assumption,
                and returning 100 without saying so lets it believe the list
                ended.
        """
        effective = DEFAULT_LIMIT if limit is None else limit
        if not 1 <= effective <= MAX_LIMIT:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                meta={
                    "errors": [
                        {"field": "limit", "message": f"must be between 1 and {MAX_LIMIT}"}
                    ]
                },
            )
        decoded = Cursor.decode(cursor, expected_order=order) if cursor else None
        return cls(limit=effective, cursor=decoded, order=order)

    @property
    def fetch_limit(self) -> int:
        """Fetch one extra row to learn `has_more` without a second COUNT query."""
        return self.limit + 1


def _invalid_cursor() -> ProblemError:
    return ProblemError(
        catalog.VALIDATION_ERROR,
        meta={"errors": [{"field": "cursor", "message": "cursor is not valid"}]},
    )

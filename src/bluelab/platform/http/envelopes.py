"""The one list shape: `{data, pagination}`, plus endpoint-specific `counts` where a
surface needs live tab counts that must agree with content under concurrent
change (api/00 §2).

## The counts rule is a correctness rule, not a convenience

Tab counts are **server-computed and travel in the same response as the list**
(FR-TRP-007, FR-HIR-010). The SPA must never derive a count from `data.length` —
a paginated list's length is the page size, and even unpaginated it would drift
the moment something changed between two requests. One response means the tab and
its content cannot disagree, and the frontend's lint carries the other half of the
rule (ux/00 §6 invariant 5).

## Single resources are not enveloped

A single resource is the object itself, not `{data: {...}}`. Wrapping everything
would be more uniform and would buy nothing: there is no pagination to carry and
no counts to reconcile.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bluelab.platform.http.pagination import Cursor, PageRequest


def _default_cursor_id(row: Any) -> UUID:
    """Read `.id` — right for every table, since UUIDv7 is the PK convention
    (ADR-0030). Overridable for the one natural-key exception, `transcript_entry`."""
    return row.id  # type: ignore[no-any-return]


class PaginationMeta(BaseModel):
    """The pagination block. `next_cursor` is null exactly when `has_more` is false."""

    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None = Field(default=None)
    has_more: bool = Field(default=False)


class Page[T](BaseModel):
    """The one list shape.

    Subclass to add `counts` where the surface requires them:

        class LibraryPage(Page[LibraryCard]):
            counts: LibraryCounts
    """

    model_config = ConfigDict(extra="forbid")

    data: list[T]
    pagination: PaginationMeta


def build_page[T](
    rows: Sequence[T],
    *,
    request: PageRequest,
    cursor_id_of: Callable[[T], UUID] | None = None,
) -> tuple[list[T], PaginationMeta]:
    """Trim an over-fetched row set into a page and mint the next cursor.

    The query fetched `request.fetch_limit` — one more than the page size — so
    `has_more` is known without a second COUNT.

    Args:
        rows: Up to `limit + 1` rows, already ordered.
        request: The validated page request.
        cursor_id_of: Callable taking a row and returning its UUID key. Defaults
            to reading `.id`, which is right for every table (ADR-0030) but
            overridable for the one natural-key exception, `transcript_entry`.

    Returns:
        The trimmed rows and their pagination block.
    """
    has_more = len(rows) > request.limit
    page_rows = list(rows[: request.limit])

    next_cursor: str | None = None
    if has_more and page_rows:
        extract = cursor_id_of or _default_cursor_id
        next_cursor = Cursor(after_id=extract(page_rows[-1]), order=request.order).encode()

    return page_rows, PaginationMeta(next_cursor=next_cursor, has_more=has_more)

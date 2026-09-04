"""The declarative base and the column conventions of data/00 §2:

    id uuid primary key, UUIDv7                 (ADR-0030)
    timestamptz created_at / updated_at, UTC
    text + CHECK enumerations, never a native Postgres enum
    numeric(3,1) scores; smallint rubric weights
    denormalized org_id / team_id scope columns
    composite foreign keys over (parent_id, org_id[, team_id])

The composite-FK rule is what makes a child row whose denormalized scope
disagrees with its parent *unrepresentable* (data/00 §3).

## Why the mixins are worth having

Each convention below is one that a hand-written table gets wrong occasionally
and silently:

* a missing `org_id` is a table RLS cannot scope;
* a plain `FOREIGN KEY (parent_id)` lets a child claim a scope its parent does
  not have — the row looks fine and reads across a tenant boundary;
* a native `ENUM` makes the next value an `ALTER TYPE`, which is neither
  transactional nor host-portable (data/00 §2, data/04 §5);
* `datetime.utcnow()` writes a naive value into a `timestamptz` column.

Composing from `OrgScoped` / `TeamScoped` and using `scoped_fk` makes the right
thing the short thing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bluelab.platform.ids import new_id

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names.

Not cosmetics: Alembic autogenerate and the RLS drift check both diff by name, so
a server-assigned name makes every regeneration look like a change.
"""


class Base(DeclarativeBase):
    """Declarative base for every table in the single `public` schema.

    All tables share one schema — the module boundary is a *code* boundary, and
    per-module Postgres schemas would add ceremony without adding enforcement,
    since every module runs as the same application role (data/00 §4).
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # `ClassVar`, because ruff reads a bare mutable class attribute as shared
    # state (RUF012). It is not — SQLAlchemy reads this once at class
    # construction to map Python types onto columns. Annotation only, no
    # runtime change.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        UUID: PgUUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        # `text`, not `varchar`. data/01 specifies `text` throughout, and in
        # PostgreSQL an unbounded VARCHAR is the same storage anyway — so the
        # only thing a default mapping would buy is DDL that no longer matches
        # the document it was written from.
        str: Text(),
    }


class UUIDPrimaryKey:
    """`id uuid primary key`, UUIDv7, minted application-side (ADR-0030).

    `sort_order=-100` puts it first in the rendered DDL. Mixin columns otherwise
    land *after* the declared ones, which produces `CREATE TABLE` text with the
    primary key tenth — legal, and unreadable next to the data/01 block it is
    meant to be reviewed against.
    """

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id, sort_order=-100)


class Timestamped:
    """`created_at`, always. `updated_at` only where the row mutates."""

    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        sort_order=100,
    )


class Mutable(Timestamped):
    """Adds `updated_at` — only for tables that actually mutate (data/00 §2)."""

    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        sort_order=101,
    )


class OrgScoped:
    """`org_id`, on every customer-data table — the CMP-004 boundary."""

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)


class TeamScoped(OrgScoped):
    """`team_id`, on every team-scoped table — the FR-IDA-009 boundary.

    **Team === owning manager**: `team_id` is the owning manager's `account.id`.
    There is no separate team entity because the specification defines none
    (data/00 §3).
    """

    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)


def scope_key(*columns: str) -> UniqueConstraint:
    """The parent side of a composite scope FK.

    A parent exposes `unique (id, org_id[, team_id])` so children can bind to it
    by scope as well as by identity. Redundant as a uniqueness claim — `id` is
    already unique — and required as an FK target.
    """
    return UniqueConstraint("id", *columns)


def scoped_fk(
    *,
    columns: tuple[str, ...],
    parent: str,
    parent_columns: tuple[str, ...],
    on_update: str | None = None,
    ondelete: str | None = None,
) -> ForeignKeyConstraint:
    """A composite foreign key that carries scope alongside identity.

    This is the mechanism behind "scope integrity is FK-enforced, not trusted"
    (data/00 §3): a child row whose denormalized `org_id`/`team_id` disagrees
    with its parent's cannot be inserted at all.

    Args:
        columns: Child columns, e.g. `("drill_id", "org_id")`.
        parent: Parent table name.
        parent_columns: Matching parent columns, e.g. `("id", "org_id")`.
        on_update: Pass `"CASCADE"` for the two legal ownership moves — a
            position transfer (FR-HIR-018) and a rep's team change
            (FR-IDA-009/010), where the subtree must follow atomically. Drills
            deliberately do **not** cascade: a drill stays in the catalog of the
            team that authored it.
        ondelete: Almost always `None`. Erasure is the only remover, and it goes
            through its own procedure (ADR-0033) rather than through cascades.

    Returns:
        The constraint, to be placed in `__table_args__`.
    """
    return ForeignKeyConstraint(
        list(columns),
        [f"{parent}.{column}" for column in parent_columns],
        onupdate=on_update,
        ondelete=ondelete,
    )


def enum_check(column: str, allowed: tuple[str, ...], *, name: str | None = None) -> CheckConstraint:
    """A text enumeration as `text` + `CHECK` — never a native Postgres enum.

    Widening or narrowing is then a constraint swap: `DROP CONSTRAINT`,
    `ADD CONSTRAINT … NOT VALID`, `VALIDATE CONSTRAINT` — transactional and
    host-portable. `ALTER TYPE` is neither, which is precisely why native enums
    were rejected (data/00 §2, data/04 §5).
    """
    values = ", ".join(f"'{value}'" for value in allowed)
    return CheckConstraint(f"{column} in ({values})", name=name or f"{column}_valid")


def table_args(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
    """Assemble `__table_args__` from constraints plus an optional options dict."""
    if kwargs:
        return (*args, kwargs)
    return args

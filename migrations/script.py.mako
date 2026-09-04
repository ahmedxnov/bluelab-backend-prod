"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

One migration = one intent, transactional.

**No `downgrade()`.** The lineage is forward-only (data/04 §1): the rollback of
record for a bad migration is point-in-time restore plus a forward fix. At this
team size, honest PITR beats fictional downgrade code that has never been run.

**Compatibility window.** This must be safe against the *released* application
version N while N+1 rolls out — the application and work planes roll
instance-by-instance and the call plane drains for up to 15 minutes. Breaking
changes stage across releases (expand -> migrate -> contract), never in one
(data/04 §2).

**Not here:** RLS policies, freeze triggers, views, and functions. Those live in
`sql/` and are re-applied idempotently at every release; putting them in the
lineage would make them un-regenerable and the drift check meaningless
(data/04 §3).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}

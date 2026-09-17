"""Persist a complete command envelope for deterministic lifecycle replay.

Revision ID: 0010_lifecycle_command_replay
Revises: 0009_org_service_terms
"""

from __future__ import annotations

from alembic import op

revision = "0010_lifecycle_command_replay"
down_revision = "0009_org_service_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table org_lifecycle_operation add column command_payload jsonb not null default '{}'::jsonb")
    op.execute("alter table org_lifecycle_operation add column command_digest text")


def downgrade() -> None:
    raise NotImplementedError("BlueLab migrations are forward-only")

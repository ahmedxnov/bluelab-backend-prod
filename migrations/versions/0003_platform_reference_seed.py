"""platform reference seed — fixed, idempotent data migrations

Revision ID: 0003_platform_reference_seed
Revises: 0002_queue_schema
Created: 2026-09-05

``legal_document_version``, ``authoring_option``, and ``badge`` are platform
reference data, not customer data.  data/04 §6 requires their stable seed to be
part of the Alembic lineage: every empty database receives the same records and
no bootstrap command needs an out-of-band fixture file.

The v1 vertical (an organisation, team, and product documents) remains outside
this migration.  It is customer data and enters through operations provisioning
and product flows.
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_platform_reference_seed"
down_revision: str | None = "0002_queue_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Upsert the environment-invariant platform reference catalog."""
    op.execute(
        """
INSERT INTO legal_document_version (id, kind, version, effective_at)
VALUES
    ('00000000-0000-7000-8000-000000000101', 'recording_consent_notice', 'v1', '2026-01-01T00:00:00Z'),
    ('00000000-0000-7000-8000-000000000102', 'terms_of_use', 'v1', '2026-01-01T00:00:00Z'),
    ('00000000-0000-7000-8000-000000000103', 'privacy_notice', 'v1', '2026-01-01T00:00:00Z')
ON CONFLICT (id) DO UPDATE
SET kind = EXCLUDED.kind,
    version = EXCLUDED.version,
    effective_at = EXCLUDED.effective_at
        """
    )
    op.execute(
        """
INSERT INTO authoring_option (id, kind, label, active)
VALUES
    ('00000000-0000-7000-8000-000000000201', 'challenge', 'Budget pressure', true),
    ('00000000-0000-7000-8000-000000000202', 'hidden_motive', 'Protect the renewal budget', true)
ON CONFLICT (id) DO UPDATE
SET kind = EXCLUDED.kind,
    label = EXCLUDED.label,
    active = EXCLUDED.active
        """
    )
    op.execute(
        """
INSERT INTO badge (code, name, rule_text, active)
VALUES
    ('first_call', 'First call', 'Complete your first practice call.', true),
    ('practice_streak', 'Practice streak', 'Complete practice calls on three separate days.', true)
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name,
    rule_text = EXCLUDED.rule_text,
    active = EXCLUDED.active
        """
    )

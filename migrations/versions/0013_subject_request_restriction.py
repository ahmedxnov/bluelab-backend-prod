"""Persist organization-scoped restrictions for accepted subject requests.

Revision ID: 0013_subject_request_restriction
Revises: 0012_erasure_guard_capability
"""

from __future__ import annotations

from alembic import op

revision = "0013_subject_request_restriction"
down_revision = "0012_erasure_guard_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep active obligations independent of an offboarding episode or org FK."""
    op.execute(
        "create table org_deletion_restriction ("
        "id uuid primary key, org_id uuid not null, offboarding_id uuid, "
        "scope text not null, reason text not null, authority_ref text not null, "
        "release_condition text not null, related_request_id uuid, "
        "status text not null default 'active', "
        "created_at timestamptz not null default now(), "
        "released_at timestamptz, released_by uuid references ops_account(id), "
        "release_reason text, release_evidence_reference text, "
        "constraint ck_org_deletion_restriction_status_valid "
        "check (status in ('active','released')), "
        "constraint ck_org_deletion_restriction_resolution "
        "check ((status='active' and released_at is null and release_reason is null "
        "and release_evidence_reference is null) or "
        "(status='released' and released_at is not null and release_reason is not null "
        "and release_evidence_reference is not null)))"
    )
    op.execute(
        "create index idx_org_deletion_restriction_active "
        "on org_deletion_restriction(org_id,id) where status='active'"
    )
    op.execute(
        "create unique index uq_org_deletion_restriction_request "
        "on org_deletion_restriction(org_id,related_request_id) "
        "where related_request_id is not null"
    )


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")

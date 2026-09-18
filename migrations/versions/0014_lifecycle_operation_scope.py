"""Enforce the authoritative lifecycle action identity shape.

Revision ID: 0014_lifecycle_operation_scope
Revises: 0013_subject_request_restriction
"""

from __future__ import annotations

from alembic import op

revision = "0014_lifecycle_operation_scope"
down_revision = "0013_subject_request_restriction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table org_lifecycle_operation add constraint "
        "ck_org_lifecycle_operation_action_scope check ("
        "(action in ('confirm_term','policy_revision') and offboarding_id is null) "
        "or (action in ('create_restriction','release_restriction') "
        "and restriction_id is not null) "
        "or action='renew_term' "
        "or (action='expire_term' and offboarding_id is not null) "
        "or (action not in ('confirm_term','renew_term','expire_term',"
        "'policy_revision','create_restriction','release_restriction') "
        "and offboarding_id is not null))"
    )


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")

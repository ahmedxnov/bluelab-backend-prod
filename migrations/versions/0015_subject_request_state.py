"""Allow independently ordered subject request state decisions.

Revision ID: 0015_subject_request_state
Revises: 0014_lifecycle_operation_scope
"""

from __future__ import annotations

from alembic import op

revision = "0015_subject_request_state"
down_revision = "0014_lifecycle_operation_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table org_lifecycle_operation drop constraint ck_org_lifecycle_operation_action_valid")
    op.execute(
        "alter table org_lifecycle_operation add constraint "
        "ck_org_lifecycle_operation_action_valid check (action in ("
        "'confirm_term','renew_term','expire_term','start','cancel','extend',"
        "'policy_revision','create_restriction','release_restriction',"
        "'set_subject_request_state','claim','authorize_destructive_step',"
        "'complete','record_post_completion_restriction'))"
    )
    op.execute("alter table org_lifecycle_operation drop constraint ck_org_lifecycle_operation_action_scope")
    op.execute(
        "alter table org_lifecycle_operation add constraint "
        "ck_org_lifecycle_operation_action_scope check ("
        "(action in ('confirm_term','policy_revision') and offboarding_id is null) "
        "or (action in ('create_restriction','release_restriction',"
        "'set_subject_request_state') and restriction_id is not null) "
        "or action='renew_term' "
        "or (action='expire_term' and offboarding_id is not null) "
        "or (action not in ('confirm_term','renew_term','expire_term',"
        "'policy_revision','create_restriction','release_restriction',"
        "'set_subject_request_state') and offboarding_id is not null))"
    )


def downgrade() -> None:
    raise NotImplementedError("BlueLab migrations are forward-only")

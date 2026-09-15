"""Phase 3 knowledge provenance and frozen drill generation state.

Revision ID: 0005_phase3_frozen_content
Revises: 0004_phase2_identity_services
Created: 2026-09-13
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_phase3_frozen_content"
down_revision: str | None = "0004_phase2_identity_services"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Expand storage compatibly for Phase 3's T-4/T-5 transactions."""
    op.execute(
        "alter table fact_set add column published_by_account_id uuid "
        "references account(id)"
    )
    op.execute(
        "update fact_set set published_by_account_id = created_by where kind = 'live'"
    )

    op.execute("alter table drill add column draft_grounding jsonb")
    op.execute(
        "alter table drill add column scenario_generation_status text "
        "not null default 'none'"
    )
    op.execute("alter table drill add column scenario_generation_request_id uuid")
    op.execute(
        "alter table drill add column rubric_generation_status text "
        "not null default 'none'"
    )
    op.execute("alter table drill add column rubric_generation_request_id uuid")
    op.execute("alter table drill add column generation_error text")
    op.execute(
        "alter table drill add constraint ck_drill_scenario_generation_status_valid "
        "check (scenario_generation_status in ('none','running','succeeded','failed'))"
    )
    op.execute(
        "alter table drill add constraint ck_drill_rubric_generation_status_valid "
        "check (rubric_generation_status in ('none','running','succeeded','failed'))"
    )
    op.execute(
        "alter table drill add constraint ck_drill_single_generation_lane "
        "check (not (scenario_generation_status = 'running' "
        "and rubric_generation_status = 'running'))"
    )

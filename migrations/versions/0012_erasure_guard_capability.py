"""Reserve freeze-guard authorization for the erasure procedure.

Revision ID: 0012_erasure_guard_capability
Revises: 0011_subject_request_evidence
"""

from __future__ import annotations

from alembic import op

revision = "0012_erasure_guard_capability"
down_revision = "0011_subject_request_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create a private, transaction-bound procedure capability."""
    op.execute("create schema bluelab_internal")
    op.execute("revoke all on schema bluelab_internal from public")
    op.execute("revoke all on schema bluelab_internal from bluelab_app")
    op.execute(
        "create table bluelab_internal.erasure_authorization ("
        "backend_pid integer not null, xact_id bigint not null, request_id uuid not null, "
        "primary key (backend_pid, xact_id))"
    )
    op.execute("revoke all on bluelab_internal.erasure_authorization from public")
    op.execute("revoke all on bluelab_internal.erasure_authorization from bluelab_app")
    # Existing installations already have this SQL module and its old grant.
    # Fresh installations apply the module after migrations; it grants only the
    # erasure role there. Keep both upgrade paths closed to the API role.
    op.execute(
        "do $$ begin "
        "if to_regprocedure('public.app_execute_erasure(uuid)') is not null then "
        "revoke all on function public.app_execute_erasure(uuid) from bluelab_app; "
        "grant execute on function public.app_execute_erasure(uuid) to bluelab_erasure; "
        "end if; end $$"
    )
    for signature in (
        "app_retention_sweep(text,timestamptz)",
        "app_stale_pending_recordings(timestamptz)",
        "app_due_org_service_terms(timestamptz,integer)",
        "app_pending_org_term_operations(integer)",
    ):
        op.execute(
            "do $$ begin "
            f"if to_regprocedure('public.{signature}') is not null then "
            f"revoke all on function public.{signature} from bluelab_app; "
            f"grant execute on function public.{signature} to bluelab_maintenance; "
            "end if; end $$"
        )
    for signature in (
        "app_verify_erasure(uuid)",
        "app_restore_erasure_marker(uuid,uuid,text,uuid,timestamptz,uuid)",
        "app_object_inventory()",
        "app_mark_missing_object(text)",
    ):
        op.execute(
            "do $$ begin "
            f"if to_regprocedure('public.{signature}') is not null then "
            f"revoke all on function public.{signature} from bluelab_app; "
            f"grant execute on function public.{signature} to bluelab_erasure; "
            "end if; end $$"
        )


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")

"""Closed organization-purge inventory and deletion order (data/03 §6)."""

from __future__ import annotations

from importlib import import_module
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import ObjectRef, RestoreObjectStore
from bluelab.platform.db.base import Base

STEP_TABLES: dict[str, tuple[str, ...]] = {
    "capabilities": ("idempotency_record",),
    "delivery_consents": (
        "email_delivery_secret", "email_send", "consent_record", "terms_acceptance",
    ),
    "ops_faults": ("ops_fault",),
    "call_scoring": (
        "moment", "dimension_score", "scorecard", "transcript_entry", "attempt",
    ),
    "training": (
        "coach_feedback_item", "badge_award", "assignment_recipient", "assignment",
    ),
    "hiring": (
        "candidate_token", "shortlist_candidate", "shortlist", "candidate_report",
        "candidate", "assessment_stage", "hr_contact", "position",
    ),
    "knowledge_drills": (
        "product_fact", "fact_set", "document_upload", "product_document",
        "rubric_dimension", "drill_concealed", "drill",
    ),
    "accounts": ("password_reset_token", "account"),
    "derived": (),
    "evidence_minimize": (),
    "org_finalize": ("org_service_term", "org_retention_policy", "org"),
}
STEP_ORDER = (
    "capabilities", "objects", "delivery_consents", "ops_faults", "call_scoring",
    "training", "hiring", "knowledge_drills", "accounts", "derived",
    "evidence_minimize", "org_finalize",
)
RETAINED_TABLES = frozenset({
    "erasure_request", "export_request", "ops_audit", "org_lifecycle_operation",
    "org_deletion_restriction", "org_purge_run", "org_purge_step",
})
SHARED_TABLES = frozenset({
    "legal_document_version", "authoring_option", "badge", "ops_account",
})
_MODEL_MODULES = (
    "identity", "drills", "training", "knowledge", "hiring", "review", "operations",
)


def table_primary_keys(table_name: str) -> tuple[str, ...]:
    """Return the declared primary key only for an explicit purge-matrix table."""
    if table_name not in {table for group in STEP_TABLES.values() for table in group}:
        raise ValueError("table is outside the purge matrix")
    for module in _MODEL_MODULES:
        import_module(f"bluelab.modules.{module}.models")
    import_module("bluelab.notifications.models")
    return tuple(column.name for column in Base.metadata.tables[table_name].primary_key)


def _inventory_sql(table_name: str, keys: tuple[str, ...]) -> str:
    select_keys = ",".join(f"t.{column}" for column in keys)
    order_keys = ",".join(f"t.{column}" for column in keys)
    if table_name == "password_reset_token":
        scope = "exists (select 1 from account a where a.id=t.account_id and a.org_id=:org)"
    elif table_name == "org":
        scope = "t.id=:org"
    else:
        scope = "t.org_id=:org"
    if table_name == "account":
        order_keys = "(t.role='manager')," + order_keys
    return f"select {select_keys} from {table_name} t where {scope} order by {order_keys}"


async def inventory_rows(
    session: AsyncSession, org_id: UUID,
) -> dict[str, list[dict[str, str]]]:
    """Inventory exact row keys before any locating parent is removed."""
    owned = set((await session.execute(text(
        "select c.table_name from information_schema.columns c "
        "join information_schema.tables t on t.table_schema=c.table_schema "
        "and t.table_name=c.table_name "
        "where c.table_schema='public' and c.column_name='org_id' "
        "and t.table_type='BASE TABLE'"
    ))).scalars())
    matrix = {table for group in STEP_TABLES.values() for table in group}
    if owned - RETAINED_TABLES != matrix - {"org", "password_reset_token"}:
        raise ValueError("organization-owned schema differs from purge matrix")
    result: dict[str, list[dict[str, str]]] = {}
    for step in STEP_ORDER:
        for table_name in STEP_TABLES.get(step, ()):
            keys = table_primary_keys(table_name)
            rows = (await session.execute(
                text(_inventory_sql(table_name, keys)), {"org": org_id},
            )).all()
            result[table_name] = [
                {key: str(value) for key, value in zip(keys, row, strict=True)}
                for row in rows
            ]
    return result


async def inventory_objects(
    session: AsyncSession, org_id: UUID, object_store: RestoreObjectStore,
) -> list[str]:
    """Collect exact owned objects, including files with no surviving DB row."""
    keys = {
        ref.key for ref in await object_store.list_prefix(f"orgs/{org_id}/")
    }
    for sql in (
        "select recording_object_key from attempt where org_id=:org",
        "select pdf_object_key from candidate_report where org_id=:org",
        "select object_key from document_upload where org_id=:org",
        "select bundle_object_key from export_request where org_id=:org",
    ):
        values = (await session.execute(text(sql), {"org": org_id})).scalars()
        keys.update(str(value) for value in values if value)
    for key in keys:
        ObjectRef(key)
        if key.startswith("orgs/") and not key.startswith(f"orgs/{org_id}/"):
            raise ValueError("purge inventory crosses organization boundary")
    return sorted(keys)


async def inventory_jobs(session: AsyncSession, org_id: UUID) -> list[int]:
    """Capture runnable ordinary jobs before their resource identifiers disappear."""
    return [int(value) for value in (await session.execute(text(
        "select id from procrastinate_jobs "
        "where args->>'org_id'=:org and task_name <> 'execute_org_purge' "
        "and status in ('todo','doing') order by id"
    ), {"org": str(org_id)})).scalars()]


def batch_keys(inventory: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    """Split each table into stable, bounded batches with explicit category keys."""
    batches: list[dict[str, Any]] = []
    for step in STEP_ORDER:
        for table_name in STEP_TABLES.get(step, ()):
            rows = inventory[table_name]
            for offset in range(0, len(rows), 1000):
                batches.append({
                    "step_key": step, "target_table": table_name,
                    "batch_key": f"{table_name}:{offset // 1000:08d}",
                    "keys": rows[offset:offset + 1000],
                })
    return batches

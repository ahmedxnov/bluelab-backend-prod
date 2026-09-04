"""The drift-check attack battery (data/04 §3, quality/07 §7).

`tools/check_rls_drift.py` is commit-gate row 4's SQL half, and the database-layer
invariants it guards sit in SQL that a Python mutation tool cannot reach
(tests/README, the three bars). So the check itself gets what the freeze guards
get: a battery that **breaks the database on purpose and asserts the check goes
red**. A drift check that has only ever been green has not been shown to be able
to fail, and a gate that cannot fail is indistinguishable from no gate.

Each test follows one shape:

    the breach      commit a specific drift; assert the check reports it
    the recovery    undo it; assert the check is green again

The recovery half is not tidiness. Without it a test could pass on drift left
behind by the test before it, and the battery would be asserting that the database
is broken rather than that the check notices.

## The drift each test injects is a real incident, not a synthetic one

Every case here is something that has a plausible path into a production database:
a hand-added policy from a late-night incident fix, a `create or replace` that
dropped the `security_invoker` reloption, a freeze guard dropped to unblock a data
correction and never restored, a helper function edited in place. The check exists
because none of these leaves a trace in the Alembic lineage.

## Why these tests run as the migration role

They install and drop policies, triggers and views. The application role cannot,
by design (data/04 §1) — and the check under test runs as the migration role for
the same reason.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import psycopg
import pytest

TOOLS = Path(__file__).resolve().parents[3] / "tools"
sys.path.insert(0, str(TOOLS))

drift = importlib.import_module("check_rls_drift")

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
    # Tests the gate script itself, so the mutation run skips it: mutmut executes
    # from a relocated copy under `mutants/` and this module's import of
    # `tools/check_rls_drift.py` cannot survive the move (pyproject [tool.mutmut]).
    pytest.mark.build_gate,
]

Findings = list[drift.Drift]


def kinds(findings: Findings) -> set[str]:
    return {f.kind for f in findings}


def subjects(findings: Findings, kind: str) -> set[str]:
    return {f.subject for f in findings if f.kind == kind}


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def url() -> str:
    """The migration-role URL, resolved exactly as the tool resolves it."""
    try:
        return drift.resolve_url(None)
    except ValueError as exc:  # pragma: no cover - environment, not logic
        pytest.skip(str(exc))


@pytest.fixture(scope="session", autouse=True)
def baseline_is_green(url: str) -> None:
    """Fail the whole battery if the database is already drifted.

    This runs first, and it is the same guard the isolation suite's role check is.
    Every test below asserts that a specific drift turns the check red — and on an
    already-red database every one of those assertions passes without the injected
    drift contributing anything. A green baseline is what makes the rest of this
    file mean what it says.
    """
    findings, _ = drift.run(url, apply=False)
    assert not findings, (
        "the database is already drifted, so no test in this file can prove "
        "anything: an assertion that the check goes red is already satisfied.\n"
        + "\n".join(f.render() for f in findings)
        + "\n\nRun: python tools/check_rls_drift.py --apply"
    )


@pytest.fixture
def sql(url: str) -> Iterator[Callable[[str], None]]:
    """Commit a statement against the live database, outside the check's transaction.

    Committed on purpose. The check opens its own connection and rolls back, so a
    tamper held in an uncommitted transaction would be invisible to it — the test
    would pass its recovery assertion and never have proven the breach.
    """

    def _execute(statement: str) -> None:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(statement)

    yield _execute


@pytest.fixture
def tamper(url: str, sql: Callable[[str], None]) -> Iterator[Callable[..., Findings]]:
    """Break the database, run the check, and guarantee the repair.

    `undo` defaults to re-applying the repo's SQL — which is the release path, and
    the honest way to restore anything the repo defines. Drift the repo *cannot*
    repair has to name its own undo: nothing in `sql/` drops an object it does not
    create, so an extra policy, view, trigger or function stays until something
    removes it explicitly. That asymmetry is the point of the `-extra` findings,
    and the tests for those pass their own `undo`.
    """
    undo: list[str] = []

    def _tamper(*statements: str, undo_with: Sequence[str] = ()) -> Findings:
        undo.extend(undo_with)
        for statement in statements:
            sql(statement)
        findings, _ = drift.run(url, apply=False)
        return findings

    yield _tamper

    for statement in undo:
        sql(statement)
    drift.run(url, apply=True)
    remaining, _ = drift.run(url, apply=False)
    assert not remaining, (
        "the tamper was not fully undone, so the next test would inherit it:\n"
        + "\n".join(f.render() for f in remaining)
    )


@pytest.fixture
def viewdef(url: str) -> Callable[[str], str]:
    def _def(name: str) -> str:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "select pg_catalog.pg_get_viewdef(c.oid, true) from pg_catalog.pg_class c "
                "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relname = %s",
                (name,),
            ).fetchone()
        assert row is not None, f"{name} is not a view in public"
        return str(row[0]).strip().rstrip(";")

    return _def


# ── the headline: security_invoker on the views ───────────────────────────────


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
def test_definer_semantics_on_a_view_is_drift(tamper):
    """The single most dangerous reloption in the schema.

    Without `security_invoker`, a view runs as its owner — the migration role, the
    only role holding BYPASSRLS. `v_team_month` would return every org's rows to
    every caller while every base-table isolation test stayed green, because the
    base tables are still filtered. Nothing else in the suite would notice.
    """
    findings = tamper("alter view v_team_month set (security_invoker = false)")

    assert "security_invoker" in kinds(findings)
    assert "v_team_month (database)" in subjects(findings, "security_invoker")


@pytest.mark.verifies("CMP-004")
def test_a_view_the_repo_never_defined_is_checked_for_security_invoker(tamper):
    """A view from outside `sql/views/` is exactly what a repo-only check misses.

    Reported twice on purpose: `view-extra` because nothing in the repo creates it,
    and `security_invoker` because the rule is absolute rather than a property of
    the eleven files. A check that only validated the reloption on views it
    generated would wave this one straight through.
    """
    findings = tamper(
        "create view v_shadow as select id, org_id from attempt",
        undo_with=["drop view if exists v_shadow"],
    )

    assert "view-extra" in kinds(findings)
    assert "v_shadow" in subjects(findings, "view-extra")
    assert "v_shadow (database)" in subjects(findings, "security_invoker")


@pytest.mark.verifies("CMP-004")
def test_a_changed_view_body_is_drift(tamper, viewdef):
    """Wrapping the body preserves every column name and type, so this is a pure
    body change — the case `pg_get_viewdef` normalization must still catch."""
    original = viewdef("v_team_month")
    findings = tamper(
        f"create or replace view v_team_month with (security_invoker = true) as "
        f"select * from ({original}) tampered"
    )

    assert "view-differs" in kinds(findings)
    assert "v_team_month" in subjects(findings, "view-differs")


# ── policies ──────────────────────────────────────────────────────────────────


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
def test_a_widened_policy_predicate_is_drift(tamper):
    """`using (true)` on a scoped table is the whole tenant boundary gone.

    The generated policy and the tampered one share a name, a table and a command,
    so only the rendered predicate distinguishes them. This is the case that
    justifies running both sides through Postgres's own parser rather than
    comparing SQL text.
    """
    findings = tamper(
        "drop policy attempt_system_select on attempt",
        "create policy attempt_system_select on attempt for select using (true)",
    )

    assert "policy-differs" in kinds(findings)
    assert "attempt.attempt_system_select" in subjects(findings, "policy-differs")


@pytest.mark.verifies("CMP-004")
def test_a_dropped_policy_is_drift(tamper):
    findings = tamper("drop policy attempt_manager_team_read on attempt")

    assert "policy-missing" in kinds(findings)
    assert "attempt.attempt_manager_team_read" in subjects(findings, "policy-missing")


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
def test_an_extra_policy_is_drift_and_survives_the_apply(tamper, url):
    """The direction a before/after diff is blind to, and the one that matters most.

    `create or replace` is a no-op against a database that already matches, so an
    extra policy sits in both snapshots unchanged. It is also the only drift class
    the release cannot repair — the generated file drops only what it creates — so
    this test asserts both halves: the check reports it, and `--apply` does not
    make it go away.
    """
    findings = tamper(
        "create policy attempt_backdoor on attempt for select using (true)",
        undo_with=["drop policy if exists attempt_backdoor on attempt"],
    )

    assert "policy-extra" in kinds(findings)
    assert "attempt.attempt_backdoor" in subjects(findings, "policy-extra")

    after_apply, _ = drift.run(url, apply=True)
    assert "attempt.attempt_backdoor" in subjects(after_apply, "policy-extra"), (
        "the release path silently absorbed an unreviewed grant"
    )


# ── the RLS flags themselves ──────────────────────────────────────────────────


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
def test_force_row_level_security_switched_off_is_drift(tamper):
    """Without FORCE the policies still exist — the owner just stops obeying them.

    The generated file re-issues `force row level security` for every declared
    table, so the apply repairs this and the repo-side snapshot looks perfect. The
    flags are therefore checked on the live side too; a before/after diff alone
    reports nothing here.
    """
    findings = tamper("alter table attempt no force row level security")

    assert "rls" in kinds(findings)
    assert "attempt (database)" in subjects(findings, "rls")


@pytest.mark.verifies("CMP-004")
def test_row_level_security_disabled_is_drift(tamper):
    findings = tamper("alter table attempt disable row level security")

    assert "rls" in kinds(findings)
    assert "attempt (database)" in subjects(findings, "rls")


@pytest.mark.verifies("CMP-004")
def test_a_table_no_declaration_covers_is_drift(tamper):
    """The completeness hole the generator cannot see.

    `check_complete` compares declarations against `Base.metadata`, so a table
    created by hand-written migration DDL — no model, no declaration — is invisible
    to it. This check compares against `pg_class`, which sees the table and finds
    it carrying neither RLS nor a policy: wide open.
    """
    findings = tamper(
        "create table rogue_notes (id uuid primary key, org_id uuid not null, body text)",
        undo_with=["drop table if exists rogue_notes"],
    )

    assert "rls" in kinds(findings)
    assert "rogue_notes (database)" in subjects(findings, "rls")


# ── the SECURITY DEFINER helpers ──────────────────────────────────────────────


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
def test_a_rewritten_definer_helper_is_drift(tamper):
    """These nine functions run with BYPASSRLS. A body edit is a tenant boundary.

    `app_stage_in_position` backs the candidate's stage read and the DDL backstop
    under T-1's stage-order check. Returning `true` unconditionally admits any
    candidate to any position's stage, and no policy text changes at all.
    """
    findings = tamper(
        "create or replace function app_stage_in_position(p_stage_id uuid) "
        "returns boolean language sql stable security definer set search_path = '' "
        "as $$ select true $$"
    )

    assert "function-differs" in kinds(findings)
    assert "app_stage_in_position" in subjects(findings, "function-differs")


@pytest.mark.verifies("CMP-004")
def test_an_overloaded_definer_helper_is_drift(tamper):
    """An overload is a resolution target nobody reviewed (CWE-426).

    The original is untouched, so every definition-level comparison passes. What
    changed is which function a call resolves to when the argument types do not
    match the intended signature exactly.
    """
    findings = tamper(
        "create function app_stage_in_position(p_stage_id text) "
        "returns boolean language sql stable security definer set search_path = '' "
        "as $$ select true $$",
        undo_with=["drop function if exists app_stage_in_position(text)"],
    )

    assert "function-overload" in kinds(findings)
    assert "app_stage_in_position" in subjects(findings, "function-overload")


@pytest.mark.verifies("CMP-004")
def test_a_function_the_repo_never_defined_is_drift(tamper):
    findings = tamper(
        "create function app_shadow_helper() returns boolean language sql "
        "stable security definer as $$ select true $$",
        undo_with=["drop function if exists app_shadow_helper()"],
    )

    assert "function-extra" in kinds(findings)
    assert "app_shadow_helper" in subjects(findings, "function-extra")


# ── the freeze guards ─────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-DRL-015")
def test_a_dropped_freeze_guard_is_drift(tamper):
    """Dropping a trigger to unblock a data correction, and never restoring it.

    The freeze-guard attack battery would catch this — but only on a database it
    runs against. Nothing in the Alembic lineage records the trigger's absence, so
    on a long-lived environment this check is what notices.
    """
    findings = tamper("drop trigger trg_drill_freeze on drill")

    assert "trigger-missing" in kinds(findings)
    assert "drill.trg_drill_freeze" in subjects(findings, "trigger-missing")


@pytest.mark.verifies("FR-DRL-015")
def test_a_trigger_the_repo_never_defined_is_drift(tamper):
    findings = tamper(
        "create trigger trg_shadow before update on drill "
        "for each row execute function fn_drill_freeze()",
        undo_with=["drop trigger if exists trg_shadow on drill"],
    )

    assert "trigger-extra" in kinds(findings)
    assert "drill.trg_shadow" in subjects(findings, "trigger-extra")


# ── the generated file on disk ────────────────────────────────────────────────


@pytest.mark.verifies("CMP-004")
def test_a_hand_edited_generated_file_is_drift(url, tmp_path, monkeypatch):
    """Drift with the paperwork in order.

    If the committed file is edited to match a tampered database, every catalog
    comparison passes: the database matches the repo's *file*. What it no longer
    matches is the scope model the file claims to have come from — and the file is
    what a reviewer reads and a release applies (ADR-0031 decision 2).
    """
    tampered = tmp_path / "policies.sql"
    tampered.write_text(
        drift.gen.render_all().replace(
            "create policy attempt_system_select",
            "create policy attempt_system_select /* hand-edited */",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(drift.gen, "OUTPUT_DIR", tmp_path)

    findings, _ = drift.run(url, apply=False)

    assert "generated-file" in kinds(findings)
    assert kinds(findings) == {"generated-file"}, (
        "only the on-disk file was tampered with, so nothing about the database "
        "should be reported"
    )


@pytest.mark.verifies("CMP-004")
def test_a_missing_generated_file_is_drift(url, tmp_path, monkeypatch):
    monkeypatch.setattr(drift.gen, "OUTPUT_DIR", tmp_path)

    findings, _ = drift.run(url, apply=False)

    assert "generated-file" in kinds(findings)


# ── the check's own guarantees ────────────────────────────────────────────────


@pytest.mark.verifies("CMP-004")
def test_the_check_does_not_mutate_the_database(url):
    """It applies real DDL, so this is worth asserting from outside the tool.

    The tool self-checks after its rollback, but a bug in that self-check would
    hide the very thing it verifies. Snapshot, run, snapshot, compare — with an
    independent connection.
    """
    with psycopg.connect(url) as conn:
        before = drift.snapshot(conn).comparable()

    drift.run(url, apply=False)

    with psycopg.connect(url) as conn:
        after = drift.snapshot(conn).comparable()

    assert before == after


@pytest.mark.verifies("CMP-004")
def test_an_unreadable_module_fails_rather_than_narrowing_silently():
    """The extras check is name-based, so a file this parser cannot read stops
    being audited. That has to be loud: the check would still report "no drift",
    having quietly stopped looking at part of the schema."""
    with pytest.raises(ValueError, match="no create statement recognised"):
        drift.take_inventory([("sql/views/v99_reformatted.sql", "-- nothing here\n")])


@pytest.mark.verifies("CMP-004")
def test_every_committed_module_is_parseable():
    """The other half of the test above: the regexes must actually match the files
    as they are committed today, not merely reject an empty one."""
    inventory = drift.take_inventory(drift.module_files())

    assert len(inventory.views) == 11, "V-1…V-11 (data/02 §3)"
    assert ("drill", "trg_drill_freeze") in inventory.triggers
    assert ("transcript_entry", "trg_transcript_freeze") in inventory.triggers, (
        "four triggers live in trg_scorecard_freeze.sql — the inventory reads "
        "content, not filenames"
    )
    assert "fn_score_band" in inventory.functions


def test_a_missing_url_is_exit_two_not_zero(monkeypatch):
    """A gate that reports success when it never ran is a false green
    (quality/07 §7)."""
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_MIGRATION_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["check_rls_drift.py"])

    assert drift.main() == 2


def test_a_sqlalchemy_url_is_reduced_to_libpq():
    """`MIGRATION_DATABASE_URL` is shared with Alembic and the async test harness,
    so it arrives carrying a driver libpq does not accept."""
    assert (
        drift.libpq_url("postgresql+psycopg://u:p@h:5433/db") == "postgresql://u:p@h:5433/db"
    )
    assert (
        drift.libpq_url("postgresql+asyncpg://u:p@h:5433/db") == "postgresql://u:p@h:5433/db"
    )
    assert drift.libpq_url("postgresql://u:p@h:5433/db") == "postgresql://u:p@h:5433/db"

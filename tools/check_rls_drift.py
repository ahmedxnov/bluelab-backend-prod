#!/usr/bin/env python3
"""Regenerate policies and SQL modules, diff them against a live migrated database,
fail on any drift (data/04 §3).

This is what keeps "RLS and ORM scoping never disagree" true over time, and it is
the SQL-side equivalent of a surviving-mutant check. It also asserts the
`security_invoker = true` reloption on every customer-data view.

    python tools/check_rls_drift.py            # commit gate stage 4 — read-only
    python tools/check_rls_drift.py --apply    # make the database match, then prove it

## Why the diff does not compare SQL text to SQL text

Postgres does not keep the DDL it was given. It keeps a parse tree, and
`pg_get_expr` renders that back with its own casts, its own parentheses and its
own keyword case. A generated `using (… = org_id)` returns as something no
generator would ever emit, so diffing generated text against rendered catalog text
produces a permanent, uninformative red.

So both sides go through the same parser:

1. Snapshot the catalog — policies, functions, views, triggers, RLS flags.
2. Apply the repo's SQL modules and the freshly generated policies.
3. Snapshot again.
4. `ROLLBACK`, always.

Two identical definitions render identically, so a database that matches the repo
yields two identical snapshots and an empty diff. Any difference is real: the
deployed object is not the object the repo describes.

## What before/after cannot see, and the check that covers it

`create or replace` against an object the database already has in the right shape
is a no-op, so a policy the database has that **no repo file creates** sits in both
snapshots unchanged and the diff stays silent. That is the dangerous direction — a
hand-added policy, or one orphaned by a rename, is an unreviewed grant that
survives every release, because the generated file drops only what it also
creates. So the repo's SQL is additionally parsed for the objects it claims, and
anything live beyond that claim is reported as an extra.

Procrastinate's tables, functions and triggers are excluded throughout: the queue
is library-owned and carries no customer scope (data/04 §4).

## Exit codes

* **0** — the database matches the repo.
* **1** — drift. The gate is red (pipeline/02 §2 row 4).
* **2** — the check could not run: no URL, connection refused, an incomplete
  generator. Deliberately not 0. A gate that reports success when it never
  executed is the false green quality/07 §7 warns about, wearing a different
  costume: there, RLS "passes" because the roles enforcing it were never created;
  here, the drift check "passes" because it never reached the database.

Under `--apply` the differences the apply *repaired* are reported and do not
affect the exit code — on a freshly migrated database that set is every object,
which is the point of the mode. A second, independent pass decides: 0 if the apply
converged, 1 if anything survived it (an extra policy, for one, cannot be repaired
by SQL that only ever creates).
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_rls_policies as gen

LIBRARY_PREFIX = "procrastinate_"
"""The queue owns its own schema (data/04 §4, ADR-0023): its tables carry no
customer scope and legitimately have no RLS, and its functions and triggers are
not ours to diff. The generator excludes the same prefix from its completeness
check."""

LINEAGE_TABLE = "alembic_version"
"""Alembic's marker. Not customer data, so no policy and no RLS."""

APPLY_ORDER = ("functions", "views", "triggers")
"""Directory order, and it is a dependency order.

`triggers/` call `app_in_erasure_context` and `app_scope_only_change` from
`functions/`. Within `views/` the numeric filename prefixes *are* the topological
order — `v02` selects from `v01`'s `v_counted_attempt`, `v05` and `v06` from
`v04`'s `v_rep_month_tier` — which is why those files are numbered rather than
named alphabetically.

`roles/` is absent on purpose. Roles are cluster-level, are not captured by
`pg_dump`, and belong to environment provisioning rather than to this drift
checker (data/04 §3). Docker, WSL, and CI execute `sql/roles/bootstrap.sql`
before the lineage. The facts it establishes are asserted by the isolation
suite's first fixture and by the clone test, not here.
"""

# ── what the repo claims ──────────────────────────────────────────────────────
#
# Bounded by `[^;]*?` rather than `.*?`: a statement cannot span a semicolon, so a
# match can never run past the end of the statement it started in.

_CREATE_FUNCTION = re.compile(r"^create\s+(?:or\s+replace\s+)?function\s+(\w+)\s*\(", re.MULTILINE)
_CREATE_VIEW = re.compile(r"^create\s+(?:or\s+replace\s+)?view\s+(\w+)\b", re.MULTILINE)
_CREATE_TRIGGER = re.compile(
    r"^create\s+(?:or\s+replace\s+)?trigger\s+(\w+)\b[^;]*?\bon\s+(\w+)\b", re.MULTILINE | re.DOTALL
)
_CREATE_POLICY = re.compile(r"^create\s+policy\s+(\w+)\s+on\s+(\w+)\b", re.MULTILINE)
_ENABLE_RLS = re.compile(r"^alter\s+table\s+(\w+)\s+enable\s+row\s+level\s+security", re.MULTILINE)
_FORCE_RLS = re.compile(r"^alter\s+table\s+(\w+)\s+force\s+row\s+level\s+security", re.MULTILINE)

POLICY_COMMANDS = {"*": "all", "r": "select", "a": "insert", "w": "update", "d": "delete"}

# ── catalog queries ───────────────────────────────────────────────────────────

_Q_POLICIES = """
select c.relname                                                              as table_name,
       p.polname                                                              as name,
       p.polcmd                                                               as cmd,
       p.polpermissive                                                        as permissive,
       coalesce((select string_agg(r.rolname, ',' order by r.rolname)
                   from pg_catalog.pg_roles r
                  where r.oid = any(p.polroles)), 'public')                   as roles,
       coalesce(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '(none)')      as qual,
       coalesce(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), '(none)') as withcheck
  from pg_catalog.pg_policy p
  join pg_catalog.pg_class c     on c.oid = p.polrelid
  join pg_catalog.pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
"""

_Q_TABLES = """
select c.relname, c.relrowsecurity, c.relforcerowsecurity
  from pg_catalog.pg_class c
  join pg_catalog.pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'r'
"""

_Q_FUNCTIONS = """
select p.proname,
       pg_catalog.pg_get_function_identity_arguments(p.oid) as args,
       pg_catalog.pg_get_functiondef(p.oid)                 as def
  from pg_catalog.pg_proc p
  join pg_catalog.pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.prokind in ('f', 'p')
"""

_Q_VIEWS = """
select c.relname,
       pg_catalog.pg_get_viewdef(c.oid, true) as def,
       coalesce(c.reloptions, '{}'::text[])   as reloptions
  from pg_catalog.pg_class c
  join pg_catalog.pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and c.relkind = 'v'
"""

_Q_TRIGGERS = """
select c.relname                                 as table_name,
       t.tgname                                  as name,
       pg_catalog.pg_get_triggerdef(t.oid, true) as def
  from pg_catalog.pg_trigger t
  join pg_catalog.pg_class c     on c.oid = t.tgrelid
  join pg_catalog.pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public' and not t.tgisinternal
"""


def _ours(name: str) -> bool:
    """Is this object one the repo is responsible for?"""
    return not name.startswith(LIBRARY_PREFIX) and name != LINEAGE_TABLE


def _rel(path: Path) -> str:
    """A repo-relative label, falling back to the absolute path.

    `Path.relative_to` raises for anything outside the repo, and a label is not
    worth an exception: the caller is reporting a finding, and a crash while
    formatting one would surface as "the check could not run" — strictly worse than
    the drift it was about to name.
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ── findings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Drift:
    """One reportable disagreement between the repo and the database."""

    kind: str
    subject: str
    detail: str

    def render(self) -> str:
        lines = [f"  [{self.kind}] {self.subject}"]
        lines.extend(f"      {line}" for line in self.detail.splitlines())
        return "\n".join(lines)


def _diff_text(subject: str, live: str, repo: str) -> str:
    """A unified diff of two rendered definitions.

    The direction is fixed and load-bearing for reading the output: `-` is what is
    deployed right now, `+` is what the repo says should be deployed.
    """
    return "\n".join(
        difflib.unified_diff(
            live.splitlines(),
            repo.splitlines(),
            fromfile=f"{subject} (database)",
            tofile=f"{subject} (repo)",
            lineterm="",
            n=1,
        )
    )


# ── the repo's claim ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Inventory:
    """Every object the repo's SQL creates, by name.

    Parsed from the SQL text, not inferred from filenames, because one file
    routinely creates several objects: `trg_scorecard_freeze.sql` installs four
    triggers — scorecard, dimension_score, moment, transcript_entry — plus the
    function behind them, and the generated policy file creates the nine
    `SECURITY DEFINER` helpers alongside its 218 policies.
    """

    functions: frozenset[str]
    views: frozenset[str]
    triggers: frozenset[tuple[str, str]]
    policies: frozenset[tuple[str, str]]
    rls_enabled: frozenset[str]


def module_files() -> list[tuple[str, str]]:
    """The versioned SQL modules as `(label, text)`, in application order."""
    out: list[tuple[str, str]] = []
    for directory in APPLY_ORDER:
        for path in sorted((REPO_ROOT / "sql" / directory).glob("*.sql")):
            out.append((_rel(path), path.read_text(encoding="utf-8")))
    return out


def take_inventory(sources: list[tuple[str, str]]) -> Inventory:
    functions: set[str] = set()
    views: set[str] = set()
    triggers: set[tuple[str, str]] = set()
    policies: set[tuple[str, str]] = set()
    enabled: set[str] = set()

    for label, text in sources:
        found = 0
        for match in _CREATE_FUNCTION.finditer(text):
            functions.add(match.group(1))
            found += 1
        for match in _CREATE_VIEW.finditer(text):
            views.add(match.group(1))
            found += 1
        for match in _CREATE_TRIGGER.finditer(text):
            triggers.add((match.group(2), match.group(1)))
            found += 1
        for match in _CREATE_POLICY.finditer(text):
            policies.add((match.group(2), match.group(1)))
            found += 1
        enabled.update(m.group(1) for m in _ENABLE_RLS.finditer(text))
        enabled.update(m.group(1) for m in _FORCE_RLS.finditer(text))

        if not found:
            # A file that creates nothing is either dead or — far more likely — a
            # file whose `create` statements this parser stopped recognising after
            # a reformat. Either way the extras check has quietly lost coverage of
            # it, so fail rather than narrow in silence.
            raise ValueError(
                f"{label}: no create statement recognised. The extras check is "
                f"name-based, so a file this parser cannot read silently stops "
                f"being audited."
            )

    return Inventory(
        functions=frozenset(functions),
        views=frozenset(views),
        triggers=frozenset(triggers),
        policies=frozenset(policies),
        rls_enabled=frozenset(enabled),
    )


# ── the database's state ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Catalog:
    """A rendered snapshot of every object class this check owns."""

    policies: dict[tuple[str, str], str]
    tables: dict[str, tuple[bool, bool]]
    functions: dict[str, str]
    views: dict[str, str]
    view_options: dict[str, str]
    triggers: dict[tuple[str, str], str]
    overloads: dict[str, list[str]]

    def comparable(self) -> tuple[Any, ...]:
        return (self.policies, self.tables, self.functions, self.views, self.view_options, self.triggers)


def _canonical(definition: str) -> str:
    """One line-ending convention, so a difference the diff cannot SHOW is not a
    difference this check REPORTS.

    PostgreSQL stores a function body byte for byte, line endings included, and
    `pg_get_functiondef` returns them. So the same helper applied from a CRLF file
    and from an LF one compares unequal while being semantically identical — and
    `_diff_text` splits lines, which normalises away the very bytes that differ,
    printing an empty diff under a `-differs` heading.

    The generator now writes LF (see `generate_rls_policies.main`), which stops new
    databases from entering that state. This handles the ones already in it:
    without it, every helper reports drifted, the battery in
    `tests/l3_integration/migrations/test_rls_drift_check.py` refuses to run
    against a drifted database, and 21 isolation assertions quietly stop
    executing — reported as errors, which read like an environment hiccup.

    Deliberately narrow. Only `\\r\\n` and a lone `\\r` collapse to `\\n`; nothing
    else about the text is touched. Whitespace inside a predicate can change what
    it matches, and a check that normalised its way to agreement would be worse
    than no check.
    """
    return definition.replace("\r\n", "\n").replace("\r", "\n")


def snapshot(conn: psycopg.Connection[Any]) -> Catalog:
    policies: dict[tuple[str, str], str] = {}
    for table, name, cmd, permissive, roles, qual, check in conn.execute(_Q_POLICIES):
        if not _ours(table):
            continue
        policies[(table, name)] = _canonical(
            f"for {POLICY_COMMANDS.get(cmd, cmd)} to {roles} "
            f"{'permissive' if permissive else 'restrictive'}\n"
            f"using      {qual}\n"
            f"with check {check}"
        )

    tables = {
        name: (enabled, forced) for name, enabled, forced in conn.execute(_Q_TABLES) if _ours(name)
    }

    functions: dict[str, str] = {}
    overloads: dict[str, list[str]] = {}
    for name, args, definition in conn.execute(_Q_FUNCTIONS):
        if not _ours(name):
            continue
        if name in functions:
            # Two functions sharing a name in `public` is not something this repo
            # writes, and it is a hazard rather than an untidiness: the nine
            # definer helpers execute with BYPASSRLS, so an added overload is a
            # resolution target for any call whose argument types do not match the
            # intended signature exactly (CWE-426, sql/roles/bootstrap.sql).
            overloads.setdefault(name, []).append(args)
            continue
        functions[name] = _canonical(definition)

    views: dict[str, str] = {}
    view_options: dict[str, str] = {}
    for name, definition, reloptions in conn.execute(_Q_VIEWS):
        if not _ours(name):
            continue
        views[name] = _canonical(definition)
        view_options[name] = ",".join(sorted(reloptions))

    triggers = {
        (table, name): _canonical(definition)
        for table, name, definition in conn.execute(_Q_TRIGGERS)
        if _ours(table)
    }

    return Catalog(
        policies=policies,
        tables=tables,
        functions=functions,
        views=views,
        view_options=view_options,
        triggers=triggers,
        overloads=overloads,
    )


def apply_modules(conn: psycopg.Connection[Any], sources: list[tuple[str, str]]) -> list[Drift]:
    """Apply every source under its own savepoint.

    One savepoint per file, so a single broken module yields one finding and the
    remaining files still run. A first-error-only report turns a three-line fix
    into three round trips through CI.

    A failure here is drift, not a crash: `create or replace view` refuses to
    change an existing column's name or type, so a view whose shape moved fails
    exactly this way — and the release path, which re-applies these same files,
    would fail identically.
    """
    out: list[Drift] = []
    for label, sql in sources:
        try:
            with conn.transaction():
                conn.execute(sql)
        except psycopg.Error as exc:
            out.append(
                Drift(
                    "apply-failed",
                    label,
                    f"{type(exc).__name__}: {str(exc).strip()}\n"
                    f"The release re-applies this same file, so this fails the deploy too.",
                )
            )
    return out


# ── the comparisons ───────────────────────────────────────────────────────────


def compare(live: Catalog, repo: Catalog, inventory: Inventory) -> list[Drift]:
    """Diff the two snapshots, then check the database for what the repo never claimed."""
    out: list[Drift] = []

    out.extend(_compare_map("policy", live.policies, repo.policies))
    out.extend(_compare_map("function", live.functions, repo.functions))
    out.extend(_compare_map("view", live.views, repo.views))
    out.extend(_compare_map("view-reloptions", live.view_options, repo.view_options))
    out.extend(_compare_map("trigger", live.triggers, repo.triggers))

    out.extend(_rls_flags(live, repo, inventory))
    out.extend(_overloads(repo))
    out.extend(_extras(repo, inventory))
    out.extend(_security_invoker(live, repo))
    return out


def _compare_map(kind: str, live: dict[Any, Any], repo: dict[Any, Any]) -> list[Drift]:
    """Diff two rendered maps: missing, changed, and — for completeness — dropped.

    A key in `repo` but not `live` means the repo creates an object the database
    does not have. A key in `live` but not `repo`, after an apply that only ever
    creates, would mean the apply *dropped* something; no module should, so it is
    reported rather than assumed impossible.
    """
    out: list[Drift] = []
    for key in sorted(repo.keys() - live.keys(), key=str):
        out.append(
            Drift(
                f"{kind}-missing",
                _subject(key),
                "the repo creates it; the database does not have it",
            )
        )
    for key in sorted(live.keys() - repo.keys(), key=str):
        out.append(
            Drift(
                f"{kind}-dropped",
                _subject(key),
                "applying the repo's SQL removed it — no module should drop an object",
            )
        )
    for key in sorted(repo.keys() & live.keys(), key=str):
        if live[key] != repo[key]:
            subject = _subject(key)
            out.append(Drift(f"{kind}-differs", subject, _diff_text(subject, live[key], repo[key])))
    return out


def _subject(key: Any) -> str:
    return f"{key[0]}.{key[1]}" if isinstance(key, tuple) else str(key)


def _rls_flags(live: Catalog, repo: Catalog, inventory: Inventory) -> list[Drift]:
    """Every customer table must be both RLS-enabled and RLS-forced, on both sides.

    The two halves fail differently. Without ENABLE there are no policies at all.
    Without FORCE the policies exist but the table owner — the migration role, the
    only role holding BYPASSRLS — walks straight past them (ADR-0031 §3).

    **Both sides, and that is the whole point of checking here rather than in the
    before/after diff.** The generated file re-issues `enable` and `force` for
    every declared table, so a live table whose FORCE had been switched off is
    repaired by the apply and looks identical in the repo snapshot. Diffing alone
    would report nothing about the most consequential single flag in the schema.

    The repo side catches the other case: a table the *database* has that no
    generated block covers. The generator's `check_complete` compares declarations
    against `Base.metadata`, which cannot see a table created by hand-written
    migration DDL; `pg_class` can.
    """
    out: list[Drift] = []
    for side, catalog in (("database", live), ("repo", repo)):
        for table, (enabled, forced) in sorted(catalog.tables.items()):
            if enabled and forced:
                continue
            reason = (
                "The generated file enables and forces it, so this is the deployed "
                "state drifting from the repo."
                if table in inventory.rls_enabled
                else "No generated block covers this table — it has no policy "
                "declaration. Declare it in its module's policies.py."
            )
            out.append(
                Drift(
                    "rls",
                    f"{table} ({side})",
                    f"enable={enabled} force={forced}; both must be true. {reason}",
                )
            )
    return out


def _overloads(repo: Catalog) -> list[Drift]:
    return [
        Drift(
            "function-overload",
            name,
            f"also defined as {name}({'), ('.join(args)}). The definer helpers run "
            f"with BYPASSRLS; an overload is a resolution target nobody reviewed "
            f"(CWE-426).",
        )
        for name, args in sorted(repo.overloads.items())
    ]


def _extras(repo: Catalog, inventory: Inventory) -> list[Drift]:
    """Objects the database has that no repo file creates.

    The direction before/after is blind to, and the one that matters most: an
    extra policy is an unreviewed grant, and nothing in `sql/` drops an object it
    does not name, so it survives every release and every regeneration.
    """
    out: list[Drift] = []

    for table, name in sorted(repo.policies.keys() - inventory.policies):
        out.append(
            Drift(
                "policy-extra",
                f"{table}.{name}",
                "no generator branch emits this policy. Nobody writes a policy by "
                "hand (ADR-0031 decision 2), and the generated file drops only what "
                "it creates — so an unreviewed grant here survives every release.",
            )
        )
    for name in sorted(repo.views.keys() - inventory.views):
        out.append(Drift("view-extra", name, "no file in sql/views/ creates this view"))
    for name in sorted(repo.functions.keys() - inventory.functions):
        out.append(Drift("function-extra", name, "no file in sql/ creates this function"))
    for table, name in sorted(repo.triggers.keys() - inventory.triggers):
        out.append(
            Drift("trigger-extra", f"{table}.{name}", "no file in sql/triggers/ creates this trigger")
        )

    # The inventory is a regex over our own SQL, so every name it claims must exist
    # after the apply. If one does not, the parser has drifted from the files and
    # each extras check above has silently narrowed.
    for missing in sorted(inventory.views - repo.views.keys()):
        out.append(Drift("inventory", missing, "parsed as a created view but absent after the apply — the parser is wrong, not the database"))
    for missing in sorted(inventory.functions - repo.functions.keys()):
        out.append(Drift("inventory", missing, "parsed as a created function but absent after the apply — the parser is wrong, not the database"))
    for table, name in sorted(inventory.triggers - repo.triggers.keys()):
        out.append(Drift("inventory", f"{table}.{name}", "parsed as a created trigger but absent after the apply — the parser is wrong, not the database"))

    return out


def _security_invoker(live: Catalog, repo: Catalog) -> list[Drift]:
    """Assert `security_invoker = true` on every customer-data view (data/04 §3).

    Checked on **both** snapshots, not just the repo's. A view is only invoker-safe
    if the deployed one is, and a view the repo does not define — an extra, or one
    a hand-written migration created — is exactly the case a repo-only check waves
    through.

    Without the reloption a view runs as its owner, the migration role, the only
    role holding BYPASSRLS. Every view would return every org's rows while the
    base-table isolation tests stayed green. The PG15+ floor in
    `scripts/dev-postgres.sh` exists for this and nothing else.
    """
    out: list[Drift] = []
    for name in sorted(set(live.view_options) | set(repo.view_options)):
        for side, options in (("database", live.view_options), ("repo", repo.view_options)):
            if name in options and "security_invoker=true" not in options[name]:
                out.append(
                    Drift(
                        "security_invoker",
                        f"{name} ({side})",
                        f"reloptions = [{options[name]}]. A definer-semantics view is "
                        f"owned by the migration role, the only role holding "
                        f"BYPASSRLS: it returns every org's rows while the "
                        f"base-table isolation tests stay green.",
                    )
                )
    return out


def check_generated_file(regenerated: str) -> list[Drift]:
    """Assert the on-disk generated file is what the generator emits *now*.

    `sql/policies/generated/policies.sql` is **not** in version control — `.gitignore`
    excludes everything under that directory but the `.gitkeep`, because nobody
    writes a policy by hand (ADR-0031 decision 2). So this is not a
    committed-artifact check; it is a staleness-and-tampering check on the file the
    release is about to apply.

    Two ways it earns its place. A file left over from an older declaration set is
    applied verbatim by `--apply` and by the release path, which would install
    policies the scope model no longer describes. And a file edited by hand lets the
    database match the *file* while disagreeing with the model that file claims to
    come from — drift with the paperwork in order.

    Compared through `read_text`, whose universal-newline translation makes this
    line-ending agnostic: the repo carries no `.gitattributes`, so whether the file
    holds CRLF or LF depends on which machine last ran the generator, and that is
    not a fact worth failing a build over.
    """
    target = gen.OUTPUT_DIR / "policies.sql"
    label = _rel(target)
    if not target.exists():
        return [Drift("generated-file", label, "missing — run tools/generate_rls_policies.py")]
    on_disk = target.read_text(encoding="utf-8")
    if on_disk == regenerated:
        return []
    return [
        Drift(
            "generated-file",
            label,
            _diff_text("policies.sql", on_disk, regenerated)
            + "\n\nGENERATED FILE — DO NOT EDIT (ADR-0031 decision 2). Re-run "
            "tools/generate_rls_policies.py; the file is gitignored, so it is "
            "regenerated rather than committed.",
        )
    ]


# ── driver ────────────────────────────────────────────────────────────────────


def regenerate() -> str:
    """The policy set, straight from the scope model.

    Imported rather than shelled out to. `--stdout` adds a trailing newline of its
    own, and a text comparison that has to guess how many newlines to strip is a
    comparison that will one day be wrong in the passing direction.

    The generator's own completeness checks run first. A phantom or undeclared
    table is a harder failure than drift — and diffing a knowingly incomplete
    policy set against a database reports a confusing cascade instead of the one
    line that matters.
    """
    gen.load_declarations()
    undeclared, phantom = gen.check_complete()
    if phantom or undeclared:
        raise ValueError(
            "the policy generator is not complete, so there is nothing sound to diff:\n"
            + "".join(f"  phantom declaration: {t}\n" for t in phantom)
            + "".join(f"  undeclared table:    {t}\n" for t in undeclared)
        )
    return gen.render_all()


def libpq_url(url: str) -> str:
    """Strip the SQLAlchemy driver suffix.

    `MIGRATION_DATABASE_URL` is shared with Alembic and the test harness, so it
    arrives as `postgresql+psycopg://…` or `postgresql+asyncpg://…`. libpq accepts
    neither; `migrations/env.py` solves the same problem the same way.
    """
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def resolve_url(explicit: str | None) -> str:
    url = (
        explicit
        or os.environ.get("MIGRATION_DATABASE_URL")
        or os.environ.get("TEST_MIGRATION_URL")
    )
    if not url:
        raise ValueError(
            "no database URL. Set MIGRATION_DATABASE_URL or pass --database-url.\n"
            "This check runs as the MIGRATION role: it reads pg_policy and applies "
            "DDL, and the application role is non-superuser by design (data/04 §1)."
        )
    return libpq_url(url)


def run(url: str, *, apply: bool) -> tuple[list[Drift], str]:
    """Snapshot, apply, snapshot, compare. Returns `(findings, summary)`.

    Nothing commits unless `apply` is set. psycopg opens the transaction on the
    first statement and this function calls `commit()` nowhere else, so rollback is
    the default rather than a cleanup step an early return could skip.

    `lock_timeout` is bounded for the reason `migrations/env.py` bounds it:
    installing a policy or a trigger takes ACCESS EXCLUSIVE on the table, and a
    check that blocks behind a long-running query is a CI job that has stopped
    without saying so.
    """
    regenerated = regenerate()
    sources = module_files() + [("sql/policies/generated/policies.sql", regenerated)]
    inventory = take_inventory(sources)

    lock_timeout = os.environ.get("MIGRATION_LOCK_TIMEOUT_MS", "5000")
    connect_timeout = os.environ.get("MIGRATION_CONNECT_TIMEOUT_S", "10")
    findings: list[Drift] = check_generated_file(regenerated)

    # `connect_timeout` is not optional, and a bounded `lock_timeout` does not cover
    # it: that one bounds waiting for a lock once connected. A database that is
    # unreachable-but-not-refusing — a suspended VM, a dropped route, a firewalled
    # port — leaves the connect itself waiting on the OS default, and the gate hangs
    # rather than failing. A CI job that never finishes reports nothing at all.
    with psycopg.connect(
        url,
        autocommit=False,
        connect_timeout=int(connect_timeout),
        options=f"-c lock_timeout={lock_timeout}",
    ) as conn:
        live = snapshot(conn)
        findings.extend(apply_modules(conn, sources))
        repo = snapshot(conn)
        findings.extend(compare(live, repo, inventory))

        if apply:
            conn.commit()
        else:
            conn.rollback()
            # A drift check that mutated the database it audits would be a very
            # quiet bug: every later run would come back clean regardless of what
            # the first one found. Cheap to prove it did not.
            if snapshot(conn).comparable() != live.comparable():
                findings.append(
                    Drift(
                        "internal",
                        "rollback",
                        "the database did not return to its pre-check state. This "
                        "check must never mutate what it audits.",
                    )
                )
            # The verification snapshot opened a transaction of its own, and
            # psycopg's connection context manager commits on a clean exit. Leave
            # it nothing to commit.
            conn.rollback()

    summary = (
        f"{len(repo.policies)} policies over {len(repo.tables)} tables, "
        f"{len(repo.functions)} functions, {len(repo.views)} views, "
        f"{len(repo.triggers)} triggers"
    )
    return findings, summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff the regenerated policies and SQL modules against a live database."
    )
    parser.add_argument(
        "--database-url", help="defaults to $MIGRATION_DATABASE_URL, then $TEST_MIGRATION_URL"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "commit the applied modules instead of rolling back, then assert the "
            "result is clean — the bootstrap and release path, after "
            "`alembic upgrade head` (data/04 §3)"
        ),
    )
    args = parser.parse_args()

    try:
        url = resolve_url(args.database_url)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        findings, summary = run(url, apply=args.apply)
        if args.apply:
            # What the apply just changed is not a failure — on a freshly migrated
            # database it is *every* object, which is the whole point of the mode.
            # Report it, then let a second, independent pass decide the exit code:
            # the apply either converged or it did not, and only the state after it
            # commits can say which.
            _report(findings, f"repaired against {summary}", stream=sys.stdout)
            findings, summary = run(url, apply=False)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(
            f"ERROR: could not reach the database — {type(exc).__name__}: "
            f"{str(exc).strip()}\nExiting 2, not 0: a gate that reports success "
            f"when it never executed is a false green (quality/07 §7).",
            file=sys.stderr,
        )
        return 2

    if not findings:
        print(f"no drift — {summary}")
        return 0

    _report(findings, f"DRIFT — {len(findings)} finding(s) against {summary}", stream=sys.stderr)
    return 1


def _report(findings: list[Drift], headline: str, *, stream: Any) -> None:
    if not findings:
        return
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    print(f"{headline}\n", file=stream)
    for finding in findings:
        print(finding.render(), end="\n\n", file=stream)
    print("  ".join(f"{kind}={count}" for kind, count in sorted(counts.items())), file=stream)
    # `--apply` writes the repaired set to stdout and the surviving set to stderr.
    # Unflushed, the two arrive in the wrong order and the log reads as though the
    # apply happened after the verification.
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())

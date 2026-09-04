#!/usr/bin/env python3
"""Emit the `CREATE POLICY` set from the per-module policy-class declarations.

Five principal kinds across roughly 35 tables is ~170 policy decisions.
Hand-written they drift, and **drift here is a breach** (ADR-0031 context).
Generated from one declaration per table, the whole set is regenerable and
diffable, and the isolation suite attacks one mechanism instead of thirty-five.

    python tools/generate_rls_policies.py            # write sql/policies/generated/
    python tools/generate_rls_policies.py --stdout   # print, for the drift check

## The predicate vocabulary

Every policy reads the transaction-local GUCs that
`platform.db.scope.apply_scope` writes:

    app.principal_kind   account | candidate | ops | system
    app.org_id  app.team_id  app.account_id  app.candidate_id
    app.position_id      (route-back — see platform/db/scope.py)
    app.role

Read through `nullif(current_setting(k, true), '')` so **unset and empty both
yield NULL**, and a NULL predicate matches nothing: deny by default (ADR-0031
decision 1). A request that somehow reaches the database without a scope context
reads zero rows rather than all of them.

## What this generator will not do

It emits no policy for a table it has no declaration for, and then **fails** —
see `check_complete`. An undeclared table under `FORCE ROW LEVEL SECURITY` is
invisible; an undeclared table *without* it is wide open. Neither may happen
quietly.

Column-level concealment is deliberately out of scope: RLS filters rows, the
Review module's projection filters columns (rubric weights to non-authors, the
candidate's own attempt projection, the internal note). That split is recorded in
ADR-0031 §4 and is not re-litigated here.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from bluelab.platform.db.base import Base
from bluelab.platform.db.policy_class import (
    IDENTIFIER,
    REGISTRY,
    PolicyClass,
    TablePolicy,
)

MODULES = (
    "identity",
    "knowledge",
    "drills",
    "review",
    "training",
    "hiring",
    "assessment",
    "operations",
)
"""The eight modules of ADR-0002. Imported for their side effect: each
`policies.py` registers its tables at import time. Omitting one here would
silently drop its tables from the generated set — which `check_complete` catches.
"""

OUTPUT_DIR = REPO_ROOT / "sql" / "policies" / "generated"

# ── predicate fragments ───────────────────────────────────────────────────────

KIND = "nullif(pg_catalog.current_setting('app.principal_kind', true), '')"
ORG = "nullif(pg_catalog.current_setting('app.org_id', true), '')::uuid"
TEAM = "nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid"
ACCOUNT = "nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid"
CANDIDATE = "nullif(pg_catalog.current_setting('app.candidate_id', true), '')::uuid"
POSITION = "nullif(pg_catalog.current_setting('app.position_id', true), '')::uuid"
ROLE = "nullif(pg_catalog.current_setting('app.role', true), '')"

IS_ACCOUNT = f"{KIND} = 'account'"
IS_CANDIDATE = f"{KIND} = 'candidate'"
IS_OPS = f"{KIND} = 'ops'"
IS_SYSTEM = f"{KIND} = 'system'"
IS_MANAGER = f"{IS_ACCOUNT} and {ROLE} = 'manager'"
IS_REP = f"{IS_ACCOUNT} and {ROLE} = 'rep'"

HELPERS = """\
-- ── Enumerated SECURITY DEFINER helpers ──────────────────────────────────────
--
-- ADR-0031 sanctions SECURITY DEFINER escape hatches provided each is enumerated
-- and audited. These eleven are that list. Nine exist for one reason: a policy
-- predicate that reads an RLS-protected table triggers THAT table's policies —
-- at best a hidden cost on every row, at worst infinite recursion. A definer
-- function reads the one column the rule needs and stops.
--
-- Those nine are read-only, STABLE, take no user input beyond an id, return a
-- boolean, and are reachable only from inside a policy.
--
-- The remaining five are exceptions on every one of those counts and are called
-- directly from a request path, so each is held to its own limits instead.
--
-- READS:
--   app_account_for_sign_in   one table, one WHERE on a unique column, a fixed
--                             seven-column projection a caller cannot widen
--   app_account_has_accepted  returns a bare boolean; the account id comes from
--                             the caller's own session record, never a request
--
-- WRITES — the three below, and they are a genuinely new category here:
--   app_record_consent            insert one consent_record row
--   app_record_terms_acceptance   insert one terms_acceptance row
--   app_set_initial_credential    flip one account from 'initial' to 'set'
--
-- They exist because the first-sign-in and acceptances gates (FR-IDA-004,
-- CMP-002, CMP-005) have to WRITE three things the product surface cannot reach:
-- `consent_record` and `terms_acceptance` are P9_OPS/system_write_only, and
-- P2_ACCOUNT grants an account `self_read` but no self-UPDATE, so an account
-- cannot even set its own password. Without these the gates have no exit and
-- every user is stuck behind them permanently.
--
-- **Every one takes its subject from the transaction GUCs, never from an
-- argument.** There is no `p_account_id` parameter on any of them, so a caller
-- has no way to name a person other than itself — the same property the scope
-- model gives ordinary queries, kept intact across the definer boundary. That is
-- the single most important line in this block: a write helper that accepted an
-- account id would let any authenticated user record consent, or set a password,
-- for anyone in the database.
--
-- **The org is read off the subject, never off `app.org_id`.** The two inserts
-- take `org_id` from the account row they are writing about. Nothing in the
-- schema ties `consent_record.org_id` to `consent_record.account_id` — the FKs
-- are independent — so a scope tuple naming another org would file a compliance
-- record under the wrong customer with nothing to catch it.
--
-- Each is also **idempotent**: the two inserts are `on conflict do nothing`
-- against the existing unique constraints, and the update is guarded on
-- `credential_state = 'initial'`. Replay is a no-op, which is what api/00 §6
-- requires of both operations.
--
-- **A scope naming no account RAISES; it does not return a value.** The two
-- recorders are `plpgsql` for exactly that — the only place in this file that is
-- not `language sql`, and the reason is that a compliance write which silently
-- did not happen is worse than an error. An intermediate draft returned a boolean
-- there and a caller that ignored it would have lost the consent record without a
-- trace.
--
-- `app_set_initial_credential` still returns boolean, because "the credential is
-- already set" is a normal business outcome the caller renders as
-- `409 first-sign-in-not-pending` — not a fault. It raises on the no-account case
-- like the other two, so false has exactly one meaning.
--
-- `p_id` is passed in rather than defaulted, because ids are UUIDv7 minted
-- application-side (ADR-0030). A `gen_random_uuid()` here would quietly seed v4
-- rows into a v7 table.
--
-- All five are listed here rather than hidden elsewhere precisely because
-- "enumerated and audited" is the condition ADR-0031 attaches to definer
-- functions — an escape hatch kept somewhere else is one nobody re-reads.
--
-- EXECUTE is revoked from PUBLIC on all fourteen and granted to the application
-- role only.
--
-- SEARCH_PATH IS EMPTY AND EVERY NAME IS QUALIFIED. These run as the migration
-- role, which is the only role holding BYPASSRLS (ADR-0031 §3) — so if the app
-- role could CREATE in a searched schema it would shadow a name here and inherit
-- BYPASSRLS. sql/roles/bootstrap.sql revokes that CREATE; this is the second
-- belt (CWE-426).

create or replace function app_attempt_readable_by_account(p_attempt_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- The rep's own attempt, or a team attempt that is NOT self-authored.
    -- AC-TRP-004: a rep's private practice is invisible to their manager.
    select exists (
        select 1 from public.attempt a
        where a.id = p_attempt_id
          and a.org_id = nullif(pg_catalog.current_setting('app.org_id', true), '')::uuid
          and (
                a.rep_account_id = nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid
             or (nullif(pg_catalog.current_setting('app.role', true), '') = 'manager'
                 and a.team_id = nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid
                 and not a.self_authored)
          )
    )
$$;

create or replace function app_scorecard_readable_by_account(p_scorecard_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    select exists (
        select 1 from public.scorecard s
        where s.id = p_scorecard_id
          and public.app_attempt_readable_by_account(s.attempt_id)
    )
$$;

create or replace function app_assignment_granted_to_account(p_assignment_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- Does the caller hold an allowance on this assignment? The rep's half of the
    -- library card is split across two tables: `attempts_used` on
    -- assignment_recipient, `due_date` and `attempts_allowed` here. The recipient
    -- row carries no drill_id, so without this the rep cannot even resolve which
    -- drill they were assigned (FR-TRP-006/013, AC-TRP-005).
    --
    -- Membership, NOT team: `team_id = app.team_id` ALONE would hand every rep on
    -- the team every assignment made to any of their peers, including due dates
    -- and allowances they are not party to. Who else received the same assignment
    -- is peer practice data and P-1 forbids surfacing it. So the account predicate
    -- is what grants the read, and it is not negotiable.
    --
    -- The team predicate is an AND on top of it, and it is about TRANSFERS. An
    -- `assignment_recipient` row carries the ASSIGNMENT's team (it rides the
    -- composite FK), while `app.team_id` is the caller's team right now. Those
    -- agree until the rep moves teams — `account.team_id` changes and the old
    -- recipient row does not, because nothing clears it. Without this line the
    -- rep keeps reading their former team's assignment indefinitely: a team
    -- surface outliving membership of the team, which FR-IDA-009 does not permit.
    --
    -- Strictly narrowing. In the ordinary case the two are equal and this changes
    -- nothing; it only ever removes rows.
    select exists (
        select 1 from public.assignment_recipient ar
        where ar.assignment_id = p_assignment_id
          and ar.org_id = nullif(pg_catalog.current_setting('app.org_id', true), '')::uuid
          and ar.team_id = nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid
          and ar.rep_account_id = nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid
    )
$$;

create or replace function app_drill_in_team_positions(p_drill_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- After a position transfer an assessment legitimately spans teams
    -- (FR-HIR-018), and its reviews must still render with weights (FR-HIR-011).
    -- So the owning manager reads drills their OWN positions' stages reference,
    -- even when another team authored them.
    select exists (
        select 1
        from public.assessment_stage st
        join public.position p on p.id = st.position_id
        where st.drill_id = p_drill_id
          and p.team_id = nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid
          and p.org_id  = nullif(pg_catalog.current_setting('app.org_id',  true), '')::uuid
    )
$$;

create or replace function app_drill_manageable_by_team(p_drill_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- The owning team's manager, on a drill that is NOT a rep's private one.
    select exists (
        select 1 from public.drill d
        where d.id = p_drill_id
          and d.org_id  = nullif(pg_catalog.current_setting('app.org_id',  true), '')::uuid
          and d.team_id = nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid
          and not d.self_authored
    )
$$;

create or replace function app_drill_readable_published(p_drill_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- A rep reads their team's PUBLISHED drills. Drafts are not shareable.
    select exists (
        select 1 from public.drill d
        where d.id = p_drill_id
          and d.org_id  = nullif(pg_catalog.current_setting('app.org_id',  true), '')::uuid
          and d.team_id = nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid
          and d.status = 'published'
          and not d.self_authored
    )
$$;

create or replace function app_drill_authored_by_account(p_drill_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- The self-authoring rep. Authorship lifts concealment; publication does not
    -- (FR-SCR-017) — which is why drill_concealed uses this and has no team read.
    select exists (
        select 1 from public.drill d
        where d.id = p_drill_id
          and d.org_id = nullif(pg_catalog.current_setting('app.org_id', true), '')::uuid
          and d.self_authored
          and d.author_account_id = nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid
    )
$$;

create or replace function app_stage_in_position(p_stage_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- Does this stage belong to the caller's own position? Backs two things:
    -- the candidate's read of the drills their assessment references, and the
    -- DDL backstop under T-1's stage-order check — without it, the admission
    -- INSERT verifies only that the candidate is themselves.
    select exists (
        select 1 from public.assessment_stage st
        where st.id = p_stage_id
          and st.position_id = nullif(pg_catalog.current_setting('app.position_id', true), '')::uuid
    )
$$;

create or replace function app_drill_in_position(p_drill_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- drill ROWS only — never rubric_dimension (FR-SCR-018, AC-CND-003).
    select exists (
        select 1 from public.assessment_stage st
        where st.drill_id = p_drill_id
          and st.position_id = nullif(pg_catalog.current_setting('app.position_id', true), '')::uuid
    )
$$;

create or replace function app_candidate_in_team(p_candidate_id uuid)
returns boolean language sql stable security definer set search_path = '' as $$
    -- email_send carries org_id but no team_id (data/01 §8), so the owning
    -- manager's read of E-2 delivery state (FR-HIR-010) has to hop through the
    -- candidate. A denormalised team_id would be cheaper — see open-items.
    select exists (
        select 1 from public.candidate c
        where c.id = p_candidate_id
          and c.team_id = nullif(pg_catalog.current_setting('app.team_id', true), '')::uuid
          and c.org_id  = nullif(pg_catalog.current_setting('app.org_id',  true), '')::uuid
    )
$$;

create or replace function app_account_for_sign_in(p_email text)
returns table (
    id uuid, org_id uuid, team_id uuid, role text,
    password_hash text, status text, credential_state text
) language sql stable security definer set search_path = '' as $$
    -- THE SIGN-IN CHICKEN-AND-EGG, and the only place it is solved.
    --
    -- Authentication has to find an account before a principal exists, so no
    -- scope tuple can authorise the read: app.account_id is what we are trying to
    -- discover, and app.org_id is not known either. Every other route would be
    -- worse — an unscoped session (which session.py refuses to expose), the
    -- migration role (BYPASSRLS on everything), or a policy on `account` keyed on
    -- an anonymous principal (RLS filters rows, not columns, so it would hand a
    -- whole row to an unauthenticated caller).
    --
    -- So: one function, one question, one row. It takes an email and returns ONLY
    -- the seven columns sign-in needs. `display_name` is absent on purpose —
    -- rendering it happens after the credential is verified, through the caller's
    -- own scope, where account_self_read applies.
    --
    -- It discloses nothing a caller did not already supply: an attacker who calls
    -- it with an email learns whether a row came back, which is exactly what
    -- verify_password + dummy_verify then equalise away in timing and response.
    -- The enumeration defence lives in identity.service, not here.
    select a.id, a.org_id, a.team_id, a.role,
           a.password_hash, a.status, a.credential_state
    from public.account a
    where a.email = lower(p_email)
$$;

create or replace function app_account_has_accepted(
    p_account_id uuid, p_kind text, p_version text
) returns boolean language sql stable security definer set search_path = '' as $$
    -- THE CONSENT GATE'S READ, and the second thing a request path calls directly.
    --
    -- consent_record and terms_acceptance are P9_OPS with system_write_only: the
    -- evidence trail is ops-readable and system-written, so an ACCOUNT cannot read
    -- its own acceptance rows. That is deliberate for the trail, and it left the
    -- consent/terms gates (CMP-002 / CMP-005) unevaluable — a count of zero means
    -- "no consent" and "cannot see consent" alike, and guessing either way is
    -- wrong: one gates every user forever, the other reports a compliance control
    -- as satisfied without checking it.
    --
    -- Widening the policies instead would have made the whole evidence trail
    -- account-readable to answer a yes/no question. This returns the boolean and
    -- nothing else: no timestamps, no row, no other account. `p_account_id` is
    -- supplied by the caller from its own session record, never from a request.
    select case p_kind
        when 'privacy_notice' then exists (
            select 1 from public.consent_record c
            where c.account_id = p_account_id and c.notice_version = p_version
        )
        when 'terms_of_use' then exists (
            select 1 from public.terms_acceptance t
            where t.account_id = p_account_id and t.terms_version = p_version
        )
        -- An unknown kind is FALSE, which reads as "not accepted" and gates the
        -- user. Fail closed: a typo in a kind must not open a compliance gate.
        else false
    end
$$;

create or replace function app_record_consent(p_id uuid, p_notice_version text)
returns void language plpgsql volatile security definer set search_path = '' as $$
    -- CMP-002's evidence row, written by the only path an account has to it.
    --
    -- The subject comes from the transaction GUC — there is no account parameter,
    -- so this cannot record consent for anyone but the caller.
    --
    -- **`org_id` is READ OFF THE ACCOUNT, not off `app.org_id`.** Nothing in the
    -- schema ties the two: `fk_consent_record_org_id` and
    -- `fk_consent_record_account_id` are independent, so a scope tuple naming a
    -- different org would file this row under that org — a compliance record
    -- attributed to the wrong customer, and no constraint would catch it. Taking
    -- the org from the row being written about makes the mismatch unrepresentable
    -- rather than merely unlikely. Verified: with a deliberately mismatched tuple
    -- the old form wrote a misattributed row.
    --
    -- RAISES when the scope names no account, rather than returning a value the
    -- caller might not read. An earlier draft returned false here; that turns
    -- "consent was never recorded" into a quiet boolean, which is precisely the
    -- outcome this is supposed to make impossible. A compliance write that
    -- silently did not happen is worse than an error, so it is an error.
    declare
        v_org uuid;
        v_account uuid := nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid;
    begin
        select a.org_id into v_org from public.account a where a.id = v_account;
        if not found then
            raise exception 'app_record_consent: no account in scope (app.account_id=%)', v_account
                using errcode = 'raise_exception';
        end if;

        -- The version must be one the platform actually published. Without this a
        -- caller could file consent against a version string that never existed,
        -- and the evidence trail — the artefact CMP-002 exists to produce — would
        -- contain acceptances of documents nobody wrote.
        --
        -- This is integrity, NOT a gate control: `pending_gates` reads the current
        -- version from this same table and asks about *that*, so a bogus version
        -- never opened a gate. Verified — consent recorded against a made-up
        -- version left the real gate closed. The check exists so the trail cannot
        -- hold junk, not because the junk was dangerous.
        if not exists (
            select 1 from public.legal_document_version v
            where v.kind = 'privacy_notice' and v.version = p_notice_version
        ) then
            raise exception 'app_record_consent: no published privacy_notice version %',
                p_notice_version using errcode = 'raise_exception';
        end if;

        insert into public.consent_record (id, org_id, account_id, notice_version)
        values (p_id, v_org, v_account, p_notice_version)
        -- Replay is a no-op (api/00 §6). The unique constraint already models
        -- "once per person per version"; this makes a second call succeed quietly
        -- instead of 500-ing a user who double-submitted a form.
        on conflict (account_id, notice_version) do nothing;
    end;
$$;

create or replace function app_record_terms_acceptance(
    p_id uuid, p_terms_version text, p_privacy_version text
) returns void language plpgsql volatile security definer set search_path = '' as $$
    -- CMP-005's evidence row. A SEPARATE instrument from consent, in a separate
    -- table, written by a separate function — the same separation the schema
    -- makes structural, kept structural here rather than collapsed into one
    -- "record everything" helper that a caller could half-invoke.
    --
    -- `org_id` off the account, for the reason given on `app_record_consent`.
    --
    -- BOTH versions are parameters, and neither is derived here even though the
    -- current pair is a cheap lookup away. That is deliberate: an acceptance is
    -- of the exact documents the user was SHOWN. Deriving "whatever is current"
    -- would record acceptance of a version published between render and submit —
    -- a document the person never read, entered into the evidence trail as though
    -- they had. The caller passes what it displayed.
    --
    -- Note the asymmetry this leaves: the unique key spans the pair, but
    -- `app_account_has_accepted('terms_of_use', v)` matches on `terms_version`
    -- alone. A row with the right terms and a stale privacy version therefore
    -- satisfies the terms gate. Correct as far as the gate goes — the terms were
    -- accepted — and the privacy notice has its own gate through
    -- `privacy_notice`, so nothing is unguarded.
    -- Raises on a scope naming no account, as `app_record_consent` does and for
    -- the same reason.
    declare
        v_org uuid;
        v_account uuid := nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid;
    begin
        select a.org_id into v_org from public.account a where a.id = v_account;
        if not found then
            raise exception 'app_record_terms_acceptance: no account in scope (app.account_id=%)',
                v_account using errcode = 'raise_exception';
        end if;

        -- BOTH versions must be published, and they are checked against different
        -- kinds: this row asserts the person accepted a specific Terms of Use AND
        -- a specific Privacy Notice. Checking only `terms_version` would let the
        -- privacy half be anything at all, which is the half the acceptance
        -- shares with `consent_record` and the half a reader is most likely to
        -- trust. Integrity, not a gate control — see `app_record_consent`.
        if not exists (
            select 1 from public.legal_document_version v
            where v.kind = 'terms_of_use' and v.version = p_terms_version
        ) then
            raise exception 'app_record_terms_acceptance: no published terms_of_use version %',
                p_terms_version using errcode = 'raise_exception';
        end if;
        if not exists (
            select 1 from public.legal_document_version v
            where v.kind = 'privacy_notice' and v.version = p_privacy_version
        ) then
            raise exception 'app_record_terms_acceptance: no published privacy_notice version %',
                p_privacy_version using errcode = 'raise_exception';
        end if;

        insert into public.terms_acceptance (
            id, org_id, account_id, terms_version, privacy_version
        )
        values (p_id, v_org, v_account, p_terms_version, p_privacy_version)
        on conflict (account_id, terms_version, privacy_version) do nothing;
    end;
$$;

create or replace function app_set_initial_credential(p_password_hash text)
returns boolean language plpgsql volatile security definer set search_path = '' as $$
    -- The credential half of the first-sign-in gate, and the ONLY write an
    -- account may make to its own row. P2_ACCOUNT grants `self_read` and no
    -- self-UPDATE, deliberately: an account that could update itself could change
    -- its own `role`, its `team_id`, or its `status`, and each of those is a
    -- privilege decision that belongs to ops (ADR-0010). So the escape hatch is
    -- this — one column pair, one direction.
    --
    -- Guarded on `credential_state = 'initial'`, which makes the guard the gate:
    -- an account whose credential is already set updates zero rows and gets
    -- `false` back, and the caller answers `409 first-sign-in-not-pending`. That
    -- also makes the function safe to replay — the second call simply reports
    -- false rather than overwriting a password the user has since chosen.
    --
    -- **`status = 'active'` is load-bearing, not decoration.** Without it a
    -- DEACTIVATED account holding a live session sets its own password and flips
    -- its credential to 'set' — verified, it did. Deactivation revokes sessions
    -- (FR-IDA-010), so the window is small, but "small window" is not a control
    -- and this function is the boundary. The endpoint may also check; if both
    -- assume the other does, neither does.
    --
    -- The `org_id` predicate is redundant against a primary key and is kept
    -- anyway: it means a mismatched scope tuple updates nothing instead of
    -- reaching across an org. Unlike the two inserts above it FILTERS on the org
    -- rather than writing it, so reading it from the GUC is the right direction
    -- here — a wrong tuple must match nothing, not silently correct itself.
    --
    -- **The no-account case raises rather than returning false**, so `false` has
    -- exactly one meaning: the account exists, is active, and its credential is
    -- already set. Folding "no scope" into the same false would have had the
    -- caller answer `409 first-sign-in-not-pending` to what is really a missing
    -- session — pointing whoever reads that response at entirely the wrong bug.
    declare
        v_account uuid := nullif(pg_catalog.current_setting('app.account_id', true), '')::uuid;
        v_updated int;
    begin
        if not exists (select 1 from public.account a where a.id = v_account) then
            raise exception 'app_set_initial_credential: no account in scope (app.account_id=%)',
                v_account using errcode = 'raise_exception';
        end if;

        update public.account
        set password_hash = p_password_hash,
            credential_state = 'set'
        where id = v_account
          and org_id = nullif(pg_catalog.current_setting('app.org_id', true), '')::uuid
          and credential_state = 'initial'
          and status = 'active';

        get diagnostics v_updated = row_count;
        return v_updated > 0;
    end;
$$;

revoke execute on function
    app_account_for_sign_in(text),
    app_account_has_accepted(uuid, text, text),
    app_record_consent(uuid, text),
    app_record_terms_acceptance(uuid, text, text),
    app_set_initial_credential(text),
    app_attempt_readable_by_account(uuid),
    app_scorecard_readable_by_account(uuid),
    app_assignment_granted_to_account(uuid),
    app_drill_in_team_positions(uuid),
    app_drill_manageable_by_team(uuid),
    app_drill_readable_published(uuid),
    app_drill_authored_by_account(uuid),
    app_stage_in_position(uuid),
    app_drill_in_position(uuid),
    app_candidate_in_team(uuid)
from public;
"""


def _ident(value: str, *, what: str) -> str:
    """Assert a declaration string is a plain SQL identifier before it is
    interpolated into generated DDL.

    `TablePolicy.__post_init__` now checks every identifier field at construction,
    so by the time anything reaches this generator it has already passed. This
    stays as the belt to that braces: it is the guard at the point of
    interpolation, and it does not depend on the caller having come through a
    declaration at all.

    The regex is imported rather than re-declared. Two copies of "what a valid
    identifier is" drift, and the copy that matters is whichever one the DDL path
    happens to consult.
    """
    if not IDENTIFIER.match(value):
        raise ValueError(f"{what}: {value!r} is not a plain identifier")
    return value


def _and(*parts: str) -> str:
    return " and ".join(f"({part})" for part in parts if part)


def _org_match(policy: TablePolicy) -> str:
    """`org` binds on its own `id`; every other table on `org_id`."""
    return f"{_ident(policy.scope_self_column, what=policy.table)} = {ORG}"


class Policy:
    """One `CREATE POLICY` statement."""

    def __init__(
        self,
        *,
        table: str,
        name: str,
        command: str,
        using: str | None = None,
        check: str | None = None,
        comment: str = "",
    ) -> None:
        self.table = table
        self.name = f"{table}_{name}"
        self.command = command
        self.using = using
        self.check = check
        self.comment = comment

    def render(self) -> str:
        """One drop-then-create pair.

        **The drop is not optional.** data/04 §3 requires these to be re-applied
        idempotently at every release, and `create policy` has no `IF NOT EXISTS`
        — so without the drop, the second deploy fails at migration time with
        `policy already exists`. Drop-then-create inside the migration's
        transaction means the window where a policy is absent is never visible to
        another session.
        """
        lines: list[str] = []
        if self.comment:
            lines.append(f"-- {self.comment}")
        lines.append(f"drop policy if exists {self.name} on {self.table};")
        lines.append(f"create policy {self.name} on {self.table}")
        lines.append(f"    for {self.command}")
        if self.using:
            lines.append(f"    using ({self.using})")
        if self.check:
            lines.append(f"    with check ({self.check})")
        return "\n".join(lines) + ";"


def _grants(
    policy: TablePolicy,
    *,
    table: str,
    name: str,
    predicate: str,
    comment: str,
    read_only: bool = False,
) -> list[Policy]:
    """Expand one logical grant into per-command policies.

    Postgres policies are per-command, and `for all` includes DELETE. That is the
    over-grant this function exists to prevent: **erasure is the only remover**
    (data/00 §2, ADR-0033), so DELETE is emitted only where a table's declaration
    explicitly asks for it. Without this, a rep could delete an attempt carrying a
    bad score and an author could delete a published drill.

    `insert` takes only `with check`; `select` and `delete` take only `using`;
    `update` takes both.
    """
    commands = ("select",) if read_only else policy.principal_commands
    out: list[Policy] = []
    for command in commands:
        out.append(
            Policy(
                table=table,
                name=f"{name}_{command}",
                command=command,
                using=None if command == "insert" else predicate,
                check=predicate if command in ("insert", "update") else None,
                comment=comment if command == commands[0] else "",
            )
        )
    return out


def _candidate_read(policy: TablePolicy) -> Policy:
    """The P7 candidate-journey read: own row, matched on the binding.

    Plain column comparisons because the scope tuple carries `position_id` —
    see the route-back in `platform/db/scope.py`. Without it each of these would
    need a subquery back to `candidate`, i.e. an RLS-recursion hazard per table.
    """
    column = policy.candidate_link
    target = CANDIDATE if policy.candidate_link_target == "candidate" else POSITION
    return Policy(
        table=policy.table,
        name="candidate_own_read",
        command="select",
        using=_and(IS_CANDIDATE, _org_match(policy), f"{column} = {target}"),
        comment="the candidate's own journey — rows here, column projection at the API",
    )


def _p4(policy: TablePolicy) -> list[Policy]:
    """`drill` and `rubric_dimension` — the row-conditional class.

    Three predicates over one table, because data/01 §4 tags it
    `P4 (published) / P3 (drafts) / P5 (self-authored)` and all three are true
    across different rows. Plus two cross-team reads that exist only because a
    position can be transferred.

    Both tables go through the definer helpers rather than inline predicates.
    `rubric_dimension` genuinely has to — it carries no `status`,
    `self_authored`, or `author_account_id` of its own — and a correlated
    subquery on `drill` would trigger `drill`'s policies, which contain a further
    subquery on `assessment_stage`. That is the RLS-recursion hazard the helpers
    exist for; using them on the parent too keeps one rule in one place.
    """
    table = policy.table
    child = policy.parent_table is not None
    ref = f"{table}.drill_id" if child else f"{table}.id"

    out: list[Policy] = []

    out.extend(
        _grants(
            policy,
            table=table,
            name="manager_team",
            predicate=_and(IS_MANAGER, _org_match(policy), f"app_drill_manageable_by_team({ref})"),
            comment="P3 branch: the owning manager, drafts included — never a rep's private drill",
        )
    )

    out.append(
        Policy(
            table=table,
            name="team_published_read",
            command="select",
            using=_and(IS_REP, _org_match(policy), f"app_drill_readable_published({ref})"),
            comment="P4 branch: a rep reads their team's PUBLISHED drills only",
        )
    )

    out.extend(
        _grants(
            policy,
            table=table,
            name="author_self",
            predicate=_and(
                IS_ACCOUNT, _org_match(policy), f"app_drill_authored_by_account({ref})"
            ),
            comment="P5 branch: the self-authoring rep — invisible even to the manager (AC-TRP-004)",
        )
    )

    if not child:
        # Cross-team reads exist on `drill` only. rubric_dimension deliberately
        # gets neither: no candidate surface renders rubric content
        # (FR-SCR-018), and the transferred-position manager reads weights
        # through the Review projection, not through this table.
        out.append(
            Policy(
                table=table,
                name="manager_via_own_positions_read",
                command="select",
                using=_and(IS_MANAGER, _org_match(policy), "app_drill_in_team_positions(id)"),
                comment="after a transfer the assessment spans teams (FR-HIR-018)",
            )
        )
        out.append(
            Policy(
                table=table,
                name="candidate_via_own_stages_read",
                command="select",
                using=_and(IS_CANDIDATE, _org_match(policy), f"app_drill_in_position({table}.id)"),
                comment="drill ROWS only — never rubric_dimension (FR-SCR-018, AC-CND-003)",
            )
        )

    return out


def _p6(policy: TablePolicy) -> list[Policy]:
    """The participant record. Access is the attempt's, wherever the row hangs."""
    table = policy.table
    out: list[Policy] = []

    if policy.parent_table is None:
        # `attempt` itself carries the participant columns.
        rep = _and(IS_ACCOUNT, _org_match(policy), f"{policy.owner_column} = {ACCOUNT}")
        # SELECT + INSERT only, overriding the declaration. A rep needs to start
        # a call (T-1) and read the result; T-2 and T-6 run from the internal
        # seam in SYSTEM context. UPDATE would let a rep rewrite status,
        # duration, or recording_status on their own graded record — and NO
        # freeze trigger covers `attempt` (trg_scorecard_freeze guards the
        # scorecard, transcript, dimension scores and moments, not this row).
        for command in ("select", "insert"):
            out.append(
                Policy(
                    table=table,
                    name=f"rep_own_{command}",
                    command=command,
                    using=None if command == "insert" else rep,
                    check=rep if command == "insert" else None,
                    comment=(
                        "the rep's own attempts — read, and the T-1 admission insert. "
                        "No update, no delete: the attempt IS the record"
                        if command == "select"
                        else ""
                    ),
                )
            )
        out.append(
            Policy(
                table=table,
                name="manager_team_read",
                command="select",
                using=_and(
                    IS_MANAGER, _org_match(policy), f"team_id = {TEAM}", "not self_authored"
                ),
                comment="team attempts, excluding a rep's private practice (AC-TRP-004)",
            )
        )
    else:
        # Children delegate one hop: they carry neither the participant identity
        # nor `self_authored`, and a definer helper avoids RLS recursion.
        helper = (
            "app_attempt_readable_by_account(attempt_id)"
            if policy.parent_table == "attempt"
            else "app_scorecard_readable_by_account(scorecard_id)"
        )
        out.append(
            Policy(
                table=table,
                name="account_read",
                command="select",
                using=_and(IS_ACCOUNT, _org_match(policy), helper),
                comment=f"visibility follows the {policy.parent_table} (definer helper — no RLS recursion)",
            )
        )

    if policy.candidate_read:
        candidate = _and(IS_CANDIDATE, _org_match(policy), f"candidate_id = {CANDIDATE}")
        out.append(
            Policy(
                table=table,
                name="candidate_own_read",
                command="select",
                using=candidate,
                comment="the candidate's own attempt rows",
            )
        )
        if policy.candidate_insert:
            out.append(
                Policy(
                    table=table,
                    name="candidate_admission_insert",
                    command="insert",
                    # The stage must belong to THEIR position. Without this the
                    # check verifies only that the candidate is themselves, and
                    # T-1 is the sole gate on assessment integrity — every other
                    # invariant here carries a DDL backstop.
                    check=_and(candidate, "app_stage_in_position(assessment_stage_id)"),
                    comment="T-1 creates the attempt; admission is the candidate's own act",
                )
            )
    # No else-branch, and that is the point: transcript_entry, scorecard,
    # dimension_score and moment get NO candidate policy at all. Candidates see
    # no evaluation, ever — an absence, not a projection filter (FR-SCR-018,
    # AC-CND-003).

    return out


def build(policy: TablePolicy) -> list[Policy]:
    """Emit the policy set for one table, from its class."""
    table = policy.table
    out: list[Policy] = []

    # The system context reaches everything inside its org. Workers resolve scope
    # from the job row, so the work plane has no unscoped path (ADR-0005).
    system_predicate = _and(IS_SYSTEM, _org_match(policy)) if policy.org_scoped else IS_SYSTEM
    for command in ("select", "insert", "update"):
        out.append(
            Policy(
                table=table,
                name=f"system_{command}",
                command=command,
                using=None if command == "insert" else system_predicate,
                check=system_predicate if command in ("insert", "update") else None,
                # NO DELETE, for the same reason no principal has it: erasure is
                # the only remover (data/00 §2, ADR-0033), and the retention
                # sweeps are SECURITY DEFINER procedures rather than policy-bound
                # queries — ADR-0031 enumerates both as escape hatches. A `for
                # all` here would have been the one place the rule was skipped.
                comment=(
                    "work plane and internal procedures, org-bounded"
                    if command == "select"
                    else ""
                ),
            )
        )

    if policy.system_only:
        # No principal policy at all. Not even ops — see the declaration.
        return out

    cls = policy.policy_class

    if cls is PolicyClass.P0_REFERENCE:
        out.append(
            Policy(
                table=table,
                name="read_all",
                command="select",
                using="true",
                comment="platform reference: every principal reads, migrations write",
            )
        )

    elif cls is PolicyClass.P1_ORG:
        out.append(
            Policy(
                table=table,
                name="member_read",
                command="select",
                using=_and(IS_ACCOUNT, _org_match(policy)),
                comment="own org, read only",
            )
        )
        out.append(
            Policy(
                table=table,
                name="ops_verbs",
                command="all",
                using=IS_OPS,
                check=IS_OPS,
                comment="provisioning verbs (ADR-0010)",
            )
        )

    elif cls is PolicyClass.P2_ACCOUNT:
        out.append(
            Policy(
                table=table,
                name="self_read",
                command="select",
                using=_and(IS_ACCOUNT, _org_match(policy), f"id = {ACCOUNT}"),
                comment="a rep sees itself",
            )
        )
        out.append(
            Policy(
                table=table,
                name="manager_team_read",
                command="select",
                using=_and(IS_MANAGER, _org_match(policy), f"team_id = {TEAM}"),
                comment="a manager sees its own team",
            )
        )
        out.append(
            Policy(
                table=table,
                name="ops_verbs",
                command="all",
                using=IS_OPS,
                check=IS_OPS,
                comment="provision, deactivate, change team mapping",
            )
        )

    elif cls is PolicyClass.P3_TEAM_MANAGER:
        if policy.authorship_gated:
            # A team predicate alone is WRONG here, and the isolation suite caught
            # it: a rep's self-authored drill carries the manager's team_id, so
            # `team_id = app.team_id` handed the manager its concealed set —
            # exactly what AC-TRP-004 forbids. The helper adds `not
            # self_authored`, so the manager reaches the team's own drills only.
            manager_predicate = _and(
                IS_MANAGER, _org_match(policy), f"app_drill_manageable_by_team({table}.drill_id)"
            )
        else:
            manager_predicate = _and(IS_MANAGER, _org_match(policy), f"team_id = {TEAM}")

        out.extend(
            _grants(
                policy,
                table=table,
                name="manager",
                predicate=manager_predicate,
                comment=(
                    "manager-only team data — NO rep policy exists; "
                    "the concealed set lives in this class (FR-SCR-017)"
                ),
            )
        )
        if policy.authorship_gated:
            # drill_concealed: the rep who authored it, and NO team-read branch
            # even once published. Authorship lifts concealment; publication
            # does not (FR-SCR-017).
            out.extend(
                _grants(
                    policy,
                    table=table,
                    name="author",
                    predicate=_and(
                        IS_ACCOUNT,
                        _org_match(policy),
                        f"app_drill_authored_by_account({table}.drill_id)",
                    ),
                    comment=(
                        "the self-authoring rep — authorship lifts concealment, "
                        "publication does not (FR-SCR-017)"
                    ),
                )
            )
        if policy.rep_own_read:
            # Team-scoped when the table carries a team, for the reason
            # `app_assignment_granted_to_account` is: the row records the team the
            # allowance was granted IN, and nothing clears it when the rep moves.
            # Without this the counter outlives the membership — readable forever,
            # attached to an assignment the same transfer already hid.
            own_row = _and(
                IS_ACCOUNT, _org_match(policy), f"{policy.owner_column} = {ACCOUNT}"
            )
            out.append(
                Policy(
                    table=table,
                    name="rep_own_read",
                    command="select",
                    using=_and(own_row, f"team_id = {TEAM}") if policy.team_scoped else own_row,
                    comment="the rep reads their own allowance, never a teammate's (AC-TRP-005, P-1)",
                )
            )
        if policy.rep_recipient_read:
            out.append(
                Policy(
                    table=table,
                    name="rep_recipient_read",
                    command="select",
                    using=_and(
                        IS_ACCOUNT,
                        _org_match(policy),
                        f"app_assignment_granted_to_account({table}.id)",
                    ),
                    comment=(
                        "the rep reads the assignment they hold an allowance on — "
                        "due date and allowed count (FR-TRP-006/013, AC-TRP-005)"
                    ),
                )
            )

    elif cls is PolicyClass.P5_OWNER_PRIVATE:
        out.extend(
            _grants(
                policy,
                table=table,
                name="owner",
                predicate=_and(
                    IS_ACCOUNT, _org_match(policy), f"{policy.owner_column} = {ACCOUNT}"
                ),
                comment="owner only — invisible even to the manager (AC-TRP-004)",
            )
        )

    elif cls is PolicyClass.P4_TEAM_PUBLISHED:
        out.extend(_p4(policy))

    elif cls is PolicyClass.P6_PARTICIPANT_RECORD:
        out.extend(_p6(policy))

    elif cls is PolicyClass.P8_HIRING_MANAGER:
        if policy.team_scoped:
            predicate = _and(IS_MANAGER, _org_match(policy), f"team_id = {TEAM}")
        else:
            # email_send has org_id only, so ownership hops through the candidate.
            predicate = _and(
                IS_MANAGER, _org_match(policy), "app_candidate_in_team(candidate_id)"
            )
        out.extend(
            _grants(
                policy,
                table=table,
                name="manager",
                predicate=predicate,
                comment="the owning manager's hiring data",
            )
        )
        if policy.candidate_read:
            out.append(_candidate_read(policy))

    elif cls is PolicyClass.P9_OPS:
        if policy.system_write_only:
            out.append(
                Policy(
                    table=table,
                    name="ops_read",
                    command="select",
                    using=IS_OPS,
                    comment="ops reads the evidence trail; only the system writes it",
                )
            )
        else:
            out.append(
                Policy(
                    table=table,
                    name="ops_all",
                    command="all",
                    using=IS_OPS,
                    check=IS_OPS,
                    comment="the operations plane's own tables",
                )
            )

    else:
        raise NotImplementedError(f"{table}: class {cls.value} has no generator branch")

    return out


def render_header() -> str:
    return (
        "-- GENERATED FILE — DO NOT EDIT.\n"
        "-- Emitted by tools/generate_rls_policies.py from each module's policies.py.\n"
        "-- Nobody writes a policy by hand (ADR-0031 decision 2); tools/check_rls_drift.py\n"
        "-- regenerates this and diffs it against the live database on every commit."
    )


def render_table(policy: TablePolicy) -> str:
    """One table's block: enable RLS, force it, then its policies."""
    parts = [
        f"-- {policy.table} — class {policy.policy_class.value}",
        f"alter table {policy.table} enable row level security;",
    ]
    if policy.force_rls:
        # FORCE applies policies to the table owner too. Without it a
        # migration-role query bypasses everything silently (ADR-0031 §3).
        parts.append(f"alter table {policy.table} force row level security;")
    parts.extend(p.render() for p in build(policy))
    return "\n".join(parts)


def load_declarations() -> None:
    """Import every module's `models.py` **and** `policies.py`.

    Both, and in that order. `policies.py` registers declarations; `models.py`
    registers tables on `Base.metadata`. Loading only the former leaves the
    metadata empty, and `check_complete` then reports every declared table as a
    phantom — which is what the first run of this generator actually did.
    """
    for module in MODULES:
        importlib.import_module(f"bluelab.modules.{module}.models")
        importlib.import_module(f"bluelab.modules.{module}.policies")
    importlib.import_module("bluelab.notifications.models")
    importlib.import_module("bluelab.notifications.policies")


def check_complete() -> tuple[list[str], list[str]]:
    """Compare declarations against the mapped tables, **both directions**.

    Returns `(undeclared, phantom)`.

    * **undeclared** — a real table nobody declared. It gets no policies: under
      `FORCE ROW LEVEL SECURITY` it is invisible, and without it, wide open.
    * **phantom** — a declaration naming a table that does not exist, i.e. a typo
      in a `table=` string. The same failure wearing the other face: the generator
      emits policies for nothing, the real table silently gets none, and a
      one-directional check reports success.

    Procrastinate's tables are excluded — the queue is library-owned and carries
    no customer scope (data/04 §4).
    """
    mapped = frozenset(
        name for name in Base.metadata.tables if not name.startswith("procrastinate_")
    )
    declared = frozenset(p.table for p in REGISTRY.all())
    return sorted(mapped - declared), sorted(declared - mapped)


def render_all() -> str:
    """The whole emitted file, as text.

    Factored out so `tools/check_rls_drift.py` regenerates through exactly this
    path rather than reassembling the header, the helpers and the table blocks in
    its own order. Two assemblers would agree on the day they were written and
    diverge on the day one of them gained a section — and the drift check's whole
    job is to be the thing that notices divergence.
    """
    blocks = [render_header(), HELPERS]
    blocks.extend(render_table(policy) for policy in REGISTRY.all())
    return "\n\n".join(blocks) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate RLS policies from the scope model.")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="emit despite undeclared tables — build-order only, never in CI",
    )
    args = parser.parse_args()

    load_declarations()

    undeclared, phantom = check_complete()

    # A phantom declaration is ALWAYS fatal, even under --allow-incomplete: it is
    # a typo, never a work-in-progress, and it makes the undeclared list lie.
    if phantom:
        print(
            "ERROR: {} declaration(s) name a table that does not exist:\n  {}\n\n"
            "A mistyped table= string emits policies for nothing while the real "
            "table silently gets none.".format(len(phantom), "\n  ".join(phantom)),
            file=sys.stderr,
        )
        return 1

    if undeclared:
        message = (
            f"{len(undeclared)} table(s) have no policy declaration:\n  "
            + "\n  ".join(undeclared)
            + "\n\nAn undeclared table has no policies — invisible under FORCE RLS, "
            "wide open without it. Declare it in its module's policies.py."
        )
        if not args.allow_incomplete:
            print(f"ERROR: {message}", file=sys.stderr)
            return 1
        print(f"WARNING: {message}\n", file=sys.stderr)

    output = render_all()

    if args.stdout:
        print(output)
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        target = OUTPUT_DIR / "policies.sql"
        # `newline=""` writes LF verbatim instead of translating to the platform's
        # ending. Not cosmetic: a function body applied to PostgreSQL is stored
        # BYTE FOR BYTE, line endings included, and `pg_get_functiondef` hands them
        # back. So a file written with CRLF and applied by any route other than
        # `check_rls_drift.py --apply` — `psql -f`, a release script, a hand-run —
        # stores CRLF bodies, while the drift check's own apply stores LF from this
        # same string in memory. Every helper then reports as drifted.
        #
        # That failure is unusually nasty for a security tool: it fires on all of
        # them at once, its diff renders EMPTY (the renderer splits lines, which
        # normalises exactly the difference being reported), and the remedy it
        # prints is `--apply` — which overwrites the database. A genuine drift
        # sitting in that noise would be destroyed unread.
        #
        # Writing LF makes the artifact byte-identical whoever generates it, so
        # every apply route agrees. `check_rls_drift` normalises on top of this for
        # databases already carrying CRLF bodies from before this line existed.
        target.write_text(output, encoding="utf-8", newline="")
        print(
            f"wrote {target.relative_to(REPO_ROOT)} — "
            f"{len(REGISTRY.all())} tables declared, {len(undeclared)} undeclared"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

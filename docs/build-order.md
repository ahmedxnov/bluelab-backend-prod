# Build order

Some things must exist *before* the first feature merge, not after. Each of the
entries below is an explicit gate condition from an upstream phase, and each exists
because the alternative — retrofitting the check under deadline — is the check that
never lands.

## Entry conditions (before the first feature merge)

| # | What | Why it gates entry | Owner |
|---|---|---|---|
| 1 | **The RLS policy generator + the drift check**, with the isolation suite passing through the `security_invoker` views | No feature migration merges until the generator emits the full set and this suite passes against *generated* policies | data FS-1, ADR-0031 |
| 2 | **The conformance diff**, build-failing on any drift between `api/openapi.yaml`, the generated client, and what the server serves | Interface drift caught at merge, not at integration | api C-2, ADR-0035 |
| 3 | **The role bootstrap**: app role non-superuser without `BYPASSRLS`, migration role the only holder, `FORCE ROW LEVEL SECURITY` on every customer-data table | Otherwise RLS "passes" only because the roles that enforce it were never created — a false green | SEC-041, data/04 §3 |
| 4 | **a11y CI + RTL snapshots + the direction lint** — in the SPA repository, before the first chart- or Arabic-bearing UI merges | Promised accessibility rots under deadline | ux C-1 |

`ADR-0031` states the obligation plainly: the generator is *"load-bearing tooling
that Phase 13 must build early and Phase 9 must test first"* — failing isolation
tests before feature code.

## The order that follows from that

1. **Platform floor** — config, ids, clock, the scoped-session factory, the scope
   GUCs, problem details and denial semantics, cookies and sessions, the HMAC
   primitive, telemetry.
2. **Schema + generator + isolation suite** — the lineage, `sql/`, the policy
   generator, the drift check, and the isolation suite that attacks them. Red
   first.
3. **The nine invariant transactions** — T-1…T-9 with their races and replays, and
   the freeze guards with their attack battery. These are invariant-path code:
   branch coverage plus mutation testing plus an explicit adversarial case.
4. **The contract surface** — routers per module, the conformance diff green.
5. **The call seam** — admission, the bundle builder, completion, interruption,
   the sweep and the webhook. The bundle builder is invariant-path code.
6. **The work plane** — the six lanes, each idempotent by its stated identity.
7. **Product surfaces** — module by module, each landing with its tests,
   instrumentation, and docs *in the same diff*.

## Instrumentation is not a later phase

`C-4` requires per-turn latency and per-attempt cost instrumentation live **before
the first call**, and the Definition of Done requires the detector to ship with the
code it watches, from the first tier (quality/08 §1 item 6). Observability wave 1 —
turn-handoff latency, per-attempt cost with its modeled-vs-billed divergence guard,
the vendor spend 80% warning, the backup-failure alarm, host-up plus a cheap HTTP
journey check, and the security signal counters — is *"the price of processing real
data at all"* (LADDER.md Pre-T1).

## What "Done" means for one change

The nine items of quality/08 §1, every one of them either CI-enforced or a named
review sign-off. In short: traced to a requirement; tested at the right level to
its band's bar; PR gate green; no interface drift; **no tier-conditional
application code**; instrumentation landed with it; docs landed with it; passed
*independent* code review **and** *independent* security review; and
definition-safe — no production data in any test, no secret in code/image/log, the
cookie audit passing.

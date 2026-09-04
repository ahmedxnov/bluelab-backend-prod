# Tests

The levels, the bars, and where each blocks are quality/01. This repository owns
the levels that do not need a browser fleet or real vendor minutes:

| Directory          | Level | Exercises                                                        | Blocks |
|--------------------|-------|------------------------------------------------------------------|--------|
| `l1_unit/`         | L1    | Pure logic: V-1…V-11 arithmetic, banding, allowance and restart accounting, the unweighted candidate mean, the sum-to-100 gate, snapshot readers | every PR |
| `l2_contract/`     | L2    | Every operation against the Prism mock, positive and contract-negative; the conformance diff | every PR |
| `l3_integration/`  | L3    | Real `postgres:16`: T-1…T-9 races and replays, the freeze guards, the `Idempotency-Key` store, RLS **through the views** | every PR touching a data path |
| `l6_load/`         | L6    | k6 scripts for NFR-001/005/006, admission throughput, the capacity refusal path | tier promotion |
| `l7_security/`     | L7    | Isolation by category x role, projection absence, denial byte/timing equality, the concealment red-team battery, injection and bidi neutralisation, the freeze-guard attack battery, the erasure drill | PR (deterministic subset) + tier gates |
| `l8_resilience/`   | L8    | The degradation ladder, each dependency killed in turn; call-plane instance loss; the lost-completion reconciliation path; the restore drill | tier gates |
| `l9_grading/`      | L9    | Fixture smoke on PR; the re-grade variance study and the bias validation are experiment-design streams | PR (smoke) + evaluator selection |

**L4 journey and L5 frontend live with the SPA**, not here — they are Playwright
and Vitest suites against a running stack (quality/01 §L4-L5).

## Rules that are not negotiable

* **Every test carries a traceability tag** naming the `FR-*` / `NFR-*` / `CMP-*` /
  `SEC-*` it verifies. `tools/check_requirement_coverage.py` fails the build on any
  requirement with no tagged, non-skipped test — **a skipped or `xfail` placeholder
  counts as uncovered**, because a test that cannot fail proves nothing
  (quality/08 §5, ADR-0062).
* **TDD is the default loop**, especially on invariant paths and formulas: a test
  that has never been red has not been shown to be able to fail (quality/08 §2).
* **No production data in any fixture**, ever — and the check is automated
  (quality/07 §2, pipeline/02 §2 row 9).
* **Order-independence is mandatory.** Each test seeds and tears down its own data.
* **Flake policy:** quarantine and root-cause within one working day, never re-run
  until green. A quarantined *invariant-path* test blocks release — it guards a
  `CMP-*`/`SEC-*`, and a muted guard is an open door.

## The three bars (quality/01 §4) — there is no global coverage number

* **Invariant paths** — isolation and scope filtering, concealment projection, the
  consent backstop, the nine transactions and the freeze guards, admission, grading
  arithmetic, erasure: branch coverage **plus mutation testing** on the
  application-layer code, plus an explicit adversarial suite. The database-layer
  invariants sit in SQL a mutation tool cannot reach, and are guarded instead by the
  L3 negative/attack suite plus the generate-and-diff drift check.
* **Product logic** — line + branch >= 85%, every FR reachable and asserted, every
  AC realised.
* **Peripheral** — smoke and view-state only. Over-testing here is waste.

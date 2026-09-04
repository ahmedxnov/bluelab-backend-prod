# Build-gate tooling

Each script here is a stage of the commit gate (pipeline/02 §2). They exist
because the alternative — a review checklist — rots.

| Script | Gate stage | Red condition |
|---|---|---|
| `generate_rls_policies.py` | (build input) | — emits the whole `CREATE POLICY` set from the per-module policy-class declarations (ADR-0031) |
| `check_mutation_score.py` | 2 | Any **invariant-path mutant surviving** beyond the recorded ceiling (quality/01 §4, ADR-0059) — reads `mutmut export-cicd-stats`, does not run mutmut itself |
| `check_rls_drift.py` | 4 | Any drift between regenerated policies + SQL modules and the migrated database; also asserts `security_invoker` on every customer-data view |
| `check_conformance_diff.py` | 3 | **Any** drift between `api/openapi.yaml`, the generated client, and what the server serves (api C-2, ADR-0035) |
| `check_requirement_coverage.py` | 10 | Any `FR-*`/`NFR-*`/`CMP-*`/`SEC-*` with no tagged, non-skipped test (ADR-0062) |
| `scan_tier_branches.py` | 9 | Any application branch keyed on the rollout tier (SEC-026, infra C-6) |
| `audit_cookie_attributes.py` | 9 | Any cookie missing `__Host-` / `HttpOnly` / `Secure` / `SameSite=Strict` / `Path=/`, or carrying `Domain` (SEC-002) |
| `check_job_payload_window.py` | (migration diffs) | A job payload change that breaks N/N+1 coexistence in either direction (pipeline/02 §3, gate F-2) |
| `check_telemetry_content_free.py` | scheduled | Any telemetry field carrying content rather than an identifier or a timing (SEC-024, observability/01 §5) |

The secret scan and the no-production-data check run in the same stage from
off-the-shelf tooling; they are configured in `pyproject.toml` and the workflow,
not reimplemented here.

## The ratchet, and why two of these carry a baseline

`check_conformance_diff` and `check_requirement_coverage` assert **completeness** —
every contracted operation is served, every requirement has a live test. Read
literally against a part-built product they are red on day one and stay red until
v1 ships, and a gate that is always red is one everybody learns to merge through.

So each splits its findings in two (`_ratchet.py`):

* **Contradiction** — code and specification actively disagree: a served path the
  contract does not define, a problem type whose status differs, a test tagged with
  an id no document declares. **Never forgiven**, never baselined.
* **Gap** — something specified but not yet built. Recorded in
  `baselines/<check>.json`, which may only shrink. A new gap fails as a regression;
  a gap that closes *also* fails, until the baseline is tightened with
  `--update-baseline`. That second half is what makes it a ratchet rather than a
  permanent excuse list.

The baseline files are meant to be read in review — each is the list of what its
gate is currently forgiving, and its length is an honest measure of what is left.

```bash
python tools/check_requirement_coverage.py --list-uncovered   # what to write next
```

**Exit codes are uniform across every script here:** `0` clean, `1` the gate is
red, `2` the check could not run. `2` is never collapsed into `0` — a gate that
reports success when it never executed is the false green quality/07 §7 names.

## Mutation testing

The band is `[tool.mutmut].only_mutate` in `pyproject.toml`: the nine invariant
transactions, scope filtering, and denial semantics — quality/01 §4's invariant
paths, application layer only. Database-layer invariants are out of a Python
mutation tool's reach by construction and are covered by the L3 attack suite plus
the drift check instead.

```bash
mutmut run                                          # POSIX only — refuses on native Windows
mutmut export-cicd-stats                            # writes mutants/mutmut-cicd-stats.json
python tools/check_mutation_score.py                # the gate
python tools/check_mutation_score.py --update-baseline
mutmut browse                                       # which mutants survived, and where
```

This ratchet stores a **count**, not a named set, because a mutant has no stable
name — mutmut identifies it by file plus an ordinal, so inserting a line renumbers
every mutant below it. A named baseline would churn on unrelated edits and train
everyone to regenerate it without reading.

Where the band stands — 597 mutants in ~47s, score **74.5%**, ceiling 152:

| File | Mutants | Killed | Unkilled | Score |
|---|---:|---:|---:|---:|
| `platform/db/scope.py` | 7 | 7 | 0 | 100.0% |
| `modules/hiring/invites.py` (T-7) | 90 | 89 | 1 | 98.9% |
| `calls/interruption.py` (T-6) | 22 | 21 | 1 | 95.5% |
| `modules/knowledge/publish.py` (T-4) | 35 | 32 | 3 | 91.4% |
| `calls/completion.py` (T-2) | 77 | 64 | 13 | 83.1% |
| `modules/drills/freeze.py` (T-5) | 54 | 42 | 12 | 77.8% |
| `modules/hiring/shortlist.py` (T-8/9) | 103 | 79 | 24 | 76.7% |
| `modules/review/grading.py` (T-3) | 88 | 53 | 35 | 60.2% |
| `calls/admission.py` (T-1) | 96 | 54 | 42 | 56.2% |
| `platform/errors/denial.py` | 18 | 3 | 15 | 16.7% |
| `platform/db/privileged.py` | 7 | 1 | 6 | 14.3% |

T-7 was 0.0% — 87 mutants, every one `no_tests` — until the T-7 battery landed.
Writing it found two contract defects in `invites.py` that no other check had
caught, which is the argument for the band including code nothing tests yet
rather than quietly excluding it.

The three worth reading now:

* **`platform/errors/denial.py` (16.7%)** — the application half of
  denial-as-absence (ADR-0036, AC-IDA-006). The L3 suite proves the *database*
  denies; almost nothing exercises the code shaping the response.
* **`platform/db/privileged.py` (14.3%)** — 6 of 7 mutants have no test at all.
* **`grading.py` (60.2%)** and **`admission.py` (56.2%)** — 35 and 10 survivors
  with coverage present. The opposite problem: exercised but under-asserted.

`invites.py`'s last survivor is a documented **equivalent** mutant —
`datetime.now(UTC)` → `datetime.now(None)` stores the same instant while the
process TZ and the database session TZ agree, as they do here and on CI. It is
noted in the test rather than chased.

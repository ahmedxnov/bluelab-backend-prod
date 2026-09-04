# Module boundaries

`ADR-0002` draws the application plane's internal boundaries **exactly on the
Phase 1 specification ownership lines** — one module per owning spec file, plus an
operations module. That is what keeps specification ownership and code ownership
the same line, and it is why a requirement's home file names its module.

## The map

| Package | Owns | Spec file | Tables (data/01) |
|---|---|---|---|
| `modules/identity` | `FR-IDA-*` | specs/10 | §2 |
| `modules/knowledge` | `FR-KNW-*` | specs/11 | §3 |
| `modules/drills` | `FR-DRL-*` | specs/12 | §4 |
| `modules/review` | `FR-SCR-*` | specs/14 | §5 |
| `modules/training` | `FR-TRP-*`, `FR-TRM-*` | specs/20, specs/21 | §6 |
| `modules/hiring` | `FR-HIR-*` | specs/22 | §7 |
| `modules/assessment` | `FR-CND-*` | specs/23 | §7 |
| `modules/operations` | the internal surface | ADR-0010 | §9 |

Two packages sit beside the eight and are deliberately **not** modules:

* `notifications/` — the `email_send` ledger and the closed five-template registry
  (data/01 §8). No specification file owns email; adding a ninth module would break
  the one-module-per-owning-file rule for shared infrastructure with a closed
  inventory.
* `calls/` — the application plane's half of the call seam. `FR-LIV-*` behaviour
  belongs to the call session runtime, which is a separate deployment unit
  (ADR-0071); what lives here is the part of the call path that must touch the
  transactional store.

## The rule

> Modules interact through published interfaces, never through each other's stored
> state. — ADR-0002

Concretely:

* a cross-module caller enters through `service.py`;
* `models.py` and `router.py` are private to their module;
* cross-module **foreign keys** are legal and used freely — all tables live in one
  `public` schema, and the module boundary is a *code* boundary, not a Postgres one
  (data/00 §4).

ADR-0002 records the honest risk: *"module boundaries inside one process are a
discipline, not a physical constraint, and erode unless enforced."* So the rule is
not a review convention — the `import-linter` contracts in `pyproject.toml` make a
violation a build failure, and they run as commit-gate stage 1 (ADR-0012,
pipeline/02 §2).

## Why `review` is different

The Review module is the **sole renderer of any scorecard-bearing surface**. That
is what makes `FR-SCR-017` — the concealment rule — enforceable in one place rather
than in eight, and auditable rather than distributed. `calls` and `work` reach the
attempt lifecycle through `modules/review/attempts.py`, never through
`modules/review/models.py`, precisely so the concealment-bearing tables stay behind
one door.

Extracting Review as its own *service* was considered and rejected as premature: a
module boundary already gives single-point enforcement, and a network boundary adds
latency to every review render for no additional guarantee.

## The layering

```
entrypoints  →  api  →  calls  →  work  →  modules  →  notifications  →  adapters  →  platform
```

Enforced as an `import-linter` layers contract. `platform` knows no domain;
`adapters` know one external capability each; `modules` know their own requirements
and each other's published interfaces only.

`bluelab_runtime_bundle` sits outside this stack: it is the version-locked contract
package shared with the call plane, and **only `calls/bundle_builder.py` may import
it** (ADR-0071 rule 5).

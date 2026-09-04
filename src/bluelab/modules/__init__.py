"""The eight modules of the application plane (ADR-0002).

Module boundaries sit **exactly on the Phase 1 specification ownership lines**,
so a requirement's home file names its module and traceability stays cheap
through implementation:

    identity     FR-IDA-*    specs/10-identity-and-access.spec.md
    knowledge    FR-KNW-*    specs/11-knowledge.spec.md
    drills       FR-DRL-*    specs/12-drill-lifecycle.spec.md
    review       FR-SCR-*    specs/14-scoring-and-review.spec.md
    training     FR-TRP-*    specs/20-training-rep.spec.md
                 FR-TRM-*    specs/21-training-manager.spec.md
    hiring       FR-HIR-*    specs/22-hiring-manager.spec.md
    assessment   FR-CND-*    specs/23-candidate-flow.spec.md
    operations   the BlueLab-internal surface (ADR-0010)

Live-call behaviour (FR-LIV-*) has no module of its own: the runtime is a
separate deployment unit, and its application-plane half is `bluelab.calls`.

**The boundary rule (ADR-0002).** Modules interact through published interfaces,
never through each other's stored state. Cross-module *foreign keys* are legal
and used freely; cross-module *code reach into another module's tables* is a
defect, and the import-linter contracts in `pyproject.toml` make it a build
failure rather than a review finding (ADR-0012).

All tables live in the single `public` schema — the module boundary is a code
boundary, not a Postgres one (data/00 §4).
"""

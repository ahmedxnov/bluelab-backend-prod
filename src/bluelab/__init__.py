"""BlueLab backend — the application plane and the work plane.

Two of the three runtime planes of `architecture/00-overview.arch.md` live here:

    application plane   stateless HTTP; the modular monolith of ADR-0002
    work plane          the queue-driven job types of architecture 00 §3.3

The third plane — the call session runtime — is a separate deployment unit
(`bluelab-agent-prod`, independently deployed) and holds no data-plane credential
(`stack/adr/0071-call-plane-process-isolation.md`). Its *application-plane* half —
admission, the runtime-bundle builder, and the three signed internal endpoints —
lives in `bluelab.calls`.

The governing documents live in `bluelab-platform`; locked local contract snapshots are under
`contracts/platform/`:

    specs/          what the system must do (FR / NFR / CMP / SEC)
    architecture/   planes, components, consistency, trust boundaries
    stack/          the technology decisions (ADR-0012, 0022, 0023, 0071)
    api/            the contract; openapi.yaml is authoritative (ADR-0035)
    data/           the schema, the nine invariant transactions, RLS
    quality/        the nine test levels and the Definition of Done
    observability/  the signal catalogue and the content-free rule
    pipeline/       the commit gate this repository must stay green against
    infra/          the environments this repository is deployed into
"""

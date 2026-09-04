"""The notification ledger and the closed template registry (data/01 §8, api/02 §3).

Deliberately **not** a ninth domain module: ADR-0002 draws one module per owning
specification file plus operations, and no specification file owns email. This is
shared infrastructure with a closed inventory, consumed by identity, hiring, and
assessment through a published interface, and driven by the `dispatch_email`
worker.

    E-1 credentials / password reset   FR-IDA-003, FR-IDA-006
    E-2 candidate invite and resend    FR-HIR-008/009
    E-3 candidate's own report         FR-CND-012 (never under `withhold`)
    E-4 shortlist to HR, PDFs attached FR-HIR-014
    E-5 manager completion notice      FR-HIR-017

Assignment deliberately sends nothing (FR-TRM-011).
"""

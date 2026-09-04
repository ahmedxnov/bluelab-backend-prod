"""T-10 — erasure and export (ADR-0033, data/03 §3-§4).

The dedicated procedures here are the **only sanctioned writers through the
freeze guards**. Erasure removes the person — identity fields, transcripts,
quotes, recordings, PDFs, export bundles — and leaves the statistical residue
(scores, times, statuses) standing. Neither procedure is reachable from a request
path; both are triggered from the ops surface and audited.
"""

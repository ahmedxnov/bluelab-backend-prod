"""Scheduled-sweep entrypoint.

Runs the reconciliation duties ADR-0071 rule 7 makes load-bearing rather than
theoretical: the single-live-call lease-expiry sweep and the stranded
`in_progress` attempt sweep, plus the retention and lifecycle sweeps of data/03.
"""

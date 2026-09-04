"""The lease-expiry and stranded-attempt sweep, run from
`bluelab.entrypoints.sweeper`. A `SECURITY DEFINER` escape hatch that is
enumerated, audited, and unreachable from any request path (ADR-0031).
"""

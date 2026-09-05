"""The application plane's half of the call seam.

The call *session runtime* is not here — it is a separate deployment unit
(`bluelab-agent-prod`) that holds no data-plane credential
(ADR-0071). What lives here is everything on the call path that must touch the
transactional store:

    admission (T-1)          the gate every call passes, and the only place a
                             strongly-consistent write happens on the call path
    placement                selecting a runtime instance and handing off
    the runtime bundle       built from the frozen drill; an allowlist by
                             construction (api/02 §1.1)
    completion (T-2)         transcript insert + status flip + grade enqueue,
                             one transaction
    interruption (T-6)       void, allowance reversal, lease release
    reconciliation           the lease-expiry sweep and the LiveKit webhook, which
                             ADR-0071 rule 7 makes mandatory rather than optional

**Classification stays with the runtime; the write stays here.** The runtime is
the single authority on how a call ended, because only it has the facts — it
reports that decision, and this plane executes it (ADR-0071 rule 4).

This package reaches the attempt lifecycle through `bluelab.modules.review`'s
published interface, never through its tables.
"""

"""`POST /hooks/livekit` — the reconciliation receiver (api/01 §7, ADR-0038).

Provider-authenticated, idempotent. Together with the lease-expiry sweep this is
what guarantees no attempt is stranded `in_progress` when a completion request is
lost — a real failure mode once the runtime reports rather than writes
(ADR-0071 rule 7). Needs genuine L8 resilience coverage, not a theoretical test.
"""

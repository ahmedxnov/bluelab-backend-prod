"""The single-live-call lease (ADR-0011): `SET NX PX` in Valkey with short-TTL
renewal, keyed on the participant, released by T-6 or by expiry.

The heartbeat **re-creates** the lease (SET, not expiry-refresh) so coordination-
store loss self-heals (infra gate F-4). The expiry sweep in
`bluelab.entrypoints.sweeper` is what guarantees no attempt is stranded
`in_progress` when a completion request is lost.
"""

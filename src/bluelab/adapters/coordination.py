"""C-13 — the coordination store, Valkey (ADR-0024).

Holds exactly the volatile state architecture/00 §3.4 assigns: sessions, the
single-live-call lease, the live-call registry, placement state. Nothing here
survives its call. The lease is `SET NX PX` with short-TTL renewal, and the
heartbeat re-creates the lease rather than refreshing an expiry, so coordination
loss self-heals (ADR-0011; infra gate F-4).
"""

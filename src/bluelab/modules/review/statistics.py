"""Readers over the derived views V-1…V-11 in `sql/views/`.

Derived on read, never stored (ADR-0034). The views are `security_invoker = true`
without exception — a definer-semantics view owned by the migration role would
silently bypass every policy above it (ADR-0031, data/02 §3).
"""

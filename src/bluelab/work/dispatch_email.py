"""The email dispatcher (api/02 §3).

Renders exactly five templates and **rejects any `kind` outside the inventory** —
specs/00 §6 is closed, so the dispatcher has five templates and no sixth path to
build. Dedupe is `email_send (kind, dedupe_key)` unique: at most one send per
(recipient, event).
"""

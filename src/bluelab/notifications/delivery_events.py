"""Provider delivery-event ingestion — provider-authenticated, idempotent per
`provider_message_id`, and *deployment configuration* rather than a product HTTP
endpoint (api/02 §3).

Its product-visible result is one field: invite delivery state on the hiring
pipeline, so a candidate who never received the invite is distinguishable from one
who ignored it (FR-HIR-010). Silent drops are caught by the *absence* of a
terminal event, not by an error (observability/01 §4.3).
"""

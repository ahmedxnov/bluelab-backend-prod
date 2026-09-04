"""C-14 — transactional email, Amazon SES in-region (ADR-0026).

Send plus delivery-event ingestion (`sent -> delivered | bounced | delayed`),
provider-authenticated and idempotent per `provider_message_id` (api/02 §3).
The product-visible result is one field: invite delivery state on the pipeline,
so a candidate who never received the invite is distinguishable from one who
ignored it (FR-HIR-010).
"""

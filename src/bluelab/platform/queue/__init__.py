"""The job queue — Procrastinate tables in the same PostgreSQL database (ADR-0023).

The decisive property is *transactional enqueue*: the domain write and the job
row commit together, so no outbox and no external broker is needed at these
volumes. The worker interface is kept broker-agnostic so the named SQS escape
stays cheap.
"""

"""Adapters for the external capabilities of architecture/04 and the stack decision
table (stack/00 §3).

Every adapter is synchronous within its caller's own budget and always carries a
timeout and a circuit breaker (architecture/00 §4, architecture/04 §2) — external
latency is the dominant risk to NFR-001. Each names one capability so a vendor
swap is a file, not a migration; the C-numbers below are the capability rows.

The turn-path capabilities (C-2 STT, C-3 conversational model, C-4 TTS, C-5
demeanor) are deliberately absent: they belong to the call plane, which is a
separate deployment unit (ADR-0071).

**This layer is where the two residency paths differ, and nowhere else.**
stack/00 §2 carries both postures — Path A (cross-border permitted, managed
services in the EU) and Path B (Egypt-restricted, container/self-host variants) —
and which one binds depends on a counsel answer that has not arrived (R-9). Every
row below therefore has a Path B variant, and the discipline that keeps that cheap
is stated once in ADR-0025 and generalized here:

    the API is the contract; the implementation is configuration.

So: S3 API, not "Amazon S3" (Supabase Storage / S3 / MinIO in colo). Postgres 16,
not a managed host. The OpenTelemetry SDK, not Grafana Cloud. A LiveKit server
API, not LiveKit Cloud specifically. A collapse onto either path must be a
configuration change and a deployment change — never a code change in a module,
and never a second code path here.

Two consequences worth stating plainly, because they are easy to violate by
accident:

  * **No adapter may branch on the residency path**, exactly as no code may branch
    on the rollout tier (SEC-026, infra C-6). Both live in configuration.
  * **A vendor's own SDK belongs behind its adapter**, not imported from a module
    or a worker. That is what makes the Path B variant a file swap.

    C-1   media transport          LiveKit server API      ADR-0014
    C-6   evaluator model          Claude Sonnet 4.6       ADR-0018
    C-7   generation model         Claude Sonnet 4.6       ADR-0018
    C-8   document extraction      Azure Document Intel.   ADR-0020
    C-9   PDF rendering            Chromium (Playwright)   ADR-0021
    C-11  object store             S3 API                  ADR-0025
    C-13  coordination store       Valkey                  ADR-0024
    C-14  transactional email      Amazon SES              ADR-0026
    C-17  secrets                  AWS Secrets Manager     ADR-0028
"""

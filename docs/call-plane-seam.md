# The call-plane seam

The call session runtime is a **separate deployment unit** and holds **no
data-plane credential**: no Postgres connection, no object-store write path, no
secret beyond its scoped provider keys and one HMAC secret (ADR-0071 decision 1).

That is not tidiness. `FR-LIV-009` requires the AI buyer to stay in persona under
direct pressure and `SEC-030` makes that attackable; the schema's defence is the
physical split of `drill_concealed` into its own table, which prevents an
*accidental join* but does not prevent a session-bearing process from simply
selecting the row. Under a process boundary, "the agent never reads the answer key"
stops being a code-review convention and becomes a **parse-time failure**: the
bundle model declares no such field, so a bundle carrying one does not deserialise
— and the process has no credential with which to go and fetch it.

Two facts make it decisive rather than merely preferable:

* **At Tier 1, IAM collapses to a single role on one host** (C-5, ADR-0055).
  Per-plane separation — the normal answer — does not exist at the rung where the
  first real people's data is processed. A credential the call plane *never holds*
  is the strongest compensating control available there.
* **The call plane is the most externally exposed component.** It terminates media
  from an untrusted browser across boundary B3 and runs model output as its own
  control flow. It is the last component that should hold a data-plane credential.

## What crosses, and when

| Direction | Surface | When |
|---|---|---|
| app → runtime | LiveKit access-token metadata (api/02 §1) | at dispatch |
| runtime → app | `GET /internal/calls/{call_id}/bundle` | at session start |
| runtime → app | `POST /internal/calls/{call_id}/completion` | once, at call end |
| runtime → app | `POST /internal/calls/{call_id}/interruption` | on a detected interruption |
| provider → app | `POST /hooks/livekit` | reconciliation |

Authentication on the three internal endpoints is `X-Agent-Signature`, HMAC-SHA256,
verified constant-time, failing closed — **over a canonical string, not the bare
body.** See the ratified amendment below.

**No turn-path cost.** The seam is crossed at session start and at call end, never
inside a turn — so `NFR-001` is untouched.

## The four rules that are easy to break by accident

1. **The bundle is an allowlist, structurally.** `extra="forbid"`; no field for
   product facts, the rubric, the answer key, or any element of `drill_concealed`.
   The builder is invariant-path code — it is the one place a concealment leak
   could originate.
2. **The transcript crosses once.** Buffered in session memory for the whole call
   and delivered *with* completion. Per-segment streaming is rejected: it breaks
   T-2's single transaction and strands orphan segments behind every interrupted
   call, defeating `FR-LIV-015`'s void-and-free semantics.
3. **Classification is the runtime's; the write is the application plane's.** Only
   the runtime has the facts about how a call ended, so it still decides — it
   reports that decision rather than executing it.
4. **Egress is requested by the runtime and executed by the application plane**,
   with the application plane's own credentials, so rule 1 holds.

## The failure mode this creates, and the backstop

A lost completion request is now a real network failure mode. The lease-expiry
sweep and the `POST /hooks/livekit` receiver are therefore **mandatory, not
optional** (ADR-0071 rule 7): together they are what guarantees no attempt is
stranded `in_progress`. They need genuine L8 resilience coverage — an exercised
path, not a theoretical one.

## Ratified signature amendment — bind the whole request

**Accepted 2026-09-05; reflected in `api/02 §1.2` and ADR-0071 decision 2.**

Both documents formerly specified *"HMAC-SHA256 over the exact raw body"*. That
was exploitable as written:

- `GET /internal/calls/{call_id}/bundle` is specified with an **empty body**, so
  its signature is `HMAC(secret, b"")` — **a constant**, identical for every
  bundle request, for every call, forever. One observed header is a permanent
  credential.
- Nothing binds a signature to the **path**, so a captured signature is valid for
  any `call_id`, including other tenants'.
- Nothing binds it to a **time**, so nothing expires.

The allowlist bounds the damage exactly as ADR-0071 rule 5 intends — no answer
key, no rubric, no `drill_concealed` can be in a bundle — so what leaks is frozen
scenario and participant-safe persona content for arbitrary calls. That is the
isolation design working, and it is still an unauthenticated cross-tenant read.

**Amendment:** sign

```
METHOD \n PATH \n TIMESTAMP \n sha256(body).hexdigest()
```

with `X-Agent-Timestamp` alongside `X-Agent-Signature` and a 300-second skew
window. Method and path bind the signature to *this* request; the timestamp
bounds replay; the body digest preserves the property the original wording
wanted.

Replay *inside* the skew window on the two POSTs stays undefended on purpose: T-2
is keyed on the call and grade-once is `scorecard.attempt_id` unique (T-3), so a
replayed completion is a no-op. A nonce store would duplicate a schema guarantee
and put state on the admission path.

Implemented in `platform/security/agent_signature.py` and mirrored in the agent's
`signing.py`; both repositories carry deterministic vectors for the canonical bytes.

## Reconciled agent seam

`bluelab-agent-prod` now calls:

```
GET  /internal/calls/{call_id}/bundle
POST /internal/calls/{call_id}/completion   ← whole buffered transcript, once
POST /internal/calls/{call_id}/interruption
```

The body and path use `call_id`, so test calls (which have no `attempt_id`) use the same seam. The
completion and interruption payloads are closed in the locked `api/02` snapshot.

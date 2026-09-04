# Contracts

`api/openapi.yaml` in the repository root is **authoritative** (ADR-0035). It is
not copied here — a copy would drift. This directory holds only what is generated
*from* it and diffed *against* the running server.

The **conformance diff** regenerates the TypeScript client and the problem-type
registry from the contract and diffs them against what the server actually serves.
**Any drift fails the build**, and it must be build-failing *before the first
feature merge* (api gate C-2, pipeline/02 §2 row 3).

Consumer-side contract tests run against a **Prism** mock of the same document;
`redocly lint` must be clean.

Two contract properties are checkable in the document itself, and are:

* **no candidate-authenticated endpoint serializes a score, band, scorecard,
  review, or report field** (FR-CND-007, FR-SCR-018);
* `rubric_breakdown[].weight` is **absent, not null**, for non-authors
  (AC-SCR-003).

`generated/` is build output. Nothing in it is edited by hand.

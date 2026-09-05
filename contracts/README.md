# Contracts

The authoritative documents live in `bluelab-platform`; this independent repository never reads them
through a sibling path. `platform/` is the reviewed local snapshot used by builds and tests, and
`platform.lock.json` records its source commit and exact SHA-256 values. Run
`python tools/verify_contract_lock.py` before every consumer check. A changed byte without a deliberate
lock refresh fails closed.

The backend **conformance diff** compares its served operations and problem registry with the locked
OpenAPI and error-catalog snapshot. Frontend client generation is owned and checked by the frontend
repository; the backend gate never requires that checkout.
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

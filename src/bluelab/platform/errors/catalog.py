"""The problem-type registry.

One registry, mirroring `api/03-error-catalog.api.md`, covering every 4xx/5xx the
surface can produce. `tools/check_conformance_diff.py` build-fails on any
divergence between this registry, the contract, and what the server serves
(api C-2, ADR-0035).

## Shape

RFC 9457 `application/problem+json`, with **stable origin-relative `type` URIs**.
The client branches on `type` only — never on status, never by parsing `detail`
(ux/05 §3) — so a type slug is a contract element and renaming one is a breaking
change under ADR-0039, not a refactor.

Every response carries `request_id`. Validation failures additionally carry
field-level `errors[]`.

## Why the registry is data and not exceptions

The conformance diff needs to enumerate every problem the server can produce and
compare it against the document. That is only possible if the set is
introspectable. Raising ad-hoc `HTTPException`s scattered through handlers would
make the set discoverable by grep and by nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PROBLEM_BASE: Final = "/problems"
"""Origin-relative, so the URIs stay stable across environments and hosts."""

CONTENT_TYPE: Final = "application/problem+json"


@dataclass(frozen=True, slots=True)
class ProblemType:
    """One row of the error catalog."""

    slug: str
    status: int
    title: str

    @property
    def type_uri(self) -> str:
        return f"{PROBLEM_BASE}/{self.slug}"


class _Registry:
    """The complete, introspectable set of problem types."""

    def __init__(self) -> None:
        self._by_slug: dict[str, ProblemType] = {}

    def add(self, slug: str, status: int, title: str) -> ProblemType:
        if slug in self._by_slug:
            raise ValueError(f"duplicate problem type: {slug}")
        problem = ProblemType(slug=slug, status=status, title=title)
        self._by_slug[slug] = problem
        return problem

    def get(self, slug: str) -> ProblemType | None:
        return self._by_slug.get(slug)

    def all(self) -> tuple[ProblemType, ...]:
        """Every registered type, sorted — the conformance diff's input."""
        return tuple(self._by_slug[slug] for slug in sorted(self._by_slug))


REGISTRY: Final = _Registry()
_add = REGISTRY.add

# ── generic (ux/05 §3.1) ──────────────────────────────────────────────────────
VALIDATION_ERROR = _add("validation-error", 422, "Validation failed")
MALFORMED_REQUEST = _add("malformed-request", 400, "Malformed request")
NOT_FOUND = _add("not-found", 404, "Not found")
RATE_LIMITED = _add("rate-limited", 429, "Too many requests")
INTERNAL_ERROR = _add("internal-error", 500, "Internal error")
SERVICE_UNAVAILABLE = _add("service-unavailable", 503, "Service unavailable")

# ── session and gates (ux/05 §3.2) ────────────────────────────────────────────
SESSION_INVALID = _add("session-invalid", 401, "Session invalid")
FIRST_SIGN_IN_REQUIRED = _add("first-sign-in-required", 409, "First sign-in required")
FIRST_SIGN_IN_NOT_PENDING = _add("first-sign-in-not-pending", 409, "First sign-in not pending")
CONSENT_REQUIRED = _add("consent-required", 409, "Consent required")
TERMS_ACCEPTANCE_REQUIRED = _add("terms-acceptance-required", 409, "Terms acceptance required")
ACCOUNT_DEACTIVATED = _add("account-deactivated", 403, "Account deactivated")
INVALID_CREDENTIALS = _add("invalid-credentials", 401, "Invalid credentials")
RESET_TOKEN_INVALID = _add("reset-token-invalid", 401, "Reset link invalid")
PASSWORD_POLICY = _add("password-policy", 422, "Password does not meet policy")

# ── candidate token (ux/05 §3.3) ──────────────────────────────────────────────
TOKEN_INVALID = _add("token-invalid", 401, "Link invalid")
TOKEN_EXPIRED = _add("token-expired", 401, "Link expired")
ASSESSMENT_COMPLETED = _add("assessment-completed", 409, "Assessment already completed")
PREFLIGHT_REQUIRED = _add("preflight-required", 409, "Pre-flight required")

# ── call admission (ux/05 §3.4) ───────────────────────────────────────────────
CALL_ALREADY_ACTIVE = _add("call-already-active", 409, "A call is already active")
ALLOWANCE_EXHAUSTED = _add("allowance-exhausted", 409, "No attempts remaining")
STAGE_NOT_NEXT = _add("stage-not-next", 409, "Not the next stage")
STAGE_CONSUMED = _add("stage-consumed", 409, "Stage already taken")
DRILL_NOT_STARTABLE = _add("drill-not-startable", 409, "Drill is not available to take")
NO_ACTIVE_CALL = _add("no-active-call", 404, "No active call")
REVIEW_NOT_READY = _add("review-not-ready", 409, "Review not ready")
CALL_CAPACITY = _add("call-capacity", 503, "All call slots are busy")

# ── authoring and knowledge (ux/05 §3.5) ──────────────────────────────────────
NOT_JOB_RELEVANT = _add("not-job-relevant", 422, "Not job-relevant")
GENERATION_INCOMPLETE = _add("generation-incomplete", 409, "Generation incomplete")
GENERATION_IN_PROGRESS = _add("generation-in-progress", 409, "Generation in progress")
WEIGHTS_NOT_100 = _add("weights-not-100", 409, "Rubric weights must total 100")
DRILL_NOT_DRAFT = _add("drill-not-draft", 409, "Drill is published")
DRILL_NOT_ARCHIVABLE = _add("drill-not-archivable", 409, "Drafts cannot be archived")
DOCUMENT_CAP = _add("document-cap", 409, "Document limit reached")
FILE_TOO_LARGE = _add("file-too-large", 413, "File too large")
UNSUPPORTED_FILE_TYPE = _add("unsupported-file-type", 415, "Unsupported file type")
NO_DRAFT_TO_REVIEW = _add("no-draft-to-review", 409, "No draft to review")
EXTRACTION_PENDING = _add("extraction-pending", 409, "Extraction still running")
STALE_REVIEW = _add("stale-review", 409, "The facts changed under this review")

# ── hiring (ux/05 §3.6) ───────────────────────────────────────────────────────
ASSESSMENT_FROZEN = _add("assessment-frozen", 409, "Assessment is frozen")
ASSESSMENT_EMPTY = _add("assessment-empty", 409, "Assessment has no drills")
POSITION_CLOSED = _add("position-closed", 409, "Position is closed")
DECISION_FROZEN = _add("decision-frozen", 409, "Decision is frozen")
CANDIDATE_NOT_DECIDABLE = _add("candidate-not-decidable", 409, "No evidence to decide on")
SHORTLIST_EMPTY = _add("shortlist-empty", 409, "No approved candidates")
PDF_NOT_READY = _add("pdf-not-ready", 409, "Report PDF not ready")
DUPLICATE_EMAIL = _add("duplicate-email", 409, "Email already present")

# `idempotency-*` are never user-facing by design — the fetch layer owns keys
# (ux/05 §3.6). They are registered because the server can still produce them and
# the conformance diff enumerates what the server can produce.
IDEMPOTENCY_KEY_REUSE = _add("idempotency-key-reuse", 409, "Idempotency key reused")
IDEMPOTENCY_KEY_REQUIRED = _add("idempotency-key-required", 422, "Idempotency key required")

# ── ops (ux/05 §3.7) ──────────────────────────────────────────────────────────
OPS_SESSION_INVALID = _add("ops-session-invalid", 401, "Ops session invalid")
MANAGER_OWNS_DEPENDENTS = _add("manager-owns-dependents", 409, "Manager still owns dependents")
REASON_REQUIRED = _add("reason-required", 422, "Reason required")
FAULT_ALREADY_RESOLVED = _add("fault-already-resolved", 409, "Fault already resolved")
SUBJECT_UNKNOWN = _add("subject-unknown", 404, "Subject not found")

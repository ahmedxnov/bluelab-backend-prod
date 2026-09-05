"""Knowledge-boundary guard for the API 02 runtime bundle.

`assert_runtime_safe(bundle)` is the **forbidden-field guard** from 07 §5: before a bundle
(or any dispatch/room metadata derived from it) leaves the backend OR is trusted by the
voice worker, it asserts the payload carries **no** raw product knowledge, evaluator-only
truth, unrelated products, rubric internals, or wrapper identifiers. A positive match is a
**hard error, not a warning** — the bundle is rejected and the roleplay does not start.

Two layers protect the boundary:
  1. `RuntimeBundle` uses `extra="forbid"`, so an unexpected top-level key fails to parse.
  2. This guard does a *deep* scan of the serialized bundle for forbidden keys at any depth,
     catching anything tucked inside `runtime_config`, `prompt_versions`, `persona`, etc.

The forbidden list is sourced directly from the spec invariants, so it is auditable:

  - **Raw product knowledge**: product_facts, product_knowledge, product_reference, product_document,
    knowledge_base, facts, answer_key, selected_product_snapshot (the snapshot's frozen
    ProductVersion facts are evaluator-only truth and must never reach the prospect).
  - **Evaluator-only truth / hidden scoring**: rubric, rubric_dimensions,
    rubrics, scorecard, scorecards, dimension_scores, scoring, score, weights, success_criteria,
    failure_criteria, evidence_guidance, evaluator, evaluator_prompt, correct_answer, ground_truth.
  - **Wrapper identifiers** (the prospect never knows the wrapper; dispatch/room
    metadata carries only IDs, never these payloads, 05 §6): wrapper_references, candidate_id,
    candidate, assessment_id, assessment, drill_slot_id, rep_id, assignment_id,
    training_assignment_id, user_id. `team_id` is deliberately allowed because it is a required
    routing/scope field in the closed API 02 bundle.
  - **Secrets** (keys never live in the runtime bundle): api_key, apikey,
    api_secret, secret, hmac_secret, service_role_key, service_role, token, password,
    private_key, access_token, bearer.

Note on the persona's hidden motive: the prospect *does* need to know its own hidden motive to
roleplay (it lives inside `persona.who_you_are`, section 2A). That is allowed — it is the
persona's own psychology, not evaluator truth. What is forbidden is the *evaluator's* view of
it (the rubric dimension that scores whether the rep uncovered it). The key-based scan below
forbids `rubric`/`hidden_motive_rubric`/scoring keys, not the persona prose, so the boundary
stays where the spec draws it: the prospect carries persona, the Evaluator holds the answer key.
"""

from __future__ import annotations

from typing import Any

from .schema import RuntimeBundle

# Canonical, auditable forbidden-key set (see module docstring for spec provenance).
# Compared case-insensitively against every key at every depth of the serialized bundle.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        # Raw product knowledge / answer key
        "product_facts",
        "product_fact",
        "product_knowledge",
        "product_reference",
        "product_document",
        "product_documents",
        "product_version",
        "knowledge_base",
        "knowledge",
        "facts",
        "answer_key",
        "selected_product_snapshot",
        "product_snapshot",
        # Evaluator-only truth / hidden scoring
        "rubric",
        "rubrics",
        "rubric_dimension",
        "rubric_dimensions",
        "scorecard",
        "scorecards",
        "dimension_scores",
        "scoring",
        "score",
        "weights",
        "success_criteria",
        "failure_criteria",
        "evidence_guidance",
        "evaluator",
        "evaluator_prompt",
        "evaluator_prompt_version",
        "correct_answer",
        "correct_or_better_answer",
        "ground_truth",
        "hidden_motive_rubric",
        # Wrapper identifiers (ownership/access only — never reach the prospect)
        "wrapper_references",
        "wrapper_reference",
        "candidate_id",
        "candidate",
        "candidates",
        "assessment_id",
        "assessment",
        "drill_slot_id",
        "rep_id",
        "assignment_id",
        "training_assignment_id",
        "user_id",
        # Secrets (never in the runtime bundle)
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "hmac_secret",
        "bluelab_agent_hmac_secret",
        "service_role_key",
        "service_role",
        "token",
        "password",
        "private_key",
        "access_token",
        "bearer",
    }
)


class KnowledgeBoundaryError(ValueError):
    """Raised when a bundle/payload violates the knowledge boundary.

    Carries the offending dotted paths so the security event can be logged (09 §8 logs
    forbidden-field blocks) without echoing the offending values.
    """

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        joined = ", ".join(sorted(paths))
        super().__init__(
            "Runtime bundle violates the knowledge boundary; "
            f"forbidden field(s) present: {joined}"
        )


def _scan(value: Any, path: str, hits: list[str]) -> None:
    """Depth-first scan collecting dotted paths whose *key* is in FORBIDDEN_FIELDS."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_norm = str(key).strip().lower()
            child_path = f"{path}.{key}" if path else str(key)
            if key_norm in FORBIDDEN_FIELDS:
                hits.append(child_path)
            _scan(child, child_path, hits)
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            _scan(child, f"{path}[{i}]", hits)


def find_forbidden_fields(payload: dict[str, Any]) -> list[str]:
    """Return the dotted paths of any forbidden keys in an arbitrary payload (no raise).

    Use this to guard dispatch/room metadata too — not just the bundle — since 07 §5 requires
    the same check on anything that leaves the backend toward the room.
    """
    hits: list[str] = []
    _scan(payload, "", hits)
    return hits


def assert_payload_safe(payload: dict[str, Any]) -> None:
    """Hard-reject any dict payload containing forbidden fields."""
    hits = find_forbidden_fields(payload)
    if hits:
        raise KnowledgeBoundaryError(hits)


def assert_runtime_safe(bundle: RuntimeBundle) -> RuntimeBundle:
    """Validate a constructed `RuntimeBundle` against the knowledge boundary.

    Serializes the bundle and deep-scans for forbidden keys. Returns the bundle unchanged on
    success so it can be used inline (`bundle = assert_runtime_safe(bundle)`); raises
    `KnowledgeBoundaryError` on the first violating payload — the worker treats this as a
    do-not-start condition.
    """
    assert_payload_safe(bundle.model_dump(mode="json"))
    return bundle

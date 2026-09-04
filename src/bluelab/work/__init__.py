"""The work plane — queue-driven, stateless, all jobs idempotent by job identity and
retry-safe (architecture/00 §3.3, api/02 §2).

    grade_attempt       call completion -> FR-SCR-001…009 under NFR-005
    generate_scenario   author request  -> FR-DRL-004/008
    generate_rubric     author request  -> FR-DRL-011
    extract_facts       document upload -> FR-KNW-003
    render_report       candidate completion or manager retry -> FR-HIR-011/012
    dispatch_email      the closed five of specs/00 §6

Payloads carry ids only. Every worker re-reads current truth and resolves its
scope context from the job row, so the work plane has no unscoped path
(ADR-0005, ADR-0031).

Two model-adjacent outputs deliberately add **no** further job type: the
candidate report's cross-drill takeaway is produced inside `render_report`, and
the rep coach-feedback feed derives rule-based inside the training module
(architecture/00 §3.3, gate finding F-1).
"""

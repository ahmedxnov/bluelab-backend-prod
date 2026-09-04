"""Candidate report rendering (FR-HIR-011/012), including the cross-drill takeaway
synthesis through the evaluator capability (C-6) — inside this job, not a sixth
job type.

**Concealment-safe by construction, not by post-filtering** (api/02 §4): this
worker may read the candidate row, graded attempts with scorecards and moments,
the position, and the drills' participant-safe projection only. It must not read
`drill_concealed` or rubric weights, and the manager's `internal_note` is excluded
the same way. One artifact serves both HR and the candidate.

On PDF failure `pdf_status='failed'` and the report view still renders from data.
"""

"""The concealment projection — the single enforcement point for FR-SCR-017.

Two representations exist for concealment-bearing resources, chosen *server-side*
by authorship (api/00 §4.3):

  * the full basis (inputs, challenges, hidden motives, rubric with weights) —
    the drill's author, and for team drills the owning team's manager;
  * the participant-safe projection — everyone else eligible.

`rubric_breakdown[].weight` is **absent, not null**, for non-authors
(AC-SCR-003). Hidden motives and challenges are never revealed to a non-author,
in either product, even after the attempt ends. Rows are filtered by RLS;
columns are filtered here (ADR-0031 §4).
"""

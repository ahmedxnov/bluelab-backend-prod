"""Orchestration of scenario and rubric generation (FR-DRL-004/008/011).

Enqueues `generate_scenario` / `generate_rubric` with a `request_id` per author
click; a stale generation never overwrites a newer one. On failure there is **no
fallback content** and publish stays blocked (FR-DRL-006).
"""

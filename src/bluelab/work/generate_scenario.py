"""Scenario and persona generation (FR-DRL-004/008). Keyed on `request_id` so a
stale generation never overwrites a newer one. On failure the status carries the
reason, there is **no fallback content**, and publish stays blocked (FR-DRL-006).
"""

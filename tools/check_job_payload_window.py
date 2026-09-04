"""Job-payload compatibility in both directions across the N/N+1 window: enqueue with
N and consume with N+1, and the reverse, asserting neither poisons
(pipeline/02 §3, gate F-2).

Without this, a rollback strands N+1-shaped jobs that an N worker fails in a retry
loop — and grading silently misses its turnaround with no error surfaced.
"""

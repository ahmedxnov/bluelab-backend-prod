"""The ops fault queue (ADR-0010): grading failures (FR-SCR-009) and unavailable
playback assets (FR-SCR-013).

Content-free by construction — a fault carries identifiers and a reason, never the
content that produced it, because ops staff must be able to diagnose without
reading customer content. Resolution re-drives the job by identity.
"""

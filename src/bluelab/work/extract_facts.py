"""Fact extraction from an uploaded source document (FR-KNW-003), via C-8.

Status transitions `received -> extracting -> extracted | failed`; re-running a
terminal upload is a no-op. On failure or an empty extraction nothing reaches
review and the live facts are untouched (FR-KNW-008).
"""

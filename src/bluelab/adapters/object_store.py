"""C-11 — the object store (ADR-0025). The S3 API *is* the contract; the
implementation is configuration (Supabase Storage / S3 / MinIO).

Holds recordings, uploaded source files, rendered report PDFs, and export
bundles. Serves presigned playback (FR-SCR-011) and per-object erasure
(CMP-001). It must be able to be independently unavailable without breaking a
review (FR-SCR-013) — that degradation is asserted at L8.
"""

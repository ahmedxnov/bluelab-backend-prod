"""The three signed internal endpoints (api/02 §1.2, ADR-0071 rule 2):

    GET  /internal/calls/{call_id}/bundle       -> bundle build
    POST /internal/calls/{call_id}/completion   -> T-2, one transaction
    POST /internal/calls/{call_id}/interruption -> T-6

All authenticated with `X-Agent-Timestamp` and `X-Agent-Signature`: HMAC-SHA256 binds method,
call-specific path, timestamp, and the exact raw-body digest, verified constant-time and failing
closed. This surface is not part of
`api/openapi.yaml` — it is the plane-to-plane seam, not a product surface.

The independently deployed agent uses these exact call-keyed paths and buffers transcript segments
until the completion callback. The backend route implementations remain normal feature work; this
module may not introduce alternate compatibility paths for the retired seam.
"""

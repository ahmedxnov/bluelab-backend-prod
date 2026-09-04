"""The three signed internal endpoints (api/02 §1.2, ADR-0071 rule 2):

    GET  /internal/calls/{call_id}/bundle       -> bundle build
    POST /internal/calls/{call_id}/completion   -> T-2, one transaction
    POST /internal/calls/{call_id}/interruption -> T-6

All authenticated with `X-Agent-Signature`: HMAC-SHA256 over the exact raw body,
verified constant-time, failing closed. This surface is not part of
`api/openapi.yaml` — it is the plane-to-plane seam, not a product surface.

NOTE (open item for the owner): the deployed agent in
`Implementation/bluelab-agent-prod` currently calls
`/v1/internal/attempts/{id}/runtime-bundle`, `/v1/callbacks/agent/transcript`, and
`/v1/callbacks/agent/attempt-complete`, and streams transcript segments during the
call. api/02 §1.2 and ADR-0071 supersede both the paths and the streaming. The
seam must be reconciled on the contract's terms before the first real call.
"""

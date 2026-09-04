"""C-1 — LiveKit server-side API (ADR-0014, ADR-0038).

Access-token minting with the explicit agent-dispatch metadata of api/02 §1,
room lifecycle, webhook signature verification for `POST /hooks/livekit`, and
**egress**: the runtime *requests* recording start, the application plane
*executes* it with its own credentials, so ADR-0071 rule 1 holds.
"""

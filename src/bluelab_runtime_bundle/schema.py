"""The runtime bundle: an allowlist, structurally (api/02 §1.1, ADR-0071 rule 5).

`extra="forbid"`, and **no field** for product facts, the rubric, the answer key,
or any element of `drill_concealed`. A bundle that has smuggled forbidden
material fails to parse before it can reach prompt assembly — which is what turns
the knowledge boundary from a review-time convention into a parse-time guarantee.

It carries: the frozen scenario, the participant-safe persona material, language,
call type, voice identity, resolved runtime config, the `call_id` / `org_id` /
`team_id` correlation set, `max_call_seconds`, and `reconnect_grace_seconds`.
"""

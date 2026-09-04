"""Identity & Access — FR-IDA-* (specs/10-identity-and-access.spec.md).

Operator-mediated provisioning (there is no self-signup), authentication, the
first-sign-in gate, sessions, role and scope resolution, org and team membership,
candidate token issue and validation, and deactivation.

Two structural facts this module owns:
  * **Team === owning manager.** `team_id` is the owning manager's `account.id`;
    there is no separate team entity, because the specification defines none
    (data/00 §3).
  * **Deactivation revokes instantly**, which is why sessions are server-side and
    opaque rather than JWTs (FR-IDA-010, ADR-0028).

Also owns the platform-reference legal documents and the acceptance records that
clear the `409 consent-required` / `409 terms-acceptance-required` gates
(CMP-002 / CMP-005).
"""

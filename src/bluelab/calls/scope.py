"""Privileged, call-bound scope for trusted lifecycle mutations.

Product authentication establishes the participant identity before this helper is
used.  Runtime callbacks establish the same binding through the signed registry
entry.  RLS deliberately does not grant participants permission to mutate an
attempt's lifecycle, so those mutations run through this narrow system scope.
"""

from __future__ import annotations

from bluelab.calls.registry import CallSession
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import ScopeContext


def lifecycle_scope(call: CallSession) -> ScopeContext:
    return system_scope(org_id=call.org_id, team_id=call.team_id)

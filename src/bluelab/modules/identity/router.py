"""The public legal-reference routes mounted by `bluelab.api.v1`.

**Auth is the exception, and it lives in `bluelab.api.auth`.** A route needs
`bluelab.api.deps` for the resolved principal and the session store, and
`bluelab.modules` sits below `bluelab.api` in the layers contract — so a router
here importing `deps` is an upward import and a build failure. The Auth
*behaviour* is still this module's: `service.py` and `gates.py` hold every
decision, and the routes only render them.

Provisioning belongs to the separately authenticated ops surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from bluelab.modules.identity import service
from bluelab.modules.identity.schemas import LegalDocuments
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction

router = APIRouter(tags=["Legal"])


@router.get(
    "/legal-documents",
    operation_id="getLegalDocuments",
    response_model=LegalDocuments,
    summary="Current legal document versions",
)
async def get_legal_documents(response: Response) -> LegalDocuments:
    """Return the current catalog without requiring a session."""
    async with scoped_transaction(ScopeContext.anonymous()) as db:
        documents = await service.legal_documents(db)
    response.headers["Cache-Control"] = "public, max-age=300"
    return documents

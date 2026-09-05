"""Unauthenticated process liveness probe, excluded from the product contract."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Confirm that the HTTP process can serve requests."""
    return {"status": "ok"}

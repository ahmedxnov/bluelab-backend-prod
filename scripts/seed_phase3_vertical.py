#!/usr/bin/env python3
"""Seed the configurable v1 vertical through the manager product API.

The helper is local-only and accepts an already authenticated manager session
from ``BLUELAB_SEED_SESSION``. It refuses a non-empty catalog so seed execution
cannot replace customer facts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from bluelab.platform.security.cookies import SESSION_COOKIE

DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "verticals"
    / "egyptian-b2b-insurance.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args()


def load_vertical(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("v") != 1 or len(payload.get("documents", [])) != 2:
        raise RuntimeError("the Phase 3 vertical must contain exactly two v1 documents")
    return cast(dict[str, Any], payload)


async def seed(
    *,
    base_url: str,
    session_token: str,
    config: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("the vertical seed helper is local-only")
    vertical = load_vertical(config)
    async with httpx.AsyncClient(
        base_url=base_url,
        cookies={SESSION_COOKIE: session_token},
        headers={"Origin": base_url.rstrip("/")},
        timeout=30,
        transport=transport,
    ) as client:
        listed = await client.get("/api/v1/product-documents")
        listed.raise_for_status()
        if listed.json()["data"]:
            raise RuntimeError("the team catalog is not empty; refusing to seed")
        created: list[str] = []
        for document in vertical["documents"]:
            response = await client.post(
                "/api/v1/product-documents", json={"title": document["title"]}
            )
            response.raise_for_status()
            document_id = response.json()["id"]
            draft = await client.put(
                f"/api/v1/product-documents/{document_id}/draft",
                json={"facts": document["facts"]},
            )
            draft.raise_for_status()
            published = await client.post(
                f"/api/v1/product-documents/{document_id}/publish",
                json={"based_on_version": 0},
            )
            published.raise_for_status()
            created.append(document_id)
        return created


def main() -> int:
    args = parse_args()
    session_token = os.environ.get("BLUELAB_SEED_SESSION")
    if not session_token:
        raise RuntimeError("BLUELAB_SEED_SESSION is required")
    created = asyncio.run(
        seed(base_url=args.base_url, session_token=session_token, config=args.config)
    )
    print(json.dumps({"created_document_ids": created}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

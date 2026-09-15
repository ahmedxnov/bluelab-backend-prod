"""Scheduled-sweep entrypoint.

Runs the reconciliation duties ADR-0071 rule 7 makes load-bearing rather than
theoretical: the single-live-call lease-expiry sweep and the stranded
`in_progress` attempt sweep, plus the retention and lifecycle sweeps of data/03.
"""

from __future__ import annotations

import asyncio

from valkey.asyncio import Valkey

from bluelab.calls.sweep import run_once
from bluelab.platform.config import get_settings
from bluelab.platform.db.engine import dispose_engine


async def _run() -> None:
    settings = get_settings()
    client = Valkey.from_url(
        settings.valkey_url.get_secret_value(), decode_responses=True
    )
    try:
        while True:
            await run_once(client, settings)
            await asyncio.sleep(15)
    finally:
        await client.aclose()
        await dispose_engine()


def main() -> None:
    asyncio.run(_run())

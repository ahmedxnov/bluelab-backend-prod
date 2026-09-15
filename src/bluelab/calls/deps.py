"""Call-plane request dependencies kept below the product API layer."""

from fastapi import Request
from valkey.asyncio import Valkey


def get_call_valkey(request: Request) -> Valkey:
    client: Valkey = request.app.state.valkey
    return client

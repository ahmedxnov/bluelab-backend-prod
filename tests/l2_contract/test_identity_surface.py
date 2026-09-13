"""Executable identity-surface invariants from the locked API contract.

These checks exercise the FastAPI-generated surface rather than searching source
text.  The conformance gate separately proves that this generated surface matches
the repository's locked authoritative OpenAPI snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from bluelab.entrypoints.api import create_app
from bluelab.modules.identity.schemas import SessionView
from bluelab.modules.operations.schemas import OpsAccountCreate
from bluelab.platform.config import Settings

pytestmark = pytest.mark.l2_contract


@pytest.fixture(scope="module")
def generated_contract() -> dict[str, Any]:
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://app:password@db.example.test/bluelab",  # pragma: allowlist secret
        VALKEY_URL="redis://127.0.0.1:6379/0",
        AGENT_HMAC_SECRET="contract-test-only",  # pragma: allowlist secret
    )
    return create_app(settings).openapi()


@pytest.mark.verifies("FR-IDA-002")
def test_customer_surface_has_no_self_registration_operation(
    generated_contract: Mapping[str, Any],
) -> None:
    """Only the separate operations surface may expose account creation."""
    registration_terms = {
        "account",
        "accounts",
        "register",
        "registration",
        "signup",
        "user",
        "users",
    }
    candidates: list[str] = []
    for path, path_item in generated_contract["paths"].items():
        if path.startswith("/ops/v1/") or "post" not in path_item:
            continue
        path_terms = set(path.lower().replace("-", "/").split("/"))
        operation_id = str(path_item["post"].get("operationId", "")).lower()
        if registration_terms & path_terms or any(
            term in operation_id
            for term in ("create_account", "create_user", "createaccount", "createuser", "register", "signup")
        ):
            candidates.append(path)

    assert candidates == []


@pytest.mark.verifies("FR-IDA-007")
def test_public_identity_contract_has_exactly_manager_and_rep_roles() -> None:
    """Both customer sessions and ops provisioning use the closed role model."""
    session_roles = SessionView.model_json_schema()["properties"]["role"]["enum"]
    provisioned_roles = OpsAccountCreate.model_json_schema()["properties"]["role"]["enum"]

    assert set(session_roles) == {"manager", "rep"}
    assert set(provisioned_roles) == {"manager", "rep"}


@pytest.mark.verifies("FR-IDA-008")
def test_customer_requests_cannot_select_an_organization(
    generated_contract: Mapping[str, Any],
) -> None:
    """Organization is resolved from identity and never accepted from a customer."""
    selectable: list[str] = []
    for path, path_item in generated_contract["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method in ("delete", "get", "patch", "post", "put"):
            operation = path_item.get(method)
            if operation is None:
                continue
            parameter_names = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") in {"header", "path", "query"}
            }
            body_properties = _request_body_properties(operation, generated_contract)
            if "org_id" in parameter_names | body_properties:
                selectable.append(f"{method.upper()} {path}")

    assert selectable == []


def _request_body_properties(
    operation: Mapping[str, Any], document: Mapping[str, Any]
) -> set[str]:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return set()
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return set()
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return set()
    schema = media.get("schema")
    if not isinstance(schema, Mapping):
        return set()
    resolved = _resolve_schema(schema, document)
    properties = resolved.get("properties")
    return set(properties) if isinstance(properties, Mapping) else set()


def _resolve_schema(
    schema: Mapping[str, Any], document: Mapping[str, Any]
) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    node: Any = document
    for segment in reference.removeprefix("#/").split("/"):
        node = node[segment]
    if not isinstance(node, Mapping):
        raise TypeError(f"schema reference does not resolve to an object: {reference}")
    return node

"""Public surface for the locked runtime-bundle contract implementation."""

from .guard import (
    FORBIDDEN_FIELDS,
    KnowledgeBoundaryError,
    assert_payload_safe,
    assert_runtime_safe,
    find_forbidden_fields,
)
from .schema import (
    CallType,
    LeadType,
    Participant,
    PersonaSections,
    RuntimeBundle,
    RuntimeConfig,
    Scenario,
)

__all__ = [
    "FORBIDDEN_FIELDS",
    "CallType",
    "KnowledgeBoundaryError",
    "LeadType",
    "Participant",
    "PersonaSections",
    "RuntimeBundle",
    "RuntimeConfig",
    "Scenario",
    "assert_payload_safe",
    "assert_runtime_safe",
    "find_forbidden_fields",
]

__version__ = "1.0.0"

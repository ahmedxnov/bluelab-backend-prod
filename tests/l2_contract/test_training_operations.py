"""Consumer-side Training contract checks against the pinned Prism mock.

The mock is deliberately not the application.  These checks are the caller's
walkthrough of the authored OpenAPI document: every currently implemented
Training operation has one valid request and one request the document itself
must reject.  Behaviour, team scope, and database invariants remain L3 work.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
import pytest

_TEST_FILE = Path(__file__).resolve()
# `PRISM_BASE_URL` lets the suite point at an already-running local mock.  That
# makes the consumer checks portable to a sandbox that mounts only this test
# file, while normal repository runs still find the pinned binary and contract.
REPO_ROOT = _TEST_FILE.parents[2] if len(_TEST_FILE.parents) > 2 else Path.cwd()
CONTRACT = REPO_ROOT / "contracts" / "platform" / "api" / "openapi.yaml"
SESSION_COOKIE = "__Host-bluelab_session"
VALID_DRILL_ID = UUID("018f8b6a-7654-7bcd-8ef0-123456789abc")
VALID_ACCOUNT_ID = UUID("018f8b6a-7654-7bcd-8ef0-123456789abd")
VALID_FEEDBACK_ID = UUID("018f8b6a-7654-7bcd-8ef0-123456789abe")


@dataclass(frozen=True, slots=True)
class ContractCase:
    """One positive and schema-negative request for a Training operation."""

    name: str
    method: str
    path: str
    positive: dict[str, object]
    negative: dict[str, object]
    requirement: str


CASES = (
    ContractCase(
        "my progress",
        "GET",
        "/api/v1/me/progress",
        {"params": {"call_type": "discovery"}},
        {"params": {"call_type": "not-a-call-type"}},
        "FR-TRP-001",
    ),
    ContractCase(
        "my profile",
        "GET",
        "/api/v1/me/profile",
        {},
        {"cookies": {}},
        "FR-TRP-004",
    ),
    ContractCase(
        "coach feedback",
        "GET",
        "/api/v1/me/coach-feedback",
        {"params": {"cursor": "opaque-page", "limit": 25}},
        {"params": {"limit": 0}},
        "FR-TRP-003",
    ),
    ContractCase(
        "mark coach feedback read",
        "POST",
        "/api/v1/me/coach-feedback/mark-read",
        {"json": {"through_id": str(VALID_FEEDBACK_ID)}},
        {"json": {}},
        "FR-TRP-003",
    ),
    ContractCase(
        "my library",
        "GET",
        "/api/v1/me/library",
        {
            "params": {
                "source": "assigned",
                "attempted": "unattempted",
                "sort": "recommended",
                "limit": 25,
            }
        },
        {"params": {"source": "other"}},
        "FR-TRP-006",
    ),
    ContractCase(
        "my drill history",
        "GET",
        f"/api/v1/drills/{VALID_DRILL_ID}/my-history",
        {},
        {"path": "/api/v1/drills/not-a-uuid/my-history"},
        "FR-TRP-011",
    ),
    ContractCase(
        "team dashboard",
        "GET",
        "/api/v1/team/dashboard",
        {"params": {"month": "2026-09"}},
        {"params": {"month": "not-a-month"}},
        "FR-TRM-004",
    ),
    ContractCase(
        "team roster",
        "GET",
        "/api/v1/team/roster",
        {"params": {"month": "2026-09"}},
        {"params": {"month": "not-a-month"}},
        "FR-TRM-005",
    ),
    ContractCase(
        "rep deep dive",
        "GET",
        f"/api/v1/team/reps/{VALID_ACCOUNT_ID}",
        {"params": {"month": "2026-09"}},
        {"path": "/api/v1/team/reps/not-a-uuid"},
        "FR-TRM-006",
    ),
    ContractCase(
        "team drill catalog",
        "GET",
        "/api/v1/team/drills",
        {"params": {"status": "published", "limit": 25}},
        {"params": {"status": "not-a-status"}},
        "FR-TRM-008",
    ),
    ContractCase(
        "team drill stats",
        "GET",
        f"/api/v1/team/drills/{VALID_DRILL_ID}/stats",
        {},
        {"path": "/api/v1/team/drills/not-a-uuid/stats"},
        "FR-TRM-009",
    ),
    ContractCase(
        "team cohorts",
        "GET",
        "/api/v1/team/cohorts",
        {"params": {"month": "2026-09"}},
        {"params": {"month": "not-a-month"}},
        "FR-TRM-014",
    ),
    ContractCase(
        "assignment",
        "PUT",
        f"/api/v1/drills/{VALID_DRILL_ID}/assignment",
        {
            "json": {
                "recipient_account_ids": [str(VALID_ACCOUNT_ID)],
                "due_date": "2026-10-01",
                "attempts_allowed": 3,
            }
        },
        {"json": {"recipient_account_ids": [str(VALID_ACCOUNT_ID)]}},
        "FR-TRM-011",
    ),
)


def _free_port() -> int:
    """Return an ephemeral loopback port for a short-lived Prism process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _prism_command(port: int) -> list[str]:
    """Build the platform-native command for the pinned local Prism binary."""
    executable = REPO_ROOT / "node_modules" / ".bin" / (
        "prism.cmd" if os.name == "nt" else "prism"
    )
    if not executable.is_file():
        pytest.fail("Prism is not installed; run `npm ci` before the L2 contract suite.")
    arguments = [
        "mock",
        str(CONTRACT),
        "--dynamic",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if os.name == "nt":
        return ["cmd.exe", "/d", "/s", "/c", str(executable), *arguments]
    return [str(executable), *arguments]


@pytest.fixture(scope="module")
def prism_base_url() -> Iterator[str]:
    """Serve the locked contract through Prism for this module's requests."""
    external_base_url = os.environ.get("PRISM_BASE_URL")
    if external_base_url:
        yield external_base_url.rstrip("/")
        return

    port = _free_port()
    process = subprocess.Popen(
        _prism_command(port),
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                pytest.fail(f"Prism exited before it became ready:\n{stderr}")
            try:
                httpx.get(base_url, timeout=0.2)
            except httpx.TransportError:
                time.sleep(0.1)
            else:
                break
        else:
            pytest.fail("Prism did not become ready within 15 seconds.")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _request(
    client: httpx.Client, case: ContractCase, request: dict[str, object]
) -> httpx.Response:
    """Issue one case while always supplying the contract's session cookie."""
    request_data = dict(request)
    path = str(request_data.pop("path", case.path))
    cookies = request_data.pop("cookies", {SESSION_COOKIE: "contract-session"})
    client.cookies.clear()
    if isinstance(cookies, dict):
        client.cookies.update(cookies)
    return client.request(case.method, path, **request_data)


@pytest.mark.l2_contract
@pytest.mark.verifies(
    "FR-TRP-001",
    "FR-TRP-003",
    "FR-TRP-004",
    "FR-TRP-006",
    "FR-TRP-011",
    "FR-TRM-004",
    "FR-TRM-005",
    "FR-TRM-006",
    "FR-TRM-008",
    "FR-TRM-009",
    "FR-TRM-011",
    "FR-TRM-014",
)
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_training_operation_positive_case_comes_from_the_contract(
    prism_base_url: str, case: ContractCase
) -> None:
    """A valid consumer request receives one of the contract's successful responses."""
    with httpx.Client(base_url=prism_base_url) as client:
        response = _request(client, case, case.positive)

    assert 200 <= response.status_code < 300, response.text
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.l2_contract
@pytest.mark.verifies(
    "FR-TRP-001",
    "FR-TRP-003",
    "FR-TRP-004",
    "FR-TRP-006",
    "FR-TRP-011",
    "FR-TRM-004",
    "FR-TRM-005",
    "FR-TRM-006",
    "FR-TRM-008",
    "FR-TRM-009",
    "FR-TRM-011",
    "FR-TRM-014",
)
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_training_operation_schema_negative_is_refused(
    prism_base_url: str, case: ContractCase
) -> None:
    """Malformed inputs never receive a successful mock response."""
    with httpx.Client(base_url=prism_base_url) as client:
        response = _request(client, case, case.negative)

    assert response.status_code in {401, 404, 422}, response.text

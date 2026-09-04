"""Shared test helpers.

Deliberately tiny. Anything that grows a policy belongs in a conftest next to the
suite it serves; this holds only what every L3 suite needs identically.
"""

from __future__ import annotations

import os

import pytest


def required_url(variable: str) -> str:
    """The database URL from the environment, or skip the suite saying which.

    Args:
        variable: The environment variable to read, e.g. `TEST_MIGRATION_URL`.

    Returns:
        The URL.

    Raises:
        pytest.skip.Exception: If unset. A skip rather than an error because a
            missing database is an environment fact, not a defect in the code
            under test — and quality/08 §5 only counts a skipped test as
            *uncovered*, which is exactly right for a suite that never ran.

    ## Why there is no fallback default

    These used to read
    `os.environ.get("TEST_MIGRATION_URL", "postgresql+asyncpg://bluelab:bluelab@…")`.
    Two problems with that, and the smaller one is the scanner.

    The credentials are throwaway, but `detect-secrets` flags them and
    pipeline/02 §2 row 9's red condition is "any secret found" (SEC-019) — so the
    defaults turn that stage red the moment it is wired into CI.

    The larger problem is what a default *does*. A developer with no environment
    set gets a silent connection to whatever happens to be listening on the
    default host and port, and the suite then truncates every customer table on it
    (`isolation/conftest.py::_reset`). A test run pointed at an unexpected
    database is worse than one that refuses to start, and this is the same
    no-silent-fallback stance `tools/check_rls_drift.py::resolve_url` already
    takes for the same reason.
    """
    url = os.environ.get(variable)
    if not url:
        pytest.skip(
            f"{variable} is not set. The L3 suites run against a real PostgreSQL 16 "
            f"and will not guess at one — see the backend README for the exports.",
            allow_module_level=True,
        )
    return url

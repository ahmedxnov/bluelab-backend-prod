#!/usr/bin/env python3
"""Bootstrap the backend's locally owned dependencies and database state.

The command is intentionally repository-local.  It creates the Python 3.12
environment from the hash-locked development dependencies, starts the local
compose services, applies the full Alembic lineage (including platform reference
seed data), and applies the generated SQL modules.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
VENV_ROOT: Final = REPO_ROOT / ".venv"
LOCAL_DATABASE_URL: Final = "postgresql+psycopg://bluelab:bluelab@127.0.0.1:5432/bluelab"


def venv_python() -> Path:
    """Return the platform-specific interpreter path in the local virtualenv."""
    return VENV_ROOT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*command: str, env: dict[str, str] | None = None) -> None:
    """Run one bootstrap step from the backend repository root."""
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=REPO_ROOT, env=env, check=True)


def require_python_312() -> None:
    """Fail before creating an incompatible environment."""
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"Python 3.12 is required; found {sys.version.split()[0]}")


def docker_cli() -> str:
    """Find Docker without making this repository depend on one shell's PATH."""
    if configured := os.environ.get("DOCKER_BIN"):
        return configured
    if discovered := shutil.which("docker"):
        return discovered
    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local_app_data:
        desktop_cli = Path(local_app_data) / "Programs/DockerDesktop/resources/bin/docker.exe"
        if desktop_cli.is_file():
            return str(desktop_cli)
    raise RuntimeError("Docker CLI not found. Set DOCKER_BIN to the Docker executable.")


def main() -> int:
    """Create a reproducible local backend environment."""
    require_python_312()
    if not venv_python().is_file():
        run(sys.executable, "-m", "venv", "--upgrade-deps", str(VENV_ROOT))

    python = str(venv_python())
    run(python, "-m", "pip", "install", "--require-hashes", "-r", "requirements-dev.lock")
    run(docker_cli(), "compose", "up", "-d", "--wait")

    migration_env = os.environ.copy()
    migration_env["MIGRATION_DATABASE_URL"] = LOCAL_DATABASE_URL
    migration_env["DATABASE_URL"] = "postgresql+asyncpg://bluelab_app:bluelab@127.0.0.1:5432/bluelab"
    run(python, "-m", "alembic", "upgrade", "head", env=migration_env)
    run(python, "tools/check_rls_drift.py", "--apply", env=migration_env)
    print("Backend local bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

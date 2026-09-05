#!/usr/bin/env python3
"""Verify the pinned frontend design-token artifact consumed by report templates."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = REPO_ROOT / "templates" / "report" / "design-tokens.lock.json"


def main() -> int:
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", data.get("source_commit", "")):
            raise ValueError("invalid design-token lock metadata")
        expected = data.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid design-token SHA-256")
        target = REPO_ROOT / data["local_path"]
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if actual != expected:
        print(f"DESIGN TOKEN DRIFT: expected {expected}, got {actual}", file=sys.stderr)
        return 1
    print(f"design tokens verified from {data['source_commit']}:{data['source_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

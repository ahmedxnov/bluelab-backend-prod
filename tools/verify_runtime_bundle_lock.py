#!/usr/bin/env python3
"""Verify the vendored call-plane runtime-bundle package."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = REPO_ROOT / "contracts" / "runtime-bundle.lock.json"


def main() -> int:
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", data.get("source_commit", "")):
            raise ValueError("invalid runtime-bundle lock metadata")
        errors = []
        for relative, expected in sorted(data["files"].items()):
            target = REPO_ROOT / relative
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"{relative}: expected {expected}, got {actual}")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("RUNTIME BUNDLE DRIFT\n  - " + "\n  - ".join(errors), file=sys.stderr)
        return 1
    print(f"runtime bundle verified: {len(data['files'])} files from {data['source_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

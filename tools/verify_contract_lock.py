#!/usr/bin/env python3
"""Verify the vendored platform contract snapshot without network or siblings."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = REPO_ROOT / "contracts" / "platform.lock.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def load_lock(path: Path = LOCK) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("contract lock must be an object")
    data = cast(dict[str, Any], raw)
    if data.get("version") != 1:
        raise ValueError("contract lock version must be 1")
    if not _COMMIT.fullmatch(str(data.get("source_commit", ""))):
        raise ValueError("source_commit must be a full 40-character git commit")
    if not str(data.get("source_repository", "")).startswith("https://"):
        raise ValueError("source_repository must be an HTTPS repository URL")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty object")
    return data


def verify(path: Path = LOCK) -> list[str]:
    data = load_lock(path)
    root = REPO_ROOT / data["snapshot_root"]
    errors: list[str] = []
    expected: set[Path] = set()
    for relative, digest in sorted(data["files"].items()):
        rel = PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts:
            errors.append(f"unsafe snapshot path: {relative}")
            continue
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            errors.append(f"invalid SHA-256 for {relative}")
            continue
        target = root.joinpath(*rel.parts)
        expected.add(target)
        if not target.is_file():
            errors.append(f"missing snapshot file: {relative}")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"hash mismatch: {relative}: expected {digest}, got {actual}")
    actual_files = {item for item in root.rglob("*") if item.is_file()}
    for extra in sorted(actual_files - expected):
        errors.append(f"unlocked snapshot file: {extra.relative_to(root).as_posix()}")
    return errors


def main() -> int:
    try:
        errors = verify()
    except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("CONTRACT LOCK INVALID", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    data = load_lock()
    print(f"contract snapshot verified: {len(data['files'])} file(s) from {data['source_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

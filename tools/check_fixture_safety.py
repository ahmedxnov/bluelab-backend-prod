#!/usr/bin/env python3
"""Enforce the no-production-data boundary for tests and committed fixtures."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPO_ROOT / "tests"
FIXTURE_ROOT = TEST_ROOT / "fixtures"

TEXT_SUFFIXES = frozenset({".csv", ".json", ".md", ".py", ".txt", ".yaml", ".yml"})
SOURCE_CLASSES = frozenset({"consented-study", "synthetic", "vendor-sandbox"})
METADATA_NAMES = frozenset({".gitkeep", "README.md"})
_EMAIL = re.compile(
    r"(?<![\\\w])(?P<email>[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+))"
)
_SOURCE_CODE = re.compile(r"^[A-Z][A-Z0-9-]{2,63}$")


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    path: str
    detail: str

    def render(self) -> str:
        return f"  [{self.kind}] {self.path}\n      {self.detail}"


def _relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _reserved_domain(domain: str) -> bool:
    normalized = domain.rstrip(".").lower()
    if normalized in {"example.com", "example.net", "example.org"}:
        return True
    return normalized.rsplit(".", 1)[-1] in {"example", "invalid", "localhost", "test"}


def scan_test_identities(root: Path = TEST_ROOT) -> list[Finding]:
    """Reject email-shaped identities outside the IANA-reserved test namespaces."""
    findings: list[Finding] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _EMAIL.finditer(text):
            if not _reserved_domain(match.group("domain")):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(
                    Finding(
                        "non-synthetic-identity",
                        f"{_relative(path)}:{line}",
                        f"{match.group('email')!r} is outside a reserved test domain",
                    )
                )
    return findings


def _validate_provenance(data_path: Path, sidecar: Path) -> list[Finding]:
    location = _relative(sidecar)
    try:
        document = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Finding("provenance-invalid", location, str(exc))]
    if not isinstance(document, dict):
        return [Finding("provenance-invalid", location, "the sidecar must be a JSON object")]

    findings: list[Finding] = []
    if document.get("version") != 1:
        findings.append(Finding("provenance-version", location, "version must be 1"))
    source_class = document.get("source_class")
    if source_class not in SOURCE_CLASSES:
        findings.append(
            Finding(
                "provenance-source",
                location,
                f"source_class must be one of {sorted(SOURCE_CLASSES)}",
            )
        )
    source_code = document.get("source_code")
    if not isinstance(source_code, str) or _SOURCE_CODE.fullmatch(source_code) is None:
        findings.append(
            Finding("provenance-code", location, "source_code must be a bounded uppercase code")
        )
    if document.get("production_data") is not False:
        findings.append(
            Finding("production-data", location, "production_data must be explicitly false")
        )
    if source_class != "synthetic" and not document.get("authorization_reference"):
        findings.append(
            Finding(
                "authorization-missing",
                location,
                "consented-study and vendor-sandbox fixtures require authorization_reference",
            )
        )
    if document.get("fixture") != data_path.name:
        findings.append(
            Finding("provenance-target", location, f"fixture must equal {data_path.name!r}")
        )
    return findings


def scan_fixture_provenance(root: Path = FIXTURE_ROOT) -> list[Finding]:
    """Require an explicit provenance sidecar for every fixture data file."""
    if not root.is_dir():
        return [Finding("fixture-root-missing", _relative(root), "fixture root does not exist")]
    findings: list[Finding] = []
    files = sorted(item for item in root.rglob("*") if item.is_file())
    for path in files:
        if path.name in METADATA_NAMES or path.name.endswith(".provenance.json"):
            continue
        sidecar = path.with_name(f"{path.name}.provenance.json")
        if not sidecar.is_file():
            findings.append(
                Finding(
                    "provenance-missing",
                    _relative(path),
                    f"add {_relative(sidecar)} before admitting this fixture",
                )
            )
            continue
        findings.extend(_validate_provenance(path, sidecar))
    for sidecar in (path for path in files if path.name.endswith(".provenance.json")):
        data_path = sidecar.with_name(sidecar.name.removesuffix(".provenance.json"))
        if not data_path.is_file():
            findings.append(
                Finding("provenance-orphan", _relative(sidecar), "the named fixture does not exist")
            )
    return findings


def check() -> list[Finding]:
    return scan_test_identities() + scan_fixture_provenance()


def main() -> int:
    try:
        findings = check()
    except (OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not findings:
        print("fixture safety verified — reserved identities and explicit data provenance")
        return 0
    print(f"FIXTURE SAFETY FAILED — {len(findings)} finding(s)\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

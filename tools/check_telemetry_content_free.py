#!/usr/bin/env python3
"""Enforce the content-free telemetry boundary (SEC-024, observability/01 §5)."""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "src"
TELEMETRY_ROOT = BACKEND_SRC / "bluelab" / "platform" / "telemetry"
LOGGING_MODULE = TELEMETRY_ROOT / "logging.py"
CORRELATION_MODULE = TELEMETRY_ROOT / "correlation.py"

LOG_METHODS = frozenset({"critical", "debug", "error", "exception", "info", "warning"})
CONTENT_FIELD_TOKENS = frozenset(
    {
        "address",
        "answer_key",
        "audio",
        "authorization",
        "body",
        "candidate_token",
        "cookie",
        "display_name",
        "email",
        "hidden_motive",
        "name",
        "objection",
        "password",
        "persona",
        "phone",
        "prompt",
        "product_fact",
        "quote",
        "recording",
        "response",
        "scenario",
        "secret",
        "session_id",
        "text",
        "token",
        "transcript",
        "utterance",
    }
)
REQUIRED_REDACTIONS = CONTENT_FIELD_TOKENS
HIGH_CARDINALITY = frozenset(
    {"account_id", "attempt_id", "call_id", "candidate_id", "job_id", "request_id", "trace_id"}
)
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    location: str
    detail: str

    def render(self) -> str:
        return f"  [{self.kind}] {self.location}\n      {self.detail}"


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _location(path: Path, node: ast.AST) -> str:
    return f"{_rel(path)}:{getattr(node, 'lineno', 0)}"


def _attribute_call(node: ast.Call) -> str | None:
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def _receiver_name(node: ast.Call) -> str | None:
    if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
        return None
    return node.func.value.id


def _is_logger_call(node: ast.Call) -> bool:
    receiver = _receiver_name(node)
    return receiver is not None and (
        receiver in {"log", "logger", "_log"} or receiver.endswith("_logger")
    )


def _literal_dict_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        for key in child.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value.lower())
    return keys


def _payload_keys(call: ast.Call) -> set[str]:
    keys = _literal_dict_keys(call)
    keys.update(keyword.arg.lower() for keyword in call.keywords if keyword.arg is not None)
    return keys


def _assignment_string_set(tree: ast.Module, name: str) -> set[str]:
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target = node.targets[0] if node.targets else None
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or target.id != name or value is None:
            continue
        return {
            child.value.lower()
            for child in ast.walk(value)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }
    return set()


def _scan_logging_call(path: Path, call: ast.Call) -> list[Finding]:
    findings: list[Finding] = []
    method = _attribute_call(call)
    if method not in LOG_METHODS:
        return findings
    location = _location(path, call)
    if method == "exception" or any(
        keyword.arg == "exc_info"
        and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
        for keyword in call.keywords
    ):
        findings.append(
            Finding(
                "exception-payload",
                location,
                "raw exception messages may contain request or vendor content; emit an error class and correlation id instead",
            )
        )
    event = call.args[0] if call.args else None
    if not isinstance(event, ast.Constant) or not isinstance(event.value, str):
        findings.append(Finding("dynamic-event", location, "log event names must be fixed string literals"))
    elif not _EVENT_NAME.fullmatch(event.value):
        findings.append(Finding("free-form-event", location, f"{event.value!r} is not a bounded event name"))
    for key in sorted(_payload_keys(call) & CONTENT_FIELD_TOKENS):
        findings.append(Finding("content-field", location, f"log payload declares forbidden field {key!r}"))
    return findings


def _direct_telemetry_import(path: Path, node: ast.Import | ast.ImportFrom) -> Finding | None:
    if TELEMETRY_ROOT in path.parents:
        return None
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = {alias.name for alias in node.names}
        direct = module.startswith(("opentelemetry.metrics", "opentelemetry.trace"))
        direct = direct or (module == "opentelemetry" and bool(names & {"metrics", "trace"}))
    else:
        direct = any(
            alias.name.startswith(("opentelemetry.metrics", "opentelemetry.trace"))
            for alias in node.names
        )
    if not direct:
        return None
    return Finding(
        "telemetry-bypass",
        _location(path, node),
        "OpenTelemetry emission must pass through bluelab.platform.telemetry",
    )


def scan_source(root: Path) -> list[Finding]:
    """Scan telemetry emission sites under ``root``."""
    findings: list[Finding] = []
    calls = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                finding = _direct_telemetry_import(path, node)
                if finding is not None:
                    findings.append(finding)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            method = _attribute_call(call)
            if method in LOG_METHODS and _is_logger_call(call):
                calls += 1
                findings.extend(_scan_logging_call(path, call))
            if method in {"add_event", "set_attribute"}:
                calls += 1
                keys = _payload_keys(call)
                for key in sorted(keys & CONTENT_FIELD_TOKENS):
                    findings.append(Finding("content-field", _location(path, call), f"span payload declares forbidden field {key!r}"))
    if root == BACKEND_SRC and calls == 0:
        findings.append(Finding("telemetry-none", _rel(root), "the scan found no telemetry emission sites"))
    return findings


def check_structure() -> list[Finding]:
    """Verify redaction coverage and the metric-label cardinality boundary."""
    findings: list[Finding] = []
    logging_tree = ast.parse(LOGGING_MODULE.read_text(encoding="utf-8"), filename=str(LOGGING_MODULE))
    redactions = _assignment_string_set(logging_tree, "SENSITIVE_KEYS")
    for missing in sorted(REQUIRED_REDACTIONS - redactions):
        findings.append(Finding("redaction-gap", _rel(LOGGING_MODULE), f"SENSITIVE_KEYS omits {missing!r}"))

    correlation_tree = ast.parse(
        CORRELATION_MODULE.read_text(encoding="utf-8"), filename=str(CORRELATION_MODULE)
    )
    low = _assignment_string_set(correlation_tree, "LOW_CARDINALITY_KEYS")
    high = _assignment_string_set(correlation_tree, "HIGH_CARDINALITY_KEYS")
    for key in sorted((low & high) | (low & HIGH_CARDINALITY)):
        findings.append(Finding("high-cardinality-label", _rel(CORRELATION_MODULE), f"{key!r} is admitted as a metric label"))
    for missing in sorted(HIGH_CARDINALITY - high):
        findings.append(Finding("cardinality-gap", _rel(CORRELATION_MODULE), f"HIGH_CARDINALITY_KEYS omits {missing!r}"))
    return findings


def main() -> int:
    try:
        findings = check_structure() + scan_source(BACKEND_SRC)
    except (OSError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not findings:
        print("content-free telemetry verified — allow-listed fields, bounded events, metric chokepoint")
        return 0
    print(f"CONTENT-FREE TELEMETRY FAILED — {len(findings)} finding(s)\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

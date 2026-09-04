"""Cross-cutting platform code. No domain knowledge; importable by every plane.

This is the lowest layer of the import-linter contract in `pyproject.toml`: it
may not import `bluelab.modules`, `bluelab.calls`, `bluelab.work`,
`bluelab.notifications`, or `bluelab.api`.
"""
